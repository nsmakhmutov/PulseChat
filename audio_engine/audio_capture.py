"""
audio_capture.py  —  InPulse Audio Engine  (DLL-редакция)
==========================================================
StreamAudioCapture полностью переписан:
  • Убраны зависимости pyaudiowpatch / sounddevice для системного звука.
  • Захват идёт через InPulseAudioExclusion.dll (WASAPI Process Loopback).
  • DLL исключает звук самого процесса InPulse → зрители не слышат себя.
  • Вся логика нарезки буфера, ресемплинга и моно-свёртки сохранена as-is.

MicrophoneTrack и SystemAudioTrack — БЕЗ изменений.
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
        f"  и скопируйте InPulseAudioExclusion.dll в папку dlls/ проекта."
    )


# ---------------------------------------------------------------------------
#  Загрузка DLL и определение ctypes-сигнатур  (один раз при импорте модуля)
# ---------------------------------------------------------------------------

# Сигнатура callback-функции, которую DLL вызывает с PCM-данными:
#   void callback(float* pcm_data, int num_frames, int channels, int sample_rate)
_AudioCallbackType = ctypes.CFUNCTYPE(
    None,                    # возвращаемый тип
    ctypes.POINTER(ctypes.c_float),  # pcm_data
    ctypes.c_int,            # num_frames
    ctypes.c_int,            # channels
    ctypes.c_int,            # sample_rate
)

_dll: "ctypes.CDLL | None" = None
_dll_load_error: "str | None" = None


def _load_dll() -> "ctypes.CDLL | None":
    """
    Загружает DLL и устанавливает типы аргументов/возврата.
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

        # bool StartCapture(DWORD exclude_pid, AudioCallback callback)
        lib.StartCapture.restype  = ctypes.c_bool
        lib.StartCapture.argtypes = [ctypes.c_ulong, _AudioCallbackType]

        # void StopCapture()
        lib.StopCapture.restype  = None
        lib.StopCapture.argtypes = []

        _dll = lib
        print(f"[StreamAudio] DLL загружена: {dll_path}")
        return _dll

    except (FileNotFoundError, OSError) as e:
        _dll_load_error = str(e)
        print(f"[StreamAudio] ОШИБКА загрузки DLL: {e}")
        return None


# ===========================================================================
#  StreamAudioCapture  —  захват системного звука через DLL
# ===========================================================================

class StreamAudioCapture:
    """
    Захват системного аудио через WASAPI Process Loopback (C++ DLL).

    DLL запускает нативный поток WASAPI, исключает звук текущего процесса
    (exclude_pid = os.getpid()) и вызывает Python-колбэк с сырыми float32 PCM.
    Python-сторона: ресемплинг → моно-свёртка → нарезка по CHUNK_SIZE → WebRTC.

    Жизненный цикл:
        capture = StreamAudioCapture(pcm_callback=track._on_pcm_chunk)
        capture.start()
        capture.stop()
    """

    def __init__(self, pcm_callback: "Callable[[np.ndarray], None] | None" = None):
        """
        pcm_callback(chunk: np.ndarray[float32, shape=(CHUNK_SIZE,)])
            Вызывается для каждого готового 20-мс фрейма (960 сэмплов @ 48 кГц).
            SystemAudioTrack кладёт chunk в asyncio.Queue → recv() → WebRTC.
        """
        self._pcm_callback = pcm_callback

        # Предаллоцированный буфер — никаких np.concatenate в hot path.
        # Расчёт запаса: DLL отдаёт буферы ~10 мс (480 сэмплов @ 48 кГц × 2 кан.),
        # CHUNK_SIZE = 960 → 8× запас полностью покрывает пиковые ситуации.
        self._pcm_buf  = np.empty(CHUNK_SIZE * 8, dtype=np.float32)
        self._pcm_len  = 0
        self._buffer_lock = threading.Lock()

        # Ссылку на ctypes-колбэк ОБЯЗАТЕЛЬНО держим как атрибут экземпляра.
        # Если gc соберёт объект — DLL вызовет мусорный указатель → крэш.
        self._c_callback: "_AudioCallbackType | None" = None

        self._started = False   # флаг: DLL::StartCapture вызван успешно

    # ------------------------------------------------------------------
    #  Публичный интерфейс
    # ------------------------------------------------------------------

    def start(self, device_idx=None):
        """
        Запускает захват через DLL.
        device_idx — игнорируется (DLL всегда берёт дефолтное Render-устройство).
        Оставлен для совместимости с SystemAudioTrack.
        """
        if self._started:
            self.stop()

        lib = _load_dll()
        if lib is None:
            print("[StreamAudio] DLL недоступна — захват системного звука отключён")
            return

        # Сброс буфера
        with self._buffer_lock:
            self._pcm_len = 0

        # Создаём ctypes-обёртку над методом и сохраняем ссылку
        self._c_callback = _AudioCallbackType(self._dll_audio_cb)

        exclude_pid = os.getpid()
        ok = lib.StartCapture(ctypes.c_ulong(exclude_pid), self._c_callback)
        if ok:
            self._started = True
            print(f"[StreamAudio] ✔ DLL захват запущен (exclude PID={exclude_pid})")
        else:
            print("[StreamAudio] ✘ DLL::StartCapture вернула false — проверьте вывод DLL")
            self._c_callback = None

    def stop(self):
        """Останавливает захват и освобождает ресурсы DLL."""
        if not self._started:
            return
        lib = _load_dll()
        if lib is not None:
            lib.StopCapture()
        self._started = False
        # Теперь DLL не будет вызывать колбэк → можно убрать ссылку
        self._c_callback = None
        print("[StreamAudio] DLL захват остановлен")

    # ------------------------------------------------------------------
    #  Обратная совместимость: list_wasapi_output_devices
    #  (вызывается из ui_stream_settings.py)
    # ------------------------------------------------------------------
    @staticmethod
    def list_wasapi_output_devices():
        """
        DLL автоматически выбирает дефолтное Render-устройство.
        Метод оставлен для совместимости с UI — возвращает текущее устройство.
        """
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
        pcm_ptr:    ctypes.POINTER(ctypes.c_float),
        num_frames: int,
        channels:   int,
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

        GIL: ctypes CFUNCTYPE автоматически снимает GIL на стороне C++
        и восстанавливает его перед вызовом этой функции.
        Вся обработка происходит в нативном потоке DLL, не блокируя asyncio.
        """
        if not self._started or num_frames <= 0:
            return

        try:
            # ── 1. C-указатель → numpy (zero-copy) ────────────────────────────
            # as_array не копирует данные; lifetime буфера гарантирован до
            # ReleaseBuffer внутри DLL — мы успеем скопировать нужное.
            total_samples = num_frames * channels
            raw = np.ctypeslib.as_array(pcm_ptr, shape=(total_samples,))

            # ── 2. Stereo → Mono ───────────────────────────────────────────────
            if channels > 1:
                # reshape (num_frames, channels) → mean по оси 1
                mono = raw.reshape(num_frames, channels).mean(axis=1).astype(np.float32)
            else:
                # Копируем явно — буфер DLL будет перезаписан после return
                mono = raw.copy()

            # ── 3. Ресемплинг (если устройство не 48 кГц) ─────────────────────
            # np.interp — линейная интерполяция; для голосового сигнала (300 Гц –
            # 8 кГц полоса Opus) артефактов не даёт. Тяжёлый ресемплинг
            # (scipy.resample) здесь избыточен и добавляет задержку.
            if sample_rate != SAMPLE_RATE:
                target_len = int(round(num_frames * SAMPLE_RATE / sample_rate))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, num_frames,   dtype=np.float64)
                    x_new = np.linspace(0.0, 1.0, target_len,   dtype=np.float64)
                    mono  = np.interp(x_new, x_old, mono).astype(np.float32)

            # ── 4 & 5. Буфер + нарезка по CHUNK_SIZE ──────────────────────────
            with self._buffer_lock:
                incoming = len(mono)
                needed   = self._pcm_len + incoming

                # Расширяем буфер только при редком переполнении
                if needed > len(self._pcm_buf):
                    new_size = max(needed, len(self._pcm_buf) * 2)
                    new_buf  = np.empty(new_size, dtype=np.float32)
                    new_buf[:self._pcm_len] = self._pcm_buf[:self._pcm_len]
                    self._pcm_buf = new_buf
                    print(f"[StreamAudio] Буфер расширен до {new_size} сэмплов")

                # Дозаписываем без аллокаций
                self._pcm_buf[self._pcm_len:self._pcm_len + incoming] = mono
                self._pcm_len += incoming

                # Откусываем строго CHUNK_SIZE и передаём наверх
                while self._pcm_len >= CHUNK_SIZE:
                    chunk = self._pcm_buf[:CHUNK_SIZE].copy()

                    # Сдвиг остатка влево (C-уровень numpy, ~0 overhead)
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
            # Никогда не падаем — исключение в колбэке DLL = UB
            print(f"[StreamAudio] _dll_audio_cb exception: {exc}")


# ===========================================================================
#  WebRTC Audio Tracks  (без изменений)
# ===========================================================================

class MicrophoneTrack(AudioStreamTrack):
    """
    Захват микрофона через sounddevice → WebRTC AudioStreamTrack.
    НЕ затронут рефакторингом StreamAudioCapture.
    """

    kind = "audio"

    def __init__(self, device_name: str = None):
        super().__init__()
        self._device   = device_name
        self._running  = True
        self._queue:  "asyncio.Queue | None" = None
        self._loop:   "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None
        # Ручной счётчик PTS — не зависит от версии aiortc
        self._pts: int = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)

    def _capture_loop(self) -> None:
        def _cb(indata: np.ndarray, frames: int, time_info, status) -> None:
            if not self._running or self._loop is None or self._queue is None:
                return
            # FIX: call_soon_threadsafe + put_nowait вместо run_coroutine_threadsafe(put).
            # put() — async coroutine; при переполнении очереди корутины накапливались
            # в event loop → задержка и нагрузка CPU. Синхронный put_nowait реалтайм.
            data = indata.copy()
            def _sync_put():
                try:
                    if self._queue.full():
                        self._queue.get_nowait()   # дропаем старый кадр, сохраняем реалтайм
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

        frame = av.AudioFrame.from_ndarray(
            data.T, format='s16', layout='mono'
        )
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
    Вся логика ленивой инициализации и очереди — без изменений.
    """

    kind = "audio"

    def __init__(self, device_idx: int = None):
        super().__init__()
        self._device_idx = device_idx
        self._running    = True
        self._queue:   "asyncio.Queue | None"             = None
        self._loop:    "asyncio.AbstractEventLoop | None" = None
        self._capture: "StreamAudioCapture | None"        = None
        # Ручной счётчик PTS — не зависит от версии aiortc
        self._pts: int = 0
        self._time_base = fractions.Fraction(1, SAMPLE_RATE)

    def _on_pcm_chunk(self, chunk: np.ndarray) -> None:
        """
        float32 моно фрейм (CHUNK_SIZE,) из StreamAudioCapture.
        Отправляет в asyncio.Queue WebRTC-цикла.
        chunk уже является .copy() из _dll_audio_cb.
        """
        if not self._running or self._loop is None or self._queue is None:
            return
        # FIX: call_soon_threadsafe + put_nowait — без накопления корутин.
        # run_coroutine_threadsafe(put) при полной очереди создавал ожидающие
        # корутины в event loop → задержка нарастала, CPU рос.
        def _sync_put():
            try:
                if self._queue.full():
                    self._queue.get_nowait()   # дропаем старый кадр, сохраняем реалтайм
                self._queue.put_nowait(chunk)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(_sync_put)

    async def recv(self) -> av.AudioFrame:
        if self._queue is None:
            self._loop    = asyncio.get_event_loop()
            self._queue   = asyncio.Queue(maxsize=10)
            self._capture = StreamAudioCapture(pcm_callback=self._on_pcm_chunk)
            self._capture.start(self._device_idx)
            print("[SystemAudioTrack] Захват системного звука запущен (DLL)")

        data = await self._queue.get()  # float32 mono (CHUNK_SIZE,)

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