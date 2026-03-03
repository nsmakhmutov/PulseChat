import threading
import queue
import math
import struct
import time
from collections import deque
from typing import Optional

import numpy as np
import sounddevice as sd
import opuslib
import heapq

from PyQt6.QtCore import QObject, pyqtSignal, QSettings
from config import *

# ── Встроенная замена scipy.signal (butter / sosfilt / sosfilt_zi) ──────────
# scipy.stats содержит exec()-генерацию в _distn_infrastructure.py,
# которая ломает замороженный exe (PyInstaller) с NameError на старте.
# Покрывает ровно три вызова этого файла:
#   butter(4, 1200/4000, btype='low', fs=48000, output='sos')
#   sosfilt(sos, x, zi=zi)
#   sosfilt_zi(sos)


def butter(order: int, cutoff_hz: float, btype: str = 'low',
           fs: float = None, output: str = 'ba') -> np.ndarray:
    """Butterworth низкочастотный фильтр → SOS-матрица.

        :param order: порядок фильтра
        :param cutoff_hz: частота среза в Гц
        :param btype: тип фильтра (только 'low')
        :param fs: частота дискретизации в Гц
        :param output: формат вывода (только 'sos')
        :return: SOS-матрица (n_sec, 6)
    """
    if btype != 'low' or output != 'sos':
        raise NotImplementedError("butter(): только btype='low', output='sos'")
    if fs is None:
        raise ValueError("butter(): требуется параметр fs")

    Wn = float(cutoff_hz) / (fs * 0.5)
    wa = 2.0 * np.tan(np.pi * Wn * 0.5)          # pre-warp → аналоговая частота

    # Аналоговые полюсы Баттерворта (левая полуплоскость)
    k = np.arange(order)
    poles_a = np.exp(1j * np.pi * (2.0 * k + order + 1.0) / (2.0 * order)) * wa

    # Bilinear transform; все нули LP → z = -1
    z_d = (1.0 + 0.5 * poles_a) / (1.0 - 0.5 * poles_a)
    zeros_d = np.full(order, -1.0 + 0j)

    idx = np.argsort(-z_d.imag)
    z_d = z_d[idx]

    n_sec = order // 2
    sos = np.zeros((n_sec, 6))

    for i in range(n_sec):
        p1, p2 = z_d[i], z_d[-(i + 1)]
        z1, z2 = zeros_d[2 * i], zeros_d[2 * i + 1]
        b = np.real(np.poly([z1, z2]))
        a = np.real(np.poly([p1, p2]))
        sos[i, :3] = b
        sos[i, 3:] = a

    # Нормируем DC-gain к 1.0 равномерно по секциям
    section_gains = np.array([np.sum(sos[i, :3]) / np.sum(sos[i, 3:]) for i in range(n_sec)])
    per_sec_corr = np.prod(section_gains) ** (1.0 / n_sec)
    for i in range(n_sec):
        sos[i, :3] /= per_sec_corr

    return sos


def sosfilt(sos: np.ndarray, x: np.ndarray,
            zi: np.ndarray = None) -> tuple:
    """Применяет SOS-фильтр (Direct Form II Transposed).

        :param sos: SOS-матрица коэффициентов
        :param x: входной сигнал
        :param zi: начальные условия фильтра
        :return: (y, zf) — отфильтрованный сигнал и конечные условия
    """
    x = np.asarray(x, dtype=np.float64)
    n_s = sos.shape[0]
    zf = (np.zeros((n_s, 2), dtype=np.float64)
          if zi is None else np.array(zi, dtype=np.float64))
    y = x.copy()

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        s1, s2 = zf[i, 0], zf[i, 1]
        out = np.empty_like(y)
        for n in range(len(y)):
            v = y[n]
            out[n] = b0 * v + s1
            s1 = b1 * v - a1 * out[n] + s2
            s2 = b2 * v - a2 * out[n]
        zf[i, 0], zf[i, 1] = s1, s2
        y = out

    return y, zf


def sosfilt_zi(sos: np.ndarray) -> np.ndarray:
    """Начальные условия для sosfilt (steady-state при unit step, DF2T).

        :param sos: SOS-матрица коэффициентов
        :return: начальные условия формы (n_sections, 2)
    """
    n_s = sos.shape[0]
    zi = np.zeros((n_s, 2), dtype=np.float64)
    scale = 1.0

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        K = (b0 + b1 + b2) / (1.0 + a1 + a2)
        zi[i, 1] = (b2 - a2 * K) * scale
        zi[i, 0] = (b1 - a1 * K) * scale + zi[i, 1]
        scale *= K

    return zi


# Предвычисленные SOS-матрицы — один раз при импорте модуля
_WLP_SOS = butter(4, 1200, btype='low', fs=48000, output='sos')   # whisper-эффект
_ANON_LP_SOS = butter(4, 4000, btype='low', fs=48000, output='sos')  # анонимный голос

try:
    from pyrnnoise import RNNoise
    PYRNNOISE_AVAILABLE = True
except ImportError:
    PYRNNOISE_AVAILABLE = False
    print("[Audio] Внимание: Модуль pyrnnoise не найден.")

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
    """Захват системного аудио через WASAPI Loopback для трансляции зрителям.

    Открывает loopback-поток на выбранном устройстве вывода Windows,
    кодирует в Opus и кладёт пакеты в send_queue с флагом FLAG_STREAM_AUDIO.

    Приоритет стратегий захвата:
        0. VB-CABLE (CABLE Output) — чистый звук без AEC
        A. pyaudiowpatch (WASAPI Loopback, isLoopbackDevice)
        B. sounddevice WasapiSettings(loopback=True)
    """

    def __init__(self, send_queue: queue.Queue, uid_getter):
        self.send_queue = send_queue
        self.get_uid = uid_getter
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION)
        self.encoder.bitrate = DEFAULT_BITRATE
        self.encoder.complexity = 5
        self._sequence = 0
        self._native_sr: int = SAMPLE_RATE
        self._using_vbcable: bool = False

        # Предаллоцированный буфер для сборки точных 20 мс фреймов (CHUNK_SIZE)
        self._pcm_buf = np.empty(CHUNK_SIZE * 8, dtype=np.float32)
        self._pcm_len = 0
        self._buffer_lock = threading.Lock()

        # Очередь локального мониторинга VB-CABLE; None — мониторинг не запущен
        self._vbcable_monitor_queue: Optional[queue.Queue] = None
        self.monitor_volume: float = 1.0

    @staticmethod
    def list_wasapi_output_devices() -> list:
        """Возвращает список WASAPI output-устройств.

            :return: список кортежей (имя, индекс)
        """
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

    def start(self, device_idx: Optional[int] = None):
        """Запускает захват в фоновом потоке.

            :param device_idx: индекс WASAPI output-устройства; None — автовыбор
        """
        self.stop()
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
        """Останавливает захват и ждёт завершения потока."""
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _resolve_device(self, device_idx: Optional[int]) -> Optional[int]:
        """Находит валидный WASAPI output-индекс.

            :param device_idx: предпочтительный индекс; None — автовыбор
            :return: индекс устройства или None
        """
        try:
            apis = sd.query_hostapis()
            devs = sd.query_devices()
            w_idx = next((i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None)
            if w_idx is None:
                return None

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

    def _try_pyaudiowpatch(self, target_name: str) -> bool:
        """Захват WASAPI Loopback через pyaudiowpatch (стратегия A).

            :param target_name: имя OUTPUT-устройства для захвата
            :return: True если захват успешно запущен и отработал до stop()
        """
        if not PYAUDIOWPATCH_AVAILABLE:
            return False

        pa = None
        stream = None
        try:
            pa = _pyaudio.PyAudio()

            wasapi_idx = None
            for i in range(pa.get_host_api_count()):
                info = pa.get_host_api_info_by_index(i)
                if 'WASAPI' in info.get('name', ''):
                    wasapi_idx = i
                    break
            if wasapi_idx is None:
                print("[StreamAudio] [A] WASAPI host API не найден в pyaudiowpatch")
                return False

            candidates = [
                pa.get_device_info_by_index(i)
                for i in range(pa.get_device_count())
                if pa.get_device_info_by_index(i).get('isLoopbackDevice', False)
                and pa.get_device_info_by_index(i).get('hostApi') == wasapi_idx
            ]

            if not candidates:
                print("[StreamAudio] [A] pyaudiowpatch: loopback-устройства не найдены")
                return False

            target_lower = target_name.lower()
            loopback_dev = next(
                (d for d in candidates if d['name'].lower() == target_lower), None
            ) or next(
                (d for d in candidates
                 if target_lower in d['name'].lower() or d['name'].lower() in target_lower),
                None
            ) or candidates[0]

            if loopback_dev == candidates[0] and loopback_dev['name'].lower() != target_lower:
                print(f"[StreamAudio] [A] Точного совпадения нет, берём первый loopback: "
                      f"«{loopback_dev['name']}»")

            ch = max(1, int(loopback_dev.get('maxInputChannels', 2)))
            sr = int(loopback_dev.get('defaultSampleRate', SAMPLE_RATE))
            self._native_sr = sr

            print(f"[StreamAudio] [A] pyaudiowpatch loopback: "
                  f"«{loopback_dev['name']}» idx={loopback_dev['index']} ch={ch} sr={sr}")

            def _pa_callback(in_data, frame_count, time_info, status):
                if not self._running.is_set():
                    return (None, _pyaudio.paComplete)
                try:
                    arr = np.frombuffer(in_data, dtype=np.float32).copy().reshape(-1, ch)
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

    def _try_sounddevice_loopback(self, resolved: int, native_ch: int) -> bool:
        """Захват WASAPI Loopback через sounddevice WasapiSettings (стратегия B).

            :param resolved: индекс sounddevice OUTPUT-устройства
            :param native_ch: число каналов устройства
            :return: True если захват успешно запущен и отработал до stop()
        """
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

    def _try_vbcable(self) -> bool:
        """Захват звука из «CABLE Output» как обычного INPUT-устройства (стратегия 0).

        VB-CABLE: CABLE Input — виртуальный вывод (игра), CABLE Output — виртуальный ввод.
        Голоса зрителей физически отсутствуют в CABLE Output → AEC не нужен.

            :return: True если захват успешно запущен и отработал до stop()
        """
        cable_idx = None
        cable_ch = 2
        cable_sr = SAMPLE_RATE

        try:
            devs = sd.query_devices()
            for i, d in enumerate(devs):
                if 'cable output' in d['name'].lower() and d['max_input_channels'] > 0:
                    cable_idx = i
                    cable_ch = max(1, int(d['max_input_channels']))
                    cable_sr = int(d.get('default_samplerate', SAMPLE_RATE))
                    print(f"[StreamAudio] [VB-CABLE] Найден: «{d['name']}» "
                          f"idx={i} ch={cable_ch} sr={cable_sr}")
                    break

            if cable_idx is None:
                print("[StreamAudio] [VB-CABLE] Устройство 'CABLE Output' не найдено — "
                      "пробуем WASAPI Loopback")
                return False

            self._native_sr = cable_sr
            self._using_vbcable = True

            monitor_q: queue.Queue = queue.Queue(maxsize=80)
            self._vbcable_monitor_queue = monitor_q

            def _monitor_out_cb(outdata, frames, time_info, status):
                try:
                    raw = monitor_q.get_nowait()
                    vol = self.monitor_volume
                    if raw.shape == outdata.shape:
                        np.multiply(raw, vol, out=outdata)
                    else:
                        mono = (np.mean(raw, axis=1, keepdims=True)
                                if raw.ndim > 1 else raw.reshape(-1, 1))
                        outdata[:] = np.repeat(mono, outdata.shape[1], axis=1) * vol
                except Exception:
                    outdata.fill(0)

            try:
                _out_ch = max(1, int(sd.query_devices(kind='output')['max_output_channels']))
                _out_ch = min(_out_ch, cable_ch)
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
                print("[StreamAudio] ✔ [VB-CABLE] Захват запущен — чистый звук без AEC и ducking")
                try:
                    with sd.OutputStream(
                        samplerate=cable_sr,
                        channels=_out_ch,
                        dtype='float32',
                        blocksize=CHUNK_SIZE,
                        callback=_monitor_out_cb,
                    ):
                        print(f"[StreamAudio] ✔ [VB-CABLE] Локальный мониторинг запущен "
                              f"(ch={_out_ch} sr={cable_sr})")
                        while self._running.is_set():
                            time.sleep(0.05)
                except Exception as e_mon:
                    print(f"[StreamAudio] [VB-CABLE] Мониторинг недоступен: {e_mon}\n"
                          f"  Захват зрителям продолжается без локального мониторинга.")
                    while self._running.is_set():
                        time.sleep(0.05)
            return True

        except Exception as e:
            print(f"[StreamAudio] [VB-CABLE] Ошибка открытия потока: {e}")
            return False
        finally:
            self._using_vbcable = False
            self._vbcable_monitor_queue = None

    def _capture_loop(self, device_idx: Optional[int]):
        """Основной цикл захвата — перебирает стратегии по приоритету.

            :param device_idx: предпочтительный индекс WASAPI output-устройства
        """
        if self._try_vbcable():
            print("[StreamAudio] Захват остановлен [0/VB-CABLE]")
            return

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

        if self._try_pyaudiowpatch(output_name):
            print("[StreamAudio] Loopback поток остановлен [A/pyaudiowpatch]")
            return

        if self._try_sounddevice_loopback(resolved, native_ch):
            print("[StreamAudio] Loopback поток остановлен [B/sounddevice]")
            return

        print(
            "[StreamAudio] ✖ WASAPI Loopback захватить не удалось.\n"
            "  Решение: pip install pyaudiowpatch\n"
            "  Подробнее: https://github.com/s0d3s/PyAudioWPatch"
        )

    def _audio_cb(self, indata: np.ndarray, frames, time_info, status):
        """PortAudio callback — конвертирует фреймы в Opus и кладёт в send_queue.

            :param indata: входные PCM-данные (frames, channels) float32
        """
        if not self._running.is_set():
            return

        uid = self.get_uid()
        if uid == 0:
            return

        # Локальный мониторинг VB-CABLE: сырой фрейм → наушники стримера
        if self._using_vbcable and self._vbcable_monitor_queue is not None:
            try:
                self._vbcable_monitor_queue.put_nowait(indata.copy())
            except Exception:
                pass

        try:
            mono = np.mean(indata, axis=1) if (indata.ndim > 1 and indata.shape[1] > 1) \
                else indata.flatten()

            if self._native_sr != SAMPLE_RATE:
                target_len = int(round(len(mono) * SAMPLE_RATE / self._native_sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(mono), dtype=np.float64)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float64)
                    mono = np.interp(x_new, x_old, mono).astype(np.float32)

            with self._buffer_lock:
                incoming = len(mono)
                needed = self._pcm_len + incoming
                if needed > len(self._pcm_buf):
                    new_size = max(needed, len(self._pcm_buf) * 2)
                    new_buf = np.empty(new_size, dtype=np.float32)
                    new_buf[:self._pcm_len] = self._pcm_buf[:self._pcm_len]
                    self._pcm_buf = new_buf
                self._pcm_buf[self._pcm_len:self._pcm_len + incoming] = mono
                self._pcm_len += incoming

                while self._pcm_len >= CHUNK_SIZE:
                    chunk = self._pcm_buf[:CHUNK_SIZE].copy()
                    self._pcm_len -= CHUNK_SIZE
                    self._pcm_buf[:self._pcm_len] = self._pcm_buf[CHUNK_SIZE:CHUNK_SIZE + self._pcm_len]

                    pcm = (chunk * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm, CHUNK_SIZE)
                    self._sequence += 1
                    flags = FLAG_STREAM_AUDIO | FLAG_LOOPBACK_AUDIO
                    packet = UDP_HEADER_STRUCT.pack(uid, time.time(), self._sequence, flags) + encoded
                    self.send_queue.put_nowait(packet)

        except Exception:
            pass


class JitterBuffer:
    """Буфер с сортировкой по sequence number для компенсации джиттера сети."""

    def __init__(self, target_delay: int = 4):
        """
            :param target_delay: минимальное накопленное число пакетов перед воспроизведением
        """
        self.buffer = []
        self.target_delay = target_delay
        self.last_seq = -1
        self._lock = threading.Lock()
        self.is_buffering = True
        self.max_size = 50

    def add(self, seq: int, data: bytes):
        """Добавляет пакет в буфер; дубли и устаревшие пакеты отбрасываются.

            :param seq: порядковый номер пакета
            :param data: Opus-данные
        """
        with self._lock:
            if seq <= self.last_seq and self.last_seq != -1:
                return
            heapq.heappush(self.buffer, (seq, data))
            if len(self.buffer) > self.max_size:
                heapq.heappop(self.buffer)

    def get(self) -> Optional[bytes]:
        """Извлекает следующий пакет с наименьшим seq.

            :return: Opus-данные или None если буфер пуст/накапливается
        """
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
    """Состояние удалённого участника: буфер, декодер, громкость."""

    def __init__(self, uid: int):
        """
            :param uid: уникальный идентификатор пользователя
        """
        self.uid = uid
        self.jitter_buffer = JitterBuffer()
        self.decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
        self.last_packet_time = 0
        self.volume = 1.0
        self.volume_zero = False   # True когда vol==0.0; CPU-оптимизация: пропускаем decode
        self.is_locally_muted = False
        self.remote_muted = False
        self.remote_deafened = False


class AudioHandler(QObject):
    """Основной аудио-движок: захват микрофона, кодирование, декодирование и микширование."""

    volume_level_signal = pyqtSignal(int)
    status_changed = pyqtSignal(bool, bool)
    whisper_received = pyqtSignal(int)
    whisper_ended = pyqtSignal()
    user_volume_zero = pyqtSignal(int, bool)

    def _apply_anonymous_voice_effect(self, s: np.ndarray, state: dict) -> np.ndarray:
        """Эффект «анонимного голоса» — pitch-shift -4 полутона + LP 4 кГц.

        Использует Vectorized Dual-Tap Delay Line без аллокаций в hot path.
        state — per-uid словарь {'history', 'phase', 'lp_zi', 'buf'}.

            :param s: входной PCM float32 (CHUNK_SIZE)
            :param state: изменяемое состояние pitch-shifter для данного uid
            :return: обработанный PCM float32
        """
        N = len(s)
        max_delay = 1440          # 30 мс при 48 кГц
        speed = 2.0 ** (-4.0 / 12.0)
        rate = 1.0 - speed

        H = 2048
        history = state['history']
        buf = state['buf']
        buf[:H] = history
        buf[H:H + N] = s
        buf_view = buf[:H + N]

        phases = state['phase'] + np.arange(N) * rate / max_delay
        state['phase'] = float((phases[-1] + rate / max_delay) % 1.0)

        p1 = phases % 1.0
        p2 = (phases + 0.5) % 1.0
        d1 = p1 * max_delay
        d2 = p2 * max_delay

        base_idx = H + np.arange(N)
        r1 = base_idx - d1
        r2 = base_idx - d2

        i1_floor = np.floor(r1).astype(np.int32)
        i2_floor = np.floor(r2).astype(np.int32)

        buf_last = H + N - 1
        i1_ceil = np.clip(i1_floor + 1, 0, buf_last)
        i2_ceil = np.clip(i2_floor + 1, 0, buf_last)

        frac_1 = r1 - i1_floor
        frac_2 = r2 - i2_floor

        val_1 = buf_view[i1_floor] * (1.0 - frac_1) + buf_view[i1_ceil] * frac_1
        val_2 = buf_view[i2_floor] * (1.0 - frac_2) + buf_view[i2_ceil] * frac_2

        # Кроссфейд окном Ханна для устранения щелчков
        fade_1 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p1)
        fade_2 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p2)
        shifted = val_1 * fade_1 + val_2 * fade_2

        history[:] = buf_view[N:]

        out, state['lp_zi'] = sosfilt(self._anon_lp_sos, shifted, zi=state['lp_zi'])

        peak = np.max(np.abs(out))
        if peak > 0.9:
            out *= 0.9 / peak

        return out.astype(np.float32)

    def __init__(self):
        super().__init__()

        self.settings = QSettings("MyVoiceChat", "UserVolumes")
        self.global_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.uid_to_ip: dict = {}
        self.pending_volumes: dict = {}
        self.remote_users: dict = {}
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

        self.stream_remote_users: dict = {}
        self.stream_users_lock = threading.Lock()
        self.stream_volume: float = 1.0
        self._stream_audio_sending = threading.Event()

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

        # Шёпот (приватная передача одному пользователю)
        self.whisper_target_uid: int = 0
        self._whisper_sequence: int = 0

        # IIR-фильтр для whisper-эффекта (Butterworth LP fc=1200 Гц)
        self._wlp_sos = _WLP_SOS
        self._wlp_zi = sosfilt_zi(self._wlp_sos).astype(np.float64)

        # LP-фильтр для эффекта анонимного голоса (fc=4000 Гц)
        self._anon_lp_sos = _ANON_LP_SOS

        # Per-uid состояния pitch-shifter для входящих шёпотов
        self._whisper_states: dict = {}
        self._active_whispers: dict = {}   # uid → timestamp последнего пакета

        self._whisper_in_uid: int = 0
        self._whisper_in_ts: float = 0.0
        self._whisper_effect_reset: bool = False

        # Отдельный seq-счётчик для FLAG_STREAM_VOICES пакетов
        self._sv_sequence = 0

        # deque(maxlen=5): O(1) popleft вместо O(n) list.pop(0) в audio_callback
        self.vad_pre_buffer = deque(maxlen=5)
        self.was_talking = False
        self.stream = None
        self._lb_play_counter = 0

        self._pkt_thread: Optional[threading.Thread] = None
        self._stream_pkt_thread: Optional[threading.Thread] = None

        # Copy-on-Write снимки для audio_callback — читаются без лока
        self._audio_users_snapshot: dict = {}
        self._audio_stream_users_snapshot: dict = {}

    def set_bitrate(self, bitrate_kbps: int):
        """Устанавливает битрейт Opus-энкодера.

            :param bitrate_kbps: битрейт в кбит/с
        """
        bitrate_bps = int(bitrate_kbps) * 1000
        try:
            with self.users_lock:
                self.encoder.bitrate = bitrate_bps
                self.global_settings.setValue("audio_bitrate", bitrate_bps)
                print(f"[Audio] Bitrate changed to {bitrate_kbps} kbps")
        except Exception as e:
            print(f"[Audio] Error setting bitrate: {e}")

    def set_vad_threshold(self, slider_val: int):
        """Устанавливает порог Voice Activity Detection.

            :param slider_val: значение ползунка (1–50), делится на 1000 → порог RMS
        """
        threshold = max(1, min(50, slider_val)) / 1000.0
        self.vad_threshold = threshold
        self.global_settings.setValue("vad_threshold_slider", slider_val)
        print(f"[Audio] VAD threshold set to {threshold:.4f} (slider={slider_val})")

    def find_device_index_by_name(self, name: Optional[str], is_input: bool = True) -> Optional[int]:
        """Ищет устройство sounddevice по полному имени «Name (API)».

            :param name: полное имя устройства; None → вернёт None (системный дефолт)
            :param is_input: True — искать input-устройство, False — output
            :return: индекс устройства или None
        """
        if not name:
            return None
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            try:
                api_name = sd.query_hostapis(d['hostapi'])['name']
                full_name = f"{d['name']} ({api_name})"
                if full_name.strip() != name.strip():
                    continue
                if is_input and d['max_input_channels'] > 0:
                    return i
                if not is_input and d['max_output_channels'] > 0:
                    return i
            except Exception:
                continue
        return None

    def start(self, input_name: Optional[str] = None, output_name: Optional[str] = None):
        """Запускает аудио-поток и рабочие потоки обработки пакетов.

            :param input_name: полное имя input-устройства; None — системный дефолт
            :param output_name: полное имя output-устройства; None — системный дефолт
        """
        if self.my_uid == 0:
            return
        self.stop()
        time.sleep(0.1)

        in_idx = self.find_device_index_by_name(input_name, True)
        out_idx = self.find_device_index_by_name(output_name, False)
        print(f"[Audio] start: in_idx={in_idx}, out_idx={out_idx}")

        self._is_running.set()
        try:
            self.stream = sd.Stream(
                device=(in_idx, out_idx),
                samplerate=SAMPLE_RATE,
                blocksize=CHUNK_SIZE,
                dtype='float32',
                channels=CHANNELS,
                callback=self.audio_callback,
            )
            self.stream.start()
            self._pkt_thread = threading.Thread(
                target=self._packet_processor_loop, daemon=True)
            self._stream_pkt_thread = threading.Thread(
                target=self._stream_packet_processor_loop, daemon=True)
            self._pkt_thread.start()
            self._stream_pkt_thread.start()
            print("[Audio] start: аудио-поток и рабочие потоки запущены")
        except Exception:
            import traceback
            print(f"[Audio] start: EXCEPTION:\n{traceback.format_exc()}")
            self._is_running.clear()

    def stop(self):
        """Останавливает аудио-поток и рабочие потоки."""
        self._is_running.clear()
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
            except Exception:
                pass

    def cleanup_users(self, active_uids: set):
        """Удаляет из памяти отключившихся пользователей.

            :param active_uids: множество uid активных участников
        """
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
            self._audio_users_snapshot = dict(self.remote_users)

        with self.stream_users_lock:
            for uid in list(self.stream_remote_users.keys()):
                real_uid = uid - LOOPBACK_UID_OFFSET if uid >= LOOPBACK_UID_OFFSET else uid
                if real_uid not in active_uids:
                    del self.stream_remote_users[uid]
            self._audio_stream_users_snapshot = dict(self.stream_remote_users)

    def _packet_processor_loop(self):
        """Фоновый поток: разбирает incoming_packets и раскладывает по JitterBuffer."""
        while self._is_running.is_set():
            try:
                uid, seq, data, flags = self.incoming_packets.get(timeout=0.1)
                if uid == self.my_uid:
                    continue

                with self.users_lock:
                    if uid not in self.remote_users:
                        self.remote_users[uid] = RemoteUser(uid)
                        val = float(self.pending_volumes.pop(uid, 1.0))
                        self.remote_users[uid].volume = val
                        self.remote_users[uid].volume_zero = (val == 0.0)

                        self._audio_users_snapshot = dict(self.remote_users)

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
        """Фоновый поток: разбирает incoming_stream_packets (FLAG_STREAM_AUDIO).

        Различает два типа по флагу FLAG_LOOPBACK_AUDIO:
          - Loopback (системный звук): хранится под uid + LOOPBACK_UID_OFFSET
          - Микрофон стримера: подавляется если uid уже активен в remote_users < 1.5 с
        """
        while self._is_running.is_set():
            try:
                uid, seq, data, flags = self.incoming_stream_packets.get(timeout=0.1)
                is_loopback = bool(flags & FLAG_LOOPBACK_AUDIO)

                if is_loopback:
                    if uid == self.my_uid:
                        continue
                    storage_uid = uid + LOOPBACK_UID_OFFSET
                    with self.stream_users_lock:
                        if storage_uid not in self.stream_remote_users:
                            self.stream_remote_users[storage_uid] = RemoteUser(storage_uid)
                            print(f"[AudioHandler] Создан буфер системного звука (storage_uid={storage_uid})")
                        user = self.stream_remote_users[storage_uid]
                        if data:
                            user.jitter_buffer.add(seq, data)
                            user.last_packet_time = time.time()
                        self._audio_stream_users_snapshot = dict(self.stream_remote_users)
                else:
                    if uid == self.my_uid:
                        continue

                    with self.users_lock:
                        reg_user = self.remote_users.get(uid)
                        recently_received = (
                            reg_user is not None
                            and (time.time() - reg_user.last_packet_time) < 1.5
                        )
                    if recently_received:
                        continue

                    with self.stream_users_lock:
                        if uid not in self.stream_remote_users:
                            self.stream_remote_users[uid] = RemoteUser(uid)
                        user = self.stream_remote_users[uid]
                        if data:
                            user.jitter_buffer.add(seq, data)
                            user.last_packet_time = time.time()
                        self._audio_stream_users_snapshot = dict(self.stream_remote_users)

            except queue.Empty:
                continue
            except Exception:
                pass

    def audio_callback(self, indata: np.ndarray, outdata: np.ndarray,
                       frames: int, time_info, status):
        """PortAudio realtime callback: захват микрофона + микширование входящих голосов.

            :param indata: входные PCM-данные (frames, channels) float32
            :param outdata: выходные PCM-данные (frames, channels) float32
        """
        if status:
            print(f"[Audio] callback status: {status}")

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
                    if len(processed) == 1:
                        denoised_float = processed[0].astype(np.float32) / 32767.0
                    else:
                        denoised_float = np.concatenate(processed).astype(np.float32) / 32767.0
                    if len(denoised_float) != len(raw_input):
                        denoised_float = np.resize(denoised_float, len(raw_input))
            except Exception:
                pass

        # Soft-limit: защита от INT16 wraparound при пиках > 1.0
        _in_peak = np.max(np.abs(denoised_float))
        if _in_peak > 0.98:
            denoised_float = denoised_float * (0.98 / _in_peak)

        rms = np.sqrt(np.mean(denoised_float ** 2))

        # ОПТИМИЗАЦИЯ: Отправляем сигнал громкости не чаще чем раз в 50 мс (20 FPS)
        if curr_time - getattr(self, '_last_vol_emit_time', 0) > 0.05:
            self.volume_level_signal.emit(int(min(rms * 1000, 100)))
            self._last_vol_emit_time = curr_time

        is_talking = rms > self.vad_threshold or (curr_time - self.last_voice_time < self.vad_hangover)

        is_talking = rms > self.vad_threshold or (curr_time - self.last_voice_time < self.vad_hangover)
        if rms > self.vad_threshold:
            self.last_voice_time = curr_time

        if self.my_uid != 0:
            mute_flag = 1 if self._is_muted.is_set() else 0
            deaf_flag = 2 if self._is_deafened.is_set() else 0
            flags = mute_flag | deaf_flag
            whisper_uid = self.whisper_target_uid

            try:
                if is_talking and whisper_uid != 0:
                    # Режим шёпота: пакет только whisper_uid, в комнату ничего
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    self._whisper_sequence += 1
                    w_header = UDP_HEADER_STRUCT.pack(
                        self.my_uid, curr_time, self._whisper_sequence, FLAG_WHISPER)
                    w_payload = struct.pack('!I', whisper_uid) + encoded
                    try:
                        self.send_queue.put_nowait(w_header + w_payload)
                    except Exception:
                        pass

                elif is_talking and not self._is_muted.is_set():
                    # Обычный режим: пакет в комнату
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    self.my_sequence += 1
                    packet = UDP_HEADER_STRUCT.pack(
                        self.my_uid, curr_time, self.my_sequence, flags) + encoded

                    if not self.was_talking:
                        while self.vad_pre_buffer:
                            try:
                                self.send_queue.put_nowait(self.vad_pre_buffer.popleft())
                            except Exception:
                                pass
                        self.was_talking = True
                    self.send_queue.put_nowait(packet)

                    if self._stream_audio_sending.is_set():
                        stream_packet = UDP_HEADER_STRUCT.pack(
                            self.my_uid, curr_time,
                            self.my_sequence, flags | FLAG_STREAM_AUDIO) + encoded
                        try:
                            self.send_queue.put_nowait(stream_packet)
                        except Exception:
                            pass

                else:
                    self.was_talking = False
                    if not is_talking and whisper_uid == 0:
                        empty_packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, 0, flags)
                        self.vad_pre_buffer.append(empty_packet)
            except Exception:
                pass

        self.mix_buffer.fill(0)
        if not self._is_deafened.is_set():
            # N-speaker headroom: sqrt(2)/sqrt(N) — RMS остаётся постоянным при N говорящих
            _n_active = sum(
                1 for u in self._audio_users_snapshot.values()
                if (curr_time - u.last_packet_time < 1.5
                    and not u.is_locally_muted
                    and not u.volume_zero)
            )
            _n_active += sum(
                1 for u in self._audio_stream_users_snapshot.values()
                if curr_time - u.last_packet_time < 1.5
            )
            _speaker_gain = math.sqrt(2.0) / math.sqrt(max(2, _n_active))

            for uid, user in self._audio_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if data and not user.is_locally_muted and not user.volume_zero:
                        try:
                            decoded = user.decoder.decode(data, CHUNK_SIZE)
                            s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0

                            # Whisper effect: per-uid pitch-shift + LP для входящих шёпотов
                            _w_ts = self._active_whispers.get(uid, 0.0)
                            if _w_ts and (curr_time - _w_ts) < 2.0:
                                if uid not in self._whisper_states:
                                    self._whisper_states[uid] = {
                                        'history': np.resize(s.astype(np.float32), 2048).copy(),
                                        'phase':   0.0,
                                        'lp_zi':   np.zeros(
                                            (self._anon_lp_sos.shape[0], 2), dtype=np.float64),
                                        'buf':     np.zeros(2048 + CHUNK_SIZE, dtype=np.float32),
                                    }
                                s = self._apply_anonymous_voice_effect(
                                    s, self._whisper_states[uid])
                            else:
                                self._active_whispers.pop(uid, None)
                                self._whisper_states.pop(uid, None)

                            self.mix_buffer += s * (user.volume * _speaker_gain)

                            # Mix Minus для зрителей: ретрансляция голоса с speaker_uid
                            if self._stream_audio_sending.is_set():
                                try:
                                    self._sv_sequence += 1
                                    sv_header = UDP_HEADER_STRUCT.pack(
                                        self.my_uid, curr_time, self._sv_sequence,
                                        FLAG_STREAM_AUDIO | FLAG_STREAM_VOICES)
                                    sv_payload = struct.pack('!I', uid) + data
                                    self.send_queue.put_nowait(sv_header + sv_payload)
                                except Exception:
                                    pass
                        except Exception:
                            pass

            # Стрим-аудио: игровой звук от стримера (зрительская сторона)
            sv = self.stream_volume
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

        # Soft limiter: нормализация пика вместо жёсткого clip (устраняет waveshaping дисторшн)
        _peak = np.max(np.abs(self.mix_buffer))
        if _peak > 0.95:
            self.mix_buffer *= (0.95 / _peak)
        np.clip(self.mix_buffer, -1.0, 1.0, out=self.mix_buffer)

        outdata[:] = self.mix_buffer.reshape(-1, 1)

    def register_ip_mapping(self, uid: int, ip_addr: str):
        """Связывает uid с IP-адресом и восстанавливает сохранённую громкость.

            :param uid: идентификатор пользователя
            :param ip_addr: IP-адрес пользователя
        """
        if not ip_addr:
            return
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

    def set_user_volume(self, uid: int, vol: float):
        """Устанавливает громкость конкретного пользователя.

            :param uid: идентификатор пользователя
            :param vol: громкость в диапазоне [0.0, 2.0]
        """
        vol = max(0.0, min(2.0, float(vol)))
        emit_zero_state = None

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

        if emit_zero_state is not None:
            self.user_volume_zero.emit(uid, emit_zero_state)

    def toggle_user_mute(self, uid: int) -> bool:
        """Переключает локальное заглушение пользователя.

            :param uid: идентификатор пользователя
            :return: новое состояние mute (True — заглушён)
        """
        with self.users_lock:
            if uid in self.remote_users:
                self.remote_users[uid].is_locally_muted = not self.remote_users[uid].is_locally_muted
                return self.remote_users[uid].is_locally_muted
        return False

    def start_whisper(self, target_uid: int):
        """Начинает шёпот к конкретному пользователю.

        Голос кодируется с FLAG_WHISPER и отправляется только target_uid.
        Нормальные пакеты в комнату не отправляются — остальные не слышат.
        _whisper_sequence инициализируется от my_sequence во избежание разрыва seq.

            :param target_uid: uid получателя шёпота
        """
        self.whisper_target_uid = target_uid
        self._whisper_sequence = self.my_sequence
        self._wlp_zi = sosfilt_zi(self._wlp_sos).astype(np.float64)
        self._whisper_effect_reset = True
        print(f"[Audio] Whisper START → uid={target_uid}, seq_from={self._whisper_sequence}")

    def stop_whisper(self):
        """Останавливает шёпот и синхронизирует my_sequence для непрерывной нумерации."""
        if self._whisper_sequence > self.my_sequence:
            self.my_sequence = self._whisper_sequence
        print(f"[Audio] Whisper STOP (was → uid={self.whisper_target_uid}), "
              f"seq_sync={self.my_sequence}")
        self.whisper_target_uid = 0

    def add_incoming_packet(self, uid: int, seq: int, data: bytes, flags: int = 0):
        """Добавляет входящий аудио-пакет в очередь обработки.

            :param uid: идентификатор отправителя
            :param seq: порядковый номер пакета
            :param data: Opus-данные
            :param flags: битовые флаги пакета
        """
        try:
            self.incoming_packets.put_nowait((uid, seq, data, flags))
        except Exception:
            pass

    def add_incoming_whisper_packet(self, uid: int, seq: int, data: bytes):
        """Обрабатывает входящий шёпот (FLAG_WHISPER).

        Испускает whisper_received(uid) на каждый пакет — UI-таймер завершения
        шёпота перезапускается и не гаснет пока идут пакеты.

            :param uid: идентификатор шептуна
            :param seq: порядковый номер пакета
            :param data: Opus-данные
        """
        now = time.time()
        self._active_whispers[uid] = now
        self._whisper_in_uid = uid
        self._whisper_in_ts = now
        self.whisper_received.emit(uid)
        self.add_incoming_packet(uid, seq, data, 0)

    def add_incoming_stream_packet(self, uid: int, seq: int, data: bytes, flags: int = 0):
        """Добавляет входящий пакет стрим-аудио (FLAG_STREAM_AUDIO) в очередь.

            :param uid: идентификатор отправителя
            :param seq: порядковый номер пакета
            :param data: Opus-данные
            :param flags: битовые флаги пакета
        """
        try:
            self.incoming_stream_packets.put_nowait((uid, seq, data, flags))
        except Exception:
            pass

    def set_stream_audio_enabled(self, enabled: bool):
        """Включает/выключает отправку микрофона и системного звука зрителям.

            :param enabled: True — включить трансляцию, False — выключить
        """
        if enabled:
            self._stream_audio_sending.set()
        else:
            self._stream_audio_sending.clear()
        self.start_stream_audio() if enabled else self.stop_stream_audio()
        print(f"[Audio] Stream mic & loopback sending: {'ON' if enabled else 'OFF'}")

    def set_stream_volume(self, volume: float):
        """Устанавливает громкость стрима для зрителя.

            :param volume: громкость в диапазоне [0.0, 2.0]
        """
        self.stream_volume = max(0.0, min(2.0, volume))
        print(f"[Audio] Stream volume set to {self.stream_volume:.2f}")

    def start_stream_audio(self, device_idx: Optional[int] = None):
        """Запускает захват системного аудио (WASAPI Loopback) для трансляции.

            :param device_idx: индекс WASAPI output-устройства; None — автовыбор
        """
        self.stream_audio_capture.start(device_idx)

    def stop_stream_audio(self):
        """Останавливает захват системного аудио."""
        self.stream_audio_capture.stop()

    @property
    def is_muted(self) -> bool:
        return self._is_muted.is_set()

    @is_muted.setter
    def is_muted(self, value: bool):
        if value:
            self._is_muted.set()
        else:
            self._is_muted.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)

    @property
    def is_deafened(self) -> bool:
        return self._is_deafened.is_set()

    @is_deafened.setter
    def is_deafened(self, value: bool):
        if value:
            self._is_deafened.set()
        else:
            self._is_deafened.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)