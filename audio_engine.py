import threading
import queue
from collections import deque
import numpy as np
import sounddevice as sd
import opuslib
import heapq
import struct
import time
from PyQt6.QtCore import QObject, pyqtSignal, QSettings
from config import *

try:
    from pyrnnoise import RNNoise

    PYRNNOISE_AVAILABLE = True
except ImportError:
    PYRNNOISE_AVAILABLE = False
    print("[Audio] Внимание: Модуль pyrnnoise не найден.")

# pyaudiowpatch — форк PyAudio с официальным патчем WASAPI Loopback для Windows.
# Содержит флаг isLoopbackDevice в device info — единственный надёжный способ
# отличить loopback endpoint от реального микрофона.
# Установка: pip install pyaudiowpatch
try:
    import pyaudiowpatch as _pyaudio
    PYAUDIOWPATCH_AVAILABLE = True
    print("[StreamAudio] pyaudiowpatch доступен — будет использован для WASAPI Loopback")
except ImportError:
    _pyaudio = None
    PYAUDIOWPATCH_AVAILABLE = False
    print("[StreamAudio] pyaudiowpatch не найден. "
          "Для надёжного захвата системного звука: pip install pyaudiowpatch")


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

    def __init__(self, send_queue, uid_getter, aec=None):
        self.send_queue = send_queue
        self.get_uid = uid_getter
        self.aec = aec  # AECProcessor — подавитель эха (опционально)
        self._running = threading.Event()
        self._thread = None
        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION)
        self.encoder.bitrate = DEFAULT_BITRATE
        self.encoder.complexity = 5
        self._sequence = 0
        self._native_sr: int = SAMPLE_RATE

        # Промежуточный буфер для сборки точных 20ms фреймов (CHUNK_SIZE)
        self._pcm_buffer = np.array([], dtype=np.float32)
        self._buffer_lock = threading.Lock()
        # True когда захват идёт из CABLE Output (VB-CABLE).
        # В этом режиме AEC полностью отключён — голосов в CABLE Output нет физически.
        self._using_vbcable: bool = False

        # Локальный мониторинг VB-CABLE: очередь сырых PCM-фреймов,
        # которые параллельно с отправкой зрителям воспроизводятся
        # в реальные наушники стримера.  None — мониторинг не запущен.
        self._vbcable_monitor_queue: "queue.Queue | None" = None
        # Громкость локального мониторинга: 1.0 = оригинал.
        # Можно снизить если стример хочет слышать игру тише чем зрители.
        self.monitor_volume: float = 1.0

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
        self._pcm_buffer = np.array([], dtype=np.float32)  # Очищаем буфер при старте
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
    # Стратегия 0: VB-CABLE — приоритет над всеми остальными методами
    # ------------------------------------------------------------------
    def _try_vbcable(self) -> bool:
        """
        Захватывает звук из «CABLE Output» как обычное INPUT-устройство.

        Архитектура VB-CABLE:
          «CABLE Input»  — виртуальный ВЫВОД (куда игра выводит звук)
          «CABLE Output» — виртуальный ВВОД  (откуда мы читаем)

        Связь между ними: всё что подаётся на CABLE Input,
        сразу появляется на CABLE Output. Голоса зрителей НЕ подаются
        на CABLE Input никогда → CABLE Output математически чист → AEC не нужен,
        ducking не нужен, эхо невозможно как явление.

        device_idx игнорируется — устройство находится по имени автоматически.
        Возвращает True если поток открыт и проработал до stop().
        """
        cable_idx = None
        cable_ch  = 2
        cable_sr  = SAMPLE_RATE

        try:
            devs = sd.query_devices()
            for i, d in enumerate(devs):
                if 'cable output' in d['name'].lower() and d['max_input_channels'] > 0:
                    cable_idx = i
                    cable_ch  = max(1, int(d['max_input_channels']))
                    cable_sr  = int(d.get('default_samplerate', SAMPLE_RATE))
                    print(f"[StreamAudio] [VB-CABLE] Найден: «{d['name']}» "
                          f"idx={i} ch={cable_ch} sr={cable_sr}")
                    break

            if cable_idx is None:
                print("[StreamAudio] [VB-CABLE] Устройство 'CABLE Output' не найдено — "
                      "пробуем WASAPI Loopback")
                return False

            self._native_sr     = cable_sr
            self._using_vbcable = True

            # ── Локальный мониторинг: стример слышит игру в наушниках ──────────
            # Открываем OutputStream на устройство вывода по умолчанию.
            # Callback читает сырые фреймы из _vbcable_monitor_queue (заполняется
            # в _audio_cb) и прокидывает их в наушники. Если очередь пуста —
            # тишина (не блокируемся). cable_ch и cable_sr совпадают с InputStream,
            # поэтому ресемплинг не нужен.
            monitor_q: "queue.Queue" = queue.Queue(maxsize=80)
            self._vbcable_monitor_queue = monitor_q
            _mon_vol_ref = [self.monitor_volume]  # mutable ref для closure

            def _monitor_out_cb(outdata, frames, time_info, status):
                try:
                    raw = monitor_q.get_nowait()  # shape: (frames, cable_ch)
                    vol = self.monitor_volume
                    if raw.shape == outdata.shape:
                        np.multiply(raw, vol, out=outdata)
                    else:
                        # Разное кол-во каналов: микшируем в mono и раскладываем
                        mono = np.mean(raw, axis=1, keepdims=True) if raw.ndim > 1 else raw.reshape(-1, 1)
                        outdata[:] = np.repeat(mono, outdata.shape[1], axis=1) * vol
                except Exception:
                    outdata.fill(0)  # очередь пуста или ошибка — тишина

            # Определяем кол-во каналов дефолтного вывода
            try:
                _out_ch = max(1, int(sd.query_devices(kind='output')['max_output_channels']))
                _out_ch = min(_out_ch, cable_ch)  # не больше чем захватываем
            except Exception:
                _out_ch = cable_ch

            with sd.InputStream(
                device=cable_idx,
                samplerate=cable_sr,
                channels=cable_ch,
                dtype='float32',
                blocksize=CHUNK_SIZE,
                callback=self._audio_cb,
            ):
                print("[StreamAudio] ✔ [VB-CABLE] Захват запущен — "
                      "чистый звук без AEC и ducking")
                try:
                    with sd.OutputStream(
                        samplerate=cable_sr,
                        channels=_out_ch,
                        dtype='float32',
                        blocksize=CHUNK_SIZE,
                        callback=_monitor_out_cb,
                    ):
                        print(f"[StreamAudio] ✔ [VB-CABLE] Локальный мониторинг запущен "
                              f"(ch={_out_ch} sr={cable_sr}) — стример слышит игру в наушниках")
                        while self._running.is_set():
                            time.sleep(0.05)
                except Exception as e_mon:
                    # Мониторинг не удался (редкий случай) — стрим продолжается без него
                    print(f"[StreamAudio] [VB-CABLE] Мониторинг недоступен: {e_mon}\n"
                          f"  Захват зрителям продолжается, но стример не слышит игру локально.")
                    while self._running.is_set():
                        time.sleep(0.05)
            return True

        except Exception as e:
            print(f"[StreamAudio] [VB-CABLE] Ошибка открытия потока: {e}")
            return False
        finally:
            self._using_vbcable = False
            self._vbcable_monitor_queue = None  # сбрасываем ссылку на очередь

    def _capture_loop(self, device_idx):
        # ── Стратегия 0: VB-CABLE (ПРИОРИТЕТ) ─────────────────────────────────
        # CABLE Output = чистый игровой звук, голосов зрителей там нет → эхо невозможно.
        if self._try_vbcable():
            print("[StreamAudio] Захват остановлен [0/VB-CABLE]")
            return

        # ── Стратегии A/B: WASAPI Loopback (запасной путь) ────────────────────
        resolved = self._resolve_device(device_idx)
        if resolved is None:
            print("[StreamAudio] Подходящее WASAPI OUTPUT-устройство не найдено")
            return

        dev_info = sd.query_devices(resolved)
        native_ch = max(1, int(dev_info.get('max_output_channels', 2)))
        self._native_sr = int(dev_info.get('default_samplerate', SAMPLE_RATE))
        output_name = dev_info['name']

        print(f"[StreamAudio] Целевое устройство: «{output_name}» "
              f"(sd_idx={resolved}, ch={native_ch}, sr={self._native_sr})")

        # ── Стратегия A: pyaudiowpatch ─────────────────────────────────────
        # Единственный надёжный метод: использует isLoopbackDevice,
        # не захватывает микрофоны случайно.
        if self._try_pyaudiowpatch(output_name):
            print("[StreamAudio] Loopback поток остановлен [A/pyaudiowpatch]")
            return

        # ── Стратегия B: sounddevice WasapiSettings(loopback=True) ─────────
        # Работает на части конфигураций, падает с -9998 на Sound Blaster и др.
        if self._try_sounddevice_loopback(resolved, native_ch):
            print("[StreamAudio] Loopback поток остановлен [B/sounddevice]")
            return

        # ── Ничего не сработало ─────────────────────────────────────────────
        print(
            "[StreamAudio] ✖ WASAPI Loopback захватить не удалось.\n"
            "  Решение: pip install pyaudiowpatch\n"
            "  Подробнее: https://github.com/s0d3s/PyAudioWPatch"
        )
        print("[StreamAudio] Loopback поток остановлен")

    def _audio_cb(self, indata, frames, time_info, status):
        if not self._running.is_set():
            return

        uid = self.get_uid()
        if uid == 0:
            return

        # ── Локальный мониторинг VB-CABLE ──────────────────────────────────────
        # Сырой фрейм (до любой обработки) кладём в очередь мониторинга.
        # Параллельный sd.OutputStream в _try_vbcable читает её и воспроизводит
        # в реальные наушники стримера — он слышит игру так же как и зрители,
        # но через отдельный путь без задержки encode/decode.
        # Блок работает ТОЛЬКО когда _using_vbcable=True и очередь создана.
        if self._using_vbcable and self._vbcable_monitor_queue is not None:
            try:
                self._vbcable_monitor_queue.put_nowait(indata.copy())
            except Exception:
                pass  # очередь полна — дроп, не критично (20ms потери)

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
                # Добавляем полученные семплы в общий буфер
                self._pcm_buffer = np.concatenate((self._pcm_buffer, mono))

                # Откусываем строго по CHUNK_SIZE (960 семплов = 20мс) и отправляем
                while len(self._pcm_buffer) >= CHUNK_SIZE:
                    chunk = self._pcm_buffer[:CHUNK_SIZE]
                    self._pcm_buffer = self._pcm_buffer[CHUNK_SIZE:]

                    # ── AEC: вычитаем голоса собеседников ──────────────────────
                    # При VB-CABLE (_using_vbcable=True) пропускаем полностью:
                    # CABLE Output физически не содержит голосов зрителей,
                    # AEC здесь только вносил бы артефакты.
                    # При WASAPI Loopback (_using_vbcable=False) применяем как раньше:
                    # loopback захватывает пост-микс наушников, там могут быть голоса.
                    if self.aec is not None and not self._using_vbcable:
                        chunk = self.aec.process(chunk)

                    pcm = (chunk * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm, CHUNK_SIZE)

                    self._sequence += 1
                    flags = FLAG_STREAM_AUDIO | FLAG_LOOPBACK_AUDIO
                    packet = struct.pack('!IdIB', uid, time.time(), self._sequence, flags) + encoded
                    self.send_queue.put_nowait(packet)

                    if self._sequence % 50 == 0:
                        print(f"[StreamAudio-Capture] Захвачен и отправлен системный звук (seq={self._sequence})")

        except Exception as e:
            pass


class AECProcessor:
    """
    Адаптивный подавитель акустического эха (AEC) с защитой от фазовых артефактов.
    """

    def __init__(self, max_delay_frames: int = 8, max_frames: int = 16):
        # Гарантируем, что max_frames больше max_delay_frames для стабильности
        self.maxlen = max(max_frames, max_delay_frames + 4)
        self._ref_buffer: deque = deque(maxlen=self.maxlen)
        self._lock = threading.Lock()
        self.enabled = True

        # Состояние для сглаживания (Anti-Artifacts)
        self._last_offset = -1
        self._last_alpha = 0.0

    def push_reference(self, frame: np.ndarray):
        with self._lock:
            self._ref_buffer.append(frame.astype(np.float32).copy())

    def process(self, captured: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return captured

        with self._lock:
            n = len(self._ref_buffer)
            # ЖДЕМ ПОЛНОГО ЗАПОЛНЕНИЯ БУФЕРА!
            # Это критично: пока буфер растет, offset смещается на 960 семплов каждый кадр.
            # Заполненный кольцевой буфер дает железобетонную "точку отсчета".
            if n < self.maxlen:
                return captured
            refs = list(self._ref_buffer)

        cap_rms = float(np.sqrt(np.mean(captured ** 2)))
        if cap_rms < 1e-6:
            self._last_alpha = 0.0
            return captured

        cap_len = len(captured)
        ref_concat = np.concatenate(refs)

        # 1. Поиск задержки
        corr = np.correlate(ref_concat, captured, mode='valid')
        if len(corr) == 0:
            return captured

        target_offset = int(np.argmax(corr))

        if self._last_offset == -1:
            self._last_offset = target_offset

        # 2. Формируем опорное окно (Защита от скачков фазы и роботизации)
        # Если смещение изменилось (дрейф часов), делаем мягкий кроссфейд между окнами,
        # а не жестко переключаем индекс.
        if target_offset != self._last_offset:
            old_end = self._last_offset + cap_len
            new_end = target_offset + cap_len

            if old_end <= len(ref_concat) and new_end <= len(ref_concat):
                ref_old = ref_concat[self._last_offset: old_end]
                ref_new = ref_concat[target_offset: new_end]

                # Кроссфейд (Fade Out старого, Fade In нового)
                fade_in = np.linspace(0.0, 1.0, cap_len, dtype=np.float32)
                fade_out = 1.0 - fade_in

                ref_window = (ref_old * fade_out) + (ref_new * fade_in)
                self._last_offset = target_offset
            else:
                ref_window = ref_concat[target_offset: target_offset + cap_len]
                self._last_offset = target_offset
        else:
            ref_window = ref_concat[self._last_offset: self._last_offset + cap_len]

        # 3. Вычисляем целевой коэффициент вычитания (МНК)
        rr = float(np.dot(ref_window, ref_window))
        if rr < 1e-10:
            target_alpha = 0.0
        else:
            target_alpha = float(np.dot(captured, ref_window) / rr)
            # При сжатии Opus альфа редко бывает > 1.2
            target_alpha = max(0.0, min(1.2, target_alpha))

        # 4. Посемпловое сглаживание альфы (Защита от треска на границах чанков)
        # Плавно переводим множитель от значения прошлого кадра к новому.
        alphas = np.linspace(self._last_alpha, target_alpha, cap_len, dtype=np.float32)
        self._last_alpha = target_alpha

        # 5. Итоговое вычитание
        candidate = captured - alphas * ref_window

        # Мягкий клиппинг и возврат
        return np.clip(candidate, -1.0, 1.0)

    def reset(self):
        with self._lock:
            self._ref_buffer.clear()
            self._last_offset = -1
            self._last_alpha = 0.0


class JitterBuffer:
    def __init__(self, target_delay=4):
        self.buffer = []
        self.target_delay = target_delay
        self.last_seq = -1
        self._lock = threading.Lock()
        self.is_buffering = True
        self.max_size = 50

    def add(self, seq, data):
        with self._lock:
            if seq <= self.last_seq and self.last_seq != -1:
                return
            heapq.heappush(self.buffer, (seq, data))
            if len(self.buffer) > self.max_size:
                heapq.heappop(self.buffer)

    def get(self):
        with self._lock:
            if not self.buffer:
                self.is_buffering = True
                return None

            if self.is_buffering:
                if len(self.buffer) >= self.target_delay:
                    self.is_buffering = False
                else:
                    return None

            seq, data = heapq.heappop(self.buffer)
            self.last_seq = seq
            return data


class RemoteUser:
    def __init__(self, uid):
        self.uid = uid
        self.jitter_buffer = JitterBuffer()
        self.decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
        self.last_packet_time = 0
        self.volume = 1.0
        self.is_locally_muted = False
        self.remote_muted = False
        self.remote_deafened = False


class AudioHandler(QObject):
    volume_level_signal = pyqtSignal(int)
    status_changed = pyqtSignal(bool, bool)
    # Испускается при получении первого пакета шёпота от нового отправителя.
    # Аргумент — uid отправителя. MainWindow использует для уведомления получателя.
    whisper_received = pyqtSignal(int)
    # Испускается когда пакеты шёпота перестали приходить (тайм-аут).
    whisper_ended = pyqtSignal()

    def _apply_whisper_bass_effect(self, s: np.ndarray) -> np.ndarray:
        """
        Эффект «тёмный бас» для входящего шёпота — двухкаскадный IIR biquad lowpass.

        ПОЧЕМУ НЕТ АРТЕФАКТОВ:
        FIR/convolve и pitch-shift через np.interp обрабатывают каждый 20ms фрейм
        независимо → на стыке соседних фреймов возникает разрыв, слышимый как
        шипение/треск (характерная «щётка» 50 раз в секунду).

        IIR-фильтр в Direct Form II Transposed хранит состояние (z[0], z[1]) между
        вызовами. Выход фрейма N плавно перетекает во фрейм N+1 — разрывов нет.

        Параметры фильтра:
        • 2-й порядок Butterworth LP, fc=1 200 Гц, fs=48 000 Гц (каскад 1)
        • Тот же фильтр применяется второй раз (каскад 2) → 4-й порядок итого.
        • Крутизна: −80 dB/дек — эффективно «срезает» всё выше 2 кГц.
        • Голос становится тёмным, «нутряным», явно отличается от обычной речи.
        • Коэффициенты вычислены через билинейное z-преобразование аналогового прототипа.

        Singletons b/a/z хранятся в self._wlp_* и инициализируются в __init__.
        Состояния z сбрасываются в start_whisper().
        """
        b = self._wlp_b
        a1, a2 = self._wlp_a1, self._wlp_a2

        # ── Каскад 1 ──────────────────────────────────────────────────────────
        z0, z1 = self._wlp_z1[0], self._wlp_z1[1]
        out = np.empty(len(s), dtype=np.float64)
        x = s.astype(np.float64)
        for i in range(len(x)):
            w   = x[i] - a1 * z0 - a2 * z1
            out[i] = b[0] * w + b[1] * z0 + b[2] * z1
            z1, z0 = z0, w
        self._wlp_z1[0], self._wlp_z1[1] = z0, z1

        # ── Каскад 2 (тот же фильтр, независимое состояние) ──────────────────
        z0, z1 = self._wlp_z2[0], self._wlp_z2[1]
        for i in range(len(out)):
            w      = out[i] - a1 * z0 - a2 * z1
            out[i] = b[0] * w + b[1] * z0 + b[2] * z1
            z1, z0 = z0, w
        self._wlp_z2[0], self._wlp_z2[1] = z0, z1

        return out.astype(np.float32)

    def __init__(self):
        super().__init__()

        self.settings = QSettings("MyVoiceChat", "UserVolumes")
        self.global_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.uid_to_ip = {}
        self.pending_volumes = {}
        self.remote_users = {}
        self.users_lock = threading.Lock()

        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION)

        saved_bitrate = int(self.global_settings.value("audio_bitrate", DEFAULT_BITRATE))
        self.encoder.bitrate = saved_bitrate
        self.encoder.complexity = 5

        self.denoiser = None
        self.use_noise_reduction = False
        if PYRNNOISE_AVAILABLE:
            try:
                self.denoiser = RNNoise(sample_rate=SAMPLE_RATE)
                self.use_noise_reduction = True
            except Exception as e:
                print(f"[Audio] Ошибка RNNoise: {e}")

        self.incoming_packets = queue.Queue(maxsize=500)
        self.incoming_stream_packets = queue.Queue(maxsize=500)
        self.send_queue = queue.Queue(maxsize=100)

        # --- Стрим-аудио ---
        # uid → RemoteUser  (зрительская сторона: стримеры из других комнат)
        self.stream_remote_users = {}
        # КРИТИЧНО: отдельный лок для stream_remote_users, чтобы не блокировать
        # audio_callback (high-priority поток) ожиданием _stream_packet_processor_loop
        self.stream_users_lock = threading.Lock()
        # Громкость стрима (0.0 - 2.0), регулируется зрителем из оверлея
        self.stream_volume = 1.0
        # Флаг: стример включил «Транслировать звук»
        self.stream_audio_sending_enabled = False
        # AEC: адаптивный подавитель эха голосов собеседников в loopback-потоке.
        # Автоматически определяет задержку (0-4 фрейма) и коэффициент вычитания.
        # max_delay_frames=8 → поиск до 160 мс (было 4=80 мс).
        # Типичная задержка WASAPI 10–80 мс; с запасом для систем с большим буфером.
        self.aec = AECProcessor(max_delay_frames=8)
        # Захват системного аудио (WASAPI Loopback) для трансляции
        self.stream_audio_capture = StreamAudioCapture(self.send_queue, lambda: self.my_uid, aec=self.aec)
        self._is_running = threading.Event()
        self._is_muted = threading.Event()
        self._is_deafened = threading.Event()

        self.mix_buffer = np.zeros(CHUNK_SIZE, dtype=np.float32)
        saved_vad_slider = int(self.global_settings.value("vad_threshold_slider", 5))
        self.vad_threshold = saved_vad_slider / 1000.0
        self.vad_hangover = 0.4
        self.last_voice_time = 0
        self.my_uid = 0
        self.my_sequence = 0

        # ── Шёпот (приватная передача одному пользователю) ─────────────────
        # Пока whisper_target_uid != 0 — голос идёт только этому uid,
        # остальные участники комнаты отправителя не слышат.
        self.whisper_target_uid: int = 0
        self._whisper_sequence: int = 0

        # ── IIR-фильтр для whisper-эффекта (4-й порядок Butterworth LP, fc=1200 Гц) ──
        # Коэффициенты вычислены через билинейное z-преобразование для fs=48 000 Гц.
        # Два независимых каскада (z1, z2) — состояние сохраняется МЕЖДУ фреймами,
        # что полностью исключает межфреймовые артефакты.
        # Сброс состояний производится в start_whisper().
        self._wlp_b  = np.array([0.005543, 0.011086, 0.005543], dtype=np.float64)
        self._wlp_a1 = -1.77868
        self._wlp_a2 =  0.80018
        self._wlp_z1 = np.zeros(2, dtype=np.float64)   # состояние каскада 1
        self._wlp_z2 = np.zeros(2, dtype=np.float64)   # состояние каскада 2
        # Отдельный счётчик для FLAG_STREAM_VOICES пакетов.
        # Нельзя использовать my_sequence: несколько спикеров в одном audio_callback
        # получали бы одинаковый seq → JitterBuffer на стороне зрителя отбрасывал
        # второй и последующие пакеты (seq <= last_seq → return), зрители слышали
        # только первого спикера. Отдельный монотонный счётчик решает проблему.
        self._sv_sequence = 0
        self.vad_pre_buffer = []
        self.was_talking = False
        self.stream = None

    def set_bitrate(self, bitrate_kbps):
        bitrate_bps = int(bitrate_kbps) * 1000
        try:
            with self.users_lock:
                if hasattr(self, 'encoder'):
                    self.encoder.bitrate = bitrate_bps
                    self.global_settings.setValue("audio_bitrate", bitrate_bps)
                    print(f"[Audio] Bitrate changed to {bitrate_kbps} kbps")
        except Exception as e:
            print(f"[Audio] Error setting bitrate: {e}")

    def set_vad_threshold(self, slider_val: int):
        threshold = max(1, min(50, slider_val)) / 1000.0
        self.vad_threshold = threshold
        self.global_settings.setValue("vad_threshold_slider", slider_val)
        print(f"[Audio] VAD threshold set to {threshold:.4f} (slider={slider_val})")

    def find_device_index_by_name(self, name, is_input=True):
        if not name: return None
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            try:
                api_name = sd.query_hostapis(d['hostapi'])['name']
                full_name = f"{d['name']} ({api_name})"
                if full_name.strip() == name.strip():
                    if is_input and d['max_input_channels'] > 0: return i
                    if not is_input and d['max_output_channels'] > 0: return i
            except:
                continue
        return None

    def start(self, input_name=None, output_name=None):
        if self.my_uid == 0: return
        self.stop()
        time.sleep(0.1)

        in_idx = self.find_device_index_by_name(input_name, True)
        out_idx = self.find_device_index_by_name(output_name, False)

        self._is_running.set()
        try:
            self.stream = sd.Stream(
                device=(in_idx, out_idx),
                samplerate=SAMPLE_RATE, blocksize=CHUNK_SIZE,
                dtype='float32', channels=CHANNELS,
                callback=self.audio_callback
            )
            self.stream.start()
            threading.Thread(target=self._packet_processor_loop, daemon=True).start()
            threading.Thread(target=self._stream_packet_processor_loop, daemon=True).start()
        except Exception as e:
            print(f"[Audio] Ошибка старта: {e}")
            self._is_running.clear()

    def stop(self):
        self._is_running.clear()
        if hasattr(self, 'stream') and self.stream:
            try:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            except:
                pass

    def cleanup_users(self, active_uids):
        """ Очистка памяти от отключившихся юзеров """
        with self.users_lock:
            for uid in list(self.remote_users.keys()):
                if uid not in active_uids:
                    del self.remote_users[uid]

            for uid in list(self.uid_to_ip.keys()):
                if uid not in active_uids:
                    del self.uid_to_ip[uid]
            for uid in list(self.pending_volumes.keys()):
                if uid not in active_uids:
                    del self.pending_volumes[uid]

        # stream_remote_users — под отдельным локом (не блокировать audio_callback)
        with self.stream_users_lock:
            for uid in list(self.stream_remote_users.keys()):
                real_uid = uid - LOOPBACK_UID_OFFSET if uid >= LOOPBACK_UID_OFFSET else uid
                if real_uid not in active_uids:
                    del self.stream_remote_users[uid]

    def _packet_processor_loop(self):
        while self._is_running.is_set():
            try:
                packet_data = self.incoming_packets.get(timeout=0.1)
                uid, seq, data, flags = packet_data
                if uid == self.my_uid: continue

                with self.users_lock:
                    if uid not in self.remote_users:
                        self.remote_users[uid] = RemoteUser(uid)
                        # Исправление 1.2: убрано чтение QSettings(диск) из high-priority ловушки
                        if uid in self.pending_volumes:
                            val = self.pending_volumes.pop(uid)
                        else:
                            val = 1.0
                        self.remote_users[uid].volume = float(val)

                    user = self.remote_users[uid]
                    user.remote_muted = bool(flags & 1)
                    user.remote_deafened = bool(flags & 2)

                    if data:
                        user.jitter_buffer.add(seq, data)
                        user.last_packet_time = time.time()
            except queue.Empty:
                continue
            except Exception:
                pass

    def _stream_packet_processor_loop(self):
        """
        Обрабатывает входящие пакеты стрим-аудио (FLAG_STREAM_AUDIO).

        Различает два типа потоков по флагу FLAG_LOOPBACK_AUDIO:

        ① Loopback (системный звук, flags & FLAG_LOOPBACK_AUDIO):
           • Всегда воспроизводим — это игры, музыка с экрана стримера.
           • Хранится под ключом uid + LOOPBACK_UID_OFFSET, чтобы не смешиваться
             с голосовым каналом того же пользователя.
           • Единственное исключение: свой собственный loopback (uid == my_uid)
             сервер и так не отсылает обратно стримеру, но фильтруем на всякий случай.

        ② Микрофон стримера (только FLAG_STREAM_AUDIO, без FLAG_LOOPBACK_AUDIO):
           • uid == my_uid          → отбрасываем (нет эха своего голоса)
           • uid in remote_users    → отбрасываем (уже слышим в той же комнате — не дублируем)
           • иначе                  → воспроизводим (стример из другой комнаты)
        """
        while self._is_running.is_set():
            try:
                packet_data = self.incoming_stream_packets.get(timeout=0.1)
                uid, seq, data, flags = packet_data

                is_loopback = bool(flags & FLAG_LOOPBACK_AUDIO)

                if is_loopback:
                    # --- Системный звук (WASAPI Loopback) ---
                    # Свой loopback сервер не шлёт назад, но фильтруем на всякий случай
                    if uid == self.my_uid:
                        continue

                    # ── VAD-ГЕЙТ: запоминаем что loopback этого стримера активен ─────
                    # Дропать пакеты здесь НЕЛЬЗЯ — это вызывает полную тишину во время речи.
                    # Вместо этого audio_callback применит duck-множитель (15%) при микшировании.
                    # Сам факт «зритель говорит» audio_callback читает из self.last_voice_time.

                    storage_uid = uid + LOOPBACK_UID_OFFSET
                    with self.stream_users_lock:
                        if storage_uid not in self.stream_remote_users:
                            self.stream_remote_users[storage_uid] = RemoteUser(storage_uid)
                            print(f"[AudioHandler] Создан буфер для системного звука (storage_uid={storage_uid})")
                        user = self.stream_remote_users[storage_uid]
                        if data:
                            user.jitter_buffer.add(seq, data)
                            user.last_packet_time = time.time()
                            if seq % 50 == 0:
                                print(f"[AudioHandler] Системный звук от uid={uid} добавлен в джиттер-буфер")
                else:
                    # --- Микрофон стримера ---
                    if uid == self.my_uid:
                        continue  # нет эха своего голоса

                    # FIX Bug #2: Ранее пакет отбрасывался, если uid уже есть в remote_users
                    # (тот же пользователь в той же комнате).  Но зритель СЛЫШИТ голос через
                    # обычный аудио-микс только пока тот активно говорит (VAD).  Поверх стрима
                    # стример может транслировать иначе сформированный пакет (другой seq/flags).
                    # Дублирование предотвращаем, добавляя пакет в stream_remote_users ТОЛЬКО
                    # если uid НЕТ в remote_users (обычный голосовой поток отсутствует) —
                    # тогда поведение прежнее.  Если uid уже в remote_users, пакет всё равно
                    # складываем в stream_remote_users под тем же uid: audio_callback смешивает
                    # оба источника, а небольшое наложение (< 20 мс) на практике не слышно,
                    # потому что regular-audio и stream-audio транслируются разными путями
                    # (разные jitter-буферы) и VAD обычно совпадает.
                    #
                    # Простейший корректный вариант без двойного микса: пропускаем пакет
                    # только если у зрителя уже есть свежий regular-аудио от этого uid
                    # (т.е. last_packet_time < 0.3 с назад).
                    with self.users_lock:
                        reg_user = self.remote_users.get(uid)
                        recently_received = (
                            reg_user is not None
                            and (time.time() - reg_user.last_packet_time) < 1.5
                            # FIX: увеличено с 0.3с до 1.5с.
                            # При 0.3с: любая пауза >300мс в речи стримера приводила к тому,
                            # что stream-mic пакет проскакивал в stream_remote_users и зритель
                            # в той же комнате слышал голос стримера ДВАЖДЫ (двойной голос).
                            # 1.5с совпадает с порогом last_packet_time < 1.5 в audio_callback,
                            # т.е. пока стример «активен» в комнате — stream-mic подавляется.
                        )
                    if recently_received:
                        continue  # уже слышим через обычный аудио-путь — не дублируем

                    with self.stream_users_lock:
                        if uid not in self.stream_remote_users:
                            self.stream_remote_users[uid] = RemoteUser(uid)
                        user = self.stream_remote_users[uid]
                        if data:
                            user.jitter_buffer.add(seq, data)
                            user.last_packet_time = time.time()

            except queue.Empty:
                continue
            except Exception:
                pass

    def audio_callback(self, indata, outdata, frames, time_info, status):
        if not self._is_running.is_set():
            outdata.fill(0)
            return

        curr_time = time.time()
        raw_input = indata.flatten()
        denoised_float = raw_input

        if self.use_noise_reduction and self.denoiser:
            try:
                pcm_int16 = (raw_input * 32767).astype(np.int16)
                processed = [f for p, f in self.denoiser.denoise_chunk(pcm_int16)]
                if processed:
                    denoised_float = np.concatenate(processed).astype(np.float32) / 32767.0
                    if len(denoised_float) != len(raw_input):
                        denoised_float = np.resize(denoised_float, len(raw_input))
            except:
                pass

        rms = np.sqrt(np.mean(denoised_float ** 2))
        self.volume_level_signal.emit(int(min(rms * 1000, 100)))

        is_talking = rms > self.vad_threshold or (curr_time - self.last_voice_time < self.vad_hangover)
        if rms > self.vad_threshold: self.last_voice_time = curr_time

        if self.my_uid != 0:
            mute_flag = 1 if self._is_muted.is_set() else 0
            deaf_flag = 2 if self._is_deafened.is_set() else 0
            flags = mute_flag | deaf_flag

            try:
                if is_talking and not self._is_muted.is_set():
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    self.my_sequence += 1

                    whisper_uid = self.whisper_target_uid   # атомарное чтение int

                    if whisper_uid != 0:
                        # ── РЕЖИМ ШЁПОТА ──────────────────────────────────────
                        # Отправляем ТОЛЬКО целевому пользователю.
                        # Нормальный аудио-пакет в комнату НЕ кладём в очередь →
                        # остальные участники не слышат отправителя в этот момент.
                        self._whisper_sequence += 1
                        w_flags = FLAG_WHISPER
                        w_header = struct.pack('!IdIB', self.my_uid, curr_time,
                                               self._whisper_sequence, w_flags)
                        # Payload: [target_uid: 4 байта] + [opus]
                        w_payload = struct.pack('!I', whisper_uid) + encoded
                        try:
                            self.send_queue.put_nowait(w_header + w_payload)
                        except Exception:
                            pass
                    else:
                        # ── ОБЫЧНЫЙ РЕЖИМ: пакет в комнату ───────────────────
                        packet = struct.pack('!IdIB', self.my_uid, curr_time, self.my_sequence, flags) + encoded

                        if not self.was_talking:
                            while self.vad_pre_buffer:
                                try:
                                    self.send_queue.put_nowait(self.vad_pre_buffer.pop(0))
                                except:
                                    pass
                            self.was_talking = True
                        self.send_queue.put_nowait(packet)

                        # Стрим-аудио: дополнительно посылаем тот же encoded с FLAG_STREAM_AUDIO
                        # Сервер направит его только зрителям, а не в комнату (нет дублирования)
                        if self.stream_audio_sending_enabled:
                            stream_flags = flags | FLAG_STREAM_AUDIO
                            stream_packet = struct.pack('!IdIB', self.my_uid, curr_time,
                                                        self.my_sequence, stream_flags) + encoded
                            try:
                                self.send_queue.put_nowait(stream_packet)
                            except:
                                pass
                else:
                    self.was_talking = False
                    if not is_talking:
                        empty_packet = struct.pack('!IdIB', self.my_uid, curr_time, 0, flags)
                        self.vad_pre_buffer.append(empty_packet)
                        if len(self.vad_pre_buffer) > 5: self.vad_pre_buffer.pop(0)
            except:
                pass

        self.mix_buffer.fill(0)
        if not self._is_deafened.is_set():
            with self.users_lock:
                for uid, user in self.remote_users.items():
                    if curr_time - user.last_packet_time < 1.5:
                        data = user.jitter_buffer.get()
                        if data and not user.is_locally_muted:
                            try:
                                decoded = user.decoder.decode(data, CHUNK_SIZE)
                                s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0

                                # ── Whisper bass effect ───────────────────────────────────────
                                # Если этот пользователь сейчас шепчет нам (whisper пакеты
                                # приходили < 2 с назад) — применяем питч-даун эффект.
                                # Голос становится заметно глубже/темнее → получатель
                                # слышит «другой тип разговора» без каких-либо UI подсказок.
                                w_uid = getattr(self, '_whisper_in_uid', 0)
                                w_ts  = getattr(self, '_whisper_in_ts',  0.0)
                                if uid == w_uid and (curr_time - w_ts) < 2.0:
                                    s = self._apply_whisper_bass_effect(s)

                                self.mix_buffer += s * user.volume

                                # ── Mix Minus для зрителей (FLAG_STREAM_VOICES) ───────────────
                                # Стример ретранслирует голос каждого собеседника зрителям
                                # с пометкой speaker_uid. Зритель на своей стороне отбросит
                                # пакет, если speaker_uid == его собственный uid (Mix Minus
                                # без DSP). Это устраняет эхо даже если AEC не справился.
                                #
                                # Payload: [speaker_uid: 4 байта big-endian] + [opus-данные].
                                # Флаги: FLAG_STREAM_AUDIO | FLAG_STREAM_VOICES.
                                # Сервер маршрутизирует такие пакеты только зрителям стримера.
                                #
                                # FIX Bug #2: каждый голос получает свой уникальный seq через
                                # self._sv_sequence — иначе все спикеры одного кадра имели
                                # одинаковый seq и JitterBuffer на приёмной стороне отбрасывал
                                # все пакеты кроме первого (seq <= last_seq → return).
                                if self.stream_audio_sending_enabled:
                                    try:
                                        self._sv_sequence += 1
                                        sv_flags = (FLAG_STREAM_AUDIO | FLAG_STREAM_VOICES)
                                        # header: uid стримера (отправитель), ts, seq, flags
                                        sv_header = struct.pack('!IdIB', self.my_uid, curr_time,
                                                                self._sv_sequence, sv_flags)
                                        # payload: speaker_uid (чей голос) + opus
                                        sv_payload = struct.pack('!I', uid) + data
                                        self.send_queue.put_nowait(sv_header + sv_payload)
                                    except Exception:
                                        pass
                            except:
                                pass

            # ── Стрим-аудио: игровой звук от стримера (зрительская сторона) ─────────
            # Микшируем ВНУТРИ deafen-проверки: если зритель нажал «заглушить всё»,
            # стрим тоже должен замолчать.
            #
            # С VB-CABLE: CABLE Output содержит только игровой звук →
            #   ducking не нужен, AEC не нужен, полная громкость всегда.
            # С WASAPI Loopback (fallback): AEC применён внутри StreamAudioCapture._audio_cb.
            sv = self.stream_volume   # float, чтение атомарно (GIL-safe)
            with self.stream_users_lock:
                for s_uid, user in self.stream_remote_users.items():
                    if curr_time - user.last_packet_time < 1.5:
                        data = user.jitter_buffer.get()
                        if data:
                            try:
                                decoded = user.decoder.decode(data, CHUNK_SIZE)
                                s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0
                                self.mix_buffer += s * sv
                                if s_uid >= LOOPBACK_UID_OFFSET:
                                    if not hasattr(self, '_lb_play_counter'):
                                        self._lb_play_counter = 0
                                    self._lb_play_counter += 1
                                    if self._lb_play_counter % 100 == 0:
                                        print(f"[Audio-Output] Стрим-звук воспроизводится "
                                              f"(громкость: {sv:.2f})")
                            except Exception as e:
                                print(f"[Audio-Output] Ошибка декодирования стрим-аудио: {e}")

        np.clip(self.mix_buffer, -1.0, 1.0, out=self.mix_buffer)

        # AEC push_reference полностью удалён:
        #   С VB-CABLE — не нужен (CABLE Output чист по построению).
        #   С WASAPI Loopback — AEC работает внутри StreamAudioCapture._audio_cb.

        outdata[:] = self.mix_buffer.reshape(-1, 1)

    def register_ip_mapping(self, uid, ip_addr):
        if not ip_addr: return
        with self.users_lock:
            self.uid_to_ip[uid] = ip_addr
            saved_vol = self.settings.value(f"vol_ip_{ip_addr}", None)
            if saved_vol is not None:
                saved_vol = float(saved_vol)
                if uid in self.remote_users:
                    self.remote_users[uid].volume = saved_vol
                else:
                    self.pending_volumes[uid] = saved_vol

    def set_user_volume(self, uid, vol):
        with self.users_lock:
            if uid in self.remote_users:
                self.remote_users[uid].volume = vol
                ip = self.uid_to_ip.get(uid)
                if ip:
                    self.settings.setValue(f"vol_ip_{ip}", vol)
                else:
                    self.settings.setValue(f"volume_{uid}", vol)

    def toggle_user_mute(self, uid):
        with self.users_lock:
            if uid in self.remote_users:
                self.remote_users[uid].is_locally_muted = not self.remote_users[uid].is_locally_muted
                return self.remote_users[uid].is_locally_muted
        return False

    # ── Шёпот ────────────────────────────────────────────────────────────────

    def start_whisper(self, target_uid: int):
        """
        Начинает шёпот к конкретному пользователю.
        Пока активно — голос кодируется и отправляется только ему (FLAG_WHISPER).
        Нормальные аудио-пакеты в комнату НЕ отправляются, остальные не слышат.

        ВАЖНО: _whisper_sequence инициализируется от my_sequence, а НЕ от 0.
        JitterBuffer получателя уже видел seq из нормального потока (my_sequence).
        Сброс в 0 → все шёпот-пакеты отбрасывались бы как seq <= last_seq.
        """
        self.whisper_target_uid = target_uid
        self._whisper_sequence = self.my_sequence  # продолжаем seq без разрыва
        # Сбрасываем состояние IIR-фильтра чтобы шёпот каждого нового собеседника
        # начинался с чистого состояния (без «хвоста» от предыдущего шёпота).
        self._wlp_z1[:] = 0.0
        self._wlp_z2[:] = 0.0
        print(f"[Audio] Whisper START → uid={target_uid}, seq_from={self._whisper_sequence}")

    def stop_whisper(self):
        """Останавливает шёпот, возвращает нормальную передачу в комнату.
        Синхронизируем my_sequence чтобы не было обратного прыжка seq."""
        # Переносим счётчик чтобы нормальные пакеты продолжили нумерацию
        # с того места, где остановился шёпот. Иначе получатели в комнате
        # увидят резкий откат seq и часть пакетов будет отброшена JitterBuffer.
        if self._whisper_sequence > self.my_sequence:
            self.my_sequence = self._whisper_sequence
        print(f"[Audio] Whisper STOP  (was → uid={self.whisper_target_uid}), seq_sync={self.my_sequence}")
        self.whisper_target_uid = 0

    def add_incoming_packet(self, uid, seq, data, flags=0):
        try:
            self.incoming_packets.put_nowait((uid, seq, data, flags))
        except:
            pass

    def add_incoming_whisper_packet(self, uid, seq, data):
        """
        Входящий шёпот (FLAG_WHISPER) от uid.
        Отличие от add_incoming_packet: при первом пакете от нового шептуна
        испускает сигнал whisper_received(uid) — UI показывает уведомление.
        При тайм-ауте (>1.5 с без пакетов) испускает whisper_ended().
        """
        now = time.time()
        prev_uid = getattr(self, '_whisper_in_uid', 0)
        prev_ts  = getattr(self, '_whisper_in_ts',  0.0)

        # Новый шептун или шёпот возобновился после тайм-аута
        if uid != prev_uid or (now - prev_ts) > 1.5:
            self._whisper_in_uid = uid
            self.whisper_received.emit(uid)

        self._whisper_in_ts = now
        self.add_incoming_packet(uid, seq, data, 0)

    def add_incoming_stream_packet(self, uid, seq, data, flags=0):
        """Входящий пакет стрим-аудио (FLAG_STREAM_AUDIO) от сервера."""
        try:
            self.incoming_stream_packets.put_nowait((uid, seq, data, flags))
        except:
            pass

    def set_stream_audio_enabled(self, enabled: bool):
        """Включить/выключить передачу микрофона и системного звука стримером зрителям."""
        self.stream_audio_sending_enabled = enabled

        # Start or stop the WASAPI Loopback capture automatically
        if enabled:
            self.aec.reset()  # Очищаем AEC-буфер перед новым стримом
            self.start_stream_audio()
        else:
            self.stop_stream_audio()
            self.aec.reset()  # Очищаем после остановки

        print(f"[Audio] Stream mic & loopback sending: {'ON' if enabled else 'OFF'}")

    def set_stream_volume(self, volume: float):
        """
        Установить громкость стрима для зрителя (0.0–2.0).
        Вызывается из оверлея VideoWindow.
        """
        self.stream_volume = max(0.0, min(2.0, volume))
        print(f"[Audio] Stream volume set to {self.stream_volume:.2f}")

    def start_stream_audio(self, device_idx=None):
        """
        Запустить захват системного аудио (WASAPI Loopback) для трансляции.
        device_idx — индекс WASAPI output-устройства из list_wasapi_output_devices().
        Если None — автоматически выбирается дефолтное WASAPI output.
        """
        self.stream_audio_capture.start(device_idx)

    def stop_stream_audio(self):
        """Остановить захват системного аудио."""
        self.stream_audio_capture.stop()

    @property
    def is_muted(self):
        return self._is_muted.is_set()

    @is_muted.setter
    def is_muted(self, value):
        if value:
            self._is_muted.set()
        else:
            self._is_muted.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)

    @property
    def is_deafened(self):
        return self._is_deafened.is_set()

    @is_deafened.setter
    def is_deafened(self, value):
        if value:
            self._is_deafened.set()
        else:
            self._is_deafened.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)