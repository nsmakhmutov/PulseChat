import asyncio
import ctypes
import fractions
import os
import threading
import time
from pathlib import Path
from typing import Callable

import av
import numpy as np
import sounddevice as sd
from aiortc import AudioStreamTrack

from config import SAMPLE_RATE, CHANNELS, CHUNK_SIZE, AUDIO_DIAG_ENABLED
from .audio_processing import PYRNNOISE_AVAILABLE

try:
    from pyrnnoise import RNNoise
except ImportError:
    RNNoise = None
try:
    from scipy.signal import resample_poly as _scipy_resample_poly
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False
    _scipy_resample_poly = None
    print("[StreamAudio] scipy не установлен — ресемплинг через np.interp (aliasing)")


def _find_dll() -> Path:
    dll_name = "InPulseAudioExclusion.dll"
    candidates = [
        Path(__file__).parent.parent / "dlls" / dll_name,
        Path(__file__).parent / dll_name,
        Path(os.getcwd()) / dll_name,
        Path(os.getcwd()) / "dlls" / dll_name,
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"[StreamAudio] DLL '{dll_name}' не найдена.\n"
        f"  Ожидаемые места: {[str(c) for c in candidates]}\n"
        f"  Скомпилируйте LoopbackCapture.cpp → Release/x64 в Visual Studio\n"
        f"  и скопируйте в папку dlls/ проекта."
    )

_AudioCallbackType = ctypes.CFUNCTYPE(
    None,
    ctypes.POINTER(ctypes.c_float),
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
)

_dll: "ctypes.CDLL | None" = None
_dll_load_error: "str | None" = None


def _load_dll() -> "ctypes.CDLL | None":
    global _dll, _dll_load_error
    if _dll is not None:
        return _dll
    if _dll_load_error is not None:
        return None

    try:
        dll_path = _find_dll()
        lib = ctypes.CDLL(str(dll_path))

        lib.StartCapture.restype  = ctypes.c_bool
        lib.StartCapture.argtypes = [ctypes.c_ulong, _AudioCallbackType]

        lib.StopCapture.restype  = None
        lib.StopCapture.argtypes = []

        lib.StartRender.restype  = ctypes.c_bool
        lib.StartRender.argtypes = []

        lib.StopRender.restype  = None
        lib.StopRender.argtypes = []

        lib.PlayAudioChunk.restype  = ctypes.c_bool
        lib.PlayAudioChunk.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]

        lib.IsRawModeActive.restype  = ctypes.c_bool
        lib.IsRawModeActive.argtypes = []

        try:
            lib.IsWasapiReady.restype  = ctypes.c_bool
            lib.IsWasapiReady.argtypes = []
        except AttributeError:
            lib.IsWasapiReady = lambda: True

        try:
            lib.IsRenderPollingMode.restype  = ctypes.c_bool
            lib.IsRenderPollingMode.argtypes = []
        except AttributeError:
            lib.IsRenderPollingMode = lambda: False

        try:
            lib.ReadRenderedAudio.restype  = ctypes.c_int
            lib.ReadRenderedAudio.argtypes = [
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
                ctypes.c_int,
            ]
        except AttributeError:
            lib.ReadRenderedAudio = lambda _p, _f, _c: 0

        try:
            lib.GetRenderedAudioAvailable.restype  = ctypes.c_int
            lib.GetRenderedAudioAvailable.argtypes = []
        except AttributeError:
            lib.GetRenderedAudioAvailable = lambda: 0

        _dll = lib
        print(f"[StreamAudio] DLL загружена: {dll_path}")
        return _dll

    except (FileNotFoundError, OSError) as e:
        _dll_load_error = str(e)
        print(f"[StreamAudio] ОШИБКА загрузки DLL: {e}")
        return None


def get_dll() -> "ctypes.CDLL | None":
    return _dll

_RING_SIZE = CHUNK_SIZE * 64

# Watchdog таймауты
_DLL_SILENCE_TIMEOUT = 3.0
_DLL_MIN_ACTIVE_SEC = 2.0


class StreamAudioCapture:

    def __init__(
        self,
        pcm_callback:  "Callable[[np.ndarray], None] | None" = None,
        audio_handler: "object | None"                        = None,
        on_dll_silence: "Callable[[], None] | None"           = None,
    ):
        self._pcm_callback    = pcm_callback
        self._audio_handler   = audio_handler
        self._on_dll_silence  = on_dll_silence
        self._ring = np.zeros(_RING_SIZE, dtype=np.float32)
        self._write_pos: int = 0
        self._read_pos:  int = 0
        self._available: int = 0
        self._buffer_lock = threading.Lock()
        self._c_callback: "_AudioCallbackType | None" = None
        self._started = False
        self._log_rms_accum: float = 0.0
        self._log_rms_count: int   = 0
        self._log_next_ts:   float = 0.0
        self._watchdog_thread: "threading.Thread | None" = None
        self._watchdog_stop_evt = threading.Event()
        self._last_active_ts: float = 0.0
        self._start_ts: float = 0.0
        self._silence_triggered: bool = False

    @property
    def render_active(self) -> bool:
        return False

    def start(self, device_idx=None):
        if self._started:
            self.stop()

        lib = _load_dll()
        if lib is None:
            print("[StreamAudio] DLL недоступна — захват системного звука отключён")
            return

        with self._buffer_lock:
            self._write_pos = 0
            self._read_pos  = 0
            self._available = 0

        self._c_callback = _AudioCallbackType(self._dll_audio_cb)
        exclude_pid = os.getpid()
        try:
            ok = bool(lib.StartCapture(ctypes.c_ulong(exclude_pid), self._c_callback))
        except OSError as e:
            ok = False
            print(f"[StreamAudio] ✘ DLL::StartCapture() исключение: {e}")

        if ok:
            self._started = True
            self._start_ts = time.perf_counter()
            self._last_active_ts = self._start_ts
            self._silence_triggered = False

            if self._on_dll_silence is not None:
                self._watchdog_stop_evt.clear()
                self._watchdog_thread = threading.Thread(
                    target=self._watchdog_loop,
                    daemon=True,
                    name="dll-capture-watchdog",
                )
                self._watchdog_thread.start()

            print(f"[StreamAudio] ✔ DLL Capture запущен (exclude PID={exclude_pid})"
                  f" — голоса InPulse исключены через Session Manager")
        else:
            print("[StreamAudio] ✘ DLL::StartCapture вернула false")
            self._c_callback = None

    def stop(self):
        if not self._started:
            return
        self._watchdog_stop_evt.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1.0)
            self._watchdog_thread = None

        lib = _load_dll()
        if lib is not None:
            try:
                lib.StopCapture()
            except Exception as e:
                print(f"[StreamAudio] StopCapture error: {e}")

        self._started    = False
        self._c_callback = None
        print("[StreamAudio] DLL Capture остановлен")

    @staticmethod
    def list_wasapi_output_devices():
        result = []
        try:
            apis = sd.query_hostapis()
            devs = sd.query_devices()
            w_idx = next(
                (i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None
            )
            if w_idx is None:
                return result
            for i, d in enumerate(devs):
                if d['hostapi'] == w_idx and d['max_output_channels'] > 0:
                    result.append((d['name'], i))
        except Exception as e:
            print(f"[StreamAudio] list_wasapi_output_devices error: {e}")
        return result

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop_evt.is_set():
            if self._watchdog_stop_evt.wait(0.5):
                break

            if not self._started:
                continue

            now = time.perf_counter()
            if now - self._start_ts < _DLL_MIN_ACTIVE_SEC:
                continue

            silent_for = now - self._last_active_ts
            if silent_for >= _DLL_SILENCE_TIMEOUT:
                if not self._silence_triggered:
                    self._silence_triggered = True
                    print(
                        f"[StreamAudio] WATCHDOG: DLL Capture silent "
                        f"{silent_for:.1f}s — requesting restart",
                        flush=True,
                    )
                    try:
                        if self._on_dll_silence is not None:
                            self._on_dll_silence()
                    except Exception as e:
                        print(f"[StreamAudio] WATCHDOG callback error: {e}")
                    self._last_active_ts = now

    def _ring_write(self, samples: np.ndarray) -> None:
        n = len(samples)
        if n == 0:
            return
        if n >= _RING_SIZE:
            tail = samples[-_RING_SIZE:]
            self._ring[:] = tail
            self._write_pos = 0
            self._read_pos  = 0
            self._available = _RING_SIZE
            print(f"[StreamAudio] ring buffer overrun: drop {n - _RING_SIZE} samples")
            return

        wp = self._write_pos
        end = wp + n

        if end <= _RING_SIZE:
            self._ring[wp:end] = samples
            self._write_pos = end % _RING_SIZE
        else:
            split = _RING_SIZE - wp
            self._ring[wp:] = samples[:split]
            self._ring[:n - split] = samples[split:]
            self._write_pos = n - split

        self._available += n
        if self._available > _RING_SIZE:
            drop = self._available - _RING_SIZE
            self._read_pos = (self._read_pos + drop) % _RING_SIZE
            self._available = _RING_SIZE

    def _ring_read_chunk(self) -> "np.ndarray | None":
        if self._available < CHUNK_SIZE:
            return None

        rp = self._read_pos
        end = rp + CHUNK_SIZE

        if end <= _RING_SIZE:
            chunk = self._ring[rp:end].copy()
            self._read_pos = end % _RING_SIZE
        else:
            split = _RING_SIZE - rp
            chunk = np.empty(CHUNK_SIZE, dtype=np.float32)
            chunk[:split] = self._ring[rp:]
            chunk[split:] = self._ring[:CHUNK_SIZE - split]
            self._read_pos = CHUNK_SIZE - split

        self._available -= CHUNK_SIZE
        return chunk

    def _dll_audio_cb(
        self,
        pcm_ptr:     ctypes.POINTER(ctypes.c_float),
        num_frames:  int,
        channels:    int,
        sample_rate: int,
    ) -> None:

        if not self._started or num_frames <= 0:
            return

        try:
            total_samples = num_frames * channels
            raw_view = np.ctypeslib.as_array(pcm_ptr, shape=(total_samples,))
            raw = raw_view.copy()

            if channels > 1:
                mono = raw.reshape(num_frames, channels).mean(axis=1).astype(np.float32)
            else:
                mono = raw

            if sample_rate != SAMPLE_RATE:
                mono = self._resample(mono, sample_rate, SAMPLE_RATE)

            peak_now = float(np.abs(mono).max()) if len(mono) else 0.0
            if peak_now > 0.001:
                self._last_active_ts = time.perf_counter()

            if AUDIO_DIAG_ENABLED:
                self._log_rms_accum += float(np.dot(mono, mono))
                self._log_rms_count += len(mono)
                _now = time.perf_counter()
                if _now >= self._log_next_ts and self._log_rms_count > 0:
                    rms = (self._log_rms_accum / self._log_rms_count) ** 0.5
                    print(
                        f"[DLL-DIAG] захват: RMS={rms:.4f}  peak={peak_now:.4f}"
                        f"  frames={num_frames}  ch={channels}  sr={sample_rate}"
                        f"  buf_avail={self._available}",
                        flush=True,
                    )
                    self._log_rms_accum = 0.0
                    self._log_rms_count = 0
                    self._log_next_ts   = _now + 1.0

            chunks_to_emit = []
            with self._buffer_lock:
                self._ring_write(mono)
                while True:
                    chunk = self._ring_read_chunk()
                    if chunk is None:
                        break
                    chunks_to_emit.append(chunk)

            if self._pcm_callback is not None:
                for chunk in chunks_to_emit:
                    try:
                        self._pcm_callback(chunk)
                    except Exception as e:
                        print(f"[StreamAudio] pcm_callback error: {e}")

        except Exception as exc:
            print(f"[StreamAudio] _dll_audio_cb exception: {exc}")

    @staticmethod
    def _resample(mono: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        if src_sr == dst_sr:
            return mono.astype(np.float32, copy=False)

        if _SCIPY_AVAILABLE:
            from math import gcd
            g = gcd(dst_sr, src_sr)
            up   = dst_sr // g
            down = src_sr // g
            try:
                out = _scipy_resample_poly(mono, up, down).astype(np.float32)
                return out
            except Exception as e:
                print(f"[StreamAudio] resample_poly failed: {e} → fallback на np.interp")

        num_frames = len(mono)
        target_len = int(round(num_frames * dst_sr / src_sr))
        if target_len <= 0:
            return np.zeros(0, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, num_frames, dtype=np.float64)
        x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float64)
        return np.interp(x_new, x_old, mono).astype(np.float32)

class MicrophoneTrack(AudioStreamTrack):

    kind = "audio"

    def __init__(self, device_name: str = None):
        super().__init__()
        self._device   = device_name
        self._running  = True
        self._queue:  "asyncio.Queue | None" = None
        self._loop:   "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None
        self._pts: int = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)

    def _capture_loop(self) -> None:
        def _cb(indata: np.ndarray, frames: int, time_info, status) -> None:
            if not self._running or self._loop is None or self._queue is None:
                return
            data = indata.copy()
            def _sync_put():
                try:
                    if self._queue.full():
                        self._queue.get_nowait()
                    self._queue.put_nowait(data)
                except Exception:
                    pass
            self._loop.call_soon_threadsafe(_sync_put)

        try:
            with sd.InputStream(
                device=self._device,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype='int16',
                blocksize=CHUNK_SIZE,
                callback=_cb,
            ):
                while self._running:
                    time.sleep(0.05)
        except Exception as e:
            print(f"[MicrophoneTrack] Ошибка захвата микрофона: {e}")

    async def recv(self) -> av.AudioFrame:
        if self._queue is None:
            self._loop   = asyncio.get_event_loop()
            self._queue  = asyncio.Queue(maxsize=10)
            self._thread = threading.Thread(
                target=self._capture_loop, daemon=True, name="webrtc-mic-capture"
            )
            self._thread.start()

        data = await self._queue.get()
        frame = av.AudioFrame.from_ndarray(data.T, format='s16', layout='mono')
        frame.sample_rate = SAMPLE_RATE
        frame.pts         = self._pts
        frame.time_base   = self._time_base
        self._pts        += CHUNK_SIZE
        return frame

    def stop(self) -> None:
        self._running = False


class SystemAudioTrack(AudioStreamTrack):

    kind = "audio"

    def __init__(
        self,
        device_idx:    int = None,
        audio_handler: "object | None" = None,
        media_bridge:  "object | None" = None,
    ):
        super().__init__()
        self._device_idx    = device_idx
        self._audio_handler = audio_handler
        self._media_bridge  = media_bridge
        self._running       = True
        self._queue:   "asyncio.Queue | None"             = None
        self._loop:    "asyncio.AbstractEventLoop | None" = None
        self._capture: "StreamAudioCapture | None"        = None
        self._pts:  int = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)

    def _on_pcm_chunk(self, chunk: np.ndarray) -> None:
        if not self._running or self._loop is None or self._queue is None:
            return
        def _sync_put():
            try:
                if self._queue.full():
                    self._queue.get_nowait()
                self._queue.put_nowait(chunk)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(_sync_put)

    def _on_dll_silence(self) -> None:

        if self._media_bridge is None:
            print("[SystemAudioTrack] watchdog сработал, но media_bridge=None")
            return
        try:
            if hasattr(self._media_bridge, 'restart_capture'):
                self._media_bridge.restart_capture()
        except Exception as e:
            print(f"[SystemAudioTrack] restart_capture error: {e}")

    async def recv(self) -> av.AudioFrame:
        if self._queue is None:
            self._loop  = asyncio.get_event_loop()
            self._queue = asyncio.Queue(maxsize=10)
            self._capture = StreamAudioCapture(
                pcm_callback=self._on_pcm_chunk,
                audio_handler=self._audio_handler,
                on_dll_silence=self._on_dll_silence,
            )
            self._capture.start(self._device_idx)
            print("[SystemAudioTrack] Захват системного звука запущен DLL")

        data = await self._queue.get()

        pcm_int16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(
            pcm_int16.reshape(1, -1), format='s16', layout='mono'
        )
        frame.sample_rate = SAMPLE_RATE
        frame.pts         = self._pts
        frame.time_base   = self._time_base
        self._pts        += CHUNK_SIZE
        return frame

    def stop(self) -> None:
        self._running = False
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
