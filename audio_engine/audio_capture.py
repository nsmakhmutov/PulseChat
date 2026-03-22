"""
audio_capture.py  —  InPulse Audio Engine  (DLL-редакция v3, Gate-Free)
=========================================================================
StreamAudioCapture:
  • Захват системного звука через InPulseAudioExclusion.dll (WASAPI Process Loopback).
  • DLL исключает звук процесса InPulse через PROCESS_LOOPBACK_EXCLUDE:
      - StartRender() открывает RAW WASAPI сессию (AUDCLNT_STREAMFLAGS_RAW),
        звук идёт в обход audiodg.exe/APO → PID python.exe сохранён.
      - StartCapture(exclude_pid) захватывает loopback, исключая наш PID.
      - Зрители не слышат голоса участников чата InPulse в стриме.

v3 (Gate-Free):
  • Программный голосовой gate полностью удалён.
    Gate был костылём: глушил стрим-аудио при каждом голосовом сообщении
    в чате (зритель говорил → стример слышал → gate срабатывал → тишина).
  • Исключение InPulse целиком обеспечивает DLL нативно.
  • voice_gate / _ec_voice_ring / echo-cancellation fallback удалены.
  • StreamAudioCapture и SystemAudioTrack больше не принимают voice_gate.

Жизненный цикл:
    capture = StreamAudioCapture(pcm_callback=..., audio_handler=...)
    capture.start()   →  StartRender() + StartCapture()
    capture.stop()    →  StopCapture() + StopRender()
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

from config import SAMPLE_RATE, CHANNELS, CHUNK_SIZE
from .audio_processing import PYRNNOISE_AVAILABLE

try:
    from pyrnnoise import RNNoise
except ImportError:
    RNNoise = None


# ---------------------------------------------------------------------------
#  Поиск DLL рядом с пакетом audio_engine
# ---------------------------------------------------------------------------
def _find_dll() -> Path:
    """
    Ищет InPulseAudioExclusion.dll в нескольких ожидаемых местах:
      1. <project_root>/dlls/   — стандартное место рядом с DeepFilterNet
      2. <audio_engine dir>/    — рядом с этим модулем
      3. Папка запуска (cwd)
    """
    dll_name = "InPulseAudioExclusion.dll"
    candidates = [
        Path(__file__).parent.parent / "dlls" / dll_name,  # project/dlls/
        Path(__file__).parent / dll_name,                   # audio_engine/
        Path(os.getcwd()) / dll_name,                       # cwd
        Path(os.getcwd()) / "dlls" / dll_name,
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        f"[StreamAudio] DLL '{dll_name}' не найдена.\n"
        f"  Ожидаемые места: {[str(c) for c in candidates]}\n"
        f"  Скомпилируйте LoopbackCapture.cpp → Release/x64 в Visual Studio\n"
        f"  (Project Properties → Linker → Output File: InPulseAudioExclusion.dll)\n"
        f"  и скопируйте в папку dlls/ проекта."
    )


# ---------------------------------------------------------------------------
#  Загрузка DLL и определение ctypes-сигнатур  (один раз при импорте модуля)
# ---------------------------------------------------------------------------

# Сигнатура capture-callback:
#   void callback(float* pcm_data, int num_frames, int channels, int sample_rate)
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
    """
    Загружает DLL и устанавливает типы аргументов/возврата для ВСЕХ функций:
      Capture:  StartCapture, StopCapture
      Render:   StartRender, StopRender, PlayAudioChunk, IsRawModeActive,
                IsWasapiReady (v3)
    Вызывается лениво при первом start().
    """
    global _dll, _dll_load_error
    if _dll is not None:
        return _dll
    if _dll_load_error is not None:
        return None

    try:
        dll_path = _find_dll()
        lib = ctypes.CDLL(str(dll_path))

        # ── Capture ──────────────────────────────────────────────────────────
        # bool StartCapture(DWORD exclude_pid, AudioCallback callback)
        lib.StartCapture.restype  = ctypes.c_bool
        lib.StartCapture.argtypes = [ctypes.c_ulong, _AudioCallbackType]

        # void StopCapture()
        lib.StopCapture.restype  = None
        lib.StopCapture.argtypes = []

        # ── Render ────────────────────────────────────────────────────────────
        # bool StartRender()
        lib.StartRender.restype  = ctypes.c_bool
        lib.StartRender.argtypes = []

        # void StopRender()
        lib.StopRender.restype  = None
        lib.StopRender.argtypes = []

        # bool PlayAudioChunk(float* pcm, int num_frames, int src_channels, int src_sr)
        lib.PlayAudioChunk.restype  = ctypes.c_bool
        lib.PlayAudioChunk.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]

        # bool IsRawModeActive()
        lib.IsRawModeActive.restype  = ctypes.c_bool
        lib.IsRawModeActive.argtypes = []

        # bool IsWasapiReady()  — проверка что RenderThread завершил WASAPI init
        try:
            lib.IsWasapiReady.restype  = ctypes.c_bool
            lib.IsWasapiReady.argtypes = []
        except AttributeError:
            lib.IsWasapiReady = lambda: True

        # bool IsRenderPollingMode()  — v4: polling или event-driven режим
        try:
            lib.IsRenderPollingMode.restype  = ctypes.c_bool
            lib.IsRenderPollingMode.argtypes = []
        except AttributeError:
            lib.IsRenderPollingMode = lambda: False

        # int ReadRenderedAudio(float* dst, int max_frames, int dst_channels)
        # Читает из render tap буфера точно то, что было отдано в WASAPI hardware.
        # Используется для software EC когда RAW mode недоступен.
        try:
            lib.ReadRenderedAudio.restype  = ctypes.c_int
            lib.ReadRenderedAudio.argtypes = [
                ctypes.POINTER(ctypes.c_float),
                ctypes.c_int,
                ctypes.c_int,
            ]
        except AttributeError:
            lib.ReadRenderedAudio = lambda _p, _f, _c: 0

        # int GetRenderedAudioAvailable()
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
    """Возвращает загруженную DLL (или None). Используется AudioHandler."""
    return _dll


# ===========================================================================
#  StreamAudioCapture  —  захват системного звука через DLL
# ===========================================================================

class StreamAudioCapture:
    """
    Захват системного аудио через WASAPI Process Loopback (C++ DLL).

    StartCapture(exclude_pid) регистрирует PID python.exe — DLL не включает
    его аудио в loopback-поток. StartRender() открывает RAW WASAPI render-сессию
    (AUDCLNT_STREAMFLAGS_RAW): звук идёт в обход audiodg.exe/APO →
    Windows корректно атрибутирует PID → PROCESS_LOOPBACK_EXCLUDE работает.

    AudioHandler при render_active=True глушит PortAudio outdata (fill 0)
    и передаёт mix_buffer в DLL PlayAudioChunk() — голосовой чат идёт
    через RAW сессию, и значит тоже исключён из loopback.

    Жизненный цикл:
        capture = StreamAudioCapture(pcm_callback=..., audio_handler=...)
        capture.start()    →  StartRender() + StartCapture()
        capture.stop()     →  StopCapture() + StopRender()
    """

    def __init__(
        self,
        pcm_callback:  "Callable[[np.ndarray], None] | None" = None,
        audio_handler: "object | None"                        = None,
    ):
        """
        pcm_callback(chunk: np.ndarray[float32, (CHUNK_SIZE,)])
            Вызывается для каждого 20-мс фрейма.
        audio_handler: не используется, оставлен для обратной совместимости.

        Архитектура (v4 — без DLL Render):
        Голосовой чат воспроизводится через PortAudio WASAPI Shared сессию.
        Эта сессия принадлежит python.exe PID → Session Manager её видит.
        PROCESS_LOOPBACK_EXCLUDE(python.exe) исключает её из захвата нативно.
        Отдельный DLL Render (RAW WASAPI) удалён — он давал обратный эффект
        из-за аппаратного DSP микшера USB устройств (Sound Blaster и т.п.).
        """
        self._pcm_callback   = pcm_callback
        # audio_handler сохраняем для совместимости, но не используем
        self._audio_handler: "object | None" = audio_handler

        # Предаллоцированный буфер (8× CHUNK_SIZE запаса)
        self._pcm_buf  = np.empty(CHUNK_SIZE * 8, dtype=np.float32)
        self._pcm_len  = 0
        self._buffer_lock = threading.Lock()

        # Ссылку на ctypes-колбэк держим как атрибут — gc не соберёт указатель
        self._c_callback: "_AudioCallbackType | None" = None
        self._started = False   # флаг: DLL::StartCapture вызван успешно

        # Диагностика захвата
        self._log_rms_accum: float = 0.0
        self._log_rms_count: int   = 0
        self._log_next_ts:   float = 0.0

    # ------------------------------------------------------------------
    #  Свойство: запущен ли DLL render
    # ------------------------------------------------------------------

    @property
    def render_active(self) -> bool:
        """Всегда False — DLL Render удалён. Только Capture."""
        return False

    # ------------------------------------------------------------------
    #  Публичный интерфейс
    # ------------------------------------------------------------------

    def start(self, device_idx=None):
        """
        Запускает захват системного звука через DLL Process Loopback.

        ТОЛЬКО StartCapture(exclude_pid) — никакого StartRender.

        Голосовой чат воспроизводится через PortAudio WASAPI Shared сессию
        (AttributedTo python.exe PID). PROCESS_LOOPBACK_EXCLUDE(python.exe)
        исключает эту сессию из захвата нативно на уровне Session Manager —
        точно так же как это делают Discord, Zoom, Telegram.

        Отдельный DLL Render (RAW WASAPI) не нужен и вреден:
        - RAW сессии могут обходить Session Manager tracking
        - USB аудиоустройства (Sound Blaster) имеют аппаратный DSP mixer
          который смешивает всё ДО Session Manager → EXCLUDE не работает
        - PortAudio WASAPI Shared сессии исключаются надёжно
        """
        if self._started:
            self.stop()

        lib = _load_dll()
        if lib is None:
            print("[StreamAudio] DLL недоступна — захват системного звука отключён")
            return

        with self._buffer_lock:
            self._pcm_len = 0

        self._c_callback = _AudioCallbackType(self._dll_audio_cb)
        exclude_pid = os.getpid()
        try:
            ok = bool(lib.StartCapture(ctypes.c_ulong(exclude_pid), self._c_callback))
        except OSError as e:
            ok = False
            print(f"[StreamAudio] ✘ DLL::StartCapture() исключение: {e}")

        if ok:
            self._started = True
            print(f"[StreamAudio] ✔ DLL Capture запущен (exclude PID={exclude_pid})"
                  f" — голоса InPulse исключены через Session Manager")
        else:
            print("[StreamAudio] ✘ DLL::StartCapture вернула false")
            self._c_callback = None

    def stop(self):
        """Останавливает DLL Capture."""
        if not self._started:
            return

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

        Порядок обработки:
          1. C-указатель → numpy (zero-copy через as_array)
          2. Stereo → Mono (усреднение)
          3. Ресемплинг, если sample_rate ≠ 48 000 Гц
          4. Дозапись в предаллоцированный буфер
          5. Нарезка CHUNK_SIZE-кусков → _pcm_callback (SystemAudioTrack)

        Голоса InPulse исключены PROCESS_LOOPBACK_EXCLUDE через Session Manager.
        PortAudio WASAPI Shared сессии (python.exe PID) не попадают в захват.
        """
        if not self._started or num_frames <= 0:
            return

        try:
            # ── 1. C-указатель → numpy (zero-copy) ────────────────────────────
            total_samples = num_frames * channels
            raw = np.ctypeslib.as_array(pcm_ptr, shape=(total_samples,))

            # ── 2. Stereo → Mono ───────────────────────────────────────────────
            if channels > 1:
                mono = raw.reshape(num_frames, channels).mean(axis=1).astype(np.float32)
            else:
                mono = raw.copy()

            # ── 3. Ресемплинг (если устройство не 48 кГц) ─────────────────────
            if sample_rate != SAMPLE_RATE:
                target_len = int(round(num_frames * SAMPLE_RATE / sample_rate))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, num_frames,   dtype=np.float64)
                    x_new = np.linspace(0.0, 1.0, target_len,   dtype=np.float64)
                    mono  = np.interp(x_new, x_old, mono).astype(np.float32)

            # ── 4. Диагностика ─────────────────────────────────────────────
            self._log_rms_accum += float(np.dot(mono, mono))
            self._log_rms_count += len(mono)
            _now = time.perf_counter()
            if _now >= self._log_next_ts and self._log_rms_count > 0:
                rms  = (self._log_rms_accum / self._log_rms_count) ** 0.5
                peak = float(np.abs(mono).max())
                print(
                    f"[DLL-DIAG] захват: RMS={rms:.4f}  peak={peak:.4f}"
                    f"  frames={num_frames}  ch={channels}  sr={sample_rate}"
                    f"  buf_fill={self._pcm_len}",
                    flush=True,
                )
                self._log_rms_accum = 0.0
                self._log_rms_count = 0
                self._log_next_ts   = _now + 1.0

            # ── 5 & 6. Буфер + нарезка по CHUNK_SIZE ──────────────────────────
            with self._buffer_lock:
                incoming = len(mono)
                needed   = self._pcm_len + incoming

                if needed > len(self._pcm_buf):
                    new_size = max(needed, len(self._pcm_buf) * 2)
                    new_buf  = np.empty(new_size, dtype=np.float32)
                    new_buf[:self._pcm_len] = self._pcm_buf[:self._pcm_len]
                    self._pcm_buf = new_buf
                    print(f"[StreamAudio] Буфер расширен до {new_size} сэмплов")

                self._pcm_buf[self._pcm_len:self._pcm_len + incoming] = mono
                self._pcm_len += incoming

                while self._pcm_len >= CHUNK_SIZE:
                    chunk = self._pcm_buf[:CHUNK_SIZE].copy()
                    self._pcm_len -= CHUNK_SIZE
                    self._pcm_buf[:self._pcm_len] = (
                        self._pcm_buf[CHUNK_SIZE:CHUNK_SIZE + self._pcm_len]
                    )
                    if self._pcm_callback is not None:
                        try:
                            self._pcm_callback(chunk)
                        except Exception:
                            pass

        except Exception as exc:
            print(f"[StreamAudio] _dll_audio_cb exception: {exc}")


# ===========================================================================
#  WebRTC Audio Tracks
# ===========================================================================

class MicrophoneTrack(AudioStreamTrack):
    """
    Захват микрофона через sounddevice → WebRTC AudioStreamTrack.
    Без изменений.
    """

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

    Использует StreamAudioCapture (DLL-редакция).
    Принимает audio_handler для уведомления о DLL render состоянии:
      - capture.start() → audio_handler.enable_dll_render(True)  → PortAudio молчит
      - capture.stop()  → audio_handler.enable_dll_render(False) → PortAudio активен
    """

    kind = "audio"

    def __init__(
        self,
        device_idx:    int = None,
        audio_handler: "object | None" = None,
    ):
        super().__init__()
        self._device_idx    = device_idx
        self._audio_handler = audio_handler   # AudioHandler для enable_dll_render()
        self._running       = True
        self._queue:   "asyncio.Queue | None"             = None
        self._loop:    "asyncio.AbstractEventLoop | None" = None
        self._capture: "StreamAudioCapture | None"        = None
        self._pts:  int = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)

    def _on_pcm_chunk(self, chunk: np.ndarray) -> None:
        """
        float32 моно фрейм (CHUNK_SIZE,) из StreamAudioCapture.
        Отправляет в asyncio.Queue WebRTC-цикла.
        """
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

    async def recv(self) -> av.AudioFrame:
        if self._queue is None:
            self._loop  = asyncio.get_event_loop()
            self._queue = asyncio.Queue(maxsize=10)
            self._capture = StreamAudioCapture(
                pcm_callback=self._on_pcm_chunk,
                audio_handler=self._audio_handler,
            )
            self._capture.start(self._device_idx)
            print("[SystemAudioTrack] Захват системного звука запущен DLL")

        data = await self._queue.get()   # float32 mono (CHUNK_SIZE,)

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