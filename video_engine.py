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

try:
    pass  # PIL не используется — ресайз через libswscale (av.VideoFrame.reformat)
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("[Video] ОШИБКА: Pillow не установлен!")

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
        self.decode_queues = {}  # uid → Queue(maxsize=2)
        self.decode_threads = {}  # uid → Thread
        self.decoders = {}  # uid → av.CodecContext  (только внутри decode_worker)

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

        # -------------------------------------------------------------------
        # FIX #4: Периодическая чистка протухших фреймов вынесена в отдельный поток.
        # -------------------------------------------------------------------
        threading.Thread(
            target=self._frame_cleanup_loop,
            daemon=True,
            name="video-frame-cleanup",
        ).start()

    # ------------------------------------------------------------------
    # Энкодер
    # ------------------------------------------------------------------
    def _init_encoder(self, width, height, fps, bitrate):
        encoders_to_try = [
            ('h264_nvenc', {
                'preset': 'p1',
                'tune': 'ull',
                'rc': 'cbr',
                'forced-idr': '1',
                'delay': '0',
            }),
            ('libx264', {
                'preset': 'ultrafast',
                'tune': 'zerolatency',
                'profile': 'baseline',
                'threads': '4',
            }),
        ]

        for codec_name, options in encoders_to_try:
            try:
                codec = av.CodecContext.create(codec_name, 'w')
                codec.width = width
                codec.height = height
                codec.pix_fmt = 'yuv420p'
                codec.time_base = Fraction(1, fps)
                codec.bit_rate = bitrate
                codec.gop_size = fps
                codec.options = options
                codec.open()
                print(f"[Video] Успешно запущен энкодер: {codec_name}")
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

    def cleanup_users(self, active_uids):
        with self._buffer_lock:
            for uid in list(self.incoming_buffer.keys()):
                if uid not in active_uids:
                    del self.incoming_buffer[uid]
            for uid in list(self.assembly_info.keys()):
                if uid not in active_uids:
                    del self.assembly_info[uid]
            for uid in list(self.decoders.keys()):
                if uid not in active_uids:
                    del self.decoders[uid]

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

    def _frame_cleanup_loop(self):
        STATS_INTERVAL = 2.0

        while True:
            time.sleep(0.5)
            now = time.time()
            uids_with_loss = []

            with self._buffer_lock:
                for uid in list(self.assembly_info.keys()):
                    to_del = [
                        fid for fid, info in self.assembly_info[uid].items()
                        if now - info['ts'] > 1.0
                    ]
                    for fid in to_del:
                        self.incoming_buffer[uid].pop(fid, None)
                        self.assembly_info[uid].pop(fid, None)
                        self._stats_dropped[uid] = self._stats_dropped.get(uid, 0) + 1
                    if to_del:
                        uids_with_loss.append(uid)

            if self.net and uids_with_loss:
                current_ping = getattr(self.net, 'current_ping', 0)
                if current_ping > 1500:
                    idr_cooldown = 40.0
                elif current_ping > 500:
                    idr_cooldown = 20.0
                elif current_ping > 200:
                    idr_cooldown = 10.0
                else:
                    idr_cooldown = 5.0

                for uid in uids_with_loss:
                    last_req = self._last_keyframe_req.get(uid, 0.0)
                    if now - last_req >= idr_cooldown:
                        self._last_keyframe_req[uid] = now
                        self.net.request_viewer_keyframe(uid)
                        print(
                            f"[Video] Потеря пакетов uid={uid}: IDR запрошен "
                            f"(ping={current_ping}ms, cooldown={idr_cooldown:.0f}s)"
                        )

            if now - self._stats_window_start >= STATS_INTERVAL:
                elapsed = max(now - self._stats_window_start, 0.001)
                self._stats_window_start = now

                all_uids = set(self._stats_decoded.keys()) | set(self._stats_dropped.keys())
                for uid in all_uids:
                    decoded = self._stats_decoded.pop(uid, 0)
                    dropped = self._stats_dropped.pop(uid, 0)
                    total = decoded + dropped

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
            self.decoders.pop(uid, None)

        self._last_assembled_fid.pop(uid, None)
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
                    if codec_lq is not None:
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
            self.frame_counter_lq = (self.frame_counter_lq + 1) % 0xFFFFFFFF
            current_frame_id = self.frame_counter_lq
        else:
            self.frame_counter_hq = (self.frame_counter_hq + 1) % 0xFFFFFFFF
            current_frame_id = self.frame_counter_hq

        total_len = len(data)
        chunks_count = (total_len + MAX_VIDEO_PAYLOAD - 1) // MAX_VIDEO_PAYLOAD

        chunks = []
        for i in range(chunks_count):
            start = i * MAX_VIDEO_PAYLOAD
            end = min(start + MAX_VIDEO_PAYLOAD, total_len)
            chunk_payload = data[start:end]
            v_header = VIDEO_CHUNK_HEADER.pack(current_frame_id, i, chunks_count)
            chunks.append(v_header + chunk_payload)

        self.net.send_video_frame_chunks(chunks, flags=flags)

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

    def process_incoming_packet(self, uid, data, is_lq: bool = False):
        if len(data) < VIDEO_HEADER_SIZE:
            return

        try:
            frame_id, chunk_idx, total_chunks = VIDEO_CHUNK_HEADER.unpack(data[:VIDEO_HEADER_SIZE])
            payload = data[VIDEO_HEADER_SIZE:]

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
                        expected = (last_fid + 1) % 0xFFFFFFFF
                        if frame_id != expected:
                            cooldown = self._idr_cooldown()
                            if self.net and (now_t - self._last_keyframe_req.get(uid, 0.0) >= cooldown):
                                self._last_keyframe_req[uid] = now_t
                                self.net.request_viewer_keyframe(uid)
                                print(
                                    f"[Video] uid={uid}: UDP gap "
                                    f"(ожидали {expected}, пришёл {frame_id}) → IDR запрошен"
                                )
                    self._last_assembled_fid[uid] = frame_id

                    self._ensure_decode_worker(uid)
                    q = self.decode_queues[uid]

                    if q.full():
                        try:
                            q.get_nowait()
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
                    if self.net:
                        if now_t - self._last_keyframe_req.get(uid, 0.0) >= 5.0:
                            self._last_keyframe_req[uid] = now_t
                            self.net.request_viewer_keyframe(uid)
                            print(f"[Video] Неполный кадр uid={uid}: IDR запрошен")

        except Exception:
            pass

    def _ensure_decode_worker(self, uid):
        if uid not in self.decode_threads or not self.decode_threads[uid].is_alive():
            q = queue.Queue(maxsize=4)
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
                    expected = (last_worker_fid + 1) % 0xFFFFFFFF
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