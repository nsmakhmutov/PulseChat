import asyncio
import threading
import queue
import time
from collections import deque
from typing import Callable
import numpy as np
import sounddevice as sd
import av
from aiortc import AudioStreamTrack
from config import (
    SAMPLE_RATE, CHANNELS, CHUNK_SIZE,
)
from .audio_processing import (
    PYRNNOISE_AVAILABLE, PYAUDIOWPATCH_AVAILABLE, _pyaudio,
    butter, sosfilt, sosfilt_zi, _WLP_SOS, _ANON_LP_SOS,
)
try:
    from pyrnnoise import RNNoise
except ImportError:
    RNNoise = None


class StreamAudioCapture:
    """
    Захват системного аудио через WASAPI Loopback для трансляции зрителям.

    Открывает loopback-поток на выбранном устройстве вывода Windows (динамики /
    наушники), кодирует в Opus и кладёт готовые пакеты в send_queue с флагом
    FLAG_STREAM_AUDIO. Зрители слышат именно то, что играет на экране стримера
    (игры, музыку, системные звуки).

    Жизненный цикл:
        capture = StreamAudioCapture(send_queue, lambda: audio.my_uid)
        capture.start(device_idx=2)   # перед стримом
        capture.stop()                # после остановки стрима
    """

    def __init__(self, pcm_callback: "Callable[[np.ndarray], None] | None" = None):
        """
        pcm_callback(chunk: np.ndarray) — вызывается для каждого 20-мс PCM-фрейма
        (float32, моно, CHUNK_SIZE сэмплов). Используется SystemAudioTrack для
        подачи фреймов в WebRTC вместо кодирования Opus + UDP-отправки.
        Если None — фреймы молча дропаются (захват работает, но данные никуда не идут).
        """
        self._pcm_callback = pcm_callback
        self._running = threading.Event()
        self._thread = None
        self._native_sr: int = SAMPLE_RATE

        # Промежуточный буфер для сборки точных 20ms фреймов (CHUNK_SIZE).
        # Предаллоцируем с запасом 8× CHUNK_SIZE — ни разу не растём при обычной работе.
        # self._pcm_len — логическая длина данных в буфере (не size буфера).
        # Это устраняет np.concatenate (50x/сек) → 0 аллокаций в hot path.
        self._pcm_buf = np.empty(CHUNK_SIZE * 8, dtype=np.float32)
        self._pcm_len = 0
        self._buffer_lock = threading.Lock()

    @staticmethod
    def list_wasapi_output_devices():
        result = []
        try:
            apis = sd.query_hostapis()
            devs = sd.query_devices()
            w_idx = next((i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None)
            if w_idx is None:
                return result
            for i, d in enumerate(devs):
                if d['hostapi'] == w_idx and d['max_output_channels'] > 0:
                    result.append((d['name'], i))
        except Exception as e:
            print(f"[StreamAudio] list_wasapi_output_devices error: {e}")
        return result

    def start(self, device_idx=None):
        self.stop()
        # Сброс буфера при старте — данные от прошлого сеанса не нужны
        with self._buffer_lock:
            self._pcm_len = 0
        self._running.set()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(device_idx,),
            daemon=True,
            name="stream-audio-loopback",
        )
        self._thread.start()

    def stop(self):
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _resolve_device(self, device_idx):
        try:
            apis = sd.query_hostapis()
            devs = sd.query_devices()
            w_idx = next((i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None)
            if w_idx is None: return None

            if device_idx is not None and device_idx < len(devs):
                d = devs[device_idx]
                if d['hostapi'] == w_idx and d['max_output_channels'] > 0:
                    return device_idx

            try:
                default_out = sd.default.device[1]
                if isinstance(default_out, int) and default_out < len(devs):
                    if devs[default_out]['hostapi'] == w_idx:
                        return default_out
            except Exception:
                pass

            for i, d in enumerate(devs):
                if d['hostapi'] == w_idx and d['max_output_channels'] > 0:
                    return i
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Стратегия A: pyaudiowpatch (главная — единственная надёжная)
    # ------------------------------------------------------------------
    def _try_pyaudiowpatch(self, target_name: str) -> bool:
        """
        Использует pyaudiowpatch для захвата WASAPI Loopback.

        pyaudiowpatch патчирует PortAudio на уровне IAudioClient и добавляет
        флаг isLoopbackDevice в device_info — только так можно достоверно
        отличить loopback endpoint от реального микрофона.

        target_name — имя OUTPUT-устройства, чей loopback нужно захватить.
        Возвращает True если поток успешно открыт и отработал до stop().
        """
        if not PYAUDIOWPATCH_AVAILABLE:
            return False

        pa = None
        stream = None
        try:
            pa = _pyaudio.PyAudio()

            # 1. Найти WASAPI host API
            wasapi_idx = None
            for i in range(pa.get_host_api_count()):
                info = pa.get_host_api_info_by_index(i)
                if 'WASAPI' in info.get('name', ''):
                    wasapi_idx = i
                    break
            if wasapi_idx is None:
                print("[StreamAudio] [A] WASAPI host API не найден в pyaudiowpatch")
                return False

            # 2. Найти loopback device, соответствующий target output
            #    Порядок: точное совпадение → частичное → первый попавшийся isLoopback
            loopback_dev = None
            target_lower = target_name.lower()

            candidates = []
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if not d.get('isLoopbackDevice', False):
                    continue
                if d.get('hostApi') != wasapi_idx:
                    continue
                candidates.append(d)

            if not candidates:
                print("[StreamAudio] [A] pyaudiowpatch: loopback-устройства не найдены")
                return False

            # Точное совпадение имени
            for d in candidates:
                if d['name'].lower() == target_lower:
                    loopback_dev = d
                    break
            # Частичное совпадение
            if loopback_dev is None:
                for d in candidates:
                    dev_lower = d['name'].lower()
                    if target_lower in dev_lower or dev_lower in target_lower:
                        loopback_dev = d
                        break
            # Первый доступный
            if loopback_dev is None:
                loopback_dev = candidates[0]
                print(f"[StreamAudio] [A] Точного совпадения нет, берём первый loopback: "
                      f"«{loopback_dev['name']}»")

            ch = max(1, int(loopback_dev.get('maxInputChannels', 2)))
            sr = int(loopback_dev.get('defaultSampleRate', SAMPLE_RATE))
            self._native_sr = sr

            print(f"[StreamAudio] [A] pyaudiowpatch loopback device: "
                  f"«{loopback_dev['name']}» idx={loopback_dev['index']} ch={ch} sr={sr}")

            # 3. Открыть поток с callback
            def _pa_callback(in_data, frame_count, time_info, status):
                if not self._running.is_set():
                    return (None, _pyaudio.paComplete)
                try:
                    arr = np.frombuffer(in_data, dtype=np.float32).copy()
                    # reshape к (frames, channels) чтобы _audio_cb мог усреднить каналы
                    arr = arr.reshape(-1, ch)
                    self._audio_cb(arr, frame_count, time_info, status)
                except Exception:
                    pass
                return (None, _pyaudio.paContinue)

            stream = pa.open(
                format=_pyaudio.paFloat32,
                channels=ch,
                rate=sr,
                frames_per_buffer=CHUNK_SIZE,
                input=True,
                input_device_index=loopback_dev['index'],
                stream_callback=_pa_callback,
            )
            stream.start_stream()
            print(f"[StreamAudio] ✔ [A] pyaudiowpatch захват запущен (ch={ch} sr={sr})")

            while self._running.is_set():
                if not stream.is_active():
                    print("[StreamAudio] [A] Поток pyaudiowpatch неожиданно завершился")
                    break
                time.sleep(0.05)

            return True

        except Exception as e:
            print(f"[StreamAudio] [A] pyaudiowpatch ошибка: {e}")
            return False
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Стратегия B: sounddevice + WasapiSettings(loopback=True)
    # Работает только если PortAudio собран с поддержкой WASAPI loopback.
    # На Sound Blaster Play! 4 и многих других картах ПАДАЕТ с -9998,
    # потому что PortAudio проверяет max_input_channels==0 ДО того как
    # применить loopback-флаг к IAudioClient.
    # ------------------------------------------------------------------
    def _try_sounddevice_loopback(self, resolved: int, native_ch: int) -> bool:
        """
        Пробует открыть OUTPUT-устройство как loopback через sounddevice.
        Перебирает несколько сигнатур WasapiSettings и несколько channel counts.
        Возвращает True если успешно.
        """
        # Построить WasapiSettings — перебираем сигнатуры (разные версии sd)
        wasapi_settings = None
        for factory in [
            lambda: sd.WasapiSettings(loopback=True),
            lambda: sd.WasapiSettings(exclusive=False, loopback=True),
            lambda: sd.WasapiSettings(False, True),
        ]:
            try:
                obj = factory()
                if hasattr(obj, 'loopback') and not obj.loopback:
                    try:
                        obj.loopback = True
                    except Exception:
                        pass
                wasapi_settings = obj
                break
            except Exception:
                continue

        if wasapi_settings is None:
            print("[StreamAudio] [B] WasapiSettings недоступен")
            return False

        for ch in list(dict.fromkeys([native_ch, 2, 1])):
            try:
                with sd.InputStream(
                        device=resolved,
                        samplerate=self._native_sr,
                        channels=ch,
                        dtype='float32',
                        extra_settings=wasapi_settings,
                        callback=self._audio_cb,
                ):
                    print(f"[StreamAudio] ✔ [B] sounddevice loopback (ch={ch} sr={self._native_sr})")
                    while self._running.is_set():
                        time.sleep(0.05)
                return True
            except Exception as e:
                print(f"[StreamAudio] [B] Не удалось с channels={ch}: {e}")
        return False

    # ------------------------------------------------------------------
    # Аудио-колбэк: принимает фреймы от sounddevice, собирает 20ms чанки
    # ------------------------------------------------------------------
    def _audio_cb(self, indata, frames, time_info, status):
        if not self._running.is_set():
            return

        try:
            if indata.ndim > 1 and indata.shape[1] > 1:
                mono = np.mean(indata, axis=1)
            else:
                mono = indata.flatten()

            # Ресемплинг
            if self._native_sr != SAMPLE_RATE:
                target_len = int(round(len(mono) * SAMPLE_RATE / self._native_sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(mono), dtype=np.float64)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float64)
                    mono = np.interp(x_new, x_old, mono).astype(np.float32)

            with self._buffer_lock:
                # ── Записываем семплы в предаллоцированный буфер ────────────────
                # Аллокации нет — просто копируем в уже существующий массив.
                incoming = len(mono)
                needed = self._pcm_len + incoming
                if needed > len(self._pcm_buf):
                    # Буфер переполнен (редко): увеличиваем вдвое
                    new_size = max(needed, len(self._pcm_buf) * 2)
                    new_buf = np.empty(new_size, dtype=np.float32)
                    new_buf[:self._pcm_len] = self._pcm_buf[:self._pcm_len]
                    self._pcm_buf = new_buf
                self._pcm_buf[self._pcm_len:self._pcm_len + incoming] = mono
                self._pcm_len += incoming

                # Откусываем строго по CHUNK_SIZE (960 семплов = 20мс) и передаём
                while self._pcm_len >= CHUNK_SIZE:
                    chunk = self._pcm_buf[:CHUNK_SIZE].copy()
                    # Сдвигаем остаток влево (numpy делает это на C-уровне)
                    self._pcm_len -= CHUNK_SIZE
                    self._pcm_buf[:self._pcm_len] = self._pcm_buf[CHUNK_SIZE:CHUNK_SIZE + self._pcm_len]

                    # ── Передаём чанк через callback (WebRTC путь) ──────────────
                    # pcm_callback принимает float32 моно фрейм (CHUNK_SIZE сэмплов).
                    # SystemAudioTrack кладёт фрейм в asyncio.Queue → recv() →
                    # av.AudioFrame → WebRTC RTP поток к зрителям.
                    if self._pcm_callback is not None:
                        try:
                            self._pcm_callback(chunk)
                        except Exception:
                            pass

        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  WebRTC Audio Tracks (Шаг 3 миграции)
#  Используются только для WebRTC-стрима. Голосовой чат комнаты (AudioHandler)
#  работает независимо через opuslib + UDP — без каких-либо изменений.
# ─────────────────────────────────────────────────────────────────────────────


class MicrophoneTrack(AudioStreamTrack):
    """
    Захват микрофона через sounddevice → WebRTC AudioStreamTrack.

    Используется стримером для передачи голоса зрителям через WebRTC SFU.
    НЕ заменяет AudioHandler.audio_callback() — голосовой чат комнаты
    (opuslib + VAD + JitterBuffer) работает параллельно и независимо.

    WebRTC DTX (Discontinuous Transmission) управляет тишиной вместо VAD:
    при молчании кодек автоматически снижает поток → дополнительный VAD-гейт
    здесь избыточен и только добавил бы задержку старта речи.

    Ленивая инициализация: asyncio.Queue и захват-поток создаются при первом
    вызове recv() из WebRTC asyncio-цикла. Это гарантирует, что loop-ссылка
    получена в правильном контексте без явной передачи в конструктор.

    Формат: s16, 48 000 Гц, моно, CHUNK_SIZE=960 сэмплов (20 мс).
    """

    kind = "audio"

    def __init__(self, device_name: str = None):
        super().__init__()
        self._device    = device_name
        self._running   = True
        # Инициализируются лениво при первом recv():
        self._queue:  "asyncio.Queue | None" = None
        self._loop:   "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None

    # ── Поток захвата ─────────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        """Синхронный поток sounddevice; данные отправляются в asyncio.Queue."""

        def _cb(indata: np.ndarray, frames: int, time_info, status) -> None:
            if not self._running or self._loop is None or self._queue is None:
                return
            # indata: (CHUNK_SIZE, 1), dtype=int16
            # run_coroutine_threadsafe — единственный thread-safe способ положить
            # данные из PortAudio-потока в asyncio.Queue WebRTC-цикла.
            try:
                asyncio.run_coroutine_threadsafe(
                    self._queue.put(indata.copy()), self._loop
                )
            except Exception:
                pass

        try:
            with sd.InputStream(
                device=self._device,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,   # 1 (моно)
                dtype='int16',
                blocksize=CHUNK_SIZE,
                callback=_cb,
            ):
                while self._running:
                    time.sleep(0.05)
        except Exception as e:
            print(f"[MicrophoneTrack] Ошибка захвата микрофона: {e}")

    # ── aiortc интерфейс ──────────────────────────────────────────────────────

    async def recv(self) -> av.AudioFrame:
        # Ленивая инициализация при первом вызове из WebRTC asyncio-цикла.
        if self._queue is None:
            self._loop   = asyncio.get_event_loop()
            self._queue  = asyncio.Queue(maxsize=10)
            self._thread = threading.Thread(
                target=self._capture_loop,
                daemon=True,
                name="webrtc-mic-capture",
            )
            self._thread.start()

        data = await self._queue.get()  # (CHUNK_SIZE, 1) int16

        # av.AudioFrame.from_ndarray ожидает shape (channels, samples).
        frame = av.AudioFrame.from_ndarray(
            data.T,      # (1, CHUNK_SIZE)
            format='s16',
            layout='mono',
        )
        frame.sample_rate = SAMPLE_RATE
        pts, time_base = await self.next_timestamp()
        frame.pts       = pts
        frame.time_base = time_base
        return frame

    def stop(self) -> None:
        """Останавливает захват микрофона. Вызывать при завершении стрима."""
        self._running = False


class SystemAudioTrack(AudioStreamTrack):
    """
    Захват системного звука (WASAPI Loopback) → WebRTC.

    Используется стримером для передачи игрового звука зрителям через WebRTC SFU.

    Повторно использует всю логику захвата из StreamAudioCapture без дублирования:
      pyaudiowpatch WASAPI Loopback → sounddevice loopback (запасной путь).
    Разница: вместо Opus-encode + UDP-отправки StreamAudioCapture вызывает
    pcm_callback(chunk: float32 mono), а SystemAudioTrack кладёт chunk в asyncio.Queue.

    Ленивая инициализация: захват стартует при первом recv() из WebRTC asyncio-цикла.
    Формат вывода: s16, 48 000 Гц, моно, CHUNK_SIZE=960 сэмплов (20 мс).
    """
    kind = "audio"

    def __init__(self, device_idx: int = None):
        super().__init__()
        self._device_idx = device_idx
        self._running    = True
        # Инициализируются лениво при первом recv():
        self._queue:   "asyncio.Queue | None"             = None
        self._loop:    "asyncio.AbstractEventLoop | None" = None
        self._capture: "StreamAudioCapture | None"        = None

    # ── PCM callback из StreamAudioCapture ───────────────────────────────────

    def _on_pcm_chunk(self, chunk: np.ndarray) -> None:
        """
        Вызывается StreamAudioCapture на каждый готовый 20-мс float32-фрейм.
        Отправляет данные в asyncio.Queue WebRTC-цикла через thread-safe вызов.
        chunk уже является копией (сделан в _audio_cb) — повторное копирование
        не требуется.
        """
        if not self._running or self._loop is None or self._queue is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(chunk), self._loop,
            )
        except Exception:
            pass

    # ── aiortc интерфейс ──────────────────────────────────────────────────────

    async def recv(self) -> av.AudioFrame:
        # Ленивая инициализация при первом вызове из WebRTC asyncio-цикла.
        if self._queue is None:
            self._loop    = asyncio.get_event_loop()
            self._queue   = asyncio.Queue(maxsize=10)
            self._capture = StreamAudioCapture(pcm_callback=self._on_pcm_chunk)
            self._capture.start(self._device_idx)
            print("[SystemAudioTrack] Захват системного звука запущен")

        data = await self._queue.get()  # float32 mono (CHUNK_SIZE,)

        # float32 [-1.0, 1.0] → int16 для av.AudioFrame.
        # np.clip inline — защита от пиков (WASAPI иногда выдаёт > 1.0).
        pcm_int16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)

        frame = av.AudioFrame.from_ndarray(
            pcm_int16.reshape(1, -1),   # (channels=1, samples=CHUNK_SIZE)
            format='s16',
            layout='mono',
        )
        frame.sample_rate = SAMPLE_RATE
        pts, time_base = await self.next_timestamp()
        frame.pts       = pts
        frame.time_base = time_base
        return frame

    def stop(self) -> None:
        """Останавливает захват системного звука. Вызывать при завершении стрима."""
        self._running = False
        if self._capture is not None:
            self._capture.stop()
            self._capture = None


# ─────────────────────────────────────────────────────────────────────────────