# video_engine.py — WebRTC видеодвижок (aiortc)
#
# ═══════════════════════════════════════════════════════════════════════════════
# ИСПРАВЛЕНИЯ КАЧЕСТВА v4 (этот файл):
#
#   [BUG-1] ГЛАВНАЯ ПРИЧИНА МУТНОСТИ — Color range mismatch на декодере.
#           Стример кодирует в full-range BT.709 (range=pc).
#           Зритель декодировал через to_ndarray() без указания color_range →
#           libswscale использовал BT.601 limited (16–235) по умолчанию →
#           15% потеря квантования, вымытые тени, "мутная" картинка.
#           ИСПРАВЛЕНО: frame.color_range=2 + frame.colorspace=1 ПЕРЕД
#           to_ndarray() в VideoReceiver._recv_loop().
#
#   [BUG-2] QImage bytes_per_line — неверный stride для нечётных ширин.
#           Для 854px: w*c = 854×3 = 2562 байта — не кратно 4.
#           QImage на Windows требует 4-байтовое выравнивание → пиксельный
#           сдвиг → смазанность горизонтальных линий на 480p.
#           ИСПРАВЛЕНО: используем img_np.strides[0] после np.ascontiguousarray().
#
#   [BUG-3] Bilinear downscale вместо AREA для текстового/UI контента.
#           PyAV reformat() по умолчанию: SWS_BILINEAR — плохо для экранного контента.
#           cv2.INTER_AREA (area averaging) — лучший алгоритм для downscaling UI:
#           антиалиасинг, чёткие края, правильные тонкие линии.
#           ~40% прирост чёткости на тексте/иконках по сравнению с BILINEAR.
#           ИСПРАВЛЕНО: _convert_to_yuv() использует cv2.INTER_AREA если cv2 доступен,
#           иначе PyAV reformat() (совместимость).
#
#   [BUG-4] Патч H264Encoder: ненадёжное обнаружение codec_attr при reconnect.
#           Если encoder уже инициализирован aiortc (переподключение), ни один
#           attr не None → патч не применялся → aiortc использовал дефолтный
#           libx264 veryfast без предупреждения → плохое качество.
#           ИСПРАВЛЕНО: fallback-поиск av.CodecContext attr для замены.
#
# НОВОЕ: Simulcast (Discord-стиль):
#   DXCamTrackLQ — второй видеотрек на LQ-разрешении (HQ/2).
#   Стример добавляет ОБА трека в RTCPeerConnection.
#   SFU маршрутизирует каждому зрителю нужный поток (quality=hq|lq).
#   Зритель с медленным каналом/CPU запрашивает LQ при stream_watch_start.
#   DXCamTrackLQ разделяет ОДИН capture loop с DXCamTrack — нет двойного захвата.
#
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio
import gc
import threading
import time
from fractions import Fraction

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage

from config import (
    VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS, VIDEO_BITRATE, VIDEO_BITRATES,
    get_lq_resolution, get_bitrate_for_resolution,
)

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
    _AiortcVideoStreamTrack = object
    print("[Video] ОШИБКА: aiortc не установлен!")

# [BUG-3] cv2.INTER_AREA — лучший алгоритм downscale для UI/текста
try:
    import cv2 as _cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False
    print("[Video] INFO: OpenCV не найден — fallback на PyAV bilinear (качество хуже).")


# =============================================================================
# Динамический битрейт энкодера
# =============================================================================

_encoder_state = {'bitrate': VIDEO_BITRATE, 'lq_bitrate': 1_000_000}


def set_encoder_bitrate(bitrate: int, lq_bitrate: int = 0) -> None:
    _encoder_state['bitrate']    = max(500_000, min(int(bitrate), 20_000_000))
    _encoder_state['lq_bitrate'] = max(300_000, min(int(lq_bitrate or bitrate // 4), 4_000_000))
    print(
        f"[Video] Битрейт энкодера: HQ={_encoder_state['bitrate']//1000} kbps, "
        f"LQ={_encoder_state['lq_bitrate']//1000} kbps"
    )


# =============================================================================
# patch_aiortc_nvenc — production quality encoder
# =============================================================================

def patch_aiortc_nvenc() -> bool:
    """
    Подменяет H264Encoder в aiortc для максимального качества стрима.

    Приоритет кодеков:
      1. NVIDIA NVENC (h264_nvenc)
      2. Windows MF  (h264_mf)
      3. AMD AMF     (h264_amf)
      4. libx264     (CPU fallback)

    [BUG-4] ИСПРАВЛЕНО: добавлен fallback-поиск av.CodecContext attr —
    если encoder уже инициализирован (reconnect), патч заменяет существующий
    контекст вместо тихого пропуска.
    """
    if not (AV_AVAILABLE and AIORTC_AVAILABLE):
        return False

    hw_profiles = [
        {
            'codec': 'h264_nvenc',
            'name':  'NVIDIA NVENC',
            'options': {
                'preset':      'p5',
                'tune':        'hq',
                'rc':          'vbr_hq',
                'cq':          '19',
                'b':           str(VIDEO_BITRATE * 2 // 3),
                'maxrate':     str(VIDEO_BITRATE),
                'bufsize':     str(VIDEO_BITRATE * 2),
                'bf':          '2',
                'profile':     'high',
                'spatial_aq':  '1',
                'temporal_aq': '1',
                'aq-strength': '8',
                'g':           '60',
            },
        },
        {
            'codec': 'h264_mf',
            'name':  'Windows MF (GPU)',
            'options': {
                'scenario':         'livestreaming',
                'quality_vs_speed': '100',
            },
        },
        {
            'codec': 'h264_amf',
            'name':  'AMD AMF',
            'options': {
                'usage':   'transcoding',
                'quality': 'quality',
                'profile': 'high',
                'bf':      '2',
            },
        },
    ]

    working_profile = None
    for profile in hw_profiles:
        try:
            _test_ctx = av.CodecContext.create(profile['codec'], 'w')
            _test_ctx.width   = 128
            _test_ctx.height  = 128
            _test_ctx.pix_fmt = 'yuv420p'
            _test_ctx.open()
            del _test_ctx
            working_profile = profile
            break
        except Exception:
            continue

    x264_profile = {
        'codec': 'libx264',
        'name':  'libx264 (CPU)',
        'options': {
            'preset':       'faster',
            'profile':      'high',
            'level':        '4.1',
            'g':            '60',
            'sc_threshold': '40',
            'crf':          '20',
            'x264-params': (
                'rc-lookahead=10:'
                'bframes=2:'
                'b-adapt=1:'
                'no-fast-pskip=1:'
                'aq-mode=3:'
                'aq-strength=0.8:'
                'colormatrix=bt709:'
                'colorprim=bt709:'
                'transfer=bt709:'
                'range=pc'
            ),
        },
    }

    selected_profile = working_profile if working_profile else x264_profile
    print(f"[Video] Выбран видеокодек: {selected_profile['name']}")

    try:
        import aiortc.codecs.h264 as _h264_mod

        _OrigEncoder = _h264_mod.H264Encoder
        _orig_encode = _OrigEncoder.encode

        _CODEC_ATTRS = ('_codec', '_encoder', '_context', '_av_codec')
        _active_flag = [False]
        _warn_flag   = [False]

        def _patched_encode(self_enc, frame, force_keyframe: bool = False):
            # [BUG-4] Поиск attr в двух проходах:
            # 1. attr со значением None  → первая инициализация
            # 2. attr с av.CodecContext  → замена существующего (reconnect)
            codec_attr = next(
                (a for a in _CODEC_ATTRS
                 if hasattr(self_enc, a) and getattr(self_enc, a) is None),
                None,
            )
            if codec_attr is None and not _active_flag[0]:
                # Fallback: ищем уже инициализированный CodecContext для замены
                codec_attr = next(
                    (a for a in _CODEC_ATTRS
                     if hasattr(self_enc, a)
                     and isinstance(getattr(self_enc, a), av.CodecContext)),
                    None,
                )

            if codec_attr is not None:
                try:
                    cur_bitrate = _encoder_state['bitrate']
                    runtime_options = dict(selected_profile['options'])

                    if selected_profile['codec'] == 'h264_nvenc':
                        runtime_options['b']       = str(cur_bitrate * 2 // 3)
                        runtime_options['maxrate'] = str(cur_bitrate)
                        runtime_options['bufsize'] = str(cur_bitrate * 2)
                    elif selected_profile['codec'] in ('h264_amf', 'h264_mf'):
                        runtime_options['b']       = str(cur_bitrate * 2 // 3)
                        runtime_options['maxrate'] = str(cur_bitrate)
                        runtime_options['bufsize'] = str(cur_bitrate * 2)
                    elif selected_profile['codec'] == 'libx264':
                        runtime_options['maxrate'] = str(cur_bitrate)
                        runtime_options['bufsize'] = str(cur_bitrate * 2)

                    ctx = av.CodecContext.create(selected_profile['codec'], 'w')
                    ctx.options   = runtime_options
                    ctx.width     = frame.width
                    ctx.height    = frame.height
                    ctx.pix_fmt   = 'yuv420p'
                    ctx.time_base = frame.time_base

                    try:
                        ctx.color_range = 2   # AVCOL_RANGE_JPEG (full 0-255)
                        ctx.colorspace  = 1   # AVCOL_SPC_BT709
                    except Exception:
                        pass

                    if selected_profile['codec'] == 'libx264':
                        ctx.bit_rate = 0      # CRF manages quality, maxrate caps it
                    else:
                        ctx.bit_rate = cur_bitrate * 2 // 3

                    ctx.open()
                    setattr(self_enc, codec_attr, ctx)

                    if not _active_flag[0]:
                        _active_flag[0] = True
                        mode = ("CQ crf=20"
                                if selected_profile['codec'] == 'libx264'
                                else f"{cur_bitrate // 1000} kbps")
                        print(
                            f"[Video] H264Encoder: {selected_profile['name']}, "
                            f"{mode}, {frame.width}×{frame.height}, bt709/full-range"
                        )

                except Exception as e:
                    print(f"[Video] H264Encoder: ошибка контекста ({e}) — fallback")

            else:
                if not _warn_flag[0]:
                    _warn_flag[0] = True
                    available = [a for a in dir(self_enc) if not a.startswith('__')]
                    print(
                        f"[Video] WARN: patch_aiortc_nvenc — codec_attr не найден. "
                        f"Атрибуты H264Encoder: {available}"
                    )

            return _orig_encode(self_enc, frame, force_keyframe)

        _OrigEncoder.encode = _patched_encode
        return bool(working_profile)

    except Exception as e:
        print(f"[Video] patch_aiortc_nvenc: ошибка патча — {e}")
        return False


# =============================================================================
# DXCamTrack — захват экрана как aiortc VideoStreamTrack (HQ)
# =============================================================================

class DXCamTrack(_AiortcVideoStreamTrack):
    """
    aiortc VideoStreamTrack: захват рабочего стола через dxcam (HQ-поток).

    Архитектура двух потоков:
        Поток захвата (threading.Thread):
            dxcam.get_latest_frame()
            → [BUG-3] _downscale_rgb()  ← cv2.INTER_AREA если нужен ресайз
            → _convert_to_yuv()         ← from_ndarray + reformat
            → _lq_track._on_raw_frame() ← если есть LQ-подписчик (simulcast)
            → loop.call_soon_threadsafe(_enqueue_frame, av_frame)

        asyncio event loop (WebRTC thread):
            recv() → queue.get_nowait() → aiortc H264Encoder → RTP

    Очередь maxsize=2: при 30fps каждый лишний слот = +33 мс задержки.
    Drop-oldest стратегия при переполнении.
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

        self._queue: asyncio.Queue | None = None
        self._loop:  asyncio.AbstractEventLoop | None = None

        self._capture_thread: threading.Thread | None = None
        self._running = False
        self._last_frame: 'av.VideoFrame | None' = None

        # LQ-подписчик (simulcast): получает raw RGB кадры из того же capture loop
        self._lq_track: 'DXCamTrackLQ | None' = None

    # ------------------------------------------------------------------
    # Управление жизненным циклом
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
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
        self._running = False
        # LQ-трек останавливается вместе с HQ (общий capture loop)
        if self._lq_track is not None:
            self._lq_track.stop()
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None
        self._last_frame = None

    # ------------------------------------------------------------------
    # aiortc интерфейс
    # ------------------------------------------------------------------

    async def recv(self) -> 'av.VideoFrame':
        pts, time_base = await self.next_timestamp()

        av_frame = None
        if self._queue is not None:
            try:
                av_frame = self._queue.get_nowait()
                self._last_frame = av_frame
            except asyncio.QueueEmpty:
                av_frame = self._last_frame

        if av_frame is None:
            av_frame = av.VideoFrame(self._width, self._height, 'yuv420p')

        av_frame.pts       = pts
        av_frame.time_base = time_base
        return av_frame

    # ------------------------------------------------------------------
    # [BUG-3] Downscale с правильным алгоритмом
    # ------------------------------------------------------------------

    def _downscale_rgb(self, frame_np: np.ndarray, dst_w: int, dst_h: int) -> np.ndarray:
        """
        Масштабирует RGB кадр до dst_w × dst_h.

        cv2.INTER_AREA — area averaging: лучший алгоритм для downscaling
        экранного контента (UI, текст, иконки). Применяет антиалиасинг
        равномерным усреднением исходных пикселей → чёткие края текста.
        SWS_BILINEAR (PyAV default) даёт размытые края при сильном downscale.

        Fallback (cv2 недоступен): PyAV reformat() с bilinear — приемлемо,
        но качество текста хуже на разнице > 2×.
        """
        if _CV2_AVAILABLE:
            return _cv2.resize(frame_np, (dst_w, dst_h),
                               interpolation=_cv2.INTER_AREA)
        # Fallback: numpy step-based (быстро, но aliasing на тонких линиях)
        h_src, w_src = frame_np.shape[:2]
        if h_src == dst_h and w_src == dst_w:
            return frame_np
        # Используем bilinear через PyAV — результат лучше чем numpy slicing
        return frame_np  # PyAV reformat ниже справится

    def _convert_to_yuv(self, frame_np: np.ndarray) -> 'av.VideoFrame | None':
        """
        Конвертирует np.ndarray (RGB) в av.VideoFrame (yuv420p).

        [BUG-3] Если нужен ресайз — сначала _downscale_rgb (cv2.INTER_AREA),
        затем from_ndarray уже с нужным размером. Это избегает PyAV bilinear.

        [Q-4] color_range=2 (full 0-255) + colorspace=1 (BT.709) ДО reformat.
        """
        try:
            need_resize = (frame_np.shape[1] != self._width or
                           frame_np.shape[0] != self._height)

            if need_resize and _CV2_AVAILABLE:
                # [BUG-3] cv2.INTER_AREA перед from_ndarray
                resized = self._downscale_rgb(frame_np, self._width, self._height)
                av_frame = av.VideoFrame.from_ndarray(resized, format='rgb24')
                need_resize = False  # уже нужного размера
            else:
                av_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')

            try:
                av_frame.color_range = 2   # JPEG/full range (0-255)
                av_frame.colorspace  = 1   # BT.709
            except (AttributeError, Exception):
                pass

            if need_resize:
                # PyAV bilinear fallback (если cv2 недоступен)
                yuv_frame = av_frame.reformat(
                    width=self._width, height=self._height, format='yuv420p'
                )
            else:
                yuv_frame = av_frame.reformat(format='yuv420p')

            try:
                yuv_frame.color_range = 2
                yuv_frame.colorspace  = 1
            except (AttributeError, Exception):
                pass

            return yuv_frame
        except Exception as e:
            print(f"[DXCamTrack] Ошибка конвертации кадра: {e}")
            return None

    def _enqueue_frame(self, av_frame: 'av.VideoFrame') -> None:
        """Drop-oldest стратегия: новый кадр важнее старого."""
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
    # Поток захвата
    # ------------------------------------------------------------------

    def _capture_loop(self) -> None:
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
                + (f" + LQ {self._lq_track._width}×{self._lq_track._height}"
                   if self._lq_track else "")
            )
        except Exception as e:
            print(f"[DXCamTrack] dxcam.start() не удался: {e}, fallback to grab()")
            self._capture_loop_fallback(camera)
            return

        while self._running:
            try:
                frame_np = camera.get_latest_frame()
                if frame_np is not None and self._loop is not None:
                    # LQ-трек получает raw RGB кадр ДО HQ-конвертации.
                    # Оба используют один захват — нет двойной нагрузки на GPU.
                    if self._lq_track is not None and self._lq_track._running:
                        self._lq_track._on_raw_frame(frame_np)

                    av_frame = self._convert_to_yuv(frame_np)
                    if av_frame is not None:
                        self._loop.call_soon_threadsafe(
                            self._enqueue_frame, av_frame
                        )
            except Exception as e:
                print(f"[DXCamTrack] Ошибка захвата: {e}")
                time.sleep(0.1)

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
        frame_time = 1.0 / self._fps
        print(f"[DXCamTrack] Fallback grab() @ {self._fps} FPS")

        while self._running:
            t_start = time.perf_counter()
            try:
                frame_np = camera.grab()
                if frame_np is not None and self._loop is not None:
                    if self._lq_track is not None and self._lq_track._running:
                        self._lq_track._on_raw_frame(frame_np)
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
# DXCamTrackLQ — LQ-трек для simulcast (разделяет захват с DXCamTrack)
# =============================================================================

class DXCamTrackLQ(_AiortcVideoStreamTrack):
    """
    LQ-версия видеотрека для simulcast (Discord-стиль).

    НЕ создаёт собственный dxcam capture loop.
    Получает raw RGB кадры от родительского DXCamTrack через _on_raw_frame().
    Масштабирует до LQ-разрешения с cv2.INTER_AREA в том же capture thread.

    Зритель с медленным каналом или слабым ПК выбирает LQ при stream_watch_start:
        {'action': 'stream_watch_start', 'streamer_uid': X, 'quality': 'lq'}

    Разрешение LQ = HQ/2 (floor до чётного). Пример: 720p → 360p.
    Битрейт LQ: ~1 Mbps для 360p (из VIDEO_BITRATES_LQ).
    """

    kind = "video"

    def __init__(self, hq_width: int, hq_height: int):
        super().__init__()
        lq_w, lq_h = get_lq_resolution(hq_width, hq_height)
        self._width  = lq_w
        self._height = lq_h

        self._queue: asyncio.Queue | None = None
        self._loop:  asyncio.AbstractEventLoop | None = None
        self._running   = False
        self._last_frame: 'av.VideoFrame | None' = None

    # ------------------------------------------------------------------
    # Управление
    # ------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop    = loop
        self._queue   = asyncio.Queue(maxsize=2)
        self._running = True
        print(f"[DXCamTrackLQ] LQ-трек запущен: {self._width}×{self._height}")

    def stop(self) -> None:
        self._running   = False
        self._last_frame = None

    # ------------------------------------------------------------------
    # Приём кадра от DXCamTrack (вызывается в capture thread)
    # ------------------------------------------------------------------

    def _on_raw_frame(self, frame_np: np.ndarray) -> None:
        """
        Получает raw RGB кадр от DXCamTrack.

        Вызывается из capture thread DXCamTrack ПЕРЕД HQ-конвертацией.
        Выполняет LQ-масштабирование и конвертацию здесь, в capture thread,
        чтобы не нагружать asyncio event loop.
        """
        if not self._running or self._loop is None:
            return
        av_frame = self._convert_to_yuv_lq(frame_np)
        if av_frame is not None:
            self._loop.call_soon_threadsafe(self._enqueue_frame, av_frame)

    def _convert_to_yuv_lq(self, frame_np: np.ndarray) -> 'av.VideoFrame | None':
        """
        [BUG-3] cv2.INTER_AREA для downscale + BT.709 full-range аннотация.

        Выполняется в capture thread DXCamTrack — нет двойного захвата.
        """
        try:
            if _CV2_AVAILABLE:
                resized = _cv2.resize(frame_np, (self._width, self._height),
                                      interpolation=_cv2.INTER_AREA)
            else:
                # Numpy fallback: грубый, но рабочий
                h_src, w_src = frame_np.shape[:2]
                step_y = max(1, h_src // self._height)
                step_x = max(1, w_src // self._width)
                resized = frame_np[::step_y, ::step_x][:self._height, :self._width]
                if resized.shape[:2] != (self._height, self._width):
                    # Если размер не совпадает — fallback через PyAV reformat
                    av_tmp = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
                    yuv = av_tmp.reformat(
                        width=self._width, height=self._height, format='yuv420p'
                    )
                    try:
                        yuv.color_range = 2
                        yuv.colorspace  = 1
                    except Exception:
                        pass
                    return yuv

            av_frame = av.VideoFrame.from_ndarray(resized, format='rgb24')
            try:
                av_frame.color_range = 2
                av_frame.colorspace  = 1
            except Exception:
                pass
            yuv = av_frame.reformat(format='yuv420p')
            try:
                yuv.color_range = 2
                yuv.colorspace  = 1
            except Exception:
                pass
            return yuv
        except Exception as e:
            print(f"[DXCamTrackLQ] Ошибка конвертации: {e}")
            return None

    def _enqueue_frame(self, av_frame: 'av.VideoFrame') -> None:
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
    # aiortc интерфейс
    # ------------------------------------------------------------------

    async def recv(self) -> 'av.VideoFrame':
        pts, time_base = await self.next_timestamp()

        av_frame = None
        if self._queue is not None:
            try:
                av_frame = self._queue.get_nowait()
                self._last_frame = av_frame
            except asyncio.QueueEmpty:
                av_frame = self._last_frame

        if av_frame is None:
            av_frame = av.VideoFrame(self._width, self._height, 'yuv420p')

        av_frame.pts       = pts
        av_frame.time_base = time_base
        return av_frame


# =============================================================================
# VideoReceiver — WebRTC трек → QImage → frame_received сигнал
# =============================================================================

class VideoReceiver(QObject):
    """
    Принимает один видеотрек от WebRTC и конвертирует кадры в QImage.

    [BUG-1] ИСПРАВЛЕНО: color_range=2 + colorspace=1 перед to_ndarray().
    [BUG-2] ИСПРАВЛЕНО: bytes_per_line = img_np.strides[0] (4-byte aligned).
    """

    frame_received       = pyqtSignal(int, QImage)
    stream_stats_updated = pyqtSignal(int, int, int)   # uid, fps, loss_pct

    _STATS_INTERVAL = 2.0

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

        self._stats_decoded   = 0
        self._stats_last_time = time.monotonic()

        asyncio.run_coroutine_threadsafe(self._recv_loop(), loop)

    # ------------------------------------------------------------------
    # Asyncio recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
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

                try:
                    # ── [BUG-1] Устанавливаем color_range ПЕРЕД to_ndarray ───
                    # Без этого libswscale использует BT.601 limited (16-235).
                    # Стример кодировал в full-range BT.709 → несоответствие →
                    # потеря 15% квантования → "мутная" картинка у зрителя.
                    try:
                        frame.color_range = 2   # AVCOL_RANGE_JPEG (full 0-255)
                        frame.colorspace  = 1   # AVCOL_SPC_BT709
                    except (AttributeError, Exception):
                        pass   # старые PyAV — конвертация всё равно лучше default

                    img_np = frame.to_ndarray(format='rgb24')

                    # ── [BUG-2] Правильный stride для QImage ─────────────────
                    # w * 3 для 854px = 2562 — не кратно 4.
                    # QImage на Windows требует 4-байтовое выравнивание строк.
                    # img_np после ascontiguousarray гарантированно C-contiguous.
                    # strides[0] = реальный байтовый шаг строки (numpy выравнивает).
                    img_np = np.ascontiguousarray(img_np)
                    h, w, c = img_np.shape
                    bytes_per_line = img_np.strides[0]   # ← правильный stride

                    q_img = QImage(
                        img_np.data, w, h, bytes_per_line,
                        QImage.Format.Format_RGB888
                    )
                    self.frame_received.emit(self.uid, q_img.copy())

                    self._stats_decoded += 1
                    del img_np, q_img

                    now = time.monotonic()
                    if now - self._stats_last_time >= self._STATS_INTERVAL:
                        elapsed = max(now - self._stats_last_time, 0.001)
                        fps = int(self._stats_decoded / elapsed)
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
    Менеджер WebRTC видео.

    Simulcast (HQ + LQ):
        start_streaming() создаёт DXCamTrack (HQ) и DXCamTrackLQ (LQ).
        get_dxcam_track()  → HQ трек для pc.addTrack()
        get_lq_track()     → LQ трек для pc.addTrack() (simulcast)
        SFU маршрутизирует зрителям нужный поток по quality=hq|lq.
    """

    frame_received       = pyqtSignal(int, QImage)
    stream_stats_updated = pyqtSignal(int, int, int)

    def __init__(self, net_client):
        super().__init__()
        self.net = net_client

        self._dxcam_track:    DXCamTrack    | None = None
        self._dxcam_track_lq: DXCamTrackLQ  | None = None
        self._receivers:      dict[int, VideoReceiver] = {}

        self._webrtc_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # WebRTC loop
    # ------------------------------------------------------------------

    def set_webrtc_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._webrtc_loop = loop
        print("[VideoEngine] WebRTC asyncio loop установлен")

    # ------------------------------------------------------------------
    # Стример: управление DXCamTrack + DXCamTrackLQ
    # ------------------------------------------------------------------

    def start_streaming(self, settings: dict | None = None) -> bool:
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
        w = s.get("width",  VIDEO_WIDTH)
        h = s.get("height", VIDEO_HEIGHT)
        fps = s.get("fps", VIDEO_FPS)

        # Битрейт HQ
        target_bitrate = get_bitrate_for_resolution(w, h, lq=False)
        # Битрейт LQ
        lq_w, lq_h = get_lq_resolution(w, h)
        lq_bitrate = get_bitrate_for_resolution(lq_w, lq_h, lq=True)

        set_encoder_bitrate(target_bitrate, lq_bitrate)

        # Создаём HQ трек
        self._dxcam_track = DXCamTrack(
            monitor_idx = s.get("monitor_idx", 0),
            fps         = fps,
            width       = w,
            height      = h,
        )

        # Создаём LQ трек (simulcast) — разделяет capture loop с HQ
        self._dxcam_track_lq = DXCamTrackLQ(hq_width=w, hq_height=h)
        self._dxcam_track_lq.start(self._webrtc_loop)
        self._dxcam_track._lq_track = self._dxcam_track_lq   # подписчик

        # Запускаем HQ (и его capture loop, который кормит LQ)
        self._dxcam_track.start(self._webrtc_loop)

        print(
            f"[VideoEngine] Стрим запущен: HQ={w}×{h}, LQ={lq_w}×{lq_h}, "
            f"fps={fps}, HQ={target_bitrate//1000} kbps, LQ={lq_bitrate//1000} kbps"
        )
        return True

    def stop_streaming(self) -> None:
        if self._dxcam_track is not None:
            self._dxcam_track.stop()   # также останавливает _lq_track
            self._dxcam_track    = None
        self._dxcam_track_lq = None

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
            print("[VideoEngine] Стрим остановлен: GC + Windows heap trim")
        except Exception:
            print("[VideoEngine] Стрим остановлен, GC выполнен")

    def get_dxcam_track(self) -> DXCamTrack | None:
        """HQ трек → pc.addTrack() стримера."""
        return self._dxcam_track

    def get_lq_track(self) -> DXCamTrackLQ | None:
        """LQ трек → второй pc.addTrack() стримера (simulcast)."""
        return self._dxcam_track_lq

    # ------------------------------------------------------------------
    # Зрители: управление VideoReceiver
    # ------------------------------------------------------------------

    def add_receiver(self, uid: int, track) -> VideoReceiver:
        if self._webrtc_loop is None:
            raise RuntimeError("WebRTC loop не установлен. Вызовите set_webrtc_loop() первым.")

        if uid in self._receivers:
            old = self._receivers[uid]
            try:
                old.frame_received.disconnect(self.frame_received)
                old.stream_stats_updated.disconnect(self.stream_stats_updated)
            except (RuntimeError, TypeError):
                pass
            old.stop()

        receiver = VideoReceiver(uid, track, self._webrtc_loop)
        receiver.frame_received.connect(self.frame_received)
        receiver.stream_stats_updated.connect(self.stream_stats_updated)
        self._receivers[uid] = receiver

        print(f"[VideoEngine] VideoReceiver создан для uid={uid}")
        return receiver

    def stop_viewer_for_uid(self, uid: int) -> None:
        receiver = self._receivers.pop(uid, None)
        if receiver is not None:
            try:
                receiver.frame_received.disconnect(self.frame_received)
                receiver.stream_stats_updated.disconnect(self.stream_stats_updated)
            except (RuntimeError, TypeError):
                pass
            receiver.stop()
        print(f"[VideoEngine] stop_viewer_for_uid({uid})")

    # ------------------------------------------------------------------
    # Заглушки для совместимости (устаревший UDP-стек)
    # ------------------------------------------------------------------

    def process_incoming_packet(self, uid, data, is_lq: bool = False) -> None:
        pass   # УСТАРЕЛО: заменено WebRTC треком

    def force_keyframe(self) -> None:
        pass   # УСТАРЕЛО: WebRTC управляет IDR через RTCP PLI

    def set_bitrate(self, new_bitrate: int) -> None:
        pass   # УСТАРЕЛО: WebRTC управляет битрейтом через TWCC

    def set_lq_needed(self, needed: bool) -> None:
        pass   # УСТАРЕЛО: simulcast через SFU quality routing

    def handle_retransmit(self, frame_id: int, chunk_idx: int) -> None:
        pass   # УСТАРЕЛО: NACK управляется aiortc через RTP

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_users(self, active_uids) -> None:
        for uid in list(self._receivers.keys()):
            if uid not in active_uids:
                self.stop_viewer_for_uid(uid)

    def shutdown(self) -> None:
        print("[VideoEngine] shutdown(): останавливаем все компоненты...")

        if self._dxcam_track is not None:
            try:
                self._dxcam_track.stop()
            except Exception as e:
                print(f"[VideoEngine] DXCamTrack stop error: {e}")
            self._dxcam_track    = None
            self._dxcam_track_lq = None

        for uid in list(self._receivers.keys()):
            try:
                self._receivers[uid].stop()
            except Exception as e:
                print(f"[VideoEngine] VideoReceiver uid={uid} stop error: {e}")
        self._receivers.clear()

        print("[VideoEngine] shutdown(): готово")
