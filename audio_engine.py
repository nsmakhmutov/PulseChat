import threading
import queue
import math
from collections import deque
import numpy as np
import sounddevice as sd
import opuslib
import heapq
import struct
import time
# ── Встроенная замена scipy.signal (butter / sosfilt / sosfilt_zi) ─────────────
# Причина: scipy.signal при импорте транзитивно подтягивает scipy.stats, которая
# содержит exec()-генерацию в _distn_infrastructure.py. В замороженном exe
# (PyInstaller) имя 'obj' теряется из exec()-контекста → NameError на старте.
# collect_submodules/collect_data_files не устраняют эту проблему.
#
# Данная реализация покрывает ровно три вызова этого файла:
#   butter(4, 1200, btype='low', fs=48000, output='sos')
#   sosfilt(sos, x, zi=zi)
#   sosfilt_zi(sos)
# Алгоритм: аналоговый прототип Баттерворта → bilinear transform → SOS.

def butter(order: int, cutoff_hz, btype: str = 'low',
           fs: float = None, output: str = 'ba') -> np.ndarray:
    """
    Butterworth LP-фильтр → SOS матрица.
    Поддерживает только btype='low', output='sos'.
    """
    if btype != 'low' or output != 'sos':
        raise NotImplementedError("butter(): только btype='low', output='sos'")
    if fs is None:
        raise ValueError("butter(): требуется параметр fs")

    Wn  = float(cutoff_hz) / (fs * 0.5)          # нормированная (0..1, 1=Найквист)
    wa  = 2.0 * np.tan(np.pi * Wn * 0.5)         # pre-warp → аналоговая частота

    # Аналоговые полюсы Баттерворта (левая полуплоскость, |p|=1)
    k       = np.arange(order)
    poles_a = np.exp(1j * np.pi * (2.0*k + order + 1.0) / (2.0 * order))
    poles_a = poles_a * wa                        # масштаб по частоте среза

    # Bilinear transform z = (1 + s/2) / (1 - s/2)
    # Все нули аналогового LP → z = -1 после преобразования
    z_d    = (1.0 + 0.5*poles_a) / (1.0 - 0.5*poles_a)
    zeros_d = np.full(order, -1.0 + 0j)

    # Сортируем по убыванию Im, чтобы сопряжённые пары стояли рядом
    idx = np.argsort(-z_d.imag)
    z_d = z_d[idx]

    n_sec = order // 2
    sos   = np.zeros((n_sec, 6))

    for i in range(n_sec):
        p1, p2 = z_d[i],      z_d[-(i+1)]        # сопряжённая пара полюсов
        z1, z2 = zeros_d[2*i], zeros_d[2*i+1]    # нули (-1, -1)

        b = np.real(np.poly([z1, z2]))             # числитель:  [1, -(z1+z2), z1*z2]
        a = np.real(np.poly([p1, p2]))             # знаменатель:[1, -(p1+p2), p1*p2]

        sos[i, :3] = b
        sos[i, 3:] = a

    # Нормируем общий DC-gain (H(z=1)) к 1.0.
    # Считаем текущий gain и распределяем коррекцию равномерно по секциям.
    section_gains = np.array([
        np.sum(sos[i, :3]) / np.sum(sos[i, 3:]) for i in range(n_sec)
    ])
    total_gain = np.prod(section_gains)
    per_sec_corr = total_gain ** (1.0 / n_sec)
    for i in range(n_sec):
        sos[i, :3] /= per_sec_corr

    return sos


def sosfilt(sos: np.ndarray, x: np.ndarray,
            zi: np.ndarray = None):
    """
    Применяет SOS-фильтр к сигналу x. Возвращает (y, zf).
    Direct Form II Transposed (DF2T) — совместимо с scipy.signal.sosfilt.
    """
    x   = np.asarray(x, dtype=np.float64)
    n_s = sos.shape[0]
    zf  = (np.zeros((n_s, 2), dtype=np.float64)
           if zi is None else np.array(zi, dtype=np.float64))
    y   = x.copy()

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        s1, s2 = zf[i, 0], zf[i, 1]
        out = np.empty_like(y)
        for n in range(len(y)):                    # hot-path: 960 итераций × 2 секции
            v      = y[n]
            out[n] = b0 * v + s1
            s1     = b1 * v - a1 * out[n] + s2
            s2     = b2 * v - a2 * out[n]
        zf[i, 0], zf[i, 1] = s1, s2
        y = out

    return y, zf


def sosfilt_zi(sos: np.ndarray) -> np.ndarray:
    """
    Начальные условия для sosfilt (unit step, без переходного процесса).
    DF2T steady-state при x=1: zi[i] = [s1_ss, s2_ss] для каждой секции.
    """
    n_s  = sos.shape[0]
    zi   = np.zeros((n_s, 2), dtype=np.float64)
    scale = 1.0                                    # накопленный gain от предыдущих секций

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        K       = (b0 + b1 + b2) / (1.0 + a1 + a2)   # DC gain этой секции
        zi[i,1] = (b2 - a2 * K) * scale               # s2 в steady-state
        zi[i,0] = (b1 - a1 * K) * scale + zi[i,1]     # s1 в steady-state
        scale  *= K                                    # выход → вход следующей секции

    return zi
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

    def __init__(self, send_queue, uid_getter):
        self.send_queue = send_queue
        self.get_uid = uid_getter
        self._running = threading.Event()
        self._thread = None
        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION)
        self.encoder.bitrate = DEFAULT_BITRATE
        self.encoder.complexity = 5
        self._sequence = 0
        self._native_sr: int = SAMPLE_RATE

        # Промежуточный буфер для сборки точных 20ms фреймов (CHUNK_SIZE).
        # Предаллоцируем с запасом 8× CHUNK_SIZE — ни разу не растём при обычной работе.
        # self._pcm_len — логическая длина данных в буфере (не size буфера).
        # Это устраняет np.concatenate (50x/сек) → 0 аллокаций в hot path.
        self._pcm_buf = np.empty(CHUNK_SIZE * 8, dtype=np.float32)
        self._pcm_len = 0
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

                # Откусываем строго по CHUNK_SIZE (960 семплов = 20мс) и отправляем
                while self._pcm_len >= CHUNK_SIZE:
                    chunk = self._pcm_buf[:CHUNK_SIZE].copy()
                    # Сдвигаем остаток влево (numpy делает это на C-уровне)
                    self._pcm_len -= CHUNK_SIZE
                    self._pcm_buf[:self._pcm_len] = self._pcm_buf[CHUNK_SIZE:CHUNK_SIZE + self._pcm_len]

                    # ── Отправляем чанк зрителям ───────────────────────────
                    # VB-CABLE физически не содержит голосов зрителей →
                    # AEC не нужен, эхо невозможно как явление.
                    pcm = (chunk * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm, CHUNK_SIZE)

                    self._sequence += 1
                    flags = FLAG_STREAM_AUDIO | FLAG_LOOPBACK_AUDIO
                    # Используем прекомпилированный struct вместо struct.pack('!IdIB', ...)
                    packet = UDP_HEADER_STRUCT.pack(uid, time.time(), self._sequence, flags) + encoded
                    self.send_queue.put_nowait(packet)

                    if self._sequence % 50 == 0:
                        print(f"[StreamAudio-Capture] Захвачен и отправлен системный звук (seq={self._sequence})")

        except Exception as e:
            pass


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
        # volume_zero=True когда vol==0.0 (ползунок в 0).
        # Отдельный флаг — не конфликтует с кнопкой is_locally_muted.
        # audio_callback использует его чтобы пропустить Opus-decode (экономия CPU).
        # UI использует его чтобы показать ban-иконку, как при заглушении.
        self.volume_zero = False
        self.remote_muted = False
        self.remote_deafened = False


class AudioHandler(QObject):
    volume_level_signal = pyqtSignal(int)
    status_changed = pyqtSignal(bool, bool)
    whisper_received = pyqtSignal(int)
    whisper_ended = pyqtSignal()
    # Испускается когда ползунок громкости пользователя достигает/покидает 0.
    # (uid, is_zero) — UI показывает ban-иконку при is_zero=True,
    # точно так же как при нажатии кнопки «Заглушить».
    user_volume_zero = pyqtSignal(int, bool)

    def _apply_whisper_bass_effect(self, s: np.ndarray) -> np.ndarray:
        """
        [УСТАРЕЛО — оставлен для совместимости, не вызывается]
        Эффект «рация» — LP Butterworth 4-го порядка, fc=1200 Гц.
        Заменён на _apply_anonymous_voice_effect (pitch shift + LP 4 кГц).
        """
        out, self._wlp_zi = sosfilt(self._wlp_sos, s.astype(np.float64), zi=self._wlp_zi)
        return out.astype(np.float32)

    def _apply_anonymous_voice_effect(self, s: np.ndarray) -> np.ndarray:
        """
        Эффект «анонимного голоса» (Dark TV Interview).
        Использует Vectorized Dual-Tap Delay Line для pitch-shift без артефактов.
        """
        N = len(s)
        max_delay = 1440  # 30 мс при 48kHz — оптимальный размер окна для голоса
        speed = 2.0 ** (-4.0 / 12.0)  # -4 полутона
        rate = 1.0 - speed  # Скорость накопления задержки

        # 1. Склеиваем историю и текущий фрейм в предаллоцированный буфер.
        # Избегаем np.concatenate (аллокация ~12 KB каждые 20 мс в hot path).
        H = len(self._anon_history)   # 2048 — константа
        self._anon_buf[:H] = self._anon_history
        self._anon_buf[H:H + N] = s
        buf = self._anon_buf[:H + N]

        # 2. Генерируем фазы для двух читающих "головок" (0.0 ... 1.0)
        phases = self._anon_phase + np.arange(N) * rate / max_delay
        self._anon_phase = phases[-1] + rate / max_delay
        self._anon_phase %= 1.0

        p1 = phases % 1.0
        p2 = (phases + 0.5) % 1.0

        # Задержка в сэмплах
        d1 = p1 * max_delay
        d2 = p2 * max_delay

        # 3. Индексы чтения (относительно начала массива buf)
        base_idx = len(self._anon_history) + np.arange(N)
        r1 = base_idx - d1
        r2 = base_idx - d2

        # 4. Линейная интерполяция для плавности
        i1_floor = np.floor(r1).astype(np.int32)
        i2_floor = np.floor(r2).astype(np.int32)

        # Безопасный +1 индекс
        i1_ceil = np.clip(i1_floor + 1, 0, len(buf) - 1)
        i2_ceil = np.clip(i2_floor + 1, 0, len(buf) - 1)

        frac_1 = r1 - i1_floor
        frac_2 = r2 - i2_floor

        val_1 = buf[i1_floor] * (1.0 - frac_1) + buf[i1_ceil] * frac_1
        val_2 = buf[i2_floor] * (1.0 - frac_2) + buf[i2_ceil] * frac_2

        # 5. Кроссфейд (окно Ханна) для устранения щелчков
        fade_1 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p1)
        fade_2 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p2)

        shifted = val_1 * fade_1 + val_2 * fade_2

        # 6. Обновляем историю для следующего фрейма
        self._anon_history = buf[-len(self._anon_history):]

        # 7. LP-фильтр (4 кГц) для "тёмного" окраса (скрывает артефакты формант)
        out, self._anon_lp_zi = sosfilt(self._anon_lp_sos, shifted, zi=self._anon_lp_zi)

        # 8. Мягкая нормализация пика
        peak = np.max(np.abs(out))
        if peak > 0.9:
            out *= 0.9 / peak

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
        # FIX #5: threading.Event вместо простого bool.
        # bool-присваивание GIL-атомарно, но Event явно выражает намерение и
        # согласуется со стилем _is_muted / _is_deafened в этом же классе.
        self._stream_audio_sending = threading.Event()
        # AEC удалён: при использовании VB-CABLE (CABLE Output) эхо физически
        # невозможно — голоса зрителей никогда не попадают в CABLE Output.
        # Захват системного аудио
        self.stream_audio_capture = StreamAudioCapture(self.send_queue, lambda: self.my_uid)
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
        # Реализован через scipy sosfilt (C-уровень) вместо pure-Python цикла.
        # SOS-матрица постоянна, начальные условия zi хранят состояние между фреймами.
        # Сброс zi производится в start_whisper().
        self._wlp_sos = butter(4, 1200, btype='low', fs=48000, output='sos')
        self._wlp_zi  = sosfilt_zi(self._wlp_sos).astype(np.float64)

        # ── LP-фильтр для эффекта анонимного голоса (fc=4000 Гц) ──────────────
        # Мягче чем whisper LP (1200 Гц): сохраняет согласные и зону присутствия.
        # Используется в _apply_anonymous_voice_effect (получатель шёпота).
        # Отдельная SOS/zi — не пересекается с _wlp_sos (старый эффект рации).
        self._anon_lp_sos = butter(4, 4000, btype='low', fs=48000, output='sos')
        self._anon_lp_zi  = sosfilt_zi(self._anon_lp_sos).astype(np.float64)
        self._anon_history = np.zeros(2048, dtype=np.float32)
        self._anon_phase = 0.0
        # Предаллоцированный буфер для _apply_anonymous_voice_effect.
        # Фиксированный размер: 2048 (история) + CHUNK_SIZE (960) = 3008 сэмплов.
        # Убирает np.concatenate (аллокацию) из hot path (~50/сек при активном шёпоте).
        self._anon_buf = np.zeros(2048 + CHUNK_SIZE, dtype=np.float32)
        # Флаг сброса whisper-эффекта. Выставляется из UI-потока (start_whisper),
        # читается и сбрасывается из audio_callback — безопасный межпоточный handoff
        # без мьютекса (GIL-атомарное чтение/запись bool).
        self._whisper_effect_reset: bool = False
        # Отслеживаем uid последнего шептуна: при смене сбрасываем состояние фильтра.
        self._whisper_effect_uid: int = 0
        # Отдельный счётчик для FLAG_STREAM_VOICES пакетов.
        # Нельзя использовать my_sequence: несколько спикеров в одном audio_callback
        # получали бы одинаковый seq → JitterBuffer на стороне зрителя отбрасывал
        # второй и последующие пакеты (seq <= last_seq → return), зрители слышали
        # только первого спикера. Отдельный монотонный счётчик решает проблему.
        self._sv_sequence = 0
        # FIX #3: deque(maxlen=5) вместо list.
        # vad_pre_buffer.pop(0) на list — O(n): сдвигает все элементы влево.
        # deque.popleft() — O(1), что важно для audio_callback hot path.
        # maxlen=5 заменяет ручную проверку `if len > 5: pop(0)`.
        self.vad_pre_buffer = deque(maxlen=5)
        self.was_talking = False
        self.stream = None
        # Счётчик воспроизведённых loopback-кадров (для периодического лога).
        # Инициализируем здесь чтобы убрать hasattr() из audio_callback hot path.
        self._lb_play_counter = 0
        # Ссылки на рабочие потоки — нужны для корректного join() в stop().
        # Без явного join() повторные вызовы start() (переподключение, смена
        # устройства) накапливают «зомби»-потоки: каждый поток висит в памяти
        # пока не завершится _is_running.wait(), что может занять до 0.1 сек
        # после clear(). За 10 переподключений = 20 лишних потоков.
        self._pkt_thread: threading.Thread | None = None
        self._stream_pkt_thread: threading.Thread | None = None

        # -------------------------------------------------------------------
        # FIX #1: Copy-on-Write снимки для audio_callback.
        #
        # Проблема: audio_callback — реалтайм-поток с дедлайном 20 мс.
        # Захват users_lock / stream_users_lock внутри callback'а блокировал
        # его на время работы _packet_processor_loop (удерживает тот же лок).
        # Результат: пропуск дедлайна → слышимые щелчки и глитчи в аудио.
        #
        # Решение: _packet_processor_loop берёт снимок dict после каждого
        # изменения remote_users (внутри того же with users_lock).
        # audio_callback читает _audio_users_snapshot БЕЗ лока:
        #   - Присваивание ссылки dict GIL-атомарно → нет torn read.
        #   - Снимок «отстаёт» максимум на 1 пакет (~20 мс) — для аудио незаметно.
        #   - RemoteUser.jitter_buffer имеет собственный лок → thread-safe.
        #   - RemoteUser.volume / .is_locally_muted — простые примитивы, GIL-safe.
        # -------------------------------------------------------------------
        self._audio_users_snapshot: dict = {}
        self._audio_stream_users_snapshot: dict = {}

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
        direction = "INPUT" if is_input else "OUTPUT"
        if not name:
            print(f"[DEBUG] find_device_index_by_name: {direction} name=None → вернём None (дефолт системы)", flush=True)
            return None
        devices = sd.query_devices()
        print(f"[DEBUG] find_device_index_by_name: ищем {direction} '{name}'", flush=True)
        for i, d in enumerate(devices):
            try:
                api_name = sd.query_hostapis(d['hostapi'])['name']
                full_name = f"{d['name']} ({api_name})"
                if full_name.strip() == name.strip():
                    if is_input and d['max_input_channels'] > 0:
                        print(f"[DEBUG] find_device_index_by_name: найдено {direction} idx={i} '{full_name}'", flush=True)
                        return i
                    if not is_input and d['max_output_channels'] > 0:
                        print(f"[DEBUG] find_device_index_by_name: найдено {direction} idx={i} '{full_name}'", flush=True)
                        return i
            except:
                continue
        print(f"[DEBUG] find_device_index_by_name: {direction} '{name}' НЕ НАЙДЕНО → None (дефолт)", flush=True)
        return None

    def start(self, input_name=None, output_name=None):
        print(f"[DEBUG] AudioHandler.start: BEGIN — input_name={input_name!r}, output_name={output_name!r}", flush=True)
        if self.my_uid == 0:
            print("[DEBUG] AudioHandler.start: my_uid==0, выход", flush=True)
            return
        print("[DEBUG] AudioHandler.start: вызов stop()...", flush=True)
        self.stop()
        time.sleep(0.1)
        print("[DEBUG] AudioHandler.start: stop() выполнен", flush=True)

        print("[DEBUG] AudioHandler.start: поиск устройств...", flush=True)
        in_idx = self.find_device_index_by_name(input_name, True)
        out_idx = self.find_device_index_by_name(output_name, False)
        print(f"[DEBUG] AudioHandler.start: in_idx={in_idx}, out_idx={out_idx}", flush=True)

        self._is_running.set()
        try:
            print("[DEBUG] AudioHandler.start: создание sd.Stream...", flush=True)
            self.stream = sd.Stream(
                device=(in_idx, out_idx),
                samplerate=SAMPLE_RATE, blocksize=CHUNK_SIZE,
                dtype='float32', channels=CHANNELS,
                callback=self.audio_callback
            )
            print("[DEBUG] AudioHandler.start: sd.Stream создан, вызов stream.start()...", flush=True)
            self.stream.start()
            print("[DEBUG] AudioHandler.start: stream.start() выполнен", flush=True)
            self._pkt_thread = threading.Thread(target=self._packet_processor_loop, daemon=True)
            self._stream_pkt_thread = threading.Thread(target=self._stream_packet_processor_loop, daemon=True)
            self._pkt_thread.start()
            self._stream_pkt_thread.start()
            print("[DEBUG] AudioHandler.start: рабочие потоки запущены — DONE", flush=True)
        except Exception as e:
            import traceback
            print(f"[DEBUG] AudioHandler.start: EXCEPTION:\n{traceback.format_exc()}", flush=True)
            self._is_running.clear()

    def stop(self):
        self._is_running.clear()
        # Дожидаемся завершения рабочих потоков — иначе повторный start()
        # создаст дублирующие потоки (утечка памяти и CPU)
        for attr in ('_pkt_thread', '_stream_pkt_thread'):
            t = getattr(self, attr, None)
            if t is not None and t.is_alive():
                t.join(timeout=0.5)
            setattr(self, attr, None)
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

            # FIX #1: обновляем COW-снимок после удаления пользователей
            self._audio_users_snapshot = dict(self.remote_users)

        # stream_remote_users — под отдельным локом (не блокировать audio_callback)
        with self.stream_users_lock:
            for uid in list(self.stream_remote_users.keys()):
                real_uid = uid - LOOPBACK_UID_OFFSET if uid >= LOOPBACK_UID_OFFSET else uid
                if real_uid not in active_uids:
                    del self.stream_remote_users[uid]
            # FIX #1: обновляем COW-снимок после удаления
            self._audio_stream_users_snapshot = dict(self.stream_remote_users)

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
                        val = float(val)
                        self.remote_users[uid].volume = val
                        self.remote_users[uid].volume_zero = (val == 0.0)

                    user = self.remote_users[uid]
                    user.remote_muted = bool(flags & 1)
                    user.remote_deafened = bool(flags & 2)

                    if data:
                        user.jitter_buffer.add(seq, data)
                        user.last_packet_time = time.time()

                    # FIX #1: обновляем COW-снимок внутри лока — согласованное состояние.
                    # dict() копирует только ссылки (не RemoteUser объекты) — это быстро.
                    # audio_callback читает снимок без лока, опираясь на GIL-атомарность
                    # присваивания ссылки.
                    self._audio_users_snapshot = dict(self.remote_users)

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
                        # FIX #1: COW-снимок для audio_callback
                        self._audio_stream_users_snapshot = dict(self.stream_remote_users)
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
                        # FIX #1: COW-снимок для audio_callback
                        self._audio_stream_users_snapshot = dict(self.stream_remote_users)

            except queue.Empty:
                continue
            except Exception:
                pass

    def audio_callback(self, indata, outdata, frames, time_info, status):
        # Логируем только первый вызов — подтверждает что callback запустился
        if not getattr(self, '_cb_first_logged', False):
            self._cb_first_logged = True
            print("[DEBUG] audio_callback: ПЕРВЫЙ ВЫЗОВ — PortAudio callback работает", flush=True)
        if status:
            print(f"[DEBUG] audio_callback: status={status}", flush=True)

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

        # ── Pre-encode input normalization ──────────────────────────────────
        # Если denoised_float содержит пики > 1.0 (микрофонный буст Windows,
        # RNNoise иногда выходит за ±1.0, некоторые ASIO-драйверы) →
        # умножение на 32767 даёт значения > INT16_MAX → wraparound в
        # отрицательную зону → жёсткий треск именно при громком голосе
        # («на пределе микрофона»). Soft-limit здесь — единственная защита.
        # Используем in-place операцию: аллокаций нет.
        _in_peak = np.max(np.abs(denoised_float))
        if _in_peak > 0.98:
            # Нормализуем к 0.98 — оставляем 2% запас до INT16_MAX
            denoised_float = denoised_float * (0.98 / _in_peak)

        rms = np.sqrt(np.mean(denoised_float ** 2))
        self.volume_level_signal.emit(int(min(rms * 1000, 100)))

        is_talking = rms > self.vad_threshold or (curr_time - self.last_voice_time < self.vad_hangover)
        if rms > self.vad_threshold: self.last_voice_time = curr_time

        if self.my_uid != 0:
            mute_flag = 1 if self._is_muted.is_set() else 0
            deaf_flag = 2 if self._is_deafened.is_set() else 0
            flags = mute_flag | deaf_flag

            # Читаем whisper_target_uid ДО проверки мута — шёпот обходит мут.
            # Это атомарное чтение int (GIL-safe).
            whisper_uid = self.whisper_target_uid

            try:
                if is_talking and whisper_uid != 0:
                    # ── РЕЖИМ ШЁПОТА ─────────────────────────────────────────
                    # Шёпот отправляется НЕЗАВИСИМО от состояния мута.
                    # Мут означает «не говорить в комнату» — шёпот приватный
                    # и не нарушает намерение пользователя заглушить себя от
                    # остальных. PTT-кнопка шёпота — явное действие отправить.
                    #
                    # Нормальный аудио-пакет в комнату НЕ кладём в очередь →
                    # остальные участники не слышат отправителя в этот момент.
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    # FIX: убираем лишний my_sequence += 1.
                    # В режиме шёпота пакет в комнату НЕ отправляется — my_sequence
                    # не должен расти. stop_whisper() синхронизирует его с
                    # _whisper_sequence, так что разрыва seq при возврате не будет.
                    self._whisper_sequence += 1
                    w_flags = FLAG_WHISPER
                    w_header = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                           self._whisper_sequence, w_flags)
                    # Payload: [target_uid: 4 байта] + [opus]
                    w_payload = struct.pack('!I', whisper_uid) + encoded
                    try:
                        self.send_queue.put_nowait(w_header + w_payload)
                    except Exception:
                        pass

                elif is_talking and not self._is_muted.is_set():
                    # ── ОБЫЧНЫЙ РЕЖИМ: пакет в комнату ───────────────────────
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    self.my_sequence += 1
                    packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, self.my_sequence, flags) + encoded

                    if not self.was_talking:
                        # FIX #3: deque.popleft() — O(1) вместо list.pop(0) — O(n)
                        while self.vad_pre_buffer:
                            try:
                                self.send_queue.put_nowait(self.vad_pre_buffer.popleft())
                            except:
                                pass
                        self.was_talking = True
                    self.send_queue.put_nowait(packet)

                    # Стрим-аудио: дополнительно посылаем тот же encoded с FLAG_STREAM_AUDIO
                    # Сервер направит его только зрителям, а не в комнату (нет дублирования)
                    if self._stream_audio_sending.is_set():
                        stream_flags = flags | FLAG_STREAM_AUDIO
                        stream_packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                                    self.my_sequence, stream_flags) + encoded
                        try:
                            self.send_queue.put_nowait(stream_packet)
                        except:
                            pass

                else:
                    # Не говорим (или мут без шёпота) — сбрасываем was_talking,
                    # пополняем pre_buffer для следующего старта речи.
                    self.was_talking = False
                    if not is_talking and whisper_uid == 0:
                        empty_packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, 0, flags)
                        # FIX #3: deque(maxlen=5) — автоматически вытесняет старые
                        # элементы при переполнении, ручная проверка len > 5 не нужна.
                        self.vad_pre_buffer.append(empty_packet)
            except:
                pass

        self.mix_buffer.fill(0)
        if not self._is_deafened.is_set():
            # ── N-speaker headroom ────────────────────────────────────────────
            # Проблема: 3+ участников говорят одновременно → сумма амплитуд
            # до 3.0–4.0 → даже soft limiter давит сигнал в 3× → все тихие
            # и «мутные». Это не дисторшн, но воспринимается как «плохое качество».
            #
            # Решение: заранее вычисляем gain для каждого активного спикера
            # по формуле sqrt(2) / sqrt(N_active). При N=1: gain=1.0 (без изменений).
            # При N=2: gain=1.0 (пара = норма). При N=3: gain=0.82. При N=4: gain=0.71.
            # Это стандартный incoherent sources scaling — суммарная RMS остаётся
            # постоянной независимо от числа говорящих.
            #
            # Считаем «активных»: last_packet < 1.5с AND не заглушен AND volume > 0.
            # Не блокируемся — читаем уже готовый COW-снимок без лока.
            _n_active = sum(
                1 for u in self._audio_users_snapshot.values()
                if (curr_time - u.last_packet_time < 1.5
                    and not u.is_locally_muted
                    and not u.volume_zero)
            )
            # Включаем стрим-пользователей в подсчёт (они тоже добавляются в mix)
            _n_active += sum(
                1 for u in self._audio_stream_users_snapshot.values()
                if curr_time - u.last_packet_time < 1.5
            )
            # gain: при 1–2 спикерах = 1.0 (без изменений),
            # при 3+ — плавно снижается, сохраняя суммарную громкость.
            # Не меняем gain агрессивно: берём max(2, N) чтобы 2 человека
            # никогда не получали ослабления.
            _speaker_gain = math.sqrt(2.0) / math.sqrt(max(2, _n_active))

            # FIX #1: читаем COW-снимок БЕЗ лока.
            # _packet_processor_loop обновляет _audio_users_snapshot внутри
            # users_lock после каждого изменения. Снимок «отстаёт» максимум
            # на 1 пакет (~20 мс) — для аудиомикширования незаметно.
            # JitterBuffer.get() имеет собственный внутренний лок — thread-safe.
            for uid, user in self._audio_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if data and not user.is_locally_muted and not user.volume_zero:
                        try:
                            decoded = user.decoder.decode(data, CHUNK_SIZE)
                            s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0

                            # ── Whisper voice effect ─────────────────────────────────────
                            # Если этот пользователь сейчас шепчет нам (whisper пакеты
                            # приходили < 2 с назад) — применяем питч-даун эффект.
                            # Голос становится заметно глубже/темнее → получатель
                            # слышит «другой тип разговора» без каких-либо UI подсказок.
                            w_uid = getattr(self, '_whisper_in_uid', 0)
                            w_ts  = getattr(self, '_whisper_in_ts',  0.0)
                            if uid == w_uid and (curr_time - w_ts) < 2.0:
                                # FIX: сброс состояния эффекта при смене шептуна
                                # или по флагу от start_whisper().
                                # Без этого первые ~50 мс нового шёпота воспроизводятся
                                # с хвостом фильтра от предыдущего шептуна → артефакт.
                                if (self._whisper_effect_uid != w_uid
                                        or self._whisper_effect_reset):
                                    self._whisper_effect_uid = w_uid
                                    self._whisper_effect_reset = False
                                    self._anon_history.fill(0)
                                    self._anon_phase = 0.0
                                    self._anon_lp_zi = sosfilt_zi(
                                        self._anon_lp_sos).astype(np.float64)
                                s = self._apply_anonymous_voice_effect(s)
                            else:
                                # Шептун неактивен — сбрасываем uid чтобы следующий
                                # шептун всегда стартовал с чистым состоянием фильтра.
                                if self._whisper_effect_uid != 0:
                                    self._whisper_effect_uid = 0

                            self.mix_buffer += s * (user.volume * _speaker_gain)

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
                            if self._stream_audio_sending.is_set():
                                try:
                                    self._sv_sequence += 1
                                    sv_flags = (FLAG_STREAM_AUDIO | FLAG_STREAM_VOICES)
                                    # header: uid стримера (отправитель), ts, seq, flags
                                    sv_header = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
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
            # FIX #1: читаем COW-снимок БЕЗ лока — аналогично блоку remote_users выше.
            for s_uid, user in self._audio_stream_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if data:
                        try:
                            decoded = user.decoder.decode(data, CHUNK_SIZE)
                            s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0
                            self.mix_buffer += s * sv * _speaker_gain
                            if s_uid >= LOOPBACK_UID_OFFSET:
                                self._lb_play_counter += 1
                                if self._lb_play_counter % 100 == 0:
                                    print(f"[Audio-Output] Стрим-звук воспроизводится "
                                          f"(громкость: {sv:.2f})")
                        except Exception as e:
                            print(f"[Audio-Output] Ошибка декодирования стрим-аудио: {e}")

        # FIX: Soft limiter вместо жёсткого clip.
        # Жёсткий clip при пиках > 1.0 (2-3 говорящих + stream audio) создаёт
        # waveshaping дисторшн — нелинейные гармоники, слышимые как хруст/артефакт.
        # Решение: если пик > 0.95 — нормализуем весь буфер пропорционально.
        # Это аналог look-ahead limiter без attack/release (приемлемо для 20 мс фреймов).
        # np.clip остаётся как safety net для float-погрешностей.
        _peak = np.max(np.abs(self.mix_buffer))
        if _peak > 0.95:
            self.mix_buffer *= (0.95 / _peak)
        np.clip(self.mix_buffer, -1.0, 1.0, out=self.mix_buffer)

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
                    self.remote_users[uid].volume_zero = (saved_vol == 0.0)
                else:
                    self.pending_volumes[uid] = saved_vol

    def set_user_volume(self, uid, vol):
        # Зажимаем в [0.0 … 2.0]. vol=0.0 → «тихий мут» через ползунок.
        vol = max(0.0, min(2.0, float(vol)))
        emit_zero_state = None  # None = состояние не изменилось

        with self.users_lock:
            if uid in self.remote_users:
                user = self.remote_users[uid]
                prev_zero = user.volume_zero
                user.volume = vol
                user.volume_zero = (vol == 0.0)
                if user.volume_zero != prev_zero:
                    emit_zero_state = user.volume_zero
                ip = self.uid_to_ip.get(uid)
                if ip:
                    self.settings.setValue(f"vol_ip_{ip}", vol)
                else:
                    self.settings.setValue(f"volume_{uid}", vol)

        # Эмитируем сигнал ВНЕ лока — не блокируем аудиопоток
        if emit_zero_state is not None:
            self.user_volume_zero.emit(uid, emit_zero_state)

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
        # Сбрасываем состояние sosfilt-фильтра чтобы шёпот каждого нового собеседника
        # начинался с чистого состояния (без «хвоста» от предыдущего шёпота).
        self._wlp_zi = sosfilt_zi(self._wlp_sos).astype(np.float64)
        # FIX race condition: сброс состояния фильтра через флаг, а не напрямую.
        # Прямой вызов _anon_history.fill(0) / _anon_phase=0 из UI-потока конкурирует
        # с audio_callback (PortAudio thread). numpy снимает GIL → torn read/write →
        # щелчки. Флаг — атомарный bool, audio_callback сбросит состояние сам.
        self._whisper_effect_reset = True
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

        Испускает whisper_received(uid) на КАЖДЫЙ пакет — это необходимо
        для корректной работы UI-таймера завершения шёпота (_whisper_end_timer).

        Почему раньше было неправильно:
          Сигнал испускался только при первом пакете или после паузы >1.5с.
          _whisper_end_timer (1500 мс, single-shot) перезапускался только тогда.
          Результат: через ~1.5с после начала шёпота таймер срабатывал и скрывал
          оверлей, хотя шептун всё ещё держал PTT-кнопку.

        Почему теперь правильно:
          Сигнал испускается на каждый пакет (~50/сек). MainWindow._on_whisper_received
          перезапускает таймер при каждом сигнале, но обновляет текст/показывает
          оверлей только при смене отправителя (uid != текущий) — без визуального
          мерцания. Пока идут пакеты — таймер никогда не истекает.
        """
        now = time.time()
        prev_uid = getattr(self, '_whisper_in_uid', 0)

        # Обновляем метаданные шёпота
        self._whisper_in_uid = uid
        self._whisper_in_ts  = now

        # Эмитим на каждый пакет: UI-таймер перезапускается, оверлей не гаснет.
        # MainWindow._on_whisper_received сам решает нужно ли показывать оверлей заново
        # (только если uid изменился или оверлей ещё не виден).
        self.whisper_received.emit(uid)

        self.add_incoming_packet(uid, seq, data, 0)

    def add_incoming_stream_packet(self, uid, seq, data, flags=0):
        """Входящий пакет стрим-аудио (FLAG_STREAM_AUDIO) от сервера."""
        try:
            self.incoming_stream_packets.put_nowait((uid, seq, data, flags))
        except:
            pass

    def set_stream_audio_enabled(self, enabled: bool):
        """Включить/выключить передачу микрофона и системного звука стримером зрителям."""
        if enabled:
            self._stream_audio_sending.set()
        else:
            self._stream_audio_sending.clear()
        self.start_stream_audio() if enabled else self.stop_stream_audio()
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