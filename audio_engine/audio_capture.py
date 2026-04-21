"""
audio_capture.py — InPulse Audio Engine (DLL-редакция v4, Gate-Free, Fixed)
=========================================================================

StreamAudioCapture: захват системного звука через InPulseAudioExclusion.dll
(WASAPI Process Loopback). DLL исключает звук процесса InPulse через
PROCESS_LOOPBACK_EXCLUDE — зрители не слышат голоса участников чата InPulse
в стриме.

─── Исправления v4 ────────────────────────────────────────────────────────

  FIX #43 (КРИТИЧНО): zero-copy as_array → immediate .copy().
    Старый код: np.ctypeslib.as_array(pcm_ptr, ...) создаёт numpy view на
    C-память DLL. Эта память принадлежит WASAPI буферу и освобождается
    после возврата из callback. Если под buffer_lock произойдёт GIL release
    (а np.dot может!) и DLL успеет выдать следующий буфер, старый указатель
    станет мусором → либо SIGSEGV, либо silent corruption аудио-данных.

    Исправление: копируем ptr → Vec<u8> ПЕРЕД любой операцией под локом.

  FIX #44 (ВАЖНО): O(n) shift буфера → ring buffer.
    Старый код на каждом нарезанном CHUNK_SIZE делал:
        self._pcm_buf[:self._pcm_len] = self._pcm_buf[CHUNK_SIZE:CHUNK_SIZE+...]
    Это O(buffer_len) memmove из C-слоя numpy. Под нагрузкой (много аудио,
    вытесняющая очередь) давало явные паузы/хрусты в потоке.

    Исправление: ring buffer с write_pos/read_pos и wrap-around индексацией.

  FIX #50 (ВАЖНО): np.interp → scipy.signal.resample_poly.
    np.interp — линейная интерполяция без anti-alias фильтра. Даёт заметный
    aliasing на музыке (ВЧ-компоненты → наложения в слышимом диапазоне).
    resample_poly использует polyphase FIR с правильным low-pass.

    Fallback: если scipy не установлен — оставляем np.interp (лучше чем
    поломанное поведение).

  WATCHDOG: перенесён сюда из мёртвого media_engine_bridge._read_stderr
    (который парсил stderr Rust — не там, где пишется [DLL-DIAG]).
    Теперь watchdog работает на реальных RMS/peak из _dll_audio_cb и при
    затишье >= DLL_SILENCE_TIMEOUT сек вызывает _on_dll_silence() callback,
    который верхний уровень может привязать к MediaEngineBridge.restart_capture().
"""

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

# FIX #50: опциональный scipy для polyphase ресемплинга
try:
    from scipy.signal import resample_poly as _scipy_resample_poly
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False
    _scipy_resample_poly = None
    print("[StreamAudio] scipy не установлен — ресемплинг через np.interp (aliasing)")


# ---------------------------------------------------------------------------
#  Поиск DLL рядом с пакетом audio_engine
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
#  Загрузка DLL и определение ctypes-сигнатур
# ---------------------------------------------------------------------------

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


# ===========================================================================
#  StreamAudioCapture — захват системного звука через DLL
# ===========================================================================

# FIX #44: параметры ring buffer (в сэмплах, 48kHz mono)
_RING_SIZE = CHUNK_SIZE * 64  # ≈1.3 сек @ 48kHz = достаточно для всплесков jitter

# Watchdog таймауты
_DLL_SILENCE_TIMEOUT = 3.0    # если RMS=0 удерживается дольше — рестарт
_DLL_MIN_ACTIVE_SEC = 2.0     # игнорируем первые N сек после старта


class StreamAudioCapture:
    """
    Захват системного аудио через WASAPI Process Loopback (C++ DLL).

    StartCapture(exclude_pid) регистрирует PID python.exe — DLL не включает
    его аудио в loopback-поток. Голоса InPulse исключены через Session Manager
    (PROCESS_LOOPBACK_EXCLUDE) нативно.
    """

    def __init__(
        self,
        pcm_callback:  "Callable[[np.ndarray], None] | None" = None,
        audio_handler: "object | None"                        = None,
        on_dll_silence: "Callable[[], None] | None"           = None,
    ):
        """
        pcm_callback(chunk: np.ndarray[float32, (CHUNK_SIZE,)])
            Вызывается для каждого 20-мс фрейма.
        audio_handler: не используется, оставлен для обратной совместимости.
        on_dll_silence: вызывается если RMS=0 удерживается дольше
            _DLL_SILENCE_TIMEOUT секунд. MediaEngineBridge.restart_capture()
            может быть передан сюда. Вызов происходит из watchdog-потока,
            не из audio callback'а.
        """
        self._pcm_callback    = pcm_callback
        self._audio_handler   = audio_handler
        self._on_dll_silence  = on_dll_silence

        # FIX #44: ring buffer вместо linear buffer.
        # write_pos и read_pos перемещаются по модулю _RING_SIZE.
        # Корректные нарезки достигаются без memmove за O(1) на каждый chunk.
        self._ring = np.zeros(_RING_SIZE, dtype=np.float32)
        self._write_pos: int = 0
        self._read_pos:  int = 0
        self._available: int = 0   # количество сэмплов готовых к чтению

        self._buffer_lock = threading.Lock()

        # ctypes-колбэк — держим ссылку, иначе GC освободит указатель
        self._c_callback: "_AudioCallbackType | None" = None
        self._started = False

        # Диагностика захвата (только при AUDIO_DIAG_ENABLED)
        self._log_rms_accum: float = 0.0
        self._log_rms_count: int   = 0
        self._log_next_ts:   float = 0.0

        # Watchdog state
        self._watchdog_thread: "threading.Thread | None" = None
        self._watchdog_stop_evt = threading.Event()
        self._last_active_ts: float = 0.0  # perf_counter когда был последний RMS>0
        self._start_ts: float = 0.0        # когда стартовали (для _DLL_MIN_ACTIVE_SEC)
        self._silence_triggered: bool = False   # чтобы не зацикливать callbacks

    @property
    def render_active(self) -> bool:
        """Всегда False — DLL Render удалён."""
        return False

    # ------------------------------------------------------------------
    #  Публичный интерфейс
    # ------------------------------------------------------------------

    def start(self, device_idx=None):
        """Запускает захват системного звука через DLL Process Loopback."""
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

            # Запускаем watchdog (если есть колбэк)
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
        """Останавливает DLL Capture и watchdog."""
        if not self._started:
            return

        # Остановим watchdog первым, чтобы он не триггернулся во время shutdown
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

    # ------------------------------------------------------------------
    #  Обратная совместимость: list_wasapi_output_devices
    # ------------------------------------------------------------------
    @staticmethod
    def list_wasapi_output_devices():
        """DLL выбирает дефолтный Render endpoint автоматически."""
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

    # ------------------------------------------------------------------
    #  Watchdog
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """
        Следит за _last_active_ts. Если прошло больше _DLL_SILENCE_TIMEOUT
        секунд с момента последнего RMS>0 (и мы не в стартовой фазе) —
        вызывает _on_dll_silence() callback один раз, помечает triggered.
        Сбрасывается автоматически когда появляется звук.
        """
        while not self._watchdog_stop_evt.is_set():
            if self._watchdog_stop_evt.wait(0.5):
                break

            if not self._started:
                continue

            now = time.perf_counter()
            if now - self._start_ts < _DLL_MIN_ACTIVE_SEC:
                continue  # стартовая фаза — захват ещё не прогрелся

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
                    # Сбрасываем таймер: после рестарта даём _DLL_SILENCE_TIMEOUT
                    # чтобы заново оценить ситуацию.
                    self._last_active_ts = now

    # ------------------------------------------------------------------
    #  Ring buffer helpers
    # ------------------------------------------------------------------

    def _ring_write(self, samples: np.ndarray) -> None:
        """Пишет samples в ring buffer. Оверфлоу перезаписывает старые данные."""
        n = len(samples)
        if n == 0:
            return

        # Если данных больше чем места — потеряется начало (буфер не успевает)
        if n >= _RING_SIZE:
            # Крайний случай: отбрасываем лишние старые сэмплы, берём хвост
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
            # Помещается одним куском
            self._ring[wp:end] = samples
            self._write_pos = end % _RING_SIZE
        else:
            # Нужна двойная запись (wrap)
            split = _RING_SIZE - wp
            self._ring[wp:] = samples[:split]
            self._ring[:n - split] = samples[split:]
            self._write_pos = n - split

        self._available += n
        # Overflow защита: если write догоняет read, сдвигаем read вперёд
        if self._available > _RING_SIZE:
            drop = self._available - _RING_SIZE
            self._read_pos = (self._read_pos + drop) % _RING_SIZE
            self._available = _RING_SIZE
            # Это штатная ситуация если callback отстаёт — молча отбрасываем

    def _ring_read_chunk(self) -> "np.ndarray | None":
        """Читает CHUNK_SIZE сэмплов если они доступны. O(1) по возможности."""
        if self._available < CHUNK_SIZE:
            return None

        rp = self._read_pos
        end = rp + CHUNK_SIZE

        if end <= _RING_SIZE:
            chunk = self._ring[rp:end].copy()
            self._read_pos = end % _RING_SIZE
        else:
            # Wrap: собираем два куска (одно копирование неизбежно)
            split = _RING_SIZE - rp
            chunk = np.empty(CHUNK_SIZE, dtype=np.float32)
            chunk[:split] = self._ring[rp:]
            chunk[split:] = self._ring[:CHUNK_SIZE - split]
            self._read_pos = CHUNK_SIZE - split

        self._available -= CHUNK_SIZE
        return chunk

    # ------------------------------------------------------------------
    #  DLL-колбэк (вызывается из C++-потока DLL)
    # ------------------------------------------------------------------

    def _dll_audio_cb(
        self,
        pcm_ptr:     ctypes.POINTER(ctypes.c_float),
        num_frames:  int,
        channels:    int,
        sample_rate: int,
    ) -> None:
        """
        Вызывается C++-потоком DLL на каждый WASAPI-буфер (~10 мс).
        """
        if not self._started or num_frames <= 0:
            return

        try:
            # ── 1. C-указатель → numpy с НЕМЕДЛЕННОЙ копией ───────────────────
            # FIX #43: as_array создаёт view на C-память, которая живёт только
            # во время callback. Любой код ниже (особенно под локом) может
            # release GIL — тогда DLL может перезаписать/освободить буфер.
            # Копируем СРАЗУ в owned numpy-массив, дальше работаем с ним.
            total_samples = num_frames * channels
            raw_view = np.ctypeslib.as_array(pcm_ptr, shape=(total_samples,))
            raw = raw_view.copy()   # <-- вот эта строка.

            # ── 2. Stereo → Mono ───────────────────────────────────────────────
            if channels > 1:
                mono = raw.reshape(num_frames, channels).mean(axis=1).astype(np.float32)
            else:
                mono = raw  # уже копия из шага 1

            # ── 3. Ресемплинг ──────────────────────────────────────────────
            if sample_rate != SAMPLE_RATE:
                mono = self._resample(mono, sample_rate, SAMPLE_RATE)

            # ── 4. Обновляем watchdog: считаем peak и RMS ДО лока ──────────
            # Быстрая проверка activity для watchdog (дёшево, всегда считаем)
            peak_now = float(np.abs(mono).max()) if len(mono) else 0.0
            if peak_now > 0.001:   # порог ~ -60 dB
                self._last_active_ts = time.perf_counter()

            # Полная диагностика только при AUDIO_DIAG_ENABLED
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

            # ── 5. Ring buffer + нарезка по CHUNK_SIZE ────────────────────
            chunks_to_emit = []
            with self._buffer_lock:
                self._ring_write(mono)
                while True:
                    chunk = self._ring_read_chunk()
                    if chunk is None:
                        break
                    chunks_to_emit.append(chunk)

            # Callback вызываем ВНЕ лока, чтобы не удерживать лок во время
            # WebRTC push (который может заблокироваться на очереди).
            if self._pcm_callback is not None:
                for chunk in chunks_to_emit:
                    try:
                        self._pcm_callback(chunk)
                    except Exception as e:
                        print(f"[StreamAudio] pcm_callback error: {e}")

        except Exception as exc:
            print(f"[StreamAudio] _dll_audio_cb exception: {exc}")

    # ------------------------------------------------------------------
    #  Ресемплинг
    # ------------------------------------------------------------------

    @staticmethod
    def _resample(mono: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """
        FIX #50: правильный ресемплинг через scipy.signal.resample_poly.
        Polyphase FIR с low-pass фильтром — без aliasing на музыке.
        Fallback: np.interp (оставлен для систем без scipy).
        """
        if src_sr == dst_sr:
            return mono.astype(np.float32, copy=False)

        if _SCIPY_AVAILABLE:
            # resample_poly(x, up, down) — рациональное соотношение.
            # Находим GCD чтобы минимизировать размеры фильтра.
            from math import gcd
            g = gcd(dst_sr, src_sr)
            up   = dst_sr // g
            down = src_sr // g
            try:
                out = _scipy_resample_poly(mono, up, down).astype(np.float32)
                return out
            except Exception as e:
                print(f"[StreamAudio] resample_poly failed: {e} → fallback на np.interp")

        # Fallback: np.interp (линейный, с aliasing)
        num_frames = len(mono)
        target_len = int(round(num_frames * dst_sr / src_sr))
        if target_len <= 0:
            return np.zeros(0, dtype=np.float32)
        x_old = np.linspace(0.0, 1.0, num_frames, dtype=np.float64)
        x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float64)
        return np.interp(x_new, x_old, mono).astype(np.float32)


# ===========================================================================
#  WebRTC Audio Tracks
# ===========================================================================

class MicrophoneTrack(AudioStreamTrack):
    """Захват микрофона через sounddevice → WebRTC AudioStreamTrack."""

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
    """
    Захват системного звука (WASAPI Loopback, с исключением PID) → WebRTC.

    Принимает media_bridge для перенаправления watchdog callback'а в
    RESTART_CAPTURE Rust команду при обнаружении зависшего DLL Capture.
    """

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
        """
        Вызывается из watchdog-потока при обнаружении зависшего DLL Capture.
        Перенаправляем в MediaEngineBridge.restart_capture() если он есть.
        """
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
