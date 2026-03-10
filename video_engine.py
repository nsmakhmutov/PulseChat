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
#   — _put_frame_safe (заменён на _convert_to_yuv + _enqueue_frame — исправление
#     критического бага: конвертация теперь в capture thread, а не в asyncio loop)
#
# ЧТО СОХРАНЕНО / ПЕРЕИСПОЛЬЗОВАНО:
#   — Логика выбора NVENC/libx264 → перенесена в patch_aiortc_nvenc()
#   — Логика захвата dxcam (_capture_loop) → перенесена в DXCamTrack
#   — frame_received / stream_stats_updated сигналы → API совместимость
#   — cleanup_users, stop_viewer_for_uid → управление зрителями
#   — GC + Windows heap trim при stop_streaming → сохранено
#
# ─────────────────────────────────────────────────────────────────────────────
# ИСПРАВЛЕНИЯ (v2):
#   [FIX-1] _put_frame_safe вызывался через call_soon_threadsafe → выполнялся
#           в asyncio event loop, блокируя WebRTC (NACK/PLI/ICE keepalive).
#           Исправлено: конвертация RGB→YUV теперь в capture thread (_convert_to_yuv),
#           в asyncio loop доставляется только готовый av.VideoFrame (_enqueue_frame).
#
#   [FIX-2] VIDEO_BITRATE из config не применялся к CodecContext. Добавлено
#           ctx.bit_rate = VIDEO_BITRATE в _patched_encode.
#
#   [FIX-3] profile=baseline упоминался в комментарии, но отсутствовал в options
#           x264_profile. Добавлены: profile=baseline, level=3.1, g=60,
#           sc_threshold=0 — снижает нагрузку на декодер у зрителей и стабилизирует
#           битрейт при переходе сцен.
#
#   [FIX-4] patch_aiortc_nvenc: добавлена диагностика если codec_attr is None —
#           раньше патч тихо не применялся без каких-либо сообщений.
#
#   [FIX-5] VideoReceiver.stop(): добавлен вызов track.stop() для быстрого
#           выхода из recv_loop (без ожидания 5-секундного timeout).
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import gc
import threading
import time
from fractions import Fraction

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_BITRATE

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
    Подменяет H264Encoder в aiortc: вместо дефолтного libx264 использует
    аппаратный NVENC / Windows MF / AMF, или тюнингованный libx264 (ultrafast
    baseline) в качестве fallback.

    Вызывать ОДИН РАЗ в client_main.py ДО первого RTCPeerConnection.

    Возвращает True если найден аппаратный кодек, False если используется CPU.

    [FIX-2] Теперь передаёт VIDEO_BITRATE из config в CodecContext.
    [FIX-3] CPU-fallback (libx264) теперь включает profile=baseline, level=3.1,
            g=60, sc_threshold=0 для минимальной нагрузки на декодер у зрителей.
    [FIX-4] Добавлена диагностика если codec_attr не найден в H264Encoder.
    """
    if not (AV_AVAILABLE and AIORTC_AVAILABLE):
        return False

    hw_profiles = [
        # 1. NVIDIA NVENC (если доступен в сборке PyAV)
        {
            'codec': 'h264',
            'name': 'NVIDIA NVENC',
            'options': {
                'preset': 'p4',
                'tune': 'll',
                'zerolatency': '1',

                # Контроль битрейта (эластичность для сети)
                'rc': 'vbr',  # VBR вместо CBR
                'b': str(VIDEO_BITRATE // 2),  # Средний битрейт (позволяем падать на статике)
                'maxrate': str(VIDEO_BITRATE),  # Жесткий верхний лимит
                'bufsize': str(VIDEO_BITRATE),  # Ограничиваем буфер VBV (равен maxrate для минимизации лага)

                # Оптимизация для слабых зрителей
                'bf': '0',  # Отключаем B-кадры: минус задержка, легче декодировать
                'profile': 'main',  # Баланс между сжатием и легкостью декодирования

                # Визуальное качество
                'spatial_aq': '1',  # Адаптивное квантование: делает текст и мелкие детали четче
            },
        },

        # 2. Windows Media Foundation (RTX 5060 / любой GPU с MF-поддержкой)
        {
            'codec': 'h264_mf',
            'name':  'Windows MF (GPU)',
            'options': {
                'scenario':    'livestreaming',
                'quality_vs_speed': '100',  # max скорость
            },
        },

        # 3. AMD AMF
        {
            'codec': 'h264_amf',
            'name':  'AMD AMF',
            'options': {
                'usage':   'lowlatency',
                'quality': 'speed',
            },
        },
    ]

    working_profile = None

    # Ищем доступный аппаратный кодек
    for profile in hw_profiles:
        try:
            _test_ctx = av.CodecContext.create(profile['codec'], 'w')
            _test_ctx.width    = 128
            _test_ctx.height   = 128
            _test_ctx.pix_fmt  = 'yuv420p'
            _test_ctx.open()
            del _test_ctx
            working_profile = profile
            break
        except Exception:
            continue

    # [FIX-3] CPU-fallback: ultrafast + baseline + keyframe каждые 2 секунды.
    # profile=baseline — вдвое меньше нагрузки на декодер у зрителей.
    # g=60 при 30fps — keyframe каждые 2 с, зрители быстро восстанавливают картинку.
    # sc_threshold=0 — запрет keyframe на смене сцены (иначе битрейт скачет).
    x264_profile = {
        'codec': 'libx264',
        'name':  'libx264 (Ultrafast CPU)',
        'options': {
            'preset':        'ultrafast',
            'tune':          'zerolatency',
            'profile':       'baseline',
            'level':         '3.1',
            'g':             '60',
            'sc_threshold':  '0',
        },
    }

    selected_profile = working_profile if working_profile else x264_profile
    print(f"[Video] Выбран видеокодек: {selected_profile['name']}")

    # Монки-патч энкодера aiortc
    try:
        import aiortc.codecs.h264 as _h264_mod

        _OrigEncoder  = _h264_mod.H264Encoder
        _orig_encode  = _OrigEncoder.encode

        _CODEC_ATTRS  = ('_codec', '_encoder', '_context', '_av_codec')
        _active_flag  = [False]
        _warn_flag    = [False]    # [FIX-4] однократное предупреждение

        def _patched_encode(self_enc, frame, force_keyframe: bool = False):
            codec_attr = next(
                (a for a in _CODEC_ATTRS
                 if hasattr(self_enc, a) and getattr(self_enc, a) is None),
                None,
            )

            if codec_attr is not None:
                try:
                    ctx = av.CodecContext.create(selected_profile['codec'], 'w')
                    ctx.options    = selected_profile['options']
                    ctx.width      = frame.width
                    ctx.height     = frame.height
                    ctx.pix_fmt    = 'yuv420p'
                    ctx.time_base  = frame.time_base
                    # [FIX-2] Передаём целевой битрейт из config.
                    # Для hw-кодеков он дублируется в options (выше), здесь
                    # устанавливаем на случай если options не применились.
                    ctx.bit_rate   = VIDEO_BITRATE
                    ctx.open()

                    setattr(self_enc, codec_attr, ctx)

                    if not _active_flag[0]:
                        _active_flag[0] = True
                        print(
                            f"[Video] aiortc H264Encoder: инициализирован "
                            f"{selected_profile['name']}, bitrate={VIDEO_BITRATE//1000} kbps"
                        )

                except Exception as e:
                    print(f"[Video] aiortc H264Encoder: ошибка контекста ({e}) — fallback")

            else:
                # [FIX-4] Если ни одного ожидаемого атрибута не нашли — патч
                # не применится. Печатаем предупреждение один раз.
                if not _active_flag[0] and not _warn_flag[0]:
                    _warn_flag[0] = True
                    available = [a for a in dir(self_enc) if not a.startswith('__')]
                    print(
                        f"[Video] WARN: patch_aiortc_nvenc — codec_attr не найден. "
                        f"Патч не применён. Доступные атрибуты H264Encoder: {available}"
                    )

            return _orig_encode(self_enc, frame, force_keyframe)

        _OrigEncoder.encode = _patched_encode
        return bool(working_profile)

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
            dxcam.get_latest_frame()
            → _convert_to_yuv()     ← тяжёлая конвертация RGB→YUV здесь!
            → loop.call_soon_threadsafe(_enqueue_frame, av_frame)

        asyncio event loop (WebRTC thread):
            _enqueue_frame(av_frame) ← только put_nowait, лёгкая операция
            recv() → queue.get_nowait() → aiortc H264Encoder → RTP

    [FIX-1] Конвертация RGB→YUV выполняется в capture thread, а НЕ в asyncio loop.
    Прежний вариант (_put_frame_safe через call_soon_threadsafe) запускал
    av.VideoFrame.from_ndarray() + reformat() внутри WebRTC event loop, что
    блокировало NACK/PLI/ICE keepalive и порождало подёргивания у зрителей
    при насыщенных сценах (166 MB/s данных при 720p30).

    Очередь maxsize=2:
        При 30fps каждый лишний кадр в очереди = +33 мс задержки.
        2 слота — энкодер не голодает при кратковременных пиках,
        но задержка не накапливается. Старый кадр дропается при переполнении.

    Повтор последнего кадра:
        Если захват не успел положить новый кадр к моменту recv() —
        отдаём предыдущий. Это нормально: чёрный экран хуже чем повтор.

    Параметры:
        monitor_idx — индекс монитора (0 = основной)
        fps         — целевой FPS захвата (совпадает с VIDEO_FPS из config)
        width/height — разрешение выходного потока
    """

    kind = "video"

    def __init__(
        self,
        monitor_idx: int = 0,
        fps:         int = VIDEO_FPS,
        width:       int = VIDEO_WIDTH,
        height:      int = VIDEO_HEIGHT,
    ):
        super().__init__()
        self._monitor_idx = monitor_idx
        self._fps         = fps
        self._width       = width
        self._height      = height

        # asyncio.Queue живёт в event loop WebRTC (устанавливается в start()).
        # Хранит готовые av.VideoFrame (yuv420p) — конвертация выполняется
        # в capture thread (_convert_to_yuv), а не здесь.
        self._queue: asyncio.Queue | None = None
        self._loop:  asyncio.AbstractEventLoop | None = None

        self._capture_thread: threading.Thread | None = None
        self._running = False

        # Кэш последнего захваченного кадра (av.VideoFrame yuv420p).
        # Используется как заглушка если новый кадр ещё не пришёл.
        self._last_frame: 'av.VideoFrame | None' = None

    # ------------------------------------------------------------------
    # Управление жизненным циклом
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Запускает поток захвата экрана.

        loop — asyncio event loop WebRTC (создан в network_engine).
        Вызывать ПОСЛЕ того как loop запущен (threading.Thread + run_forever).
        """
        if self._running:
            return
        self._loop    = loop
        self._queue   = asyncio.Queue(maxsize=2)
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
        self._last_frame = None

    # ------------------------------------------------------------------
    # aiortc интерфейс
    # ------------------------------------------------------------------

    async def recv(self) -> 'av.VideoFrame':
        """
        Вызывается aiortc за каждым кадром (каждые ~1/fps секунд).

        next_timestamp() обеспечивает правильный timing для WebRTC:
            — вычисляет PTS в единицах 90000 Hz clock
            — делает asyncio.sleep до момента следующего кадра

        Очередь хранит готовые av.VideoFrame (yuv420p) — RGB→YUV конвертация
        уже выполнена в capture thread (_convert_to_yuv), поэтому asyncio loop
        НЕ тратит CPU на тяжёлую операцию reformat().

        Если очередь пуста — повторяем последний кадр (лучше чем чёрный экран).
        """
        pts, time_base = await self.next_timestamp()

        av_frame = None
        if self._queue is not None:
            try:
                av_frame = self._queue.get_nowait()
                self._last_frame = av_frame
            except asyncio.QueueEmpty:
                av_frame = self._last_frame

        # Крайний случай: ещё нет ни одного кадра (первые ~33 мс при 30fps)
        if av_frame is None:
            av_frame = av.VideoFrame(self._width, self._height, 'yuv420p')

        av_frame.pts       = pts
        av_frame.time_base = time_base
        return av_frame

    # ------------------------------------------------------------------
    # [FIX-1] Конвертация в capture thread (не в asyncio loop)
    # ------------------------------------------------------------------

    def _convert_to_yuv(self, frame_np: np.ndarray) -> 'av.VideoFrame | None':
        """
        Конвертирует np.ndarray (RGB) в av.VideoFrame (yuv420p).

        Вызывается ТОЛЬКО из capture thread (_capture_loop / _capture_loop_fallback).
        Тяжёлая операция (~83 MB/s при 720p30) выполняется здесь,
        чтобы НЕ блокировать asyncio event loop WebRTC.

        av.VideoFrame.from_ndarray() копирует данные из numpy-массива →
        copy() перед вызовом не нужен.
        """
        try:
            av_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
            if av_frame.width != self._width or av_frame.height != self._height:
                return av_frame.reformat(
                    width=self._width, height=self._height, format='yuv420p'
                )
            return av_frame.reformat(format='yuv420p')
        except Exception as e:
            print(f"[DXCamTrack] Ошибка конвертации кадра: {e}")
            return None

    def _enqueue_frame(self, av_frame: 'av.VideoFrame') -> None:
        """
        Кладёт готовый av.VideoFrame в asyncio.Queue.

        Выполняется в asyncio event loop через call_soon_threadsafe.
        Это единственная операция в event loop — лёгкая (только put_nowait).
        Тяжёлая конвертация уже выполнена в capture thread (_convert_to_yuv).

        Drop-oldest стратегия: если очередь заполнена — выбрасываем старый кадр,
        кладём новый. Новый кадр всегда актуальнее старого.
        """
        if self._queue is None:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(av_frame)
        except asyncio.QueueFull:
            pass

    # ------------------------------------------------------------------
    # Поток захвата (не asyncio)
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
        """
        Основной поток захвата экрана.

        Пробует dxcam.start() (нативный DXGI loop с target_fps).
        При неудаче — fallback на dxcam.grab() с ручным timing.

        [FIX-1] Конвертация RGB→YUV выполняется ЗДЕСЬ через _convert_to_yuv(),
        и только готовый av.VideoFrame передаётся в asyncio loop (_enqueue_frame).
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
                    # [FIX-1] Конвертация в capture thread, а не в asyncio loop.
                    # _convert_to_yuv копирует данные из frame_np →
                    # нет необходимости в frame_np.copy().
                    av_frame = self._convert_to_yuv(frame_np)
                    if av_frame is not None:
                        self._loop.call_soon_threadsafe(
                            self._enqueue_frame, av_frame
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

        [FIX-1] Та же схема: конвертация в capture thread через _convert_to_yuv.
        """
        frame_time = 1.0 / self._fps
        print(f"[DXCamTrack] Fallback grab() @ {self._fps} FPS")

        while self._running:
            t_start = time.perf_counter()
            try:
                frame_np = camera.grab()
                if frame_np is not None and self._loop is not None:
                    # [FIX-1] Конвертация здесь, в capture thread
                    av_frame = self._convert_to_yuv(frame_np)
                    if av_frame is not None:
                        self._loop.call_soon_threadsafe(
                            self._enqueue_frame, av_frame
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

    [FIX-5] stop() теперь вызывает track.stop() для немедленного завершения
    recv_loop без ожидания 5-секундного timeout.
    """

    # Сигналы идентичны старому VideoEngine — ui_main.py не меняется
    frame_received       = pyqtSignal(int, QImage)
    stream_stats_updated = pyqtSignal(int, int, int)   # uid, fps, loss_pct

    _STATS_INTERVAL = 2.0   # секунд между эмитами stream_stats_updated

    def __init__(
        self,
        uid:   int,
        track,
        loop:  asyncio.AbstractEventLoop,
    ):
        super().__init__()
        self.uid      = uid
        self._track   = track
        self._loop    = loop
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
        asyncio.wait_for(timeout=5) — защита от зависания при потере соединения.
        """
        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(self._track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: timeout 5s, жду...")
                    continue
                except Exception as e:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: recv() error — {e}")
                    break

                # ── av.VideoFrame → QImage ───────────────────────────────
                try:
                    # to_ndarray(format='rgb24') — гарантированный способ
                    # без зависимости от Pillow.
                    img_np = frame.to_ndarray(format='rgb24')
                    h, w, c = img_np.shape

                    # QImage не копирует данные — нужен .copy() перед emit.
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
                        # через RTCP NACK — зрителю не нужно об этом знать.
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
        """
        Сигнализирует recv_loop о завершении.

        [FIX-5] Вызывает track.stop() если доступно — это прерывает
        заблокированный track.recv() немедленно, без ожидания 5-секундного
        timeout. Особенно важно при 13 зрителях (было бы до 65 сек задержки).
        """
        self._running = False
        try:
            if hasattr(self._track, 'stop'):
                self._track.stop()
        except Exception:
            pass


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
        (threading.Thread с run_forever). DXCamTrack и VideoReceiver
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
        pass

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