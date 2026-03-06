# video_engine.py — WebRTC видеодвижок (aiortc)
#
# Архитектура после рефакторинга:
#
#   patch_aiortc_nvenc()    — вызвать ОДИН РАЗ в client_main.py ДО первого
#                             RTCPeerConnection. Активирует h264_nvenc в aiortc.
#
#   DXCamTrack              — aiortc VideoStreamTrack: захват экрана через
#                             dxcam (отдельный поток) → asyncio.Queue → recv()
#                             → av.VideoFrame → aiortc H264Encoder → RTP.
#
#   VideoReceiver           — asyncio-корутина: track.recv() → av.VideoFrame
#                             → QImage → frame_received сигнал (UI-поток).
#                             Один экземпляр на каждого стримера (uid).
#
#   VideoEngine             — QObject-менеджер. Публичный API совместим со
#                             старым кодом: frame_received, stream_stats_updated,
#                             start_streaming, stop_streaming, cleanup_users.
#
# ─────────────────────────────────────────────────────────────────────────────
# ЧТО УДАЛЕНО (по сравнению со старым video_engine.py):
#   — UDP-фрагментация (_fragment_and_send, VIDEO_CHUNK_HEADER, MAX_VIDEO_PAYLOAD)
#   — FEC (_fec_buffer, _try_fec_reconstruct, FEC_GROUP_SIZE/FEC_MARKER)
#   — NACK retransmit (_retransmit_buffer, handle_retransmit, CMD_NACK)
#   — Сборка входящих UDP-кадров (incoming_buffer, assembly_info, _frame_cleanup_loop)
#   — Simulcast LQ-поток (codec_lq, set_lq_needed, FLAG_VIDEO_LQ, LQ_VIDEO_*)
#   — Upload ABR (set_bitrate, _target_bitrate, ABR_TIERS, CMD_ADJUST_BITRATE)
#   — Ручной _decode_worker / decode_queues (aiortc декодирует сам)
#   — IDR-management (_last_keyframe_req, _idr_cooldown, request_viewer_keyframe)
#   — Jitter Buffer (JITTER_BUFFER_SIZE — WebRTC берёт на себя)
#
# ЧТО СОХРАНЕНО / ПЕРЕИСПОЛЬЗОВАНО:
#   — Логика выбора NVENC/libx264 → перенесена в patch_aiortc_nvenc()
#   — Логика захвата dxcam (_capture_loop) → перенесена в DXCamTrack
#   — frame_received / stream_stats_updated сигналы → API совместимость
#   — cleanup_users, stop_viewer_for_uid → управление зрителями
#   — GC + Windows heap trim при stop_streaming → сохранено
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import gc
import threading
import time
from fractions import Fraction

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS

# ─── Опциональные зависимости ─────────────────────────────────────────────────

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
    from aiortc import VideoStreamTrack as _AiortcVideoStreamTrack
    AIORTC_AVAILABLE = True
except ImportError:
    AIORTC_AVAILABLE = False
    _AiortcVideoStreamTrack = object   # заглушка для наследования
    print("[Video] ОШИБКА: aiortc не установлен!")


# =============================================================================
# patch_aiortc_nvenc
# =============================================================================

def patch_aiortc_nvenc() -> bool:
    """
    Активирует h264_nvenc в aiortc вместо libx264 (программного кодека).

    Механизм:
        aiortc.codecs.h264.H264Encoder создаёт PyAV CodecContext лениво —
        при первом вызове encode(). Monkey-patch оборачивает этот метод:
        при первой инициализации codec-контекста пробуем 'h264_nvenc' первым.
        При ошибке (нет GPU / нет nvenc-драйвера) — тихо откатываемся к
        оригинальному поведению (libx264).

    Опции nvenc (ULL + VBR):
        preset=p4  — баланс скорость/качество (p1 давал мыло при VBR)
        tune=ull   — Ultra Low Latency, компенсирует задержку p4
        rc=vbr     — тратим биты только там, где есть движение
        cq=28      — целевое качество; при статике GPU тратит 50-200 kbps
        spatial-aq=1 — чёткость текста/UI при низком битрейте
        forced-idr=1 — разрешаем форсированный IDR через frame.pict_type='I'
        delay=0    — нулевая задержка выхода пакетов (критично для стрима)

    Когда вызывать:
        client_main.py, до создания первого RTCPeerConnection (до QApplication
        или сразу после — до LoginWindow._open_connecting → network_engine).

    Возвращает:
        True  — h264_nvenc активирован
        False — nvenc недоступен, aiortc использует libx264 (всё работает)
    """
    if not (AV_AVAILABLE and AIORTC_AVAILABLE):
        return False

    # ── Шаг 1: быстрая проверка доступности h264_nvenc ────────────────────────
    try:
        _test_ctx = av.CodecContext.create('h264_nvenc', 'w')
        _test_ctx.width    = 64
        _test_ctx.height   = 64
        _test_ctx.pix_fmt  = 'yuv420p'
        _test_ctx.time_base = Fraction(1, 30)
        _test_ctx.open()
        del _test_ctx
    except Exception as e:
        print(f"[Video] patch_aiortc_nvenc: h264_nvenc недоступен — {e}")
        print("[Video] aiortc использует libx264 (программный кодек)")
        return False

    # ── Шаг 2: monkey-patch H264Encoder.encode() ──────────────────────────────
    try:
        import aiortc.codecs.h264 as _h264_mod

        _OrigEncoder = _h264_mod.H264Encoder
        _orig_encode = _OrigEncoder.encode

        # Атрибуты для поиска "не инициализированного кодека" в разных версиях aiortc.
        # Проверяем оба имени — в разных версиях может отличаться.
        _CODEC_ATTRS = ('_codec', '_encoder', '_context', '_av_codec')

        # Флаг: первый nvenc-контекст уже создан (для однократного лога)
        _nvenc_active = [False]

        def _patched_encode(self_enc, frame, force_keyframe: bool = False):
            """
            Перехватываем первую инициализацию кодека.
            Если внутренний codec-атрибут ещё None — устанавливаем h264_nvenc.
            Затем вызываем оригинальный encode() — он уже найдёт готовый контекст.
            """
            # Ищем атрибут внутреннего кодека (None = ещё не создан)
            codec_attr = next(
                (a for a in _CODEC_ATTRS
                 if hasattr(self_enc, a) and getattr(self_enc, a) is None),
                None
            )

            if codec_attr is not None:
                # Кодек ещё не инициализирован — внедряем nvenc
                try:
                    ctx = av.CodecContext.create('h264_nvenc', 'w')
                    ctx.width    = frame.width
                    ctx.height   = frame.height
                    ctx.pix_fmt  = 'yuv420p'
                    ctx.time_base = frame.time_base

                    # ULL + VBR: низкая задержка + адаптивный битрейт
                    ctx.options = {
                        'preset':      'p4',
                        'tune':        'ull',
                        'rc':          'vbr',
                        'cq':          '28',
                        'forced-idr':  '1',
                        'delay':       '0',
                        'spatial-aq':  '1',
                    }
                    ctx.open()
                    setattr(self_enc, codec_attr, ctx)

                    if not _nvenc_active[0]:
                        _nvenc_active[0] = True
                        print(
                            f"[Video] aiortc H264Encoder: "
                            f"h264_nvenc активирован (ULL+VBR, cq=28)"
                        )
                except Exception as e:
                    # nvenc не сработал в рантайме — fallback к libx264
                    # (оригинальный encode() создаст libx264 сам)
                    print(
                        f"[Video] aiortc H264Encoder: nvenc init failed ({e}) "
                        f"— libx264 fallback"
                    )

            return _orig_encode(self_enc, frame, force_keyframe)

        _OrigEncoder.encode = _patched_encode
        print("[Video] patch_aiortc_nvenc: monkey-patch применён к H264Encoder")
        return True

    except Exception as e:
        print(f"[Video] patch_aiortc_nvenc: ошибка патча — {e}")
        return False


# =============================================================================
# DXCamTrack — захват экрана как aiortc VideoStreamTrack
# =============================================================================

class DXCamTrack(_AiortcVideoStreamTrack):
    """
    aiortc VideoStreamTrack: захват рабочего стола через dxcam.

    Архитектура двух потоков:
        Поток захвата (threading.Thread):
            dxcam.get_latest_frame() → loop.call_soon_threadsafe → asyncio.Queue

        Корутина recv() (asyncio event loop):
            next_timestamp() → asyncio.Queue.get() → av.VideoFrame → aiortc

    Очередь maxsize=2:
        При 60fps каждый лишний кадр в очереди = +16 мс задержки.
        2 слота — энкодер не голодает при кратковременных пиках,
        но задержка не накапливается. Старый кадр дропается при переполнении.

    Повтор последнего кадра:
        Если захват не успел положить новый кадр к моменту recv() —
        отдаём копию предыдущего (aiortc ожидает кадр каждые 1/fps секунд).
        Это нормально: пустой экран хуже чем повтор.

    Параметры:
        monitor_idx — индекс монитора (0 = основной)
        fps         — целевой FPS захвата (совпадает с VIDEO_FPS из config)
        width/height — разрешение выходного потока (reformat в yuv420p)
    """

    kind = "video"

    def __init__(
        self,
        monitor_idx: int = 0,
        fps: int = VIDEO_FPS,
        width: int = VIDEO_WIDTH,
        height: int = VIDEO_HEIGHT,
    ):
        super().__init__()
        self._monitor_idx = monitor_idx
        self._fps         = fps
        self._width       = width
        self._height      = height

        # asyncio.Queue живёт в event loop WebRTC (устанавливается в start())
        self._queue: asyncio.Queue | None = None
        self._loop:  asyncio.AbstractEventLoop | None = None

        self._capture_thread: threading.Thread | None = None
        self._running = False

        # Кэш последнего захваченного кадра (numpy array RGB)
        # Используется как заглушка если новый кадр ещё не пришёл
        self._last_frame_np: np.ndarray | None = None

    # ------------------------------------------------------------------
    # Управление жизненным циклом
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Запускает поток захвата экрана.

        loop — asyncio event loop WebRTC (создан в network_engine).
        Вызывать ПОСЛЕ того как loop запущен (threading.Thread + asyncio.run).
        """
        if self._running:
            return
        self._loop  = loop
        self._queue = asyncio.Queue(maxsize=2)
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="dxcam-capture",
        )
        self._capture_thread.start()

    def stop(self) -> None:
        """Останавливает захват и освобождает ресурсы dxcam."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None
        self._last_frame_np = None

    # ------------------------------------------------------------------
    # aiortc интерфейс
    # ------------------------------------------------------------------

    async def recv(self) -> 'av.VideoFrame':
        """
        Вызывается aiortc за каждым кадром (каждые ~1/fps секунд).

        next_timestamp() обеспечивает правильный timing для WebRTC:
            — вычисляет PTS в единицах 90000 Hz clock
            — делает asyncio.sleep до момента следующего кадра
        После пробуждения берём самый свежий кадр из очереди.
        Если очередь пуста — повторяем последний (предпочтительнее чёрного экрана).
        """
        pts, time_base = await self.next_timestamp()

        # Берём кадр без ожидания (захват быстрее recv на fast FPS)
        frame_np = None
        if self._queue is not None:
            try:
                frame_np = self._queue.get_nowait()
                self._last_frame_np = frame_np
            except asyncio.QueueEmpty:
                frame_np = self._last_frame_np

        # Крайний случай: ещё нет ни одного кадра (первые ~16 мс)
        if frame_np is None:
            frame_np = np.zeros((self._height, self._width, 3), dtype=np.uint8)

        # rgb24 → yuv420p (формат, который ждёт H264Encoder)
        av_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
        if av_frame.width != self._width or av_frame.height != self._height:
            av_frame = av_frame.reformat(
                width=self._width, height=self._height, format='yuv420p'
            )
        else:
            av_frame = av_frame.reformat(format='yuv420p')

        av_frame.pts       = pts
        av_frame.time_base = time_base
        return av_frame

    # ------------------------------------------------------------------
    # Поток захвата (не asyncio)
    # ------------------------------------------------------------------

    def _put_frame_safe(self, frame_np: np.ndarray) -> None:
        """
        Кладёт кадр в asyncio.Queue из threading-потока.
        Вызывается через loop.call_soon_threadsafe — thread-safe.
        При переполнении дропает старый кадр (drop-oldest стратегия).
        """
        if self._queue is None:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(frame_np)
        except asyncio.QueueFull:
            pass

    def _capture_loop(self) -> None:
        """
        Основной поток захвата экрана.

        Пробует dxcam.start() (нативный DXGI loop с target_fps).
        При неудаче — fallback на dxcam.grab() с ручным timing.
        """
        camera = None
        try:
            camera = dxcam.create(output_idx=self._monitor_idx, output_color="RGB")
            if not camera:
                print(f"[DXCamTrack] Монитор {self._monitor_idx} не найден")
                self._running = False
                return
        except Exception as e:
            print(f"[DXCamTrack] Ошибка инициализации dxcam: {e}")
            self._running = False
            return

        try:
            camera.start(target_fps=self._fps, video_mode=True)
            print(
                f"[DXCamTrack] dxcam запущен: монитор={self._monitor_idx}, "
                f"fps={self._fps}, out={self._width}×{self._height}"
            )
        except Exception as e:
            print(f"[DXCamTrack] dxcam.start() не удался: {e}, fallback to grab()")
            self._capture_loop_fallback(camera)
            return

        # ── Основной цикл захвата ────────────────────────────────────────
        while self._running:
            try:
                frame_np = camera.get_latest_frame()
                if frame_np is not None and self._loop is not None:
                    # copy() критично: dxcam может переиспользовать буфер
                    self._loop.call_soon_threadsafe(
                        self._put_frame_safe, frame_np.copy()
                    )
            except Exception as e:
                print(f"[DXCamTrack] Ошибка захвата: {e}")
                time.sleep(0.1)

        # ── Очистка ──────────────────────────────────────────────────────
        try:
            camera.stop()
        except Exception:
            pass
        try:
            camera.release()
            print("[DXCamTrack] D3D11 ресурсы освобождены")
        except Exception:
            pass
        del camera

    def _capture_loop_fallback(self, camera) -> None:
        """
        Fallback: ручной grab() с sleep-таймингом.
        Используется если dxcam не поддерживает video_mode на данном GPU/ОС.
        """
        frame_time = 1.0 / self._fps
        print(f"[DXCamTrack] Fallback grab() @ {self._fps} FPS")

        while self._running:
            t_start = time.perf_counter()
            try:
                frame_np = camera.grab()
                if frame_np is not None and self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        self._put_frame_safe, frame_np.copy()
                    )
                elapsed = time.perf_counter() - t_start
                sleep_t = frame_time - elapsed
                if sleep_t > 0:
                    time.sleep(sleep_t)
            except Exception as e:
                print(f"[DXCamTrack] Fallback grab error: {e}")
                time.sleep(1.0)

        try:
            camera.release()
        except Exception:
            pass
        del camera


# =============================================================================
# VideoReceiver — WebRTC трек → QImage → frame_received сигнал
# =============================================================================

class VideoReceiver(QObject):
    """
    Принимает один видеотрек от WebRTC и конвертирует кадры в QImage.

    Жизненный цикл:
        1. Создаётся в VideoEngine.add_receiver(uid, track)
        2. _recv_loop() запускается через asyncio.run_coroutine_threadsafe
           в WebRTC event loop (отдельный threading.Thread)
        3. Каждый av.VideoFrame → rgb24 → QImage → emit frame_received
        4. Сигнал Qt доставляет QImage в UI-поток через queued connection

    Потокобезопасность:
        pyqtSignal.emit() из asyncio (другой thread) — безопасно.
        Qt доставляет сигнал в UI-поток через event loop автоматически.
        q_img.copy() гарантирует, что данные не освободятся до рендера.

    Статистика:
        stream_stats_updated эмитируется каждые 2 секунды.
        fps  — реальный декодированный FPS
        loss — 0 (WebRTC обрабатывает потери через RTCP/NACK сам)
    """

    # Сигналы идентичны старому VideoEngine — ui_main.py не меняется
    frame_received       = pyqtSignal(int, QImage)
    stream_stats_updated = pyqtSignal(int, int, int)   # uid, fps, loss_pct

    _STATS_INTERVAL = 2.0   # секунд между эмитами stream_stats_updated

    def __init__(
        self,
        uid: int,
        track,
        loop: asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self.uid    = uid
        self._track = track
        self._loop  = loop
        self._running = True

        # Счётчики для stream_stats_updated
        self._stats_decoded   = 0
        self._stats_last_time = time.monotonic()

        # Запускаем recv-корутину в WebRTC event loop
        asyncio.run_coroutine_threadsafe(self._recv_loop(), loop)

    # ------------------------------------------------------------------
    # Asyncio recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """
        Основной цикл приёма кадров от WebRTC трека.

        track.recv() блокируется до следующего RTP-пакета.
        Используем asyncio.wait_for(timeout=5) чтобы не зависнуть при
        потере соединения — loop завершится и VideoReceiver освободится.
        """
        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(self._track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Нет кадров 5 секунд — стрим завис или стример отключился
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: timeout 5s, жду...")
                    continue
                except Exception as e:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: recv() error — {e}")
                    break

                # ── av.VideoFrame → QImage ───────────────────────────────
                try:
                    # to_ndarray(format='rgb24') — единственный гарантированный
                    # способ. frame.to_image() → PIL требует Pillow, исключаем
                    # дополнительную зависимость.
                    img_np = frame.to_ndarray(format='rgb24')
                    h, w, c = img_np.shape

                    # QImage не копирует данные — нужен .copy() перед emit.
                    # q_img.copy() создаёт независимую копию буфера,
                    # img_np может быть удалён сразу после.
                    q_img = QImage(
                        img_np.data, w, h, w * c,
                        QImage.Format.Format_RGB888
                    )
                    self.frame_received.emit(self.uid, q_img.copy())

                    self._stats_decoded += 1
                    del img_np, q_img

                    # ── Статистика раз в 2 секунды ───────────────────────
                    now = time.monotonic()
                    if now - self._stats_last_time >= self._STATS_INTERVAL:
                        elapsed = max(now - self._stats_last_time, 0.001)
                        fps = int(self._stats_decoded / elapsed)
                        # loss_pct = 0: WebRTC управляет повтором и потерями
                        # через RTCP NACK сам — зрителю не нужно об этом знать.
                        # При необходимости расширить через pc.getStats().
                        self.stream_stats_updated.emit(self.uid, fps, 0)
                        self._stats_decoded   = 0
                        self._stats_last_time = now

                except Exception as e:
                    print(f"[VideoReceiver] uid={self.uid}: frame decode error — {e}")

        finally:
            self._running = False
            print(f"[VideoReceiver] uid={self.uid}: recv_loop завершён")

    # ------------------------------------------------------------------
    # Остановка
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Сигнализирует recv_loop о завершении (loop сам завершится при timeout)."""
        self._running = False


# =============================================================================
# VideoEngine — менеджер (совместимый публичный API)
# =============================================================================

class VideoEngine(QObject):
    """
    Менеджер WebRTC видео. Публичный API совместим со старым кодом.

    Стример:
        start_streaming() → создаёт DXCamTrack
        get_dxcam_track() → возвращает трек для network_engine → RTCPeerConnection

    Зритель:
        add_receiver(uid, track) → создаёт VideoReceiver, подключает сигналы
        frame_received(uid, QImage) → прокидывается из VideoReceiver в MainWindow

    Совместимость:
        frame_received       — тот же сигнал, ui_main.py не меняется
        stream_stats_updated — тот же сигнал, VideoWindow.update_stream_stats() работает
        stop_viewer_for_uid  — то же имя метода
        cleanup_users        — то же имя метода

    WebRTC loop:
        Устанавливается через set_webrtc_loop() из network_engine
        после запуска asyncio потока. DXCamTrack и VideoReceiver используют
        этот loop для asyncio-операций.
    """

    # Сигналы — идентичны старому VideoEngine (ui_main.py, ui_video.py не меняются)
    frame_received       = pyqtSignal(int, QImage)
    stream_stats_updated = pyqtSignal(int, int, int)   # uid, fps, loss_pct

    def __init__(self, net_client):
        super().__init__()
        self.net = net_client

        self._dxcam_track: DXCamTrack | None = None
        self._receivers:   dict[int, VideoReceiver] = {}

        # asyncio loop WebRTC — устанавливается из network_engine
        self._webrtc_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # WebRTC loop
    # ------------------------------------------------------------------

    def set_webrtc_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Устанавливает asyncio event loop для WebRTC.

        Вызывается из network_engine сразу после запуска asyncio потока
        (threading.Thread с asyncio.run). DXCamTrack и VideoReceiver
        используют этот loop для thread-safe операций.
        """
        self._webrtc_loop = loop
        print("[VideoEngine] WebRTC asyncio loop установлен")

    # ------------------------------------------------------------------
    # Стример: управление DXCamTrack
    # ------------------------------------------------------------------

    def start_streaming(self, settings: dict | None = None) -> bool:
        """
        Создаёт и запускает DXCamTrack для WebRTC-стрима.

        settings (опционально):
            monitor_idx — индекс монитора (default: 0)
            fps         — FPS захвата     (default: VIDEO_FPS из config)
            width       — ширина потока   (default: VIDEO_WIDTH)
            height      — высота потока   (default: VIDEO_HEIGHT)

        Возвращает False если:
            — aiortc / av / dxcam не установлены
            — стрим уже запущен
            — WebRTC loop ещё не установлен
        """
        if not (AV_AVAILABLE and DXCAM_AVAILABLE and AIORTC_AVAILABLE):
            print("[VideoEngine] start_streaming: отсутствуют зависимости")
            return False
        if self._dxcam_track is not None:
            print("[VideoEngine] start_streaming: стрим уже запущен")
            return False
        if self._webrtc_loop is None:
            print("[VideoEngine] start_streaming: WebRTC loop не установлен")
            return False

        s = settings or {}
        self._dxcam_track = DXCamTrack(
            monitor_idx = s.get("monitor_idx", 0),
            fps         = s.get("fps",         VIDEO_FPS),
            width       = s.get("width",       VIDEO_WIDTH),
            height      = s.get("height",      VIDEO_HEIGHT),
        )
        self._dxcam_track.start(self._webrtc_loop)
        print(
            f"[VideoEngine] DXCamTrack запущен: "
            f"{s.get('width', VIDEO_WIDTH)}×{s.get('height', VIDEO_HEIGHT)} "
            f"@ {s.get('fps', VIDEO_FPS)} fps"
        )
        return True

    def stop_streaming(self) -> None:
        """
        Останавливает DXCamTrack и освобождает память.

        Windows heap trim через SetProcessWorkingSetSizeEx:
            Принудительно освобождает рабочее множество процесса.
            Эффективно возвращает память ОС после завершения стрима
            (PyAV и dxcam аллоцируют значительные нативные буферы).
        """
        if self._dxcam_track is not None:
            self._dxcam_track.stop()
            self._dxcam_track = None

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
            print("[VideoEngine] Стрим остановлен: GC + Windows heap trim выполнен")
        except Exception:
            print("[VideoEngine] Стрим остановлен, GC выполнен")

    def get_dxcam_track(self) -> DXCamTrack | None:
        """
        Возвращает активный DXCamTrack для передачи в RTCPeerConnection.

        Вызывается из network_engine при создании WebRTC offer:
            pc.addTrack(video_engine.get_dxcam_track())
        """
        return self._dxcam_track

    # ------------------------------------------------------------------
    # Зрители: управление VideoReceiver
    # ------------------------------------------------------------------

    def add_receiver(self, uid: int, track) -> VideoReceiver:
        """
        Создаёт VideoReceiver для нового зрителя (uid).

        Вызывается из network_engine при получении WebRTC видеотрека
        (RTCPeerConnection.on("track") callback).

        Если для uid уже существует VideoReceiver — останавливает старый
        (переподключение стримера без перезапуска приложения).

        Сигналы VideoReceiver проксируются в VideoEngine:
            receiver.frame_received       → self.frame_received
            receiver.stream_stats_updated → self.stream_stats_updated
        Это сохраняет совместимость: MainWindow подключён к VideoEngine,
        ничего не меняется в ui_main.py.
        """
        if self._webrtc_loop is None:
            print(f"[VideoEngine] add_receiver uid={uid}: WebRTC loop не установлен!")
            raise RuntimeError("WebRTC loop не установлен. Вызовите set_webrtc_loop() первым.")

        # Останавливаем предыдущий receiver если есть (переподключение)
        if uid in self._receivers:
            self._receivers[uid].stop()

        receiver = VideoReceiver(uid, track, self._webrtc_loop)
        # Проксируем сигналы наверх (в MainWindow)
        receiver.frame_received.connect(self.frame_received)
        receiver.stream_stats_updated.connect(self.stream_stats_updated)
        self._receivers[uid] = receiver

        print(f"[VideoEngine] VideoReceiver создан для uid={uid}")
        return receiver

    def stop_viewer_for_uid(self, uid: int) -> None:
        """
        Останавливает VideoReceiver для отключившегося зрителя.

        Вызывается из network_engine / MainWindow когда:
            — стример нажал «Стоп трансляцию»
            — зритель нажал «Прекратить просмотр»
            — WebRTC соединение закрылось
        """
        receiver = self._receivers.pop(uid, None)
        if receiver is not None:
            receiver.stop()
        print(f"[VideoEngine] stop_viewer_for_uid({uid})")

    # ------------------------------------------------------------------
    # Заглушки для инкрементальной миграции
    # ------------------------------------------------------------------
    # Следующие методы были частью UDP-стека и удалены по плану рефакторинга.
    # Заглушки нужны чтобы не падать AttributeError пока network_engine.py
    # ещё не обновлён (Шаги 2→5 миграции выполняются не одновременно).
    # После обновления network_engine.py — эти заглушки удалить.
    # ------------------------------------------------------------------

    def process_incoming_packet(self, uid, data, is_lq: bool = False) -> None:
        """
        УСТАРЕЛО: заменено WebRTC треком.
        UDP-сборка кадров → VideoReceiver.recv() через aiortc.
        Заглушка: вызов игнорируется.
        """
        pass  # WebRTC трек заменяет UDP сборку

    def force_keyframe(self) -> None:
        """
        УСТАРЕЛО: WebRTC управляет IDR/keyframe через RTCP PLI автоматически.
        aiortc отправляет Picture Loss Indication при ошибке декодирования.
        Заглушка: вызов игнорируется.
        """
        pass

    def set_bitrate(self, new_bitrate: int) -> None:
        """
        УСТАРЕЛО: WebRTC управляет битрейтом через TWCC (Transport-Wide CC).
        ABR-таблица по RTT заменена congestion control в aiortc.
        Заглушка: вызов игнорируется.
        """
        pass

    def set_lq_needed(self, needed: bool) -> None:
        """
        УСТАРЕЛО: simulcast LQ-поток через UDP удалён.
        WebRTC SFU управляет слоями качества (Scalable Video Coding).
        Заглушка: вызов игнорируется.
        """
        pass

    def handle_retransmit(self, frame_id: int, chunk_idx: int) -> None:
        """
        УСТАРЕЛО: NACK-retransmit через UDP удалён.
        WebRTC NACK управляется aiortc прозрачно на уровне RTP.
        Заглушка: вызов игнорируется.
        """
        pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_users(self, active_uids) -> None:
        """
        Останавливает VideoReceiver для всех uid не из active_uids.

        Вызывается из MainWindow при обновлении списка пользователей
        (CMD_SYNC_USERS от сервера). Аналог старого cleanup_users.

        active_uids — set или список uid активных пользователей.
        """
        for uid in list(self._receivers.keys()):
            if uid not in active_uids:
                self.stop_viewer_for_uid(uid)