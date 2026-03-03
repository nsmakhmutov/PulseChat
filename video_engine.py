import threading
import time
import queue
import gc
from fractions import Fraction
from typing import Optional

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage
from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_BITRATE,
    MAX_VIDEO_PAYLOAD, VIDEO_CHUNK_HEADER, VIDEO_CHUNK_STRUCT,
    VIDEO_HEADER_SIZE, FLAG_VIDEO,
)

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
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("[Video] ОШИБКА: Pillow не установлен!")


class VideoEngine(QObject):
    """Захват экрана, H264-кодирование/декодирование и фрагментация видео-пакетов."""

    frame_received = pyqtSignal(int, QImage)

    def __init__(self, net_client):
        super().__init__()
        self.net = net_client
        self.running = False
        self.capture_thread: Optional[threading.Thread] = None
        self.encode_thread: Optional[threading.Thread] = None

        # maxsize=4: небольшой запас при кратковременных пиках без накопления задержки
        self.frame_queue = queue.Queue(maxsize=4)

        self._buffer_lock = threading.RLock()
        self.incoming_buffer: dict = {}   # uid → {frame_id → {chunk_idx → bytes}}
        self.assembly_info: dict = {}     # uid → {frame_id → {total, received, ts}}

        # Декодирование вынесено в отдельный поток на каждого стримера.
        # process_incoming_packet() только собирает фрагменты и кладёт готовый
        # frame в decode_queue[uid]; decode_worker декодирует асинхронно.
        # Очередь ограничена maxsize=2 — дроп кадра лучше накопления задержки.
        self.decode_queues: dict = {}    # uid → Queue(maxsize=2)
        self.decode_threads: dict = {}   # uid → Thread
        self.decoders: dict = {}         # uid → av.CodecContext (внутри decode_worker)

        self.frame_counter = 0
        self._force_keyframe = False

        # Чистка незавершённых кадров раз в 500 мс в отдельном потоке,
        # чтобы не нагружать UDP-поток O(N) итерациями под локом.
        threading.Thread(
            target=self._frame_cleanup_loop,
            daemon=True,
            name="video-frame-cleanup",
        ).start()

    # ── Энкодер ───────────────────────────────────────────────────────────────

    def _init_encoder(self, width: int, height: int, fps: int, bitrate: int):
        """Создаёт H264-энкодер: сначала пробует NVENC, затем libx264.

            :param width: ширина кадра в пикселях
            :param height: высота кадра в пикселях
            :param fps: целевой FPS
            :param bitrate: битрейт в бит/с
            :return: av.CodecContext в режиме записи
            :raises RuntimeError: если ни один кодек не найден
        """
        encoders_to_try = [
            ('h264_nvenc', {
                'preset':     'p1',
                'tune':       'ull',
                'rc':         'cbr',
                'forced-idr': '1',
                'delay':      '0',
            }),
            ('libx264', {
                'preset':  'ultrafast',
                'tune':    'zerolatency',
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
                codec.options = options
                codec.open()
                print(f"[Video] Успешно запущен энкодер: {codec_name}")
                return codec
            except Exception as e:
                print(f"[Video] Не удалось запустить {codec_name}: {e}")

        raise RuntimeError("Ни один видео-кодек не найден!")

    # ── Управление стримом ────────────────────────────────────────────────────

    def start_streaming(self, settings: Optional[dict] = None) -> bool:
        """Запускает захват экрана и кодирование.

            :param settings: словарь с ключами monitor_idx, width, height, fps;
                             None — используются значения из config
            :return: True если стрим запущен, False если уже запущен или нет зависимостей
        """
        if not (AV_AVAILABLE and DXCAM_AVAILABLE):
            return False
        if self.running:
            return False

        self.current_settings = settings or {
            "monitor_idx": 0,
            "width":       VIDEO_WIDTH,
            "height":      VIDEO_HEIGHT,
            "fps":         VIDEO_FPS,
        }
        self.running = True

        with self.frame_queue.mutex:
            self.frame_queue.queue.clear()

        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.encode_thread = threading.Thread(target=self._encode_loop, daemon=True)
        self.capture_thread.start()
        self.encode_thread.start()
        return True

    def stop_streaming(self):
        """Останавливает стрим, освобождает D3D11/FFmpeg ресурсы и trim памяти."""
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

        # Windows: сбрасываем рабочее множество страниц обратно в ОС
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
        """Форсирует отправку IDR-кадра при следующей итерации энкодера."""
        self._force_keyframe = True

    def cleanup_users(self, active_uids: set):
        """Удаляет буферы и decode-потоки для отключившихся пользователей.

            :param active_uids: множество uid активных участников
        """
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

    def _frame_cleanup_loop(self):
        """Периодически удаляет незавершённые фрагменты кадров старше 1 сек."""
        while True:
            time.sleep(0.5)
            now = time.time()
            with self._buffer_lock:
                for uid in list(self.assembly_info.keys()):
                    to_del = [
                        fid for fid, info in self.assembly_info[uid].items()
                        if now - info['ts'] > 1.0
                    ]
                    for fid in to_del:
                        self.incoming_buffer[uid].pop(fid, None)
                        self.assembly_info[uid].pop(fid, None)

    def stop_viewer_for_uid(self, uid: int):
        """Останавливает decode_worker и очищает буферы для данного uid.

        Не блокирует: посылает None в очередь и поток завершается сам.
        Все Python-ссылки снимаются сразу — GC освободит декодер после завершения потока.

            :param uid: идентификатор стримера
        """
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

        print(f"[Video] stop_viewer_for_uid({uid}): сигнал завершения декодера отправлен")

    # ── Захват экрана ─────────────────────────────────────────────────────────

    def _capture_loop(self):
        """Основной цикл захвата экрана через dxcam (нативный режим)."""
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
            print(f"[Video] DXcam запущен {target_fps} FPS на мониторе {monitor_idx}")
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
                    self.frame_queue.put(frame_np)
            except Exception as e:
                print(f"[Capture] Error: {e}")
                time.sleep(0.1)

        camera.stop()
        # Явное освобождение D3D11 ресурсов (staging textures, device context)
        try:
            camera.release()
            print("[Video] DXCam D3D11 ресурсы освобождены")
        except Exception:
            pass
        del camera

    def _capture_loop_fallback(self, camera):
        """Fallback polling-режим через grab() если dxcam.start() недоступен.

            :param camera: инициализированный dxcam-объект
        """
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
                    self.frame_queue.put(frame_np)

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

    # ── Кодирование и фрагментация ────────────────────────────────────────────

    def _encode_loop(self):
        """Цикл кодирования: берёт кадры из frame_queue, кодирует и фрагментирует."""
        width = self.current_settings.get('width', 1280)
        height = self.current_settings.get('height', 720)
        fps = self.current_settings.get('fps', 30)

        try:
            codec = self._init_encoder(width, height, fps, VIDEO_BITRATE)
        except Exception as e:
            print(f"[Video] КРИТИЧЕСКАЯ ОШИБКА: {e}")
            self.running = False
            return

        pts_counter = 0

        try:
            while self.running:
                try:
                    try:
                        frame_np = self.frame_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if frame_np.size == 0:
                        continue

                    src_h, src_w, _ = frame_np.shape

                    if src_w != width or src_h != height:
                        img = Image.fromarray(frame_np, 'RGB')
                        img = img.resize((width, height), Image.Resampling.BILINEAR)
                        frame = av.VideoFrame.from_image(img)
                        del img
                    else:
                        frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')

                    # Освобождаем DXCam-буфер сразу после конвертации (~2.8 МБ)
                    del frame_np

                    frame.pts = pts_counter
                    pts_counter += 1

                    if self._force_keyframe:
                        try:
                            frame.pict_type = 'I'
                        except (TypeError, AttributeError):
                            frame.pict_type = av.video.frame.PictureType.I
                        self._force_keyframe = False
                        print("[Video] Принудительный IDR-кадр отправлен")

                    packets = codec.encode(frame)
                    del frame

                    for packet in packets:
                        self._fragment_and_send(bytes(packet))

                except Exception as e:
                    print(f"[Encoder] Error: {e}")

            try:
                for packet in codec.encode(None):
                    self._fragment_and_send(bytes(packet))
            except Exception:
                pass

        finally:
            # del вызывает avcodec_free_context() через Cython-деструктор PyAV
            del codec
            print("[Video] Энкодер закрыт, FFmpeg-буферы освобождены")

    def _fragment_and_send(self, data: bytes):
        """Фрагментирует H264-данные на чанки и отправляет через net.send_video_packet.

            :param data: сырые H264-байты одного пакета
        """
        if not self.net or not self.net.udp_socket_bound:
            return

        self.frame_counter = (self.frame_counter + 1) % 0xFFFFFFFF
        total_len = len(data)
        chunks_count = (total_len + MAX_VIDEO_PAYLOAD - 1) // MAX_VIDEO_PAYLOAD

        for i in range(chunks_count):
            start = i * MAX_VIDEO_PAYLOAD
            end = min(start + MAX_VIDEO_PAYLOAD, total_len)
            chunk_payload = data[start:end]
            v_header = VIDEO_CHUNK_HEADER.pack(self.frame_counter, i, chunks_count)
            try:
                self.net.send_video_packet(v_header + chunk_payload)
            except Exception:
                pass

    # ── Приём и сборка входящих пакетов ──────────────────────────────────────

    def process_incoming_packet(self, uid: int, data: bytes):
        """Собирает фрагменты кадра и передаёт собранный кадр в decode_worker.

        Сборка bytearray выполняется вне _buffer_lock, чтобы минимизировать
        время удержания лока в UDP-потоке.

            :param uid: идентификатор отправителя
            :param data: сырые байты UDP-пакета
        """
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
                        self.incoming_buffer[uid].clear()
                        self.assembly_info[uid].clear()

                    self.incoming_buffer[uid][frame_id] = {}
                    self.assembly_info[uid][frame_id] = {
                        'total':    total_chunks,
                        'received': 0,
                        'ts':       time.time(),
                    }

                if chunk_idx not in self.incoming_buffer[uid][frame_id]:
                    self.incoming_buffer[uid][frame_id][chunk_idx] = payload
                    self.assembly_info[uid][frame_id]['received'] += 1

                if self.assembly_info[uid][frame_id]['received'] == total_chunks:
                    # dict() копирует только ссылки — быстро; сборка байт — вне лока
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

                if ok:
                    self._ensure_decode_worker(uid)
                    q = self.decode_queues[uid]
                    if q.full():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                    try:
                        q.put_nowait(bytes(full_data))
                    except queue.Full:
                        pass

        except Exception:
            pass

    # ── Декодирование ─────────────────────────────────────────────────────────

    def _ensure_decode_worker(self, uid: int):
        """Запускает decode_worker для uid, если поток не существует или завершился.

            :param uid: идентификатор стримера
        """
        if uid not in self.decode_threads or not self.decode_threads[uid].is_alive():
            q = queue.Queue(maxsize=2)
            self.decode_queues[uid] = q
            t = threading.Thread(
                target=self._decode_worker,
                args=(uid, q),
                daemon=True,
                name=f"decode-{uid}",
            )
            self.decode_threads[uid] = t
            t.start()

    def _decode_worker(self, uid: int, q: queue.Queue):
        """Декодирует H264-кадры и эмитит frame_received.

        thread_type='FRAME' + thread_count=2: ~10 МБ вместо ~40 МБ при AUTO × N_cores.
        None в очереди — сигнал завершения. Таймаут 2 с — авtozавершение при простое.
        FFmpeg-контекст освобождается явно через del в блоке finally.

            :param uid: идентификатор стримера
            :param q: очередь с bytes кадров
        """
        decoder = av.CodecContext.create('h264', 'r')
        decoder.thread_type = 'FRAME'
        decoder.thread_count = 2

        try:
            while True:
                try:
                    raw = q.get(timeout=2.0)
                except queue.Empty:
                    break

                if raw is None:
                    break

                try:
                    packet = av.Packet(raw)
                    frames = decoder.decode(packet)
                    for frame in frames:
                        img_np = np.ascontiguousarray(frame.to_rgb().to_ndarray())
                        h, w, _ = img_np.shape
                        q_img = QImage(
                            img_np.data, w, h,
                            img_np.strides[0],
                            QImage.Format.Format_RGB888,
                        )
                        # copy() отвязывает QImage от numpy-буфера перед del img_np
                        self.frame_received.emit(uid, q_img.copy())
                        del img_np, q_img
                    del packet, frames
                except Exception:
                    pass
                finally:
                    del raw
        finally:
            del decoder
            gc.collect()
            print(f"[Video] decode_worker uid={uid} завершён, FFmpeg контекст освобождён")