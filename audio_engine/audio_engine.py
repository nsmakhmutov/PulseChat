import asyncio
import threading
import queue
import math
from collections import deque
from typing import Callable
import numpy as np
import sounddevice as sd
import opuslib
import heapq
import struct
import time
import ctypes
import os
import av
from aiortc import AudioStreamTrack
from PyQt6.QtCore import QObject, pyqtSignal, QSettings
from config import (
    SAMPLE_RATE, CHANNELS, CHUNK_SIZE,
    OPUS_APPLICATION, DEFAULT_BITRATE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    FLAG_STREAM_VOICES, FLAG_WHISPER,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
)
from .audio_processing import (
    DeepFilterEngine,
    PYRNNOISE_AVAILABLE, PYAUDIOWPATCH_AVAILABLE, _pyaudio,
    butter, sosfilt, sosfilt_zi, _WLP_SOS, _ANON_LP_SOS,
)
from .audio_capture import StreamAudioCapture, MicrophoneTrack, SystemAudioTrack
try:
    from pyrnnoise import RNNoise
except ImportError:
    RNNoise = None


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
                # FIX: дропаем НАИБОЛЬШИЙ seq (самый новый), а не наименьший.
                # heappop() на min-heap дропает наименьший — то есть именно тот
                # пакет, который должен играть следующим. Это гарантированный треск.
                # Правильно: убираем самый новый пакет — он пришёл из сети раньше
                # времени и буфер слишком большой. Используем nlargest+remove.
                # O(n) — но вызывается крайне редко (только при шторме пакетов).
                largest = max(self.buffer, key=lambda x: x[0])
                self.buffer.remove(largest)
                heapq.heapify(self.buffer)

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

    def _apply_anonymous_voice_effect(self, s: np.ndarray, state: dict) -> np.ndarray:
        """
        Эффект «анонимного голоса» (Dark TV Interview).
        Использует Vectorized Dual-Tap Delay Line для pitch-shift без артефактов.

        state — per-uid словарь {'history', 'phase', 'lp_zi', 'buf', '_arange', '_base'}.
        Разделение состояний по uid позволяет одновременно обрабатывать
        нескольких шептунов без взаимного наложения и phase-артефактов.
        """
        N = len(s)
        max_delay = 1440  # 30 мс при 48kHz — оптимальный размер окна для голоса
        speed = 2.0 ** (-4.0 / 12.0)  # -4 полутона
        rate = 1.0 - speed  # Скорость накопления задержки

        # 1. Склеиваем историю и текущий фрейм в предаллоцированный буфер.
        H = 2048
        history = state['history']
        buf     = state['buf']
        buf[:H]      = history
        buf[H:H + N] = s
        buf_view = buf[:H + N]

        # 2. Генерируем фазы. FIX 7: используем pre-allocated _arange вместо np.arange(N)
        # np.arange(N) создавал новый массив каждые 20 мс — теперь берём из state.
        phases = state['phase'] + state['_arange'] * rate / max_delay
        state['phase'] = float((phases[-1] + rate / max_delay) % 1.0)

        p1 = phases % 1.0
        p2 = (phases + 0.5) % 1.0

        # Задержка в сэмплах
        d1 = p1 * max_delay
        d2 = p2 * max_delay

        # 3. Индексы чтения. FIX 7: используем pre-allocated _base вместо H + np.arange(N)
        base_idx = state['_base']
        r1 = base_idx - d1
        r2 = base_idx - d2

        # 4. Линейная интерполяция для плавности
        i1_floor = np.floor(r1).astype(np.int32)
        i2_floor = np.floor(r2).astype(np.int32)

        buf_last = H + N - 1
        i1_ceil = np.clip(i1_floor + 1, 0, buf_last)
        i2_ceil = np.clip(i2_floor + 1, 0, buf_last)

        frac_1 = r1 - i1_floor
        frac_2 = r2 - i2_floor

        val_1 = buf_view[i1_floor] * (1.0 - frac_1) + buf_view[i1_ceil] * frac_1
        val_2 = buf_view[i2_floor] * (1.0 - frac_2) + buf_view[i2_ceil] * frac_2

        # 5. Кроссфейд (окно Ханна)
        fade_1 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p1)
        fade_2 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p2)

        shifted = val_1 * fade_1 + val_2 * fade_2

        # 6. Обновляем историю in-place
        history[:] = buf_view[N:]

        # 7. LP-фильтр (4 кГц)
        out, state['lp_zi'] = sosfilt(self._anon_lp_sos, shifted, zi=state['lp_zi'])

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

        # --- Секция шумоподавления ---
        self.denoiser = None      # Для RNNoise
        self.dfn_engine = None    # Для DeepFilterNet
        self.dfn_available = False  # Инициализируем до try — на случай непредвиденного исключения

        # nr_mode: 0=выкл, 1=RNNoise, 2=DeepFilterNet
        # По умолчанию 1 (RNNoise) для новых установок — хорошее качество
        # без тяжёлых зависимостей. Если pyrnnoise недоступен — откат на 0
        # без записи в QSettings (при следующей установке библиотеки включится сам).
        self.nr_mode = int(self.global_settings.value("audio/nr_mode", 1))

        # 1. Сначала пробуем инициализировать DeepFilterNet
        try:
            self.dfn_engine = DeepFilterEngine()
            self.dfn_available = True
            print("[Audio] DeepFilterNet3 инициализирован успешно.")
        except Exception as e:
            self.dfn_available = False
            print(f"[Audio] DFN не загружен: {e}")

        # 2. Параллельно держим RNNoise как запасной вариант (fallback)
        if PYRNNOISE_AVAILABLE:
            try:
                self.denoiser = RNNoise(sample_rate=SAMPLE_RATE)
                print("[Audio] RNNoise инициализирован.")
            except Exception as e:
                print(f"[Audio] Ошибка RNNoise: {e}")

        # Защита: дефолт nr_mode=1, но библиотека недоступна → откат на 0
        if self.nr_mode == 1 and not PYRNNOISE_AVAILABLE:
            print("[Audio] RNNoise недоступен, шумоподавление отключено.")
            self.nr_mode = 0
        elif self.nr_mode == 2 and not self.dfn_available:
            print("[Audio] DeepFilterNet недоступен, откат на RNNoise.")
            self.nr_mode = 1 if PYRNNOISE_AVAILABLE else 0

        self.incoming_packets = queue.Queue(maxsize=500)
        self.send_queue = queue.Queue(maxsize=100)

        # ── Внутренний микшер UI-звуков ──────────────────────────────────────
        # Системные уведомления, Soundboard и Nudge подаются сюда вместо sd.play().
        # audio_callback читает список и подмешивает фреймы в mix_buffer прямо в
        # уже открытый PortAudio-поток — никаких новых WASAPI-устройств не открывается.
        self._local_sounds: list = []
        self._local_sounds_lock = threading.Lock()

        # --- Стрим-аудио (WebRTC) ---
        # Воспроизведение стрим-аудио на стороне зрителя теперь обрабатывается
        # через WebRTC (RTCPeerConnection + AudioStreamTrack).
        # Следующие атрибуты удалены: incoming_stream_packets, stream_remote_users,
        # stream_users_lock, _audio_stream_users_snapshot, _stream_pkt_thread,
        # _sv_sequence, _lb_play_counter, _stream_audio_sending.
        # Громкость стрима для зрителя (0.0–2.0) — управляется через оверлей VideoWindow.
        self._is_running = threading.Event()
        self._is_muted = threading.Event()
        self._is_deafened = threading.Event()

        self.mix_buffer = np.zeros(CHUNK_SIZE, dtype=np.float32)

        # ── Pre-allocated encode buffer (hot path, 50 Hz) ──────────────────
        # (denoised_float * 32767).astype(np.int16) создаёт новый массив каждые
        # 20 мс → GC давление ~94 KB/сек. Используем фиксированный буфер
        # и умножаем in-place через np.multiply (нет аллокации).
        self._pcm_int16_buf = np.zeros(CHUNK_SIZE, dtype=np.int16)

        # Флаг первого вызова audio_callback: хранится как атрибут (bool),
        # а не проверяется через getattr() каждые 20 мс (getattr = dict lookup
        # + hasattr fallback = лишние 2-3 мкс × 50/сек = 100-150 мкс/сек впустую)
        self._cb_first_logged: bool = False

        # FIX 9: throttle volume_level_signal с 50 Гц до 10 Гц.
        # Qt cross-thread signal каждые 20 мс — лишняя нагрузка на event loop UI.
        # VU-метр обновляется с тем же визуальным качеством при 10 Гц.
        # Каждый 5-й фрейм (50/5=10 Гц) эмитим сигнал, остальные пропускаем.
        self._vol_emit_counter: int = 0
        self.saved_vad_slider = int(self.global_settings.value("vad_threshold_slider", 5))
        self.vad_threshold = self.saved_vad_slider / 1000.0
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
        # SOS-матрица предвычислена как модульная константа _WLP_SOS (один раз при импорте).
        # Начальные условия zi хранят состояние между фреймами.
        # Сброс zi производится в start_whisper().
        self._wlp_sos = _WLP_SOS
        self._wlp_zi  = sosfilt_zi(self._wlp_sos).astype(np.float64)

        # ── LP-фильтр для эффекта анонимного голоса (fc=4000 Гц) ──────────────
        # Мягче чем whisper LP (1200 Гц): сохраняет согласные и зону присутствия.
        # SOS-матрица предвычислена как модульная константа _ANON_LP_SOS.
        self._anon_lp_sos = _ANON_LP_SOS

        # ── Whisper effect: per-uid состояния ───────────────────────────────────
        # Ключ: uid шептуна. Значение: dict с полями:
        #   'history' — np.ndarray(2048, float32): буфер предыстории pitch-shifter
        #   'phase'   — float: текущая фаза читающей головки
        #   'lp_zi'   — np.ndarray(n_sec, 2, float64): состояние LP-фильтра
        #   'buf'     — np.ndarray(2048+CHUNK_SIZE, float32): предаллоц. рабочий буфер
        #
        # Создаётся лениво при первом пакете шептуна с «тёплым» стартом:
        # history заполняется реальным сигналом (не нулями) → питч-шифтер сразу
        # читает данные из обеих головок без перехода ноль→сигнал → нет click/треск.
        #
        # Поддержка 2+ одновременных шептунов: каждый uid имеет независимое
        # состояние, смешиваются через общий mix_buffer без взаимных артефактов.
        self._whisper_states: dict = {}    # uid → state dict (см. выше)
        self._active_whispers: dict = {}   # uid → float (последний timestamp пакета)

        # Backward compat для UI-сигнала: последний шептун и его время
        self._whisper_in_uid: int = 0
        self._whisper_in_ts: float = 0.0

        # Флаг для start_whisper() — sender-side legacy (сбрасывает _wlp_zi отправителя)
        # На стороне получателя не используется (заменён ленивым созданием per-uid state).
        self._whisper_effect_reset: bool = False
        # Отдельный счётчик для FLAG_STREAM_VOICES удалён (WebRTC заменяет UDP стрим-аудио).
        # _lb_play_counter удалён (нет UDP loopback воспроизведения).
        # FIX #3: deque(maxlen=5) вместо list.
        # vad_pre_buffer.pop(0) на list — O(n): сдвигает все элементы влево.
        # deque.popleft() — O(1), что важно для audio_callback hot path.
        # maxlen=5 заменяет ручную проверку `if len > 5: pop(0)`.
        self.vad_pre_buffer = deque(maxlen=5)
        self.was_talking = False
        self.stream = None
        # Ссылки на рабочие потоки — нужны для корректного join() в stop().
        # Без явного join() повторные вызовы start() (переподключение, смена
        # устройства) накапливают «зомби»-потоки.
        self._pkt_thread: threading.Thread | None = None

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

    def play_internal_sound(self, data: np.ndarray, sr: int, vol: float = 1.0):
        """
        Воспроизводит звук через уже открытый PortAudio-поток без аллокации
        нового WASAPI-устройства. Данные подмешиваются в mix_buffer прямо внутри
        audio_callback — нулевой риск WASAPI-перебалансировки и треска.

        data: float32 numpy-массив (моно или стерео)
        sr  : частота дискретизации источника (будет ресемплирован в 48kHz)
        vol : коэффициент громкости (квадратичная кривая уже применена снаружи)
        """
        try:
            data = np.asarray(data, dtype=np.float32)
            # Стерео → моно
            if data.ndim > 1 and data.shape[1] > 1:
                data = np.mean(data, axis=1)
            elif data.ndim > 1:
                data = data[:, 0]

            # Ресемплинг только если нужен (np.interp — достаточно для кратких UI-звуков)
            if sr != SAMPLE_RATE:
                target_len = int(round(len(data) * SAMPLE_RATE / sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(data), dtype=np.float32)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float32)
                    data = np.interp(x_new, x_old, data).astype(np.float32)

            with self._local_sounds_lock:
                self._local_sounds.append({'data': data, 'pos': 0, 'vol': float(vol)})
        except Exception as e:
            print(f"[Audio] play_internal_sound error: {e}")

    def set_nr_mode(self, mode: int):
        """
        Устанавливает режим шумоподавления (мгновенно, без перезапуска аудио):
          0 = выкл
          1 = RNNoise
          2 = DeepFilterNet
        """
        self.nr_mode = int(mode)
        self.global_settings.setValue("audio/nr_mode", self.nr_mode)
        labels = {0: "выкл", 1: "RNNoise", 2: "DeepFilterNet"}
        print(f"[Audio] NR mode → {labels.get(self.nr_mode, "?")} ({self.nr_mode})")

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

        # FIX ROBOT-VOICE: сбрасываем Opus-декодеры и JitterBuffer'ы всех удалённых
        # пользователей после остановки стрима. Opus — CELP-кодек с предсказательным
        # состоянием (LPC). После паузы ≥100ms декодер ожидает следующий фрейм через
        # ровно 20ms; получив пакеты после реального перерыва без PLC-вызова он
        # выдаёт discontinuity → робовойс/артефакты на 1-2 фрейма.
        # Сброс декодера к начальному состоянию + очистка JitterBuffer устраняет это.
        with self.users_lock:
            for user in self.remote_users.values():
                user.decoder       = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
                user.jitter_buffer = JitterBuffer()
            # Обновляем COW-снимок — audio_callback не должен видеть старые декодеры
            self._audio_users_snapshot = dict(self.remote_users)

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
            self._pkt_thread.start()
            print("[DEBUG] AudioHandler.start: рабочий поток запущен — DONE", flush=True)
        except Exception as e:
            import traceback
            print(f"[DEBUG] AudioHandler.start: EXCEPTION:\n{traceback.format_exc()}", flush=True)
            self._is_running.clear()

    def stop(self):
        self._is_running.clear()
        # Дожидаемся завершения рабочего потока — иначе повторный start()
        # создаст дублирующие потоки (утечка памяти и CPU)
        t = getattr(self, '_pkt_thread', None)
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._pkt_thread = None
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

            # FIX #5: чистим состояния шёпота отключившихся пользователей.
            # Без этого ~10 KB per-uid state (history/buf/lp_zi) живут вечно.
            # audio_callback читает _whisper_states/_active_whispers без лока
            # (GIL-safe dict pop), поэтому удаляем вне users_lock не нужно —
            # делаем здесь для атомарности с удалением из remote_users.
            for uid in list(self._whisper_states.keys()):
                if uid not in active_uids:
                    del self._whisper_states[uid]
            for uid in list(self._active_whispers.keys()):
                if uid not in active_uids:
                    del self._active_whispers[uid]

            # FIX #1: обновляем COW-снимок после удаления пользователей
            self._audio_users_snapshot = dict(self.remote_users)
        # stream_remote_users удалён: стрим-аудио теперь через WebRTC (RTCPeerConnection).

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
                        user.last_packet_time = time.perf_counter()  # FIX: perf_counter точнее time.time() на Windows (15.6ms vs мкс)

                    # FIX #1: обновляем COW-снимок внутри лока — согласованное состояние.
                    # dict() копирует только ссылки (не RemoteUser объекты) — это быстро.
                    # audio_callback читает снимок без лока, опираясь на GIL-атомарность
                    # присваивания ссылки.
                    self._audio_users_snapshot = dict(self.remote_users)

            except queue.Empty:
                continue
            except Exception:
                pass

    def audio_callback(self, indata, outdata, frames, time_info, status):
        # Логируем только первый вызов — подтверждает что callback запустился
        if not self._cb_first_logged:
            self._cb_first_logged = True
            print("[DEBUG] audio_callback: ПЕРВЫЙ ВЫЗОВ — PortAudio callback работает", flush=True)
        if status:
            print(f"[DEBUG] audio_callback: status={status}", flush=True)

        if not self._is_running.is_set():
            outdata.fill(0)
            return

        curr_time = time.perf_counter()  # FIX: высокоточный таймер Windows (мкс вместо 15.6ms у time.time)
        raw_input = indata.flatten()
        denoised_float = raw_input

        # nr_mode: 0=выкл, 1=RNNoise, 2=DFN — читаем self (не QSettings каждые 20мс)
        _nr = self.nr_mode
        if _nr == 2 and self.dfn_engine:
            try:
                denoised_float = self.dfn_engine.process(denoised_float)
            except Exception:
                pass
        elif _nr == 1 and self.denoiser:
            try:
                # FIX 6: reuse _pcm_int16_buf — нет heap-аллокации каждые 20 мс.
                # Прямое приведение через view невозможно (float32 != int16 size-wise).
                # Используем np.multiply с truncation: безопасно т.к. pre-encode
                # normalization ниже уже зажимает пики в ±0.98 < 1.0.
                np.multiply(denoised_float, 32767.0,
                            out=self._pcm_int16_buf, casting='unsafe')
                processed = [f for p, f in self.denoiser.denoise_chunk(self._pcm_int16_buf)]
                if processed:
                    denoised_float = (
                        processed[0].astype(np.float32) / 32767.0
                        if len(processed) == 1
                        else np.concatenate(processed).astype(np.float32) / 32767.0
                    )
            except Exception:
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
        # FIX 9: throttle 50 Гц → 10 Гц. Эмитим каждый 5-й фрейм.
        self._vol_emit_counter += 1
        if self._vol_emit_counter >= 5:
            self._vol_emit_counter = 0
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
                    # FIX 6: _pcm_int16_buf предаллоцирован в __init__ — нет GC-давления 50/сек
                    np.multiply(denoised_float, 32767, out=self._pcm_int16_buf,
                                casting='unsafe')
                    encoded = self.encoder.encode(self._pcm_int16_buf.tobytes(), CHUNK_SIZE)
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
                    # FIX 6: reuse pre-allocated buffer
                    np.multiply(denoised_float, 32767, out=self._pcm_int16_buf,
                                casting='unsafe')
                    encoded = self.encoder.encode(self._pcm_int16_buf.tobytes(), CHUNK_SIZE)
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
                    # Стрим-аудио микрофона теперь передаётся через WebRTC (MicrophoneTrack).
                    # UDP FLAG_STREAM_AUDIO для микрофона удалён.

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
            # ── Подсчёт активных голосовых спикеров (окно 0.4с) ─────────────────
            # Было 1.5с → «фантомные» спикеры: человек сказал одно слово и
            # полторы секунды тянул gain вниз для всей комнаты. При плохом
            # микрофоне 3-го клиента (постоянный шум = VAD открыт) — он вечно
            # оставался в _n_active и давил громкость всем остальным.
            # 0.4с = комфортный хвост VAD без долгих фантомов.
            #
            # ВАЖНО: порог для воспроизведения пакетов остаётся 1.5с ниже —
            # не трогаем, чтобы не обрезать речь в конце фразы.
            _n_active = sum(
                1 for u in self._audio_users_snapshot.values()
                if (curr_time - u.last_packet_time < 0.4
                    and not u.is_locally_muted
                    and not u.volume_zero)
            )
            # Loopback/stream аудио намеренно НЕ включаем в _n_active.
            # Игровой звук — фоновый поток, а не «конкурирующий голос».
            # Раньше активный стрим постоянно добавлял +1 к N → gain у всех
            # зрителей падал на 18% даже когда никто не говорил.
            # Soft limiter (ниже) надёжно защищает от перегруза при наложении
            # голоса и игрового звука без ручного снижения gain.

            # ── Смягчённая формула headroom ──────────────────────────────────
            # Было: sqrt(2)/sqrt(N) → при N=3 gain=0.816 (внезапные -18%
            # в момент подключения 3-го собеседника — хорошо слышимый провал).
            # Теперь: ≤2 источников → 100%, далее -10% за каждого, пол 0.75.
            # Soft limiter обработает редкие случаи когда все кричат одновременно.
            if _n_active <= 2:
                _speaker_gain = 1.0
            else:
                _speaker_gain = max(0.75, 1.0 - 0.1 * (_n_active - 2))

            # FIX #1: читаем COW-снимок БЕЗ лока.
            # _packet_processor_loop обновляет _audio_users_snapshot внутри
            # users_lock после каждого изменения. Снимок «отстаёт» максимум
            # на 1 пакет (~20 мс) — для аудиомикширования незаметно.
            # JitterBuffer.get() имеет собственный внутренний лок — thread-safe.
            for uid, user in self._audio_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if not user.is_locally_muted and not user.volume_zero:
                        try:
                            if data:
                                decoded = user.decoder.decode(data, CHUNK_SIZE)
                                s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0

                                # ── Per-uid whisper effect ───────────────────────────────────
                                # _active_whispers[uid] обновляется в add_incoming_whisper_packet
                                # на каждый входящий пакет шёпота (~50 раз/сек).
                                #
                                # «Тёплый старт» при первом пакете (uid не в _whisper_states):
                                # history заполняем текущим фреймом s (повторённым до 2048).
                                # Обе читающие головки pitch-shifter'а сразу попадают в реальный
                                # сигнал — переход ноль→сигнал отсутствует → нет треска/click.
                                #
                                # LP-фильтр: нулевые начальные условия оптимальны для голосового
                                # сигнала (mean ≈ 0); sosfilt_zi(sos)*0 == zeros.
                                #
                                # Два шептуна одновременно: каждый uid имеет свой state dict →
                                # независимые history/phase/lp_zi/buf → нет взаимных артефактов →
                                # оба смешиваются в mix_buffer без потерь.
                                _w_ts = self._active_whispers.get(uid, 0.0)
                                if _w_ts and (curr_time - _w_ts) < 2.0:
                                    if uid not in self._whisper_states:
                                        # Ленивое создание: тёплый старт с реальным сигналом
                                        _warm_history = np.resize(
                                            s.astype(np.float32), 2048).copy()
                                        _N = CHUNK_SIZE
                                        self._whisper_states[uid] = {
                                            'history': _warm_history,
                                            'phase':   0.0,
                                            'lp_zi':   np.zeros(
                                                (self._anon_lp_sos.shape[0], 2),
                                                dtype=np.float64),
                                            'buf':     np.zeros(
                                                2048 + _N, dtype=np.float32),
                                            # FIX 7: pre-alloc scratch arrays —
                                            # np.arange(N) вызывался ДВАЖДЫ каждые 20 мс.
                                            # Пересоздание 2 массивов по 960 float64 = ~15 KB/фрейм.
                                            # Кэшируем: arange и base_idx неизменны пока N=CHUNK_SIZE.
                                            '_arange': np.arange(_N, dtype=np.float64),
                                            '_base':   np.arange(2048, 2048 + _N, dtype=np.float64),
                                        }
                                    s = self._apply_anonymous_voice_effect(
                                        s, self._whisper_states[uid])
                                else:
                                    # Шептун неактивен: освобождаем state (нет утечки памяти)
                                    self._active_whispers.pop(uid, None)
                                    self._whisper_states.pop(uid, None)

                                self.mix_buffer += s * (user.volume * _speaker_gain)
                                # Mix-Minus (FLAG_STREAM_VOICES) через UDP удалён.
                                # Голоса участников комнаты для зрителей стрима будут
                                # реализованы через WebRTC в следующей итерации.
                            else:
                                # FIX PLC: пакет не пришёл вовремя (jitter/потеря сети).
                                # Вызываем Opus PLC (Packet Loss Concealment) с data=None.
                                # Opus генерирует comfort noise и поддерживает внутреннее
                                # LPC-состояние предсказателя синхронизированным.
                                # Без этого вызова при следующем реальном пакете декодер
                                # «не знает» что был пропуск → выдаёт discontinuity → треск.
                                # Результат PLC в mix_buffer НЕ добавляем — тишина правильна
                                # когда пакет потерян, PLC нужен только для состояния декодера.
                                try:
                                    user.decoder.decode(None, CHUNK_SIZE)
                                except Exception:
                                    pass
                        except Exception:
                            pass

        # ── Внутренний микшер UI-звуков (уведомления, Soundboard, Nudge) ────────
        # Подмешиваем в уже заполненный mix_buffer — никаких sd.play() и
        # новых WASAPI-устройств. Lock гарантирует атомарность доступа к списку.
        with self._local_sounds_lock:
            active = []
            for snd in self._local_sounds:
                pos  = snd['pos']
                rem  = len(snd['data']) - pos
                take = min(CHUNK_SIZE, rem)
                if take > 0:
                    self.mix_buffer[:take] += snd['data'][pos:pos + take] * snd['vol']
                    snd['pos'] += take
                    if snd['pos'] < len(snd['data']):
                        active.append(snd)
            self._local_sounds = active

        # ── Математически чистый tanh soft-clipper ───────────────────────────
        # Заменяет старый пропорциональный лимитер (self.mix_buffer *= k).
        # Старая схема: один gain на весь кадр → резкая «ступенька» gain между
        # соседними кадрами → слышимый щелчок/треск при пиках.
        # tanh обрабатывает каждый семпл независимо, плавно загибая только те,
        # что вышли за limit. Результат — аналог лампового сатуратора без артефактов.
        _limit = 0.95
        _over  = np.abs(self.mix_buffer) > _limit
        if np.any(_over):
            _excess = np.abs(self.mix_buffer[_over]) - _limit
            self.mix_buffer[_over] = (
                np.sign(self.mix_buffer[_over])
                * (_limit + (1.0 - _limit) * np.tanh(_excess / (1.0 - _limit)))
            )
        # Safety-clip: float-погрешности после tanh
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
        # Зажимаем в [0.0 … 20.0].
        # Слайдер 0-200 → экспоненциальная кривая → max = 10.0 (слайдер 200).
        # Верхняя граница 20.0 оставляет запас для «Буст звука» (_BOOST_VOL=15x).
        # Soft-limiter в audio_callback (0.95-нормализация) защищает от клиппинга.
        vol = max(0.0, min(20.0, float(vol)))
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
                    # Fallback: сохраняем по uid если IP ещё не зарегистрирован.
                    # register_ip_mapping() перезапишет правильный ключ при получении IP.
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
        now = time.perf_counter()  # FIX: perf_counter точнее time.time() на Windows (15.6ms vs мкс)

        # Обновляем реестр активных шептунов — dict lookup O(1), GIL-safe.
        # audio_callback читает self._active_whispers[uid] без лока:
        # dict.__setitem__ с существующим ключом (update float значения) GIL-атомарно.
        # При новом uid — новый ключ; audio_callback увидит его на следующем фрейме
        # (max 20 мс опоздания), что приемлемо.
        self._active_whispers[uid] = now

        # Backward compat: UI-сигнал и таймер скрытия оверлея ориентируются на
        # _whisper_in_uid / _whisper_in_ts. Показываем последнего шептуна.
        self._whisper_in_uid = uid
        self._whisper_in_ts  = now

        # Эмитим на каждый пакет — UI-таймер перезапускается, оверлей не гаснет.
        self.whisper_received.emit(uid)

        self.add_incoming_packet(uid, seq, data, 0)

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