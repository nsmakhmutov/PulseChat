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
    pass  # PIL не используется — ресайз через libswscale (av.VideoFrame.reformat)
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

        # maxsize=2: минимальный буфер capture→encode.
        # При 60fps каждый лишний кадр в очереди = +16 мс задержки стрима.
        # 2 слота достаточно чтобы энкодер не голодал при кратковременных пиках,
        # но не накапливал задержку. Старый кадр дропается при переполнении.
        self.frame_queue = queue.Queue(maxsize=2)

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

        # _last_keyframe_req: uid → timestamp последнего запроса IDR у стримера.
        # Используется в _frame_cleanup_loop и process_incoming_packet для
        # rate-limiting: не чаще 1 раза в 2 секунды на uid.
        self._last_keyframe_req: dict = {}

        # --- ABR: динамическая смена битрейта ---
        # _target_bitrate: атомарно пишется из Qt-потока (set_bitrate),
        # читается из _encode_loop. GIL гарантирует безопасность int-присвоения.
        # _current_bitrate: текущий битрейт работающего энкодера.
        # При _target_bitrate != _current_bitrate энкодер перезапускается.
        self._target_bitrate:  int = VIDEO_BITRATE
        self._current_bitrate: int = VIDEO_BITRATE

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
        # Сбрасываем ABR при старте: начинаем с максимального битрейта
        self._target_bitrate  = VIDEO_BITRATE
        self._current_bitrate = VIDEO_BITRATE

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

    def set_bitrate(self, new_bitrate: int):
        """
        Запрос смены битрейта от ABR-контроллера (вызывается из Qt-потока).

        Не блокирует: просто пишет int-значение. GIL гарантирует атомарность.
        _encode_loop на следующей итерации прочитает _target_bitrate, заметит
        расхождение с _current_bitrate и перезапустит энкодер.

        IDR-кадр после перезапуска обеспечивает чистый старт декодера у зрителя.
        """
        if new_bitrate != self._target_bitrate:
            kbps_old = self._target_bitrate // 1000
            kbps_new = new_bitrate // 1000
            print(f"[Video] ABR: запрошена смена битрейта {kbps_old} → {kbps_new} kbps")
            self._target_bitrate = new_bitrate

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

        OPT: при обнаружении протухших кадров (реальная потеря UDP-пакетов)
        запрашиваем IDR у стримера с rate-limit 1 раз в 2 сек на uid.
        IDR-кадр восстанавливает декодер после потери P-кадров (без артефактов).
        """
        while True:
            time.sleep(0.5)
            now = time.time()
            uids_with_loss = []   # uid у которых в этом цикле найдены протухшие кадры

            with self._buffer_lock:
                for uid in list(self.assembly_info.keys()):
                    to_del = [
                        fid for fid, info in self.assembly_info[uid].items()
                        if now - info['ts'] > 1.0
                    ]
                    for fid in to_del:
                        self.incoming_buffer[uid].pop(fid, None)
                        self.assembly_info[uid].pop(fid, None)
                    if to_del:
                        uids_with_loss.append(uid)

            # Запрашиваем IDR вне лока, rate-limit: не чаще раза в 5 секунд на uid.
            # 2 сек (прежнее значение) создавало "IDR-шторм" при нестабильной сети:
            # каждый IDR-кадр (300 KB при 6 Mbps) временно занимал канал на ~320 мс,
            # что вызывало новые потери → новый запрос → цикл.
            # 5 сек даёт RadminVPN время на восстановление между IDR.
            if self.net and uids_with_loss:
                for uid in uids_with_loss:
                    last_req = self._last_keyframe_req.get(uid, 0.0)
                    if now - last_req >= 5.0:
                        self._last_keyframe_req[uid] = now
                        self.net.request_viewer_keyframe(uid)
                        print(f"[Video] Потеря пакетов uid={uid}: запрошен IDR-кадр")

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

                    # --- ABR: перезапуск энкодера при смене битрейта ---
                    # Проверяем каждую итерацию — изменение редкое (раз в ~4 сек).
                    # Стоимость проверки: одно сравнение int, практически бесплатно.
                    if self._target_bitrate != self._current_bitrate:
                        # Сбрасываем буферы старого энкодера (flush без отправки)
                        try:
                            for _ in codec.encode(None):
                                pass
                        except Exception:
                            pass
                        del codec

                        self._current_bitrate = self._target_bitrate
                        try:
                            codec = self._init_encoder(width, height, fps, self._current_bitrate)
                            # IDR сразу: зритель получит чистый I-кадр после смены
                            self._force_keyframe = True
                            print(f"[Video] ABR: энкодер перезапущен на {self._current_bitrate//1000} kbps")
                        except Exception as e:
                            print(f"[Video] ABR: ошибка перезапуска энкодера: {e}")
                            self.running = False
                            return

                    if frame_np.size == 0:
                        continue

                    src_h, src_w, _ = frame_np.shape

                    # FIX OPT-1: libswscale вместо Pillow.
                    #
                    # Было: Image.fromarray → PIL.resize(BILINEAR) → from_image
                    #   Каждый кадр: Python-аллокация PIL Image + BILINEAR на CPU
                    #   + повторная конвертация RGB→YUV420p внутри FFmpeg = 5-15 мс.
                    #
                    # Стало: from_ndarray(rgb24) → reformat(yuv420p, нужный размер)
                    #   libswscale делает ресайз И конвертацию цветов за ОДИН проход
                    #   на C-уровне без Python-аллокаций — ~0.5-1 мс.
                    #
                    # reformat() вызывается всегда — он no-op если размер/формат совпадает,
                    # поэтому отдельная ветка if src_w != width больше не нужна.
                    raw_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
                    del frame_np
                    frame = raw_frame.reformat(width=width, height=height, format='yuv420p')
                    del raw_frame

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
            # av.VideoCodecContext не имеет метода close() в актуальных версиях PyAV.
            # Flush уже выполнен через encode(None) выше.
            # del codec снимает Python-ссылку → FFmpeg avcodec_free_context() вызывается
            # деструктором Cython-обёртки (PyAV гарантирует это для write-контекстов).
            del codec
            print("[Video] Энкодер закрыт, FFmpeg-буферы освобождены")

    def _fragment_and_send(self, data):
        if not self.net or not self.net.udp_socket_bound:
            return

        self.frame_counter = (self.frame_counter + 1) % 0xFFFFFFFF
        total_len    = len(data)
        chunks_count = (total_len + MAX_VIDEO_PAYLOAD - 1) // MAX_VIDEO_PAYLOAD

        # FIX OPT-3: собираем ВСЕ чанки кадра в список и передаём одним вызовом.
        #
        # Было: send_video_packet() для каждого чанка отдельно.
        #   При переполнении очереди дропался ОДИН чанк — весь кадр шёл с битым
        #   GOP, декодер сыпал артефактами до следующего IDR.
        #
        # Стало: send_video_frame_chunks(list) — дропаем ВЕСЬ старый кадр целиком
        #   или кладём весь новый. Зритель получает либо полный кадр, либо ничего.
        chunks = []
        for i in range(chunks_count):
            start         = i * MAX_VIDEO_PAYLOAD
            end           = min(start + MAX_VIDEO_PAYLOAD, total_len)
            chunk_payload = data[start:end]
            v_header      = VIDEO_CHUNK_HEADER.pack(self.frame_counter, i, chunks_count)
            chunks.append(v_header + chunk_payload)

        self.net.send_video_frame_chunks(chunks)

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
                else:
                    # Не все чанки кадра присутствуют (edge-case: дублированный chunk_idx).
                    # Запрашиваем IDR у стримера, rate-limit 1 раз в 5 сек на uid.
                    if self.net:
                        now_t = time.time()
                        if now_t - self._last_keyframe_req.get(uid, 0.0) >= 5.0:
                            self._last_keyframe_req[uid] = now_t
                            self.net.request_viewer_keyframe(uid)
                            print(f"[Video] Неполный кадр uid={uid}: запрошен IDR-кадр")

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

        # SLICE-threading: параллелизирует декодирование ВНУТРИ одного кадра.
        # В отличие от FRAME-threading, НЕ добавляет задержку декодера:
        #   FRAME: кадр N выдаётся только когда начинается кадр N+1 → +16 мс при 60fps.
        #   SLICE: все слайсы кадра N декодируются параллельно, кадр выдаётся сразу.
        # Для 720p H264 baseline одного потока достаточно с запасом (~1-2 мс/кадр).
        # thread_count=1: без параллелизма (baseline содержит мало слайсов),
        #   зато минимальные накладные расходы синхронизации потоков FFmpeg.
        decoder.thread_type  = 'SLICE'
        decoder.thread_count = 1

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
                        # to_ndarray(format='rgb24') вызывает libswscale напрямую:
                        #   - возвращает C-contiguous массив (np.ascontiguousarray не нужен)
                        #   - на 1 аллокацию меньше vs to_rgb().to_ndarray()
                        img_np = frame.to_ndarray(format='rgb24')
                        h, w, c = img_np.shape
                        q_img = QImage(
                            img_np.data, w, h,
                            w * c,
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
            # SLICE/1 поток: ~5 MB (минимально). AUTO × 8 cores было ~40 MB.
            # av.CodecContext.close() отсутствует в актуальных версиях PyAV —
            # del достаточно: Cython-деструктор вызывает avcodec_free_context().
            del decoder
            gc.collect()
            print(f"[Video] decode_worker uid={uid} завершён, FFmpeg контекст освобождён")