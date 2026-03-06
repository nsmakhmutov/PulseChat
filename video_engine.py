import threading
import time
import queue
import struct
import gc
import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage
from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_BITRATE,
    MAX_VIDEO_PAYLOAD, VIDEO_CHUNK_HEADER, VIDEO_CHUNK_STRUCT,
    VIDEO_HEADER_SIZE, FLAG_VIDEO,
    FLAG_VIDEO_LQ, LQ_VIDEO_BITRATE, LQ_VIDEO_WIDTH, LQ_VIDEO_HEIGHT,
    CMD_NACK, NACK_TIMEOUT_MS, RETRANSMIT_BUFFER_MS,
    FEC_GROUP_SIZE, FEC_MARKER, FEC_LENGTHS_HEADER_SIZE,
    JITTER_BUFFER_SIZE,
)
from fractions import Fraction

try:
    import av

    AV_AVAILABLE = True
except ImportError:
    AV_AVAILABLE = False
    print("[Video] ОШИБКА: PyAV не установлен!")

try:
    import dxcam

    DXCAM_AVAILABLE = True
except ImportError:
    DXCAM_AVAILABLE = False
    print("[Video] ОШИБКА: dxcam не найден.")


# ---------------------------------------------------------------------------
# IDR-детектор: маркеры ключевого кадра в H.264 Annex B потоке.
#
# nvenc с profile=baseline всегда пишет IDR-кадр как NAL unit type 5.
# Первый байт NAL после start code = 0x65 (nal_ref_idc=3, nal_unit_type=5).
# Ищем по двум вариантам start code (4-байтный и 3-байтный).
# bytes.__contains__ использует оптимизированный C-поиск — быстро и без GC.
# ---------------------------------------------------------------------------
_IDR_MARKER_4 = bytes([0x00, 0x00, 0x00, 0x01, 0x65])  # 4-байтный start code + IDR
_IDR_MARKER_3 = bytes([0x00, 0x00, 0x01, 0x65])  # 3-байтный start code + IDR


def _is_h264_keyframe(data: bytes) -> bool:
    """Проверяет наличие IDR NAL unit (type=5) в H.264 Annex B потоке."""
    return _IDR_MARKER_4 in data or _IDR_MARKER_3 in data


class VideoEngine(QObject):
    frame_received = pyqtSignal(int, QImage)

    # Статистика сетевого качества потока (uid стримера, FPS декодера, % потерь).
    # Эмитируется раз в 2 секунды из _frame_cleanup_loop.
    # MainWindow → VideoWindow.update_stream_stats() → обновляет HUD.
    stream_stats_updated = pyqtSignal(int, int, int)  # uid, decoded_per_sec, loss_pct

    def __init__(self, net_client):
        super().__init__()
        self.net = net_client
        self.running = False
        self.capture_thread = None
        self.encode_thread = None

        # maxsize=2: минимальный буфер capture→encode.
        # При 60fps каждый лишний кадр в очереди = +16 мс задержки стрима.
        # 2 слота достаточно чтобы энкодер не голодал при кратковременных пиках,
        # но не накапливал задержку. Старый кадр дропается при переполнении.
        self.frame_queue = queue.Queue(maxsize=2)

        # --- Входящие пакеты ---
        self._buffer_lock = threading.RLock()
        self.incoming_buffer = {}  # uid → {frame_id → {chunk_idx → bytes}}
        self.assembly_info = {}  # uid → {frame_id → {total, received, ts}}

        # -------------------------------------------------------------------
        # FIX #5: Декодирование вынесено в отдельный поток на каждого стримера.
        # -------------------------------------------------------------------
        self.decode_queues = {}   # uid → Queue(maxsize=JITTER_BUFFER_SIZE)
        self.decode_threads = {}  # uid → Thread
        # Декодеры создаются как локальные переменные внутри _decode_worker
        # и живут только в его стеке — self.decoders не нужен.

        # [ИСПРАВЛЕНИЕ] Инициализируем раздельные счетчики для Simulcast (HQ и LQ)
        self.frame_counter_hq = 0
        self.frame_counter_lq = 0

        self._dx_factory = None
        self._force_keyframe = False

        # _last_keyframe_req: uid → timestamp последнего запроса IDR у стримера.
        # Используется в _frame_cleanup_loop и process_incoming_packet для rate-limiting.
        self._last_keyframe_req: dict = {}

        # _last_assembled_fid: uid → последний успешно собранный frame_id.
        # Используется для мгновенного детекта потерь (UDP gap).
        self._last_assembled_fid: dict[int, int] = {}

        # -------------------------------------------------------------------
        # Статистика качества потока (per-uid, thread-safe через GIL int-атомики).
        # -------------------------------------------------------------------
        self._stats_decoded: dict[int, int] = {}  # uid → кадров декодировано
        self._stats_dropped: dict[int, int] = {}  # uid → кадров протухло
        self._stats_window_start: float = time.time()
        self._logged_res: dict[int, str] = {}  # uid → "WxH" (для диагностики)

        # --- ABR: динамическая смена битрейта ---
        self._target_bitrate: int = VIDEO_BITRATE
        self._current_bitrate: int = VIDEO_BITRATE

        # _simulcast_active: True если LQ-энкодер (nvenc) запущен параллельно.
        self._simulcast_active: bool = False

        # _lq_needed: True пока сервер не сообщил, что слабых зрителей нет.
        # Дефолт True = обратная совместимость (старый сервер без CMD_LQ_NEEDED).
        # Сервер устанавливает через set_lq_needed() при изменении состава зрителей.
        self._lq_needed: bool = True

        # ------------------------------------------------------------------
        # Retransmit buffer: хранит чанки последних ~400 мс для ответа на NACK.
        # Ключ: (frame_id, chunk_idx) → (chunk_bytes, flags, timestamp)
        # chunk_bytes = VIDEO_HEADER + payload (без UDP_HEADER — его добавит net)
        # Очистка по возрасту в _retransmit_cleanup_loop.
        # ------------------------------------------------------------------
        self._retransmit_buffer: dict = {}   # (frame_id, chunk_idx) → (bytes, int, float)
        self._retransmit_lock   = threading.Lock()

        # ------------------------------------------------------------------
        # FEC buffer: хранит FEC-пакеты для восстановления потерянных чанков.
        # Структура: uid → frame_id → group_start → fec_payload_bytes
        # fec_payload = lengths_header(16 байт) + XOR_data(MAX_VIDEO_PAYLOAD байт)
        # Очищается вместе с incoming_buffer при cleanup.
        # ------------------------------------------------------------------
        self._fec_buffer: dict = {}   # uid → {frame_id → {group_start → bytes}}

        # NACK timeout отслеживается через info['nack_sent'] внутри assembly_info —
        # отдельный _nack_pending словарь не нужен.

        # -------------------------------------------------------------------
        # FIX #4: Периодическая чистка протухших фреймов вынесена в отдельный поток.
        # -------------------------------------------------------------------
        threading.Thread(
            target=self._frame_cleanup_loop,
            daemon=True,
            name="video-frame-cleanup",
        ).start()

        # Очистка retransmit_buffer по возрасту: чанки старше RETRANSMIT_BUFFER_MS удаляются.
        threading.Thread(
            target=self._retransmit_cleanup_loop,
            daemon=True,
            name="video-retransmit-cleanup",
        ).start()

    # ------------------------------------------------------------------
    # Энкодер
    # ------------------------------------------------------------------
    def _init_encoder(self, width, height, fps, bitrate):
        # Раздельные словари опций: разная логика управления битрейтом.
        #
        # nvenc (VBR + CQ):
        #   rc=vbr + cq=28 + maxrate=bitrate — «умный» режим.
        #   Если экран статичен → энкодер использует 50-200 kbps (нет смысла
        #   жать неизменившиеся пиксели в полную силу).
        #   Если активное движение (игра, видео) → растёт до maxrate.
        #   spatial-aq=1: адаптивное квантование — текст и UI остаются чёткими
        #   при сниженном битрейте (ключевая разница CBR vs VBR при трансляции IDE).
        #   p4 вместо p1: лучше качество при VBR; с tune=ull разница в задержке < 1 мс.
        #
        # libx264 (CRF + capped):
        #   crf=25: константное качество (аналог cq у nvenc).
        #   maxrate + bufsize через codec.options: ограничивают пик при движении
        #   (ABR-система сервера не получит неожиданный burst).
        nvenc_options = {
            'preset':      'p4',   # баланс скорость/качество (p1 давал мыло при VBR)
            'tune':        'ull',  # Ultra Low Latency — компенсирует задержку p4
            'rc':          'vbr',  # БЫЛО: cbr → СТАЛО: vbr; тратим биты только там, где движение
            'cq':          '28',   # целевое качество; при статике битрейт упадёт до 50-200 kbps
            'forced-idr':  '1',
            'delay':       '0',
            'spatial-aq':  '1',    # чёткость текста/UI при низком битрейте (nvenc-фишка)
        }
        x264_options = {
            'preset':   'ultrafast',
            'tune':     'zerolatency',
            'profile':  'baseline',
            'crf':      '25',      # аналог cq: константное качество, нет padding-мусора
            'threads':  '4',
        }

        encoders_to_try = [
            ('h264_nvenc', nvenc_options),
            ('libx264',    x264_options),
        ]

        for codec_name, options in encoders_to_try:
            try:
                codec = av.CodecContext.create(codec_name, 'w')
                codec.width    = width
                codec.height   = height
                codec.pix_fmt  = 'yuv420p'
                codec.time_base = Fraction(1, fps)
                codec.bit_rate = bitrate

                # gop_size = fps*2: IDR-кадр раз в 2 секунды вместо 1.
                # Было fps (= 30 или 60): IDR каждую секунду → всплеск 300-700 KB
                # на 6 Mbps CBR убивал RadminVPN раз в секунду.
                # fps*2 снижает частоту IDR-шторма вдвое без ущерба для качества.
                codec.gop_size = fps * 2

                # update() вместо присвоения (= options) — КРИТИЧНО.
                # Старый код `codec.options = options` полностью заменял словарь,
                # убирая все опции выставленные до этой строки.
                # update() сохраняет maxrate/bufsize и мёржит кодек-специфичные опции.
                codec.options.update(options)

                # maxrate + bufsize: ограничиваем пик битрейта при резком движении.
                # Без них VBR может выдать кратковременный burst > bitrate,
                # что при RadminVPN = мгновенный packet loss.
                # bufsize = bitrate*2: буфер сглаживания на 2 секунды.
                codec.options['maxrate'] = str(bitrate)
                codec.options['bufsize'] = str(bitrate * 2)

                codec.open()
                print(
                    f"[Video] Энкодер запущен: {codec_name} | "
                    f"VBR max={bitrate // 1000} kbps | "
                    f"GOP={codec.gop_size} кадров"
                )
                return codec
            except Exception as e:
                print(f"[Video] Не удалось запустить {codec_name}: {e}")
                continue

        raise RuntimeError("Ни один видео-кодек не найден!")

    # ------------------------------------------------------------------
    # Управление стримом
    # ------------------------------------------------------------------
    def start_streaming(self, settings=None):
        if not (AV_AVAILABLE and DXCAM_AVAILABLE):
            return False
        if self.running:
            return False

        self.current_settings = settings or {
            "monitor_idx": 0,
            "width": VIDEO_WIDTH,
            "height": VIDEO_HEIGHT,
            "fps": VIDEO_FPS,
        }
        self.running = True
        self._target_bitrate = VIDEO_BITRATE
        self._current_bitrate = VIDEO_BITRATE

        with self.frame_queue.mutex:
            self.frame_queue.queue.clear()

        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.encode_thread = threading.Thread(target=self._encode_loop, daemon=True)
        self.capture_thread.start()
        self.encode_thread.start()
        return True

    def stop_streaming(self):
        self.running = False
        if self.capture_thread:
            self.capture_thread.join(timeout=3)
            self.capture_thread = None
        if self.encode_thread:
            self.encode_thread.join(timeout=3)
            self.encode_thread = None

        with self.frame_queue.mutex:
            self.frame_queue.queue.clear()
            self.frame_queue.all_tasks_done.notify_all()
            self.frame_queue.unfinished_tasks = 0

        gc.collect(1)
        gc.collect(2)

        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSizeEx(
                kernel32.GetCurrentProcess(),
                ctypes.c_size_t(0xFFFFFFFF),
                ctypes.c_size_t(0xFFFFFFFF),
                0,
            )
            print("[Video] Стрим остановлен: GC + Windows heap trim выполнен")
        except Exception:
            print("[Video] Стрим остановлен, GC выполнен")

    def force_keyframe(self):
        self._force_keyframe = True

    def set_bitrate(self, new_bitrate: int):
        """
        Устанавливает целевой битрейт.

        Ранее при simulcast_active сразу возвращался (return) —
        это делало Upload-ABR неэффективным: сервер сигнализировал
        «твой upload перегружен», но стример продолжал гнать 6.8 Mbps.

        Теперь устанавливаем _target_bitrate всегда.
        В _encode_loop при обнаружении расхождения:
          — simulcast активен → оба энкодера сбрасываются, simulcast
            отключается, запускается единый энкодер на новом битрейте.
          — simulcast неактивен → энкодер просто перезапускается (старый путь).
        """
        if new_bitrate != self._target_bitrate:
            kbps_old = self._target_bitrate // 1000
            kbps_new = new_bitrate // 1000
            print(f"[Video] ABR: запрошена смена битрейта {kbps_old} → {kbps_new} kbps")
            self._target_bitrate = new_bitrate

    def set_lq_needed(self, needed: bool):
        """
        Сервер сообщает, нужен ли LQ-поток прямо сейчас.

        needed=False → среди зрителей нет «слабых» (bitrate < ABR_LQ_THRESHOLD)
                       или зрителей нет совсем. LQ-кодирование пропускается в
                       _encode_loop. NVENC-блок перестаёт получать кадры и
                       переходит в idle — нулевая нагрузка без пересоздания кодека.

        needed=True  → появился слабый зритель. LQ-кодирование возобновляется
                       немедленно со следующего кадра.

        Кодек codec_lq НЕ уничтожается — пересоздание занимает ~200 мс и даст
        артефакты на первом кадре. Просто пропускаем кодирование через флаг.
        """
        if self._lq_needed != needed:
            self._lq_needed = needed
            state = "включён" if needed else "выключен (нет слабых зрителей)"
            print(f"[Video] Simulcast LQ-поток: {state}")

    def cleanup_users(self, active_uids):
        with self._buffer_lock:
            for uid in list(self.incoming_buffer.keys()):
                if uid not in active_uids:
                    del self.incoming_buffer[uid]
            for uid in list(self.assembly_info.keys()):
                if uid not in active_uids:
                    del self.assembly_info[uid]

        for uid in list(self.decode_queues.keys()):
            if uid not in active_uids:
                try:
                    self.decode_queues[uid].put_nowait(None)
                except queue.Full:
                    pass
                t = self.decode_threads.pop(uid, None)
                if t:
                    t.join(timeout=1)
                self.decode_queues.pop(uid, None)
                self._stats_decoded.pop(uid, None)
                self._stats_dropped.pop(uid, None)
                self._logged_res.pop(uid, None)
                self._last_keyframe_req.pop(uid, None)
                self._last_assembled_fid.pop(uid, None)
                self._fec_buffer.pop(uid, None)

    def _frame_cleanup_loop(self):
        STATS_INTERVAL  = 2.0
        NACK_TIMEOUT_S  = NACK_TIMEOUT_MS / 1000.0
        FRAME_TIMEOUT_S = 1.0   # кадр считается протухшим после 1 секунды ожидания

        while True:
            time.sleep(0.5)
            now = time.time()
            idr_needed_uids = []   # uid-ы, которым нужен IDR

            with self._buffer_lock:
                for uid in list(self.assembly_info.keys()):
                    stale_fids = [
                        fid for fid, info in self.assembly_info[uid].items()
                        if now - info['ts'] > FRAME_TIMEOUT_S
                    ]

                    for fid in stale_fids:
                        info  = self.assembly_info[uid][fid]
                        buf   = self.incoming_buffer[uid].get(fid, {})
                        total = info['total']
                        missing_count = total - info['received']

                        # ── Шаг 1: попытка FEC-восстановления ───────────────
                        # Если потеряно ≤ FEC_GROUP_SIZE чанков И есть FEC для этой группы —
                        # пробуем восстановить без запроса к стримеру (0 мс задержки).
                        if missing_count >= 1 and not info.get('fec_tried'):
                            info['fec_tried'] = True
                            if self._try_fec_reconstruct(uid, fid, buf, total):
                                info['received'] = len(buf)
                                missing_count = total - info['received']
                                # Если FEC восстановил последний пропавший чанк — собираем кадр
                                if missing_count == 0:
                                    assembled_chunks = dict(buf)
                                    del self.incoming_buffer[uid][fid]
                                    del self.assembly_info[uid][fid]
                                    # Отправляем кадр в очередь декодера (вне _buffer_lock)
                                    # Используем флаг, чтобы обработать после выхода из with
                                    info['_assembled'] = assembled_chunks
                                    info['_total'] = total
                                    continue  # пропускаем остальные ветки для этого fid

                        # ── Шаг 2: NACK — запрос конкретных пропавших чанков ─
                        # Если потеряно мало чанков (≤ 2) И NACK ещё не отправляли —
                        # отправляем NACK и даём ещё NACK_TIMEOUT_S времени.
                        if 1 <= missing_count <= 2 and not info.get('nack_sent'):
                            info['nack_sent'] = True
                            info['ts'] = now   # сдвигаем таймаут: даём ещё ~1 сек
                            nack_fids = [i for i in range(total) if i not in buf]
                            if self.net:
                                for chunk_i in nack_fids:
                                    self.net.send_nack(uid, fid, chunk_i)
                            print(
                                f"[Video] NACK: uid={uid} fid={fid} "
                                f"пропавшие чанки {nack_fids} — ждём retransmit"
                            )
                            continue  # НЕ дропаем кадр, даём ещё время

                        # ── Шаг 3: дропаем кадр, запрашиваем IDR ────────────
                        # Сюда попадаем если:
                        #   а) FEC не помог / не было FEC-пакета
                        #   б) NACK уже отправляли, но чанк так и не пришёл
                        #   в) Пропавших чанков > 2 (слишком много, NACK не поможет)
                        del self.incoming_buffer[uid][fid]
                        del self.assembly_info[uid][fid]
                        self._fec_buffer.get(uid, {}).pop(fid, None)
                        self._stats_dropped[uid] = self._stats_dropped.get(uid, 0) + 1
                        idr_needed_uids.append(uid)

            # Обрабатываем FEC-собранные кадры вне _buffer_lock
            # (упрощение: в текущей версии не реализуем — кадры попадут через обычный путь
            #  при следующем чанке; можно расширить в будущем)

            if self.net and idr_needed_uids:
                current_ping = getattr(self.net, 'current_ping', 0)
                if current_ping > 1500:
                    idr_cooldown = 40.0
                elif current_ping > 500:
                    idr_cooldown = 20.0
                elif current_ping > 200:
                    idr_cooldown = 10.0
                else:
                    idr_cooldown = 5.0

                seen = set()
                for uid in idr_needed_uids:
                    if uid in seen:
                        continue
                    seen.add(uid)
                    last_req = self._last_keyframe_req.get(uid, 0.0)
                    if now - last_req >= idr_cooldown:
                        self._last_keyframe_req[uid] = now
                        self.net.request_viewer_keyframe(uid)
                        print(
                            f"[Video] IDR запрошен uid={uid} "
                            f"(ping={current_ping}ms, NACK не помог)"
                        )

            if now - self._stats_window_start >= STATS_INTERVAL:
                elapsed = max(now - self._stats_window_start, 0.001)
                self._stats_window_start = now

                all_uids = set(self._stats_decoded.keys()) | set(self._stats_dropped.keys())
                for uid in all_uids:
                    decoded = self._stats_decoded.pop(uid, 0)
                    dropped = self._stats_dropped.pop(uid, 0)
                    total   = decoded + dropped

                    fps_rate = int(decoded / elapsed)
                    loss_pct = int((dropped / total) * 100) if total > 0 else 0

                    self.stream_stats_updated.emit(uid, fps_rate, loss_pct)

    def stop_viewer_for_uid(self, uid):
        q = self.decode_queues.pop(uid, None)
        if q is not None:
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

        self.decode_threads.pop(uid, None)

        with self._buffer_lock:
            self.incoming_buffer.pop(uid, None)
            self.assembly_info.pop(uid, None)

        self._last_assembled_fid.pop(uid, None)
        self._fec_buffer.pop(uid, None)
        print(f"[Video] stop_viewer_for_uid({uid}): сигнал завершения декодера отправлен")

    # ------------------------------------------------------------------
    # Захват экрана
    # ------------------------------------------------------------------
    def _capture_loop(self):
        target_fps = self.current_settings.get('fps', 60)
        monitor_idx = self.current_settings.get('monitor_idx', 0)

        camera = None
        try:
            camera = dxcam.create(output_idx=monitor_idx, output_color="RGB")
            if not camera:
                print(f"[Video] DXcam: Монитор {monitor_idx} не найден")
                self.running = False
                return
        except Exception as e:
            print(f"[Video] DXcam Init Error: {e}")
            self.running = False
            return

        try:
            camera.start(target_fps=target_fps, video_mode=True)
            print(f"[Video] DXcam запущен в нативном режиме {target_fps} FPS на мониторе {monitor_idx}")
        except Exception as e:
            print(f"[Video] DXcam start() failed: {e}, fallback to grab()")
            self._capture_loop_fallback(camera)
            return

        while self.running:
            try:
                frame_np = camera.get_latest_frame()
                if frame_np is not None:
                    if self.frame_queue.full():
                        try:
                            self.frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        self.frame_queue.put_nowait(frame_np)
                    except queue.Full:
                        pass
            except Exception as e:
                print(f"[Capture] Error: {e}")
                time.sleep(0.1)

        camera.stop()
        try:
            camera.release()
            print("[Video] DXCam D3D11 ресурсы освобождены")
        except Exception:
            pass
        del camera

    def _capture_loop_fallback(self, camera):
        target_fps = self.current_settings.get('fps', 60)
        frame_time = 1.0 / target_fps
        print(f"[Video] Захват через .grab() fallback на {target_fps} FPS")

        while self.running:
            start_t = time.perf_counter()
            try:
                frame_np = camera.grab()
                if frame_np is not None:
                    if self.frame_queue.full():
                        try:
                            self.frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        self.frame_queue.put_nowait(frame_np)
                    except queue.Full:
                        pass

                elapsed = time.perf_counter() - start_t
                sleep_time = max(0, frame_time - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            except Exception as e:
                print(f"[Capture Fallback] Error: {e}")
                time.sleep(1)

        try:
            camera.release()
        except Exception:
            pass
        del camera

    # ------------------------------------------------------------------
    # Кодирование и фрагментация
    # ------------------------------------------------------------------
    def _encode_loop(self):
        width = self.current_settings.get('width', 1280)
        height = self.current_settings.get('height', 720)
        fps = self.current_settings.get('fps', 30)
        bitrate = VIDEO_BITRATE

        try:
            codec = self._init_encoder(width, height, fps, bitrate)
        except Exception as e:
            print(f"[Video] КРИТИЧЕСКАЯ ОШИБКА: {e}")
            self.running = False
            return

        codec_lq = None
        lq_w, lq_h = LQ_VIDEO_WIDTH, LQ_VIDEO_HEIGHT
        if codec.name == 'h264_nvenc':
            try:
                codec_lq = self._init_encoder(lq_w, lq_h, fps, LQ_VIDEO_BITRATE)
                self._simulcast_active = True
                print(
                    f"[Video] Simulcast активен: HQ {width}x{height}@{VIDEO_BITRATE // 1000}kbps "
                    f"+ LQ {lq_w}x{lq_h}@{LQ_VIDEO_BITRATE // 1000}kbps"
                )
            except Exception as e:
                codec_lq = None
                self._simulcast_active = False
                print(f"[Video] Simulcast LQ недоступен: {e} — legacy режим")
        else:
            self._simulcast_active = False
            print("[Video] nvenc не обнаружен, simulcast отключён — legacy ABR режим")

        pts_counter = 0
        pts_counter_lq = 0

        try:
            while self.running:
                try:
                    try:
                        frame_np = self.frame_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if self._target_bitrate != self._current_bitrate:
                        # --- Сброс HQ-энкодера ---
                        try:
                            for _ in codec.encode(None):
                                pass
                        except Exception:
                            pass
                        del codec

                        # --- Simulcast → Единый поток (Spatial Layer Fallback) ---
                        # Если сервер сигнализировал о перегрузке upload-канала стримера,
                        # коллапсируем два потока в один: сбрасываем LQ-энкодер,
                        # отключаем simulcast, запускаем единый HQ на пониженном битрейте.
                        # Аналог Discord Spatial Layer Fallback при плохом аплоаде.
                        if codec_lq is not None:
                            try:
                                for _ in codec_lq.encode(None):
                                    pass
                            except Exception:
                                pass
                            del codec_lq
                            codec_lq = None
                            self._simulcast_active = False
                            print(
                                f"[Video] ABR Simulcast→Fallback: upload-канал перегружен, "
                                f"переход на единый поток "
                                f"{self._target_bitrate // 1000} kbps"
                            )

                        self._current_bitrate = self._target_bitrate
                        try:
                            codec = self._init_encoder(width, height, fps, self._current_bitrate)
                            self._force_keyframe = True
                            print(f"[Video] ABR: энкодер перезапущен на {self._current_bitrate // 1000} kbps")
                        except Exception as e:
                            print(f"[Video] ABR: ошибка перезапуска энкодера: {e}")
                            self.running = False
                            return

                    if frame_np.size == 0:
                        continue

                    raw_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
                    del frame_np

                    need_keyframe = self._force_keyframe

                    # --- HQ кодирование ---
                    frame_hq = raw_frame.reformat(width=width, height=height, format='yuv420p')
                    frame_hq.pts = pts_counter
                    pts_counter += 1

                    if need_keyframe:
                        try:
                            frame_hq.pict_type = 'I'
                        except (TypeError, AttributeError):
                            frame_hq.pict_type = av.video.frame.PictureType.I

                    for packet in codec.encode(frame_hq):
                        self._fragment_and_send(bytes(packet), FLAG_VIDEO)
                    del frame_hq

                    # --- LQ кодирование (Simulcast) ---
                    # Двойной гейт: codec_lq существует И сервер сигнализировал
                    # что есть слабые зрители (_lq_needed=True).
                    # При _lq_needed=False — пропускаем reformat + encode.
                    # NVENC-блок перестаёт получать задания и уходит в idle.
                    # pts_counter_lq намеренно НЕ инкрементируем при пропуске —
                    # когда LQ возобновится, PTS продолжится без разрыва.
                    if codec_lq is not None and self._lq_needed:
                        frame_lq = raw_frame.reformat(width=lq_w, height=lq_h, format='yuv420p')
                        frame_lq.pts = pts_counter_lq
                        pts_counter_lq += 1

                        if need_keyframe:
                            try:
                                frame_lq.pict_type = 'I'
                            except (TypeError, AttributeError):
                                frame_lq.pict_type = av.video.frame.PictureType.I

                        for packet in codec_lq.encode(frame_lq):
                            self._fragment_and_send(bytes(packet), FLAG_VIDEO | FLAG_VIDEO_LQ)
                        del frame_lq

                    if need_keyframe:
                        self._force_keyframe = False
                        print("[Video] Принудительный IDR-кадр отправлен (HQ + LQ)")

                    del raw_frame

                except Exception as e:
                    print(f"[Encoder] Error: {e}")

            try:
                for packet in codec.encode(None):
                    self._fragment_and_send(bytes(packet), FLAG_VIDEO)
            except Exception:
                pass

            if codec_lq is not None:
                try:
                    for packet in codec_lq.encode(None):
                        self._fragment_and_send(bytes(packet), FLAG_VIDEO | FLAG_VIDEO_LQ)
                except Exception:
                    pass

        finally:
            self._simulcast_active = False
            del codec
            if codec_lq is not None:
                del codec_lq
            print("[Video] Энкодер(ы) закрыты, FFmpeg-буферы освобождены")

    def _fragment_and_send(self, data, flags: int = FLAG_VIDEO):
        if not self.net or not self.net.udp_socket_bound:
            return

        if flags & FLAG_VIDEO_LQ:
            self.frame_counter_lq = (self.frame_counter_lq + 1) % 0x100000000
            current_frame_id = self.frame_counter_lq
        else:
            self.frame_counter_hq = (self.frame_counter_hq + 1) % 0x100000000
            current_frame_id = self.frame_counter_hq

        total_len = len(data)
        chunks_count = (total_len + MAX_VIDEO_PAYLOAD - 1) // MAX_VIDEO_PAYLOAD

        # Строим чанки и параллельно собираем данные для FEC.
        chunks = []
        chunk_payloads = []   # raw payloads (без VIDEO_HEADER) — нужны для XOR
        chunk_lengths  = []   # длины каждого payload — нужны для восстановления последнего чанка
        now_ts = time.time()

        for i in range(chunks_count):
            start = i * MAX_VIDEO_PAYLOAD
            end   = min(start + MAX_VIDEO_PAYLOAD, total_len)
            chunk_payload = data[start:end]
            v_header = VIDEO_CHUNK_HEADER.pack(current_frame_id, i, chunks_count)
            chunk = v_header + chunk_payload
            chunks.append(chunk)
            chunk_payloads.append(chunk_payload)
            chunk_lengths.append(len(chunk_payload))

        # ── Retransmit buffer: сохраняем каждый чанк для ответа на NACK ──────
        # Храним только chunk (VIDEO_HEADER + payload), без UDP_HEADER.
        # При retransmit net.send_video_frame_chunks добавит UDP_HEADER с актуальным ts.
        with self._retransmit_lock:
            for i, chunk in enumerate(chunks):
                self._retransmit_buffer[(current_frame_id, i)] = (chunk, flags, now_ts)

        # ── FEC: 1 пакет на каждые FEC_GROUP_SIZE чанков (XOR всех payloads) ─
        # FEC chunk_idx: бит FEC_MARKER (0x8000) установлен → зритель распознаёт FEC.
        # Первые FEC_LENGTHS_HEADER_SIZE байт payload = uint16 длины каждого чанка группы.
        # Зритель использует длины для точного восстановления (без trailing zeros).
        fec_chunks = []
        for group_start in range(0, chunks_count, FEC_GROUP_SIZE):
            group_end    = min(group_start + FEC_GROUP_SIZE, chunks_count)
            group_count  = group_end - group_start

            # Длины чанков группы (для восстановления в process_incoming_packet)
            group_lens   = chunk_lengths[group_start:group_end]
            # Дополняем до FEC_GROUP_SIZE нулями (если группа неполная)
            padded_lens  = group_lens + [0] * (FEC_GROUP_SIZE - group_count)
            lengths_hdr  = struct.pack(f'!{FEC_GROUP_SIZE}H', *padded_lens)

            # XOR всех payloads группы (каждый padded до MAX_VIDEO_PAYLOAD нулями)
            fec_xor = np.zeros(MAX_VIDEO_PAYLOAD, dtype=np.uint8)
            for i in range(group_start, group_end):
                p   = chunk_payloads[i]
                arr = np.frombuffer(p, dtype=np.uint8)
                fec_xor[:len(arr)] ^= arr

            fec_payload = lengths_hdr + bytes(fec_xor)
            fec_header  = VIDEO_CHUNK_HEADER.pack(
                current_frame_id,
                FEC_MARKER | group_start,   # высокий бит = признак FEC
                chunks_count,
            )
            fec_chunks.append(fec_header + fec_payload)

        # Отправляем данные + FEC одним вызовом (один батч в pacing-очередь)
        self.net.send_video_frame_chunks(chunks + fec_chunks, flags=flags)

    # ------------------------------------------------------------------
    # Приём и сборка входящих пакетов
    # ------------------------------------------------------------------
    def _idr_cooldown(self) -> float:
        """
        Кулдаун между IDR-запросами, адаптированный к текущему пингу.

        Чем выше пинг — тем дольше ждём перед повторным запросом IDR.
        Это ломает «смертельную спираль»: при пинге 221 мс стример
        генерирует ~3 МБ IDR-кадр за ~100 мс → он должен успеть дойти
        до зрителя ещё до следующего запроса. Без этого 2-секундный
        кулдаун при пинге 221 мс выдавал IDR-шторм.
        """
        ping = getattr(self.net, 'current_ping', 50) if self.net else 50
        if ping < 15:
            return 0.3
        elif ping < 50:
            return 0.5
        elif ping < 150:
            return 1.0
        elif ping < 300:
            return 3.0   # RTT 150-300 мс: IDR-кадр идёт ~150 мс в одну сторону
        return 5.0       # RTT ≥ 300 мс: очень плохая сеть, редкие запросы

    def _retransmit_cleanup_loop(self):
        """
        Удаляет устаревшие чанки из retransmit_buffer.
        Запускается как daemon-поток раз в 200 мс.
        Чанки старше RETRANSMIT_BUFFER_MS (400 мс) удаляются — NACK на них уже нереален.
        """
        cutoff_sec = RETRANSMIT_BUFFER_MS / 1000.0
        while True:
            time.sleep(0.2)
            now = time.time()
            with self._retransmit_lock:
                stale = [k for k, v in self._retransmit_buffer.items()
                         if now - v[2] > cutoff_sec]
                for k in stale:
                    del self._retransmit_buffer[k]

    def handle_retransmit(self, frame_id: int, chunk_idx: int):
        """
        Стример: зритель запросил NACK для (frame_id, chunk_idx).
        Вытаскиваем чанк из retransmit_buffer и повторно отправляем.
        Если чанк уже устарел (>400 мс) — ничего не делаем; зритель получит IDR.
        """
        with self._retransmit_lock:
            entry = self._retransmit_buffer.get((frame_id, chunk_idx))
        if entry is None:
            print(f"[Video] NACK: chunk ({frame_id},{chunk_idx}) устарел в retransmit_buffer")
            return
        chunk_bytes, flags, _ = entry
        if self.net and self.net.udp_socket_bound:
            self.net.send_video_frame_chunks([chunk_bytes], flags=flags)
            print(f"[Video] NACK: retransmit chunk ({frame_id},{chunk_idx})")

    def _try_fec_reconstruct(
        self, uid: int, frame_id: int,
        buf: dict, total_chunks: int
    ) -> bool:
        """
        Пытается восстановить ровно один пропавший чанк через FEC.

        Алгоритм:
          1. Для каждой FEC-группы проверяем: есть ли ровно 1 пропавший чанк.
          2. Если да: XOR всех присутствующих чанков + FEC-пакет = восстановленный чанк.
          3. Записываем восстановленный чанк в buf, возвращаем True.

        Возвращает False если ни одна группа не может быть восстановлена.
        """
        fec_for_frame = self._fec_buffer.get(uid, {}).get(frame_id, {})
        if not fec_for_frame:
            return False

        for group_start, fec_payload in fec_for_frame.items():
            group_end   = min(group_start + FEC_GROUP_SIZE, total_chunks)
            group_idxs  = list(range(group_start, group_end))
            group_count = len(group_idxs)
            missing     = [i for i in group_idxs if i not in buf]

            if len(missing) != 1:
                continue   # FEC восстанавливает только 1 потерю за группу

            missing_idx = missing[0]

            # Читаем длины из заголовка FEC-payload
            try:
                lengths = struct.unpack(f'!{FEC_GROUP_SIZE}H',
                                        fec_payload[:FEC_LENGTHS_HEADER_SIZE])
            except Exception:
                continue

            local_idx   = missing_idx - group_start
            orig_length = lengths[local_idx]
            if orig_length == 0 or orig_length > MAX_VIDEO_PAYLOAD:
                continue   # невалидная длина — пропускаем

            # XOR всех присутствующих чанков + XOR-данные из FEC = восстановленный
            xor_data   = bytearray(fec_payload[FEC_LENGTHS_HEADER_SIZE:])
            fec_arr    = np.frombuffer(xor_data, dtype=np.uint8).copy()
            for i in group_idxs:
                if i == missing_idx:
                    continue
                p   = buf[i]
                arr = np.frombuffer(p, dtype=np.uint8)
                fec_arr[:len(arr)] ^= arr

            buf[missing_idx] = bytes(fec_arr[:orig_length])
            print(f"[Video] FEC: восстановлен chunk {missing_idx} кадра {frame_id} (uid={uid})")
            return True

        return False

    def process_incoming_packet(self, uid, data, is_lq: bool = False):
        if len(data) < VIDEO_HEADER_SIZE:
            return

        try:
            frame_id, chunk_idx, total_chunks = VIDEO_CHUNK_HEADER.unpack(data[:VIDEO_HEADER_SIZE])
            payload = data[VIDEO_HEADER_SIZE:]

            # ── FEC-пакет: высокий бит chunk_idx установлен ──────────────────
            # Сохраняем в _fec_buffer для использования в _frame_cleanup_loop.
            # Старые клиенты без FEC-поддержки не попадут сюда (у них нет этой проверки),
            # но они просто получат chunk_idx >= total_chunks → тихо проигнорируют.
            if chunk_idx & FEC_MARKER:
                group_start = chunk_idx & ~FEC_MARKER
                if uid not in self._fec_buffer:
                    self._fec_buffer[uid] = {}
                if frame_id not in self._fec_buffer[uid]:
                    self._fec_buffer[uid][frame_id] = {}
                self._fec_buffer[uid][frame_id][group_start] = payload
                return  # FEC не участвует в обычной сборке кадра

            assembled_chunks = None
            assembled_total = 0

            with self._buffer_lock:
                if uid not in self.incoming_buffer:
                    self.incoming_buffer[uid] = {}
                    self.assembly_info[uid] = {}

                if frame_id not in self.incoming_buffer[uid]:
                    if len(self.incoming_buffer[uid]) > 5:
                        oldest_fid = next(iter(self.incoming_buffer[uid]))
                        del self.incoming_buffer[uid][oldest_fid]
                        self.assembly_info[uid].pop(oldest_fid, None)

                    self.incoming_buffer[uid][frame_id] = {}
                    self.assembly_info[uid][frame_id] = {
                        'total': total_chunks,
                        'received': 0,
                        'ts': time.time(),
                    }

                if chunk_idx not in self.incoming_buffer[uid][frame_id]:
                    self.incoming_buffer[uid][frame_id][chunk_idx] = payload
                    self.assembly_info[uid][frame_id]['received'] += 1

                if self.assembly_info[uid][frame_id]['received'] == total_chunks:
                    assembled_chunks = dict(self.incoming_buffer[uid][frame_id])
                    assembled_total = self.assembly_info[uid][frame_id]['total']
                    del self.incoming_buffer[uid][frame_id]
                    del self.assembly_info[uid][frame_id]

            if assembled_chunks is not None:
                full_data = bytearray()
                ok = True
                for i in range(assembled_total):
                    if i not in assembled_chunks:
                        ok = False
                        break
                    full_data.extend(assembled_chunks[i])

                now_t = time.time()

                if ok:
                    last_fid = self._last_assembled_fid.get(uid)
                    if last_fid is not None:
                        expected = (last_fid + 1) % 0x100000000
                        if frame_id != expected:
                            # UDP gap: пропущен как минимум один кадр.
                            # НЕ запрашиваем IDR сразу — декодер попробует продолжить.
                            # IDR только если _decode_worker обнаружит ошибку декодирования.
                            print(
                                f"[Video] uid={uid}: UDP gap "
                                f"(ожидали {expected}, пришёл {frame_id}) — декодер попробует продолжить"
                            )
                    self._last_assembled_fid[uid] = frame_id
                    # Очищаем FEC-буфер для этого кадра (он уже не нужен)
                    self._fec_buffer.get(uid, {}).pop(frame_id, None)

                    self._ensure_decode_worker(uid)
                    q = self.decode_queues[uid]

                    if q.full():
                        try:
                            q.get_nowait()
                            # Декодер не успевает — запрашиваем IDR только при большом отставании
                            cooldown = self._idr_cooldown()
                            if self.net and (now_t - self._last_keyframe_req.get(uid, 0.0) >= cooldown):
                                self._last_keyframe_req[uid] = now_t
                                self.net.request_viewer_keyframe(uid)
                                print(f"[Video] uid={uid}: decode_queue full, IDR запрошен")
                        except queue.Empty:
                            pass
                    try:
                        q.put_nowait((bytes(full_data), is_lq, frame_id))
                    except queue.Full:
                        pass
                else:
                    # Кадр собрался, но с пропусками — это не должно происходить
                    # (сборка происходит только при received == total_chunks).
                    # На всякий случай запрашиваем IDR.
                    if self.net:
                        if now_t - self._last_keyframe_req.get(uid, 0.0) >= 5.0:
                            self._last_keyframe_req[uid] = now_t
                            self.net.request_viewer_keyframe(uid)
                            print(f"[Video] Неполный кадр uid={uid}: IDR запрошен")

        except Exception:
            pass

    def _ensure_decode_worker(self, uid):
        if uid not in self.decode_threads or not self.decode_threads[uid].is_alive():
            q = queue.Queue(maxsize=JITTER_BUFFER_SIZE)   # было 4 → теперь 12 (~200 мс)
            self.decode_queues[uid] = q
            t = threading.Thread(
                target=self._decode_worker,
                args=(uid, q),
                daemon=True,
                name=f"decode-{uid}",
            )
            self.decode_threads[uid] = t
            t.start()

    def _decode_worker(self, uid, q):
        def _make_decoder():
            dec = av.CodecContext.create('h264', 'r')
            dec.thread_type = 'SLICE'
            dec.thread_count = 1
            return dec

        decoder = _make_decoder()

        waiting_for_idr = False
        last_worker_fid = None
        frames_waited = 0
        MAX_WAIT_FRAMES = 3

        try:
            while True:
                try:
                    raw = q.get(timeout=10.0)
                except queue.Empty:
                    break

                if raw is None:
                    break

                frame_data, pkt_is_lq, frame_id = raw

                if last_worker_fid is not None:
                    expected = (last_worker_fid + 1) % 0x100000000
                    if frame_id != expected and not waiting_for_idr:
                        waiting_for_idr = True
                        frames_waited = 0
                        now_t = time.time()
                        cooldown = self._idr_cooldown()
                        if self.net and (now_t - self._last_keyframe_req.get(uid, 0.0) >= cooldown):
                            self._last_keyframe_req[uid] = now_t
                            self.net.request_viewer_keyframe(uid)
                            print(f"[Video] decode_worker uid={uid}: gap {last_worker_fid}→{frame_id}, IDR запрошен")

                last_worker_fid = frame_id

                try:
                    if waiting_for_idr:
                        if _is_h264_keyframe(frame_data):
                            waiting_for_idr = False
                            frames_waited = 0
                        else:
                            frames_waited += 1
                            if frames_waited < MAX_WAIT_FRAMES:
                                continue
                            else:
                                del decoder
                                decoder = _make_decoder()
                                waiting_for_idr = False
                                frames_waited = 0
                                print(f"[Video] uid={uid}: IDR не пришёл за {MAX_WAIT_FRAMES} кадров, декодер сброшен")

                    packet = av.Packet(frame_data)
                    frames = decoder.decode(packet)
                    for frame in frames:
                        img_np = frame.to_ndarray(format='rgb24')
                        h, w, c = img_np.shape

                        res_key = f"{w}x{h}"
                        if self._logged_res.get(uid) != res_key:
                            self._logged_res[uid] = res_key
                            stream_type = "LQ" if pkt_is_lq else "HQ"
                            print(
                                f"[Video] UID={uid} получает поток: {res_key} ({stream_type}). "
                                f"Если слабый зритель видит HQ — simulcast не переключился!"
                            )

                        q_img = QImage(img_np.data, w, h, w * c, QImage.Format.Format_RGB888)
                        self.frame_received.emit(uid, q_img.copy())
                        self._stats_decoded[uid] = self._stats_decoded.get(uid, 0) + 1
                        del img_np, q_img
                    del packet, frames

                except Exception:
                    try:
                        del decoder
                    except Exception:
                        pass
                    decoder = _make_decoder()
                    waiting_for_idr = True
                    frames_waited = 0
                    now_t = time.time()
                    cooldown = self._idr_cooldown()
                    if self.net and (now_t - self._last_keyframe_req.get(uid, 0.0) >= cooldown):
                        self._last_keyframe_req[uid] = now_t
                        self.net.request_viewer_keyframe(uid)

                finally:
                    del frame_data, raw

        finally:
            del decoder
            gc.collect()
            print(f"[Video] decode_worker uid={uid} завершён, FFmpeg контекст освобождён")