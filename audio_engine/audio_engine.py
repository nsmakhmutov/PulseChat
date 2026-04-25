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
    SAMPLE_RATE, CHANNELS, CHUNK_SIZE, FRAME_DURATION,
    OPUS_APPLICATION, DEFAULT_BITRATE,
    UDP_HEADER_STRUCT, UDP_HEADER_SIZE,
    FLAG_STREAM_VOICES, FLAG_WHISPER, FLAG_ANONYMOUS,
    STREAM_VOICE_HEADER_STRUCT, STREAM_VOICE_HEADER_SIZE,
    ANONYMOUS_UID,
    AUDIO_DIAG_ENABLED,
)
from .audio_processing import (
    DeepFilterEngine,
    PYRNNOISE_AVAILABLE, PYAUDIOWPATCH_AVAILABLE, _pyaudio,
    butter, sosfilt, sosfilt_zi, _WLP_SOS, _ANON_LP_SOS,
)
from .audio_capture import StreamAudioCapture, MicrophoneTrack, SystemAudioTrack, get_dll
try:
    from pyrnnoise import RNNoise
except ImportError:
    RNNoise = None


class JitterBuffer:
    """
    Буфер джиттера с защитой от устаревшего состояния после (пере)подключения.

    НОВОЕ (FIX миграция):
      - SEQ_JUMP_RESET_BACK: если прилетел пакет со seq значительно МЕНЬШЕ
        last_seq, считаем что sender пере-инициализировал свой seq-счётчик
        (на практике — другой клиент сделал полный reconnect) → сбрасываем
        состояние буфера и начинаем заново. Без этой защиты все пакеты
        после сбросившейся сессии тихо отбрасываются.

      - SEQ_JUMP_RESET_FORWARD: если прилетел пакет с seq >> last_seq
        (скачок вперёд на очень большое значение), тоже сбрасываем — так
        мы не застрянем с гигантским last_seq, если новая сессия начала
        с seq=0.

      - reset(): публичный метод для ручного сброса (вызывается из
        AudioHandler.reset_voice_state при миграции сервера).
    """

    SEQ_JUMP_RESET_BACK    = 200     # seq уменьшился > чем на 200 (= 4с звука)
    SEQ_JUMP_RESET_FORWARD = 10000   # seq подскочил на 10000 (= 200с)

    def __init__(self, target_delay=2):
        self.buffer = []
        self.target_delay = target_delay
        self.last_seq = -1
        self._lock = threading.Lock()
        self.is_buffering = True
        self.max_size = 50

    def _reset_locked(self):
        """Сброс под уже взятым _lock."""
        self.buffer.clear()
        self.last_seq = -1
        self.is_buffering = True

    def reset(self):
        """Публичный сброс — для вызова при миграции сервера."""
        with self._lock:
            self._reset_locked()

    def add(self, seq, data):
        with self._lock:
            # ── Защита от stale-сессии ──────────────────────────────────────
            if self.last_seq != -1:
                # seq откатился назад на большую величину → sender пере-создал счётчик
                if seq + self.SEQ_JUMP_RESET_BACK < self.last_seq:
                    print(f"[JB] seq reset (was last={self.last_seq}, got={seq}) — "
                          f"смена сессии, сброс буфера", flush=True)
                    self._reset_locked()
                # seq подскочил слишком далеко вперёд — тоже reinit-сигнал
                elif seq > self.last_seq + self.SEQ_JUMP_RESET_FORWARD:
                    print(f"[JB] seq jump forward (last={self.last_seq}, got={seq}) — "
                          f"reset", flush=True)
                    self._reset_locked()

            if seq <= self.last_seq and self.last_seq != -1:
                return
            heapq.heappush(self.buffer, (seq, data))
            if len(self.buffer) > self.max_size:
                # FIX: дропаем самый новый пакет (максимальный seq).
                # heappop() на min-heap забирает самый маленький — именно тот,
                # который должен играть следующим → гарантированный треск.
                #
                # Раньше было: max() + list.remove() + heapify() = три O(n) прохода.
                # Оптимизация: итерируемся один раз, находим индекс max,
                # делаем pop по индексу через swap с последним + heapify.
                #
                # Всё ещё O(n), но 1 проход вместо 3, и это запускается редко
                # (только при шторме пакетов).
                max_idx = 0
                max_seq = self.buffer[0][0]
                for i in range(1, len(self.buffer)):
                    if self.buffer[i][0] > max_seq:
                        max_seq = self.buffer[i][0]
                        max_idx = i
                # Swap с последним и pop — избегает сдвига на O(n)
                last_idx = len(self.buffer) - 1
                if max_idx != last_idx:
                    self.buffer[max_idx] = self.buffer[last_idx]
                self.buffer.pop()
                # Heapify только если мы сломали heap-invariant
                if max_idx != last_idx:
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

        # ── Стриминговый аудио-буфер (WebRTC стрим-аудио зрителя) ────────────
        # FIX AV SYNC: буфер увеличен с 30 до 60 чанков (600мс → 1.2 сек).
        # RadminVPN jitter может достигать 300-500мс в играх.
        # 30 чанков = 600мс слишком мало → underrun при нагрузке → аудио пропадает.
        # 60 чанков = 1.2 сек с запасом покрывает пики jitter RadminVPN.
        # Latency не увеличивается: при заполненном буфере drop-oldest
        # гарантирует что слушатель получает свежий звук, не старый.
        _STREAM_BUF_CHUNKS = 60   # FIX: было 30
        self._STREAM_BUF_SIZE: int  = CHUNK_SIZE * _STREAM_BUF_CHUNKS
        self._stream_buf:  np.ndarray = np.zeros(self._STREAM_BUF_SIZE + CHUNK_SIZE,
                                                  dtype=np.float32)
        self._stream_fill: int = 0          # сколько семплов в буфере
        self._stream_lock  = threading.Lock()
        self._stream_vol:   float = 1.0
        self._stream_active: bool = False

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

        # Mix-Minus удалён: программное вычитание reference из захвата DLL
        # создавало обратный эффект — инвертированный голос суммировался со стримом.
        # Исключение голоса InPulse целиком обеспечивает DLL (PROCESS_LOOPBACK_EXCLUDE).

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
        # Флаг анонимного шёпота — выставляется start_whisper(anonymous=True).
        # При True в исходящем пакете помимо FLAG_WHISPER выставляется
        # FLAG_ANONYMOUS; сервер переписывает sender_uid в UDP-заголовке на
        # ANONYMOUS_UID перед ретрансляцией получателю.
        self._whisper_anonymous: bool = False
        # Время последнего входящего анонимного whisper-пакета (perf_counter).
        # Используется для резета jitter-buffer общего ANONYMOUS_UID при смене
        # «говорящего» анонима — иначе два подряд идущих анонима с разными
        # seq-последовательностями будут бить друг другу звук в одном JB.
        self._anon_last_incoming_ts: float = 0.0

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
        # FIX -9993: разделены на два независимых потока PortAudio.
        # sd.Stream (duplex) требует ОДИНАКОВЫЙ host API для input и output.
        # После _remap_to_wasapi() output переходит на WASAPI, input остаётся
        # на MME → PaErrorCode -9993 ("Illegal combination of I/O devices").
        # Решение: sd.InputStream (MME, любой API) + sd.OutputStream (WASAPI).
        # self.stream = OutputStream — backward compat для external checks (.stream is None).
        self.stream = None        # sd.OutputStream (playback, WASAPI)
        self._in_stream = None    # sd.InputStream  (mic capture, MME)
        # Реальное число каналов открытых потоков — выставляется в start().
        self._out_channels: int = 2
        self._in_channels:  int = 1

        # ── Нативная частота дискретизации устройств ──────────────────────────
        # Выставляется в start() после query_devices().
        # Если устройство не 48000 Гц — ресемплинг выполняется автоматически:
        #   Вход:  native_sr → 48000  (перед NR и Opus encode)
        #   Выход: 48000 → native_sr  (перед записью в outdata)
        # CHUNK_SIZE и Opus всегда работают на 48000 — это не меняется.
        self._in_sr:        int = SAMPLE_RATE   # частота микрофона
        self._out_sr:       int = SAMPLE_RATE   # частота динамиков
        self._in_blocksize: int = CHUNK_SIZE    # кол-во сэмплов на колбэк (вход)
        self._out_blocksize: int = CHUNK_SIZE   # кол-во сэмплов на колбэк (выход)
        # Pre-allocated буфер ресемплированного выхода (48000 → out_sr).
        # Перевыделяется в start() если out_sr меняется.
        self._out_resamp_buf: np.ndarray = np.zeros(CHUNK_SIZE, dtype=np.float32)
        # Флаг первого вызова output-callback
        self._cb_out_first_logged: bool = False

        # PortAudio OutputStream всегда открыт — голоса воспроизводятся через него.
        # Сессия WASAPI Shared python.exe PID → PROCESS_LOOPBACK_EXCLUDE в DLL
        # исключает её из loopback захвата. Отдельный DLL Render не нужен.
        # ctypes-указатель на mix_buffer (pre-allocated, никогда не перевыделяется).
        self._mix_buffer_ptr: ctypes.POINTER(ctypes.c_float) = (
            self.mix_buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        )

        # ── Диагностика _output_callback (зритель видит раздельные уровни) ──
        # Логируем раз в секунду:
        #   [OUT-DIAG] voice_rms  = RMS декодированных голосов участников чата
        #   [OUT-DIAG] stream_rms = RMS стрим-аудио, пришедшего по WebRTC
        #   [OUT-DIAG] mix_rms    = RMS итогового mix_buffer (то что слышит зритель)
        # Если voice_rms ≈ stream_rms → стрим содержит голосовой чат (DLL не работает).
        # Если stream_rms > 0 при voice_rms ≈ 0 → только игровой звук (норма).
        self._diag_voice_sum:  float = 0.0
        self._diag_stream_sum: float = 0.0
        self._diag_mix_sum:    float = 0.0
        self._diag_cnt:        int   = 0
        self._diag_next_ts:    float = 0.0
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


    # ------------------------------------------------------------------
    #  DLL RAW Render Engine — управление
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    #  Заглушка для обратной совместимости — DLL Render удалён
    # ------------------------------------------------------------------

    def enable_dll_render(self, active: bool, raw_mode: bool = False) -> None:
        """
        No-op. DLL Render удалён в v4.
        Голоса воспроизводятся через PortAudio WASAPI Shared (python.exe PID).
        PROCESS_LOOPBACK_EXCLUDE в DLL Capture исключает эту сессию по PID
        через Session Manager — до hardware микшера — эхо не возникает.
        Оставлен для совместимости на случай вызова из старого кода.
        """
        pass  # nothing to do

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

    def add_stream_audio(self, data: np.ndarray, sr: int, vol: float = 1.0) -> None:
        """
        Пишет PCM-чанк стрим-аудио (WebRTC) в pre-allocated кольцевой буфер.
        Нет np.concatenate, нет np.empty — нет аллокаций на горячем пути.

        data: aiortc frame.to_ndarray() — (channels, samples) или (1, samples*ch)
        sr  : частота дискретизации источника
        vol : громкость (применяется здесь, не в callback)
        """
        try:
            data = np.asarray(data, dtype=np.float32)

            # Нормализация формы: любой 2D → плоский float32 моно.
            #
            # Возможные форматы на входе:
            #   (1, 960)  — моно из SystemAudioTrack.recv() [layout='mono']
            #   (1, 1920) — стерео interleaved Opus от aiortc (L,R,L,R,...)
            #   (2, 960)  — планарное стерео (channels, samples)
            #   (960, 2)  — интерливед (samples, channels)
            #
            # ВАЖНО: НЕ делаем слепое reshape(-1, 2) для shape (1, N) —
            # при моно это режет буфер вдвое и звук тянется на полпитча.
            if data.ndim == 2:
                rows, cols = data.shape
                if rows == 1:
                    # Один ряд: моно или стерео interleaved
                    # Стерео interleaved: cols кратен 2 И вдвое больше CHUNK_SIZE
                    if cols % 2 == 0 and cols >= CHUNK_SIZE * 2:
                        mono = data[0].reshape(-1, 2).mean(axis=1).astype(np.float32)
                    else:
                        # Чистое моно (960 сэмплов) — просто берём строку
                        mono = data[0]
                elif rows > 2 and cols <= 2:
                    # (samples, channels): много строк, 1-2 канала
                    mono = np.mean(data, axis=1).astype(np.float32)
                else:
                    # (channels, samples): строки = каналы
                    mono = np.mean(data, axis=0).astype(np.float32)
            elif data.ndim == 1:
                mono = data
            else:
                return

            # Ресемплинг при необходимости
            if sr != SAMPLE_RATE:
                target_len = int(round(len(mono) * SAMPLE_RATE / sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(mono), dtype=np.float64)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float64)
                    mono  = np.interp(x_new, x_old, mono).astype(np.float32)

            if vol != 1.0:
                mono = mono * vol

            n = len(mono)
            with self._stream_lock:
                # Если переполнение — отбрасываем старые данные
                if self._stream_fill + n > self._STREAM_BUF_SIZE:
                    drop = self._stream_fill + n - self._STREAM_BUF_SIZE
                    # Сдвигаем буфер влево, убирая самые старые семплы
                    remain = self._stream_fill - drop
                    if remain > 0:
                        self._stream_buf[:remain] = self._stream_buf[drop:self._stream_fill]
                    self._stream_fill = remain

                # Копируем без аллокации
                self._stream_buf[self._stream_fill:self._stream_fill + n] = mono
                self._stream_fill += n
                self._stream_active = True

        except Exception as e:
            print(f"[Audio] add_stream_audio error: {e}")

    def stop_stream_playback(self) -> None:
        """Останавливает стрим-аудио и сбрасывает буфер. Вызывать при stop_watching()."""
        self._stream_active = False
        with self._stream_lock:
            self._stream_fill = 0
            # FIX LEAK #5: явно обнуляем буфер.
            # Без обнуления numpy-массив остаётся «горячим» в памяти процесса —
            # Windows не отдаёт страницы обратно ОС даже после SetWorkingSetSize.
            # Обнуление помечает страницы как «чистые» → heap trim возвращает их ОС.
            self._stream_buf[:] = 0.0

    def set_stream_volume(self, vol: float) -> None:
        """Устанавливает громкость стрим-аудио (0.0–2.0)."""
        self._stream_vol = max(0.0, min(float(vol), 2.0))

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
        out_idx = self._remap_to_wasapi(out_idx)

        # ── Определяем нативную частоту дискретизации каждого устройства ────
        # sounddevice/PortAudio в Shared WASAPI ОБЯЗАН открывать поток на нативной
        # частоте устройства. Если передать 48000 а устройство настроено на 44100 —
        # некоторые драйверы (Creative, старые Realtek) возвращают тишину или
        # падают с PaError -9997 (Invalid sample rate).
        # Решение: открываем на нативной частоте + ресемплируем в колбэках.
        try:
            _in_dev_info  = sd.query_devices(in_idx  if in_idx  is not None else sd.default.device[0])
            self._in_sr   = int(_in_dev_info['default_samplerate'])
        except Exception:
            self._in_sr   = SAMPLE_RATE

        try:
            _out_dev_info  = sd.query_devices(out_idx if out_idx is not None else sd.default.device[1])
            self._out_sr   = int(_out_dev_info['default_samplerate'])
        except Exception:
            self._out_sr   = SAMPLE_RATE

        # Кол-во сэмплов для 20 мс фрейма на нативной частоте
        self._in_blocksize  = int(round(self._in_sr  * FRAME_DURATION / 1000))
        self._out_blocksize = int(round(self._out_sr * FRAME_DURATION / 1000))

        # Pre-allocate буфер ресемплированного выхода нужного размера
        self._out_resamp_buf = np.zeros(self._out_blocksize, dtype=np.float32)

        if self._in_sr != SAMPLE_RATE:
            print(
                f"[Audio] ⚠ Микрофон: {self._in_sr} Гц ≠ 48000 — "
                f"автоматический ресемплинг {self._in_sr}→48000 Гц",
                flush=True,
            )
        if self._out_sr != SAMPLE_RATE:
            print(
                f"[Audio] ⚠ Динамики: {self._out_sr} Гц ≠ 48000 — "
                f"автоматический ресемплинг 48000→{self._out_sr} Гц",
                flush=True,
            )

        print(f"[DEBUG] AudioHandler.start: in_idx={in_idx}, out_idx={out_idx}", flush=True)

        self._is_running.set()
        try:
            # FIX -9993: два независимых потока вместо одного дуплексного.
            # sd.Stream (duplex) требует одинаковый HostAPI для in и out.
            # После _remap_to_wasapi() out = WASAPI, in = MME → PaErrorCode -9993.
            # sd.InputStream + sd.OutputStream работают с разными HostAPI без ограничений.
            # Дополнительный бонус: OutputStream на WASAPI атрибутируется нашему PID
            # (не svchost.exe как MME) → DLL Process Loopback правильно исключает
            # воспроизводимый нами звук → зрители больше НЕ слышат эхо своих голосов.
            print("[DEBUG] AudioHandler.start: создание sd.InputStream (микрофон)...", flush=True)
            self._in_stream = sd.InputStream(
                device=in_idx,
                samplerate=self._in_sr,       # нативная частота устройства
                blocksize=self._in_blocksize, # 20 мс фрейм на нативной частоте
                dtype='float32', channels=CHANNELS,
                callback=self._input_callback,
            )
            print("[DEBUG] AudioHandler.start: создание sd.OutputStream (динамики WASAPI)...", flush=True)
            self.stream = sd.OutputStream(
                device=out_idx,
                samplerate=self._out_sr,       # нативная частота устройства
                blocksize=self._out_blocksize, # 20 мс фрейм на нативной частоте
                dtype='float32', channels=CHANNELS,
                callback=self._output_callback,
            )
            self._in_stream.start()
            self.stream.start()
            print("[DEBUG] AudioHandler.start: InputStream + OutputStream запущены", flush=True)
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
        # Закрываем InputStream (микрофон)
        if getattr(self, '_in_stream', None) is not None:
            try:
                self._in_stream.stop()
                self._in_stream.close()
            except Exception:
                pass
            self._in_stream = None
        # Закрываем OutputStream (динамики, WASAPI)
        if hasattr(self, 'stream') and self.stream:
            try:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            except Exception:
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

    def reset_voice_state(self):
        """
        Полный сброс приёмной части голоса.
        Вызывается ПЕРЕД сменой my_uid при миграции сервера.

        Что делает:
          1. Дренирует очередь incoming_packets — там могут лежать пакеты со
             старыми uid, которые после смены my_uid будут неверно
             интерпретированы (в т.ч. как "свои" пакеты если новый my_uid
             совпадёт с чьим-то старым).
          2. Удаляет всех RemoteUser — их uid устарели после выдачи новых
             uid новым сервером. Новые RemoteUser создадутся автоматически
             в _packet_processor_loop при первом пакете от каждого нового uid.
          3. Чистит uid_to_ip, pending_volumes, whisper-состояния.
          4. ВАЖНО: НЕ трогает my_sequence — свой sender-seq оставляем
             монотонным. Другие клиенты на своей стороне тоже вызовут этот
             метод и сбросят СВОИ приёмники. JitterBuffer.add теперь
             умеет ловить seq-скачок как страховку.
          5. Обновляет COW-снимок для audio_callback.

        Безопасно вызывать в любом состоянии: не зависит от _is_running,
        не требует остановки потоков. _packet_processor_loop может доложить
        новый RemoteUser сразу после — это нормально (он будет уже с новым uid).
        """
        import queue as _q
        drained = 0
        try:
            while True:
                self.incoming_packets.get_nowait()
                drained += 1
        except _q.Empty:
            pass

        with self.users_lock:
            n_users = len(self.remote_users)
            self.remote_users.clear()
            self.uid_to_ip.clear()
            self.pending_volumes.clear()

            # Whisper-состояния per-uid — тоже сбрасываем
            if hasattr(self, '_whisper_states'):
                self._whisper_states.clear()
            if hasattr(self, '_active_whispers'):
                self._active_whispers.clear()

            self._audio_users_snapshot = {}

        print(f"[Audio] reset_voice_state: drained={drained} пакетов, "
              f"удалено {n_users} RemoteUser", flush=True)

    def _packet_processor_loop(self):
        while self._is_running.is_set():
            try:
                packet_data = self.incoming_packets.get(timeout=0.1)
                uid, seq, data, flags = packet_data
                if uid == self.my_uid: continue

                with self.users_lock:
                    is_new_user = uid not in self.remote_users
                    if is_new_user:
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

                    # FIX: обновляем COW-снимок ТОЛЬКО при появлении нового пользователя.
                    # Для существующих пользователей last_packet_time и jitter_buffer
                    # доступны через ссылку в снимке напрямую — COW копирует ссылки
                    # на объекты, а не сами объекты, поэтому audio_callback видит
                    # изменения без пересоздания dict.
                    # Это устраняет конкуренцию с cleanup_users: раньше каждый пакет
                    # держал users_lock на dict() — теперь только при новом участнике.
                    if is_new_user:
                        self._audio_users_snapshot = dict(self.remote_users)

            except queue.Empty:
                continue
            except Exception:
                pass

    # ===========================================================================
    #  _input_callback  —  захват микрофона (sd.InputStream, любой HostAPI)
    #  Вызывается PortAudio каждые 20 мс из нативного потока.
    #  NR → VAD → Opus encode → send_queue.
    #  Сигнатура InputStream callback: (indata, frames, time_info, status).
    # ===========================================================================
    def _input_callback(self, indata, frames, time_info, status):
        if not self._cb_first_logged:
            self._cb_first_logged = True
            print("[DEBUG] _input_callback: ПЕРВЫЙ ВЫЗОВ — InputStream работает", flush=True)
        if status:
            print(f"[DEBUG] _input_callback: status={status}", flush=True)

        if not self._is_running.is_set():
            return

        curr_time = time.perf_counter()
        raw_input = indata.flatten()

        # ── Ресемплинг вход: native_sr → 48000 ──────────────────────────────
        # Выполняется до NR и Opus — оба работают строго на 48000 Гц / CHUNK_SIZE.
        # np.interp — линейная интерполяция, достаточна для голоса (< 4 кГц).
        # При in_sr == 48000 блок пропускается без аллокаций (fast path).
        if self._in_sr != SAMPLE_RATE:
            x_old = np.linspace(0.0, 1.0, len(raw_input), dtype=np.float64)
            x_new = np.linspace(0.0, 1.0, CHUNK_SIZE,     dtype=np.float64)
            raw_input = np.interp(x_new, x_old, raw_input).astype(np.float32)

        denoised_float = raw_input

        _nr = self.nr_mode
        if _nr == 2 and self.dfn_engine:
            try:
                denoised_float = self.dfn_engine.process(denoised_float)
            except Exception:
                pass
        elif _nr == 1 and self.denoiser:
            try:
                # FIX 6: reuse _pcm_int16_buf — нет heap-аллокации каждые 20 мс.
                np.multiply(denoised_float, 32767.0,
                            out=self._pcm_int16_buf, casting='unsafe')
                # CRITICAL FIX: denoise_chunk возвращает (channels, frame_size).
                processed = [f.flatten() for p, f in
                             self.denoiser.denoise_chunk(self._pcm_int16_buf)]
                if processed:
                    combined = np.concatenate(processed) if len(processed) > 1 else processed[0]
                    denoised_float = combined.astype(np.float32) / 32767.0
            except Exception:
                pass

        # ── Pre-encode normalization: защита от wraparound при пиках > 1.0 ──
        _in_peak = np.max(np.abs(denoised_float))
        if _in_peak > 0.98:
            denoised_float = denoised_float * (0.98 / _in_peak)

        rms = np.sqrt(np.mean(denoised_float ** 2))
        # FIX 9: throttle 50 Гц → 10 Гц.
        self._vol_emit_counter += 1
        if self._vol_emit_counter >= 5:
            self._vol_emit_counter = 0
            self.volume_level_signal.emit(int(min(rms * 1000, 100)))

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
                    # ── РЕЖИМ ШЁПОТА ─────────────────────────────────────────
                    np.multiply(denoised_float, 32767, out=self._pcm_int16_buf,
                                casting='unsafe')
                    encoded = self.encoder.encode(self._pcm_int16_buf.tobytes(), CHUNK_SIZE)
                    self._whisper_sequence += 1
                    # FLAG_WHISPER обязателен для роутинга на стороне сервера;
                    # FLAG_ANONYMOUS триггерит перезапись sender_uid в заголовке
                    # сервером на ANONYMOUS_UID — получатель не узнает, кто шептал.
                    w_flags = FLAG_WHISPER
                    if self._whisper_anonymous:
                        w_flags |= FLAG_ANONYMOUS
                    w_header = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                                      self._whisper_sequence, w_flags)
                    w_payload = struct.pack('!I', whisper_uid) + encoded
                    try:
                        self.send_queue.put_nowait(w_header + w_payload)
                    except Exception:
                        pass

                elif is_talking and not self._is_muted.is_set():
                    # ── ОБЫЧНЫЙ РЕЖИМ: пакет в комнату ───────────────────────
                    np.multiply(denoised_float, 32767, out=self._pcm_int16_buf,
                                casting='unsafe')
                    encoded = self.encoder.encode(self._pcm_int16_buf.tobytes(), CHUNK_SIZE)
                    self.my_sequence += 1
                    packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                                    self.my_sequence, flags) + encoded
                    if not self.was_talking:
                        # FIX #3: deque.popleft() — O(1)
                        while self.vad_pre_buffer:
                            try:
                                self.send_queue.put_nowait(self.vad_pre_buffer.popleft())
                            except Exception:
                                pass
                        self.was_talking = True
                    self.send_queue.put_nowait(packet)

                else:
                    self.was_talking = False
                    if not is_talking and whisper_uid == 0:
                        empty_packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, 0, flags)
                        self.vad_pre_buffer.append(empty_packet)
            except Exception:
                pass

    # ===========================================================================
    #  _output_callback  —  воспроизведение (sd.OutputStream, WASAPI)
    #  Вызывается PortAudio каждые 20 мс из нативного потока.
    #  Декодирует голоса → подмешивает UI-звуки → стрим-аудио → tanh → outdata.
    #  Сигнатура OutputStream callback: (outdata, frames, time_info, status).
    #
    #  КЛЮЧЕВОЕ: OutputStream на WASAPI атрибутируется PID нашего процесса
    #  (не svchost.exe как MME) → DLL Process Loopback корректно исключает его →
    #  зрители не слышат эхо своих голосов.
    # ===========================================================================
    def _output_callback(self, outdata, frames, time_info, status):
        if not self._cb_out_first_logged:
            self._cb_out_first_logged = True
            print("[DEBUG] _output_callback: ПЕРВЫЙ ВЫЗОВ — OutputStream (WASAPI) работает", flush=True)
        if status:
            print(f"[DEBUG] _output_callback: status={status}", flush=True)

        if not self._is_running.is_set():
            outdata.fill(0)
            return

        curr_time = time.perf_counter()

        # ── Диагностика: локальные аккумуляторы для этого фрейма ────────────
        _diag_voice_frame:  float = 0.0   # сумма RMS^2 голосов чата за фрейм
        _diag_stream_frame: float = 0.0   # RMS^2 стрим-аудио за фрейм

        self.mix_buffer.fill(0)
        # BUG-FIX: _n_active инициализируем до блока deafened.
        _n_active = 0
        if not self._is_deafened.is_set():
            # ── Подсчёт активных голосовых спикеров (окно 0.4с) ─────────────
            _n_active = sum(
                1 for u in self._audio_users_snapshot.values()
                if (curr_time - u.last_packet_time < 0.4
                    and not u.is_locally_muted
                    and not u.volume_zero)
            )

            # ── Смягчённая формула headroom ──────────────────────────────────
            if _n_active <= 2:
                _speaker_gain = 1.0
            else:
                _speaker_gain = max(0.75, 1.0 - 0.1 * (_n_active - 2))

            # FIX #1: читаем COW-снимок БЕЗ лока.
            for uid, user in self._audio_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if not user.is_locally_muted and not user.volume_zero:
                        try:
                            if data:
                                decoded = user.decoder.decode(data, CHUNK_SIZE)
                                s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0

                                # ── Per-uid whisper effect ───────────────────
                                _w_ts = self._active_whispers.get(uid, 0.0)
                                if _w_ts and (curr_time - _w_ts) < 2.0:
                                    if uid not in self._whisper_states:
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
                                            '_arange': np.arange(_N, dtype=np.float64),
                                            '_base':   np.arange(2048, 2048 + _N, dtype=np.float64),
                                        }
                                    s = self._apply_anonymous_voice_effect(
                                        s, self._whisper_states[uid])
                                else:
                                    self._active_whispers.pop(uid, None)
                                    self._whisper_states.pop(uid, None)

                                self.mix_buffer += s * (user.volume * _speaker_gain)
                                # Диагностика: накапливаем вклад этого голоса
                                # FIX: np.dot под флагом — hot path
                                if AUDIO_DIAG_ENABLED:
                                    _diag_voice_frame += float(np.dot(s, s)) / CHUNK_SIZE
                            else:
                                # FIX PLC: сохраняем состояние декодера при потере пакета.
                                try:
                                    user.decoder.decode(None, CHUNK_SIZE)
                                except Exception:
                                    pass
                        except Exception:
                            pass

        # ── Внутренний микшер UI-звуков (уведомления, Soundboard, Nudge) ────
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

        # ── Стриминговое аудио зрителя (WebRTC) ─────────────────────────────
        # FIX AV SYNC: убран auto-ducking (0.4 при активных голосах).
        # Старое поведение: при разговоре кого-либо в чате громкость стрима
        # прыгала с 100% до 40% и обратно → очень заметные скачки.
        # Пользователь сам регулирует баланс через StreamVolumePopup.
        if self._stream_active:
            with self._stream_lock:
                avail = self._stream_fill
                if avail >= CHUNK_SIZE:
                    _current_vol = self._stream_vol   # FIX: убрано * 0.4
                    self.mix_buffer += self._stream_buf[:CHUNK_SIZE] * _current_vol
                    # Диагностика: RMS стрим-аудио до масштабирования
                    # FIX: под флагом — hot path
                    if AUDIO_DIAG_ENABLED:
                        _sb = self._stream_buf[:CHUNK_SIZE]
                        _diag_stream_frame = float(np.dot(_sb, _sb)) / CHUNK_SIZE
                    remaining = avail - CHUNK_SIZE
                    if remaining > 0:
                        self._stream_buf[:remaining] = self._stream_buf[CHUNK_SIZE:avail]
                    self._stream_fill = remaining
                elif avail > 0:
                    _current_vol = self._stream_vol   # FIX: убрано * 0.4
                    _fade = np.linspace(1.0, 0.0, avail, dtype=np.float32)
                    self.mix_buffer[:avail] += (
                        self._stream_buf[:avail] * _fade * _current_vol
                    )
                    self._stream_fill = 0

        # ── Математически чистый tanh soft-clipper ───────────────────────────
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

        # ── Диагностика: накапливаем и логируем раз в секунду ──────────────
        # FIX: под флагом AUDIO_DIAG_ENABLED — np.dot на каждый фрейм это hot path
        if AUDIO_DIAG_ENABLED:
            _mix_rms_sq = float(np.dot(self.mix_buffer, self.mix_buffer)) / CHUNK_SIZE
            self._diag_voice_sum  += _diag_voice_frame
            self._diag_stream_sum += _diag_stream_frame
            self._diag_mix_sum    += _mix_rms_sq
            self._diag_cnt        += 1

            if curr_time >= self._diag_next_ts and self._diag_cnt > 0:
                self._diag_voice_sum  = 0.0
                self._diag_stream_sum = 0.0
                self._diag_mix_sum    = 0.0
                self._diag_cnt        = 0
                self._diag_next_ts    = curr_time + 1.0

        # ── Вывод через PortAudio (всегда) ──────────────────────────────────
        # PortAudio WASAPI Shared сессия python.exe атрибутируется нашему PID.
        # DLL Capture PROCESS_LOOPBACK_EXCLUDE(python.exe) исключает её
        # на уровне Session Manager до hardware микшера — эхо не возникает.
        #
        # SENIOR FIX: для устройств с частотой дискретизации ≠ 48kHz
        # используем scipy.signal.resample_poly вместо np.interp.
        # np.interp — линейная интерполяция без low-pass фильтрации →
        # aliasing на музыке и стримах (FIX #50 уже применил то же самое
        # в audio_capture.py, здесь симметрично).
        if self._out_sr != SAMPLE_RATE:
            resampled = None
            try:
                from scipy.signal import resample_poly as _rp
                g = math.gcd(int(self._out_sr), int(SAMPLE_RATE))
                up   = int(self._out_sr) // g
                down = int(SAMPLE_RATE)  // g
                resampled = _rp(self.mix_buffer, up, down).astype(np.float32)
            except Exception:
                # Fallback: линейная интерполяция (если scipy недоступен).
                x_old = np.linspace(0.0, 1.0, CHUNK_SIZE,         dtype=np.float64)
                x_new = np.linspace(0.0, 1.0, self._out_blocksize, dtype=np.float64)
                resampled = np.interp(x_new, x_old, self.mix_buffer).astype(np.float32)
            outdata[:] = resampled[:frames].reshape(-1, 1)
        else:
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

    def start_whisper(self, target_uid: int, anonymous: bool = False):
        """
        Начинает шёпот к конкретному пользователю.
        Пока активно — голос кодируется и отправляется только ему (FLAG_WHISPER).
        Нормальные аудио-пакеты в комнату НЕ отправляются, остальные не слышат.

        :param target_uid: uid адресата (0 означает «нет шёпота»).
        :param anonymous: если True — пакет помечается FLAG_ANONYMOUS, сервер
            переписывает sender_uid в UDP-заголовке на ANONYMOUS_UID, и
            получатель видит «Аноним» вместо реального ника. Хост сервера
            по-прежнему видит реального отправителя — это ограничение
            client-server модели (см. FLAG_ANONYMOUS в config.py).

        ВАЖНО: _whisper_sequence инициализируется от my_sequence, а НЕ от 0.
        JitterBuffer получателя уже видел seq из нормального потока (my_sequence).
        Сброс в 0 → все шёпот-пакеты отбрасывались бы как seq <= last_seq.
        """
        self.whisper_target_uid = target_uid
        self._whisper_anonymous = bool(anonymous)
        self._whisper_sequence = self.my_sequence  # продолжаем seq без разрыва
        # Сбрасываем состояние sosfilt-фильтра чтобы шёпот каждого нового собеседника
        # начинался с чистого состояния (без «хвоста» от предыдущего шёпота).
        self._wlp_zi = sosfilt_zi(self._wlp_sos).astype(np.float64)
        # FIX race condition: сброс состояния фильтра через флаг, а не напрямую.
        # Прямой вызов _anon_history.fill(0) / _anon_phase=0 из UI-потока конкурирует
        # с audio_callback (PortAudio thread). numpy снимает GIL → torn read/write →
        # щелчки. Флаг — атомарный bool, audio_callback сбросит состояние сам.
        self._whisper_effect_reset = True
        print(f"[Audio] Whisper START → uid={target_uid}, anon={self._whisper_anonymous}, "
              f"seq_from={self._whisper_sequence}")

    def stop_whisper(self):
        """Останавливает шёпот, возвращает нормальную передачу в комнату.
        Синхронизируем my_sequence чтобы не было обратного прыжка seq."""
        # Переносим счётчик чтобы нормальные пакеты продолжили нумерацию
        # с того места, где остановился шёпот. Иначе получатели в комнате
        # увидят резкий откат seq и часть пакетов будет отброшена JitterBuffer.
        if self._whisper_sequence > self.my_sequence:
            self.my_sequence = self._whisper_sequence
        print(f"[Audio] Whisper STOP  (was → uid={self.whisper_target_uid}, "
              f"anon={self._whisper_anonymous}), seq_sync={self.my_sequence}")
        self.whisper_target_uid = 0
        self._whisper_anonymous = False

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

    def _remap_to_wasapi(self, mme_idx: int | None) -> int | None:
        try:
            apis = sd.query_hostapis()
            devices = sd.query_devices()

            wasapi_host_idx = next(
                (i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None
            )

            if mme_idx is None:
                if wasapi_host_idx is not None:
                    default_wasapi_out = apis[wasapi_host_idx]['default_output_device']
                    print(f"[Audio] Дефолтное устройство → WASAPI (idx {default_wasapi_out})")
                    return default_wasapi_out
                return None

            dev = devices[mme_idx]
            host_api = apis[dev['hostapi']]

            if 'MME' not in host_api['name']:
                return mme_idx  # уже не MME

            if wasapi_host_idx is None:
                print("[Audio] WASAPI недоступен, остаёмся на MME (возможно эхо)")
                return mme_idx

            default_wasapi = apis[wasapi_host_idx]['default_output_device']
            phys_name = dev['name']
            best_idx = None
            best_score = -1

            for i, d in enumerate(devices):
                if d['hostapi'] != wasapi_host_idx or d['max_output_channels'] <= 0:
                    continue
                wname = d['name']
                if wname == phys_name:
                    best_idx = i
                    break
                # MME обрезает имена до 31 символа — проверяем ОБА направления
                match_len = min(len(phys_name), len(wname), 10)
                if match_len >= 8 and (
                        wname.startswith(phys_name[:match_len]) or
                        phys_name.startswith(wname[:match_len])
                ):
                    score = match_len
                    if score > best_score:
                        best_score = score
                        best_idx = i

            if best_idx is not None:
                print(
                    f"[Audio] Output MME→WASAPI: "
                    f"idx {mme_idx}→{best_idx} "
                    f"'{phys_name}' → '{devices[best_idx]['name']}'"
                )
                return best_idx

            # ── КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: нет совпадения → всё равно форсируем WASAPI ──
            # Возврат mme_idx здесь = DLL не может исключить процесс = эхо гарантировано.
            print(
                f"[Audio] WASAPI-аналог для '{phys_name}' не найден. "
                f"Форсируем дефолтный WASAPI (idx {default_wasapi})"
            )
            return default_wasapi  # ← единственная важная строка

        except Exception as e:
            print(f"[Audio] _remap_to_wasapi error: {e}")
            return mme_idx

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