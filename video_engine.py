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
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("[Video] ОШИБКА: Pillow не установлен!")


class VideoEngine(QObject):
    frame_received = pyqtSignal(int, QImage)

    def __init__(self, net_client):
        super().__init__()
        self.net = net_client
        self.running = False
        self.capture_thread = None
        self.encode_thread  = None

        # maxsize=4: encode-тред получает небольшой запас при кратковременных пиках.
        # Больше не нужно — лишние кадры только добавляют задержку (latency).
        self.frame_queue = queue.Queue(maxsize=4)

        # --- Входящие пакеты ---
        self._buffer_lock   = threading.RLock()
        self.incoming_buffer = {}   # uid → {frame_id → {chunk_idx → bytes}}
        self.assembly_info   = {}   # uid → {frame_id → {total, received, ts}}

        # -------------------------------------------------------------------
        # FIX #5: Декодирование вынесено в отдельный поток на каждого стримера.
        #
        # Было: _reassemble_and_decode() вызывался синхронно внутри
        #       process_incoming_packet() — в UDP-потоке клиента.
        #       Пока декодировался H264-кадр (несколько мс), новые пакеты
        #       не читались → back-pressure → рост пинга.
        #
        # Стало:
        #   - process_incoming_packet() только собирает фрагменты в буфер
        #     и кладёт собранный frame в decode_queue[uid] — это мгновенно.
        #   - Отдельный decode_worker (по одному на uid) забирает из очереди
        #     и декодирует асинхронно.
        #   - Очередь ограничена (maxsize=2): если декодер не успевает —
        #     старые кадры дропаются (лучше дроп, чем накопление задержки).
        # -------------------------------------------------------------------
        self.decode_queues   = {}   # uid → Queue(maxsize=2)
        self.decode_threads  = {}   # uid → Thread
        self.decoders        = {}   # uid → av.CodecContext  (только внутри decode_worker)

        self.frame_counter = 0
        self._dx_factory   = None
        self._force_keyframe = False

        # -------------------------------------------------------------------
        # FIX #4: Периодическая чистка протухших фреймов вынесена в отдельный поток.
        #
        # Было: цикл O(N незаконченных кадров) выполнялся внутри _buffer_lock
        #       на КАЖДЫЙ входящий UDP-пакет (при 60fps ≈ 300 пакетов/кадр).
        #       Это добавляло до нескольких µs удержания лока на каждый пакет
        #       в UDP-потоке.
        #
        # Стало: _frame_cleanup_loop просыпается раз в 500 мс и чистит под
        #        коротким локом только действительно протухшие записи.
        #        UDP-поток больше не занимается хозяйственными задачами.
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
                codec.width     = width
                codec.height    = height
                codec.pix_fmt   = 'yuv420p'
                codec.time_base = Fraction(1, fps)
                codec.bit_rate  = bitrate
                codec.options   = options
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
            "width":       VIDEO_WIDTH,
            "height":      VIDEO_HEIGHT,
            "fps":         VIDEO_FPS,
        }
        self.running = True

        with self.frame_queue.mutex:
            self.frame_queue.queue.clear()

        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.encode_thread  = threading.Thread(target=self._encode_loop,  daemon=True)
        self.capture_thread.start()
        self.encode_thread.start()
        return True

    def stop_streaming(self):
        self.running = False
        if self.capture_thread:
            self.capture_thread.join(timeout=3)   # +1 сек: dxcam.stop() может занять время
            self.capture_thread = None
        if self.encode_thread:
            self.encode_thread.join(timeout=3)    # +1 сек: flush энкодера
            self.encode_thread = None

        # Очистка очереди кадров: необработанные numpy-массивы (до 4 × ~2.8 МБ
        # для 1280×720) оставались в frame_queue до следующей GC-итерации.
        with self.frame_queue.mutex:
            self.frame_queue.queue.clear()
            self.frame_queue.all_tasks_done.notify_all()
            self.frame_queue.unfinished_tasks = 0

        # Принудительный запуск GC + trim памяти.
        # gc.collect(1) + (2) — ломаем циклические ссылки C-extension объектов.
        gc.collect(1)
        gc.collect(2)

        # Windows: освобождаем "рабочее множество" страниц обратно в ОС.
        # Проблема: numpy frame-массивы (4 × 2.7 МБ), FFmpeg encoder working buffers
        # (~30 МБ) освобождены Python GC, но Windows heap удерживает страницы
        # (demand zero pages) для возможного повторного использования.
        # Task Manager видит эти страницы как "частная память" (Private Bytes).
        #
        # SetProcessWorkingSetSizeEx(-1, -1, 0) принудительно сбрасывает рабочее
        # множество — страницы уходят в standby list и Task Manager их не считает.
        # При следующем обращении страницы будут page-faulted обратно (мгновенно).
        # Аналогичный трюк используют Discord, Chrome, Firefox.
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

    def cleanup_users(self, active_uids):
        """
        Очистка памяти от отключившихся юзеров.
        FIX #5: также останавливаем decode_worker для ушедших uid.
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

        # Останавливаем decode-потоки для ушедших uid
        for uid in list(self.decode_queues.keys()):
            if uid not in active_uids:
                # Сигнал завершения: None в очередь
                try:
                    self.decode_queues[uid].put_nowait(None)
                except queue.Full:
                    pass
                t = self.decode_threads.pop(uid, None)
                if t:
                    t.join(timeout=1)
                self.decode_queues.pop(uid, None)

    def _frame_cleanup_loop(self):
        """
        FIX #4: Периодически удаляет незавершённые фрагменты кадров старше 1 сек.

        Раньше эта же логика выполнялась на КАЖДЫЙ входящий UDP-пакет внутри
        _buffer_lock (в process_incoming_packet). При 60fps и ~300 пакетах на
        кадр это означало 300 лишних итераций O(N кадров) под локом в секунду.

        Теперь чистка происходит раз в 500 мс — достаточно редко, чтобы не
        нагружать CPU, и достаточно часто, чтобы буфер не разрастался.
        """
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

    def stop_viewer_for_uid(self, uid):
        """
        Немедленно останавливает decode_worker для данного uid и очищает все
        связанные буферы. Вызывать когда зритель закрывает окно просмотра
        (неважно — вручную или потому что стример остановил трансляцию).

        Не блокирует: сигнал None кладётся в очередь и worker завершается сам
        (через <2 секунды). Все Python-ссылки снимаются здесь — GC может
        освободить декодер как только поток завершится.

        Thread-safe: может вызываться из Qt main thread.
        """
        # 1. Посылаем сигнал завершения в очередь
        q = self.decode_queues.pop(uid, None)
        if q is not None:
            # Очищаем очередь (могут быть 1-2 кадра) и ставим None
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
            # q теперь не хранится в decode_queues, поэтому process_incoming_packet
            # и _ensure_decode_worker не смогут положить новые данные в старый воркер.
            # Следующий вызов _ensure_decode_worker создаст новый поток — но только
            # если окно просмотра снова открыто (on_video_frame → update_frame).

        # 2. Снимаем ссылку на поток — GC соберёт Thread объект после завершения.
        #    Не делаем join(): блокировка Qt main thread на 2 сек = freeze UI.
        self.decode_threads.pop(uid, None)

        # 3. Немедленно очищаем сборочные буферы — bytes объекты кадра (~50–200 КБ × N)
        with self._buffer_lock:
            self.incoming_buffer.pop(uid, None)
            self.assembly_info.pop(uid, None)
            self.decoders.pop(uid, None)

        print(f"[Video] stop_viewer_for_uid({uid}): сигнал завершения декодера отправлен")

    # ------------------------------------------------------------------
    # Захват экрана
    # ------------------------------------------------------------------
    def _capture_loop(self):
        target_fps  = self.current_settings.get('fps', 60)
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
                    self.frame_queue.put(frame_np)
            except Exception as e:
                print(f"[Capture] Error: {e}")
                time.sleep(0.1)

        camera.stop()
        # КРИТИЧЕСКИ ВАЖНО: явно освобождаем D3D11 ресурсы.
        # dxcam использует Direct3D11 staging textures и device context:
        #   - 1280×720 BGRA × 4 буфера (Double/Triple buffering) ≈ 15–30 МБ
        # del camera не вызывает D3D Release() немедленно — C-extension объект
        # уничтожается только при следующей GC-итерации. camera.release()
        # вызывает деструктор сразу и освобождает GPU/CPU память синхронно.
        try:
            camera.release()
            print("[Video] DXCam D3D11 ресурсы освобождены")
        except Exception:
            pass
        del camera

    def _capture_loop_fallback(self, camera):
        """Fallback polling-режим через grab() если dxcam.start() недоступен."""
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

                elapsed    = time.perf_counter() - start_t
                sleep_time = max(0, frame_time - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
            except Exception as e:
                print(f"[Capture Fallback] Error: {e}")
                time.sleep(1)

        # Fallback: camera.release() тоже нужен (те же D3D11 ресурсы)
        try:
            camera.release()
        except Exception:
            pass
        del camera

    # ------------------------------------------------------------------
    # Кодирование и фрагментация
    # ------------------------------------------------------------------
    def _encode_loop(self):
        width   = self.current_settings.get('width',  1280)
        height  = self.current_settings.get('height', 720)
        fps     = self.current_settings.get('fps',    30)
        bitrate = VIDEO_BITRATE

        try:
            codec = self._init_encoder(width, height, fps, bitrate)
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
                        img   = Image.fromarray(frame_np, 'RGB')
                        img   = img.resize((width, height), Image.Resampling.BILINEAR)
                        frame = av.VideoFrame.from_image(img)
                        del img
                    else:
                        frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')

                    # Явно освобождаем numpy-массив кадра сразу после конвертации.
                    # frame_np — это ссылка на DXCam-буфер размером ~2.8 МБ (1280×720×3).
                    # Без del он живёт до следующей итерации, удерживая буфер DXCam.
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
                    # Явно освобождаем av.VideoFrame после кодирования.
                    # Энкодер держит до 16 reference frames внутри — del здесь
                    # освобождает Python-обёртку, сам FFmpeg-буфер управляется кодеком.
                    del frame

                    for packet in packets:
                        self._fragment_and_send(bytes(packet))

                except Exception as e:
                    print(f"[Encoder] Error: {e}")

            # Flush encoder
            try:
                for packet in codec.encode(None):
                    self._fragment_and_send(bytes(packet))
            except Exception:
                pass

        finally:
            # КРИТИЧЕСКИ ВАЖНО: явно закрываем H264-энкодер.
            #
            # PyAV / FFmpeg энкодер держит в памяти:
            #   - libx264: reference frames (~14 МБ) + lookahead (~30 МБ)
            #             + motion estimation таблицы (~20 МБ) = ~64 МБ
            #   - h264_nvenc: CUDA-контекст + GPU-pinned буферы = ещё больше
            #
            # Без codec.close() эта память не освобождается до вызова Python GC,
            # который может откладываться на неопределённое время (или не вызываться
            # вообще пока heap не вырастет достаточно). Каждый цикл вкл/выкл
            # трансляции добавлял ~60-100 МБ "зависшей" памяти.
            try:
                codec.close()
                print("[Video] Энкодер закрыт, FFmpeg-буферы освобождены")
            except Exception as ex:
                print(f"[Video] Ошибка при закрытии энкодера: {ex}")
            del codec

    def _fragment_and_send(self, data):
        if not self.net or not self.net.udp_socket_bound:
            return

        self.frame_counter = (self.frame_counter + 1) % 0xFFFFFFFF
        total_len    = len(data)
        chunks_count = (total_len + MAX_VIDEO_PAYLOAD - 1) // MAX_VIDEO_PAYLOAD

        for i in range(chunks_count):
            start         = i * MAX_VIDEO_PAYLOAD
            end           = min(start + MAX_VIDEO_PAYLOAD, total_len)
            chunk_payload = data[start:end]
            v_header      = VIDEO_CHUNK_HEADER.pack(self.frame_counter, i, chunks_count)
            try:
                self.net.send_video_packet(v_header + chunk_payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Приём и сборка входящих пакетов
    # ------------------------------------------------------------------
    def process_incoming_packet(self, uid, data):
        """
        FIX #5: Этот метод только собирает фрагменты кадра в буфер.
        Как только кадр собран — кладёт raw bytes в decode_queue и немедленно
        возвращается. Никакого H264-декодирования здесь нет.

        FIX #8: Сборка bytearray (копирование данных) вынесена ЗА пределы
        _buffer_lock. Раньше цикл `full_data.extend(chunks[i])` выполнялся
        внутри `with self._buffer_lock`, удерживая лок на время копирования
        всех чанков кадра. Теперь внутри лока делается только быстрый снимок
        словаря chunks (dict copy), а само копирование байт — уже вне лока.
        Это сокращает время удержания _buffer_lock и снижает back-pressure
        на UDP-поток.

        Декодирование происходит в decode_worker() — отдельном потоке на uid.
        """
        if len(data) < VIDEO_HEADER_SIZE:
            return

        try:
            frame_id, chunk_idx, total_chunks = VIDEO_CHUNK_HEADER.unpack(data[:VIDEO_HEADER_SIZE])
            payload = data[VIDEO_HEADER_SIZE:]

            # Переменные для передачи данных за пределы лока
            assembled_chunks = None
            assembled_total  = 0

            with self._buffer_lock:
                if uid not in self.incoming_buffer:
                    self.incoming_buffer[uid] = {}
                    self.assembly_info[uid]   = {}

                if frame_id not in self.incoming_buffer[uid]:
                    # Ограничение глубины буфера: не храним больше 5 незаконченных кадров
                    if len(self.incoming_buffer[uid]) > 5:
                        self.incoming_buffer[uid].clear()
                        self.assembly_info[uid].clear()

                    self.incoming_buffer[uid][frame_id]  = {}
                    self.assembly_info[uid][frame_id] = {
                        'total':    total_chunks,
                        'received': 0,
                        'ts':       time.time(),
                    }

                if chunk_idx not in self.incoming_buffer[uid][frame_id]:
                    self.incoming_buffer[uid][frame_id][chunk_idx]      = payload
                    self.assembly_info[uid][frame_id]['received'] += 1

                if self.assembly_info[uid][frame_id]['received'] == total_chunks:
                    # Снимаем снимок чанков и удаляем из буфера — всё ещё под локом,
                    # но dict() копирует только ссылки на байты, это быстро.
                    assembled_chunks = dict(self.incoming_buffer[uid][frame_id])
                    assembled_total  = self.assembly_info[uid][frame_id]['total']

                    # Удаляем собранный кадр из буфера до выхода из лока
                    del self.incoming_buffer[uid][frame_id]
                    del self.assembly_info[uid][frame_id]

                # FIX #4: Чистка протухших незаконченных кадров вынесена в
                # _frame_cleanup_loop (раз в 500 мс). Раньше этот O(N) цикл
                # выполнялся на каждый UDP-пакет под _buffer_lock — лишняя нагрузка.

            # --- Сборка bytearray ВНЕ лока ---
            # _buffer_lock уже освобождён. UDP-поток может продолжать работу
            # пока мы копируем байты кадра.
            if assembled_chunks is not None:
                full_data = bytearray()
                ok = True
                for i in range(assembled_total):
                    if i not in assembled_chunks:
                        ok = False
                        break
                    full_data.extend(assembled_chunks[i])

                if ok:
                    # Убеждаемся, что decode_worker запущен для этого uid
                    self._ensure_decode_worker(uid)
                    q = self.decode_queues[uid]
                    # Если декодер не успевает — дропаем старый кадр, берём новый.
                    # Лучше дроп одного кадра, чем накопление задержки.
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

    # ------------------------------------------------------------------
    # Декодирование (отдельный поток на каждого стримера)
    # ------------------------------------------------------------------
    def _ensure_decode_worker(self, uid):
        """Запускает decode_worker для uid, если он ещё не запущен."""
        if uid not in self.decode_threads or not self.decode_threads[uid].is_alive():
            q = queue.Queue(maxsize=2)
            self.decode_queues[uid]  = q
            t = threading.Thread(
                target=self._decode_worker,
                args=(uid, q),
                daemon=True,
                name=f"decode-{uid}",
            )
            self.decode_threads[uid] = t
            t.start()

    def _decode_worker(self, uid, q):
        """
        FIX #5: Декодирует H264-данные в отдельном потоке.

        Принимает bytes из decode_queue[uid] и эмитит frame_received сигнал
        в Qt-поток. None в очереди = сигнал завершения.

        FIX MEM: decoder явно закрывается в блоке finally — без этого PyAV
        удерживал нативный H264-контекст (FFmpeg avcodec_context) до сборки
        GC, что при частых вкл/выкл трансляции накапливало ~10-15 МБ.
        """
        decoder = av.CodecContext.create('h264', 'r')

        # FIX MEM: thread_type='AUTO' заставлял FFmpeg создавать N_cores потоков,
        # каждый со своими копиями reference frame буферов.
        # Для 1280×720 H264: AUTO × 8 cores = 8 × ~5MB = ~40MB только для декодера.
        # FRAME-threading + 2 потока: ~10MB и нет артефактов при baseline профиле.
        decoder.thread_type  = 'FRAME'
        decoder.thread_count = 2

        try:
            while True:
                try:
                    raw = q.get(timeout=2.0)
                except queue.Empty:
                    # Если 2 секунды нет данных — поток сам завершится.
                    # Сигнал None или закрытие decode_queues[uid] приходит раньше.
                    break

                if raw is None:
                    # Явный сигнал завершения от stop_viewer_for_uid / cleanup_users
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
                        # q_img.copy() отвязывает QImage от numpy-буфера
                        # перед del img_np — обязательно.
                        self.frame_received.emit(uid, q_img.copy())
                        del img_np, q_img
                    del packet, frames
                except Exception:
                    pass
                finally:
                    # raw — bytes объект сжатого H264 кадра (~20–80 КБ).
                    # Без явного del он держится до следующего q.get() (до 2 сек).
                    del raw
        finally:
            # Явное освобождение FFmpeg контекста.
            # С thread_count=2 вместо AUTO освобождает ~10MB вместо ~40MB.
            try:
                decoder.close()
            except Exception:
                pass
            del decoder
            gc.collect()
            print(f"[Video] decode_worker uid={uid} завершён, FFmpeg контекст освобождён")