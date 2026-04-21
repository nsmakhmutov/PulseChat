# video_engine.py — WebRTC видеодвижок (aiortc)
#
# ── Изменения v5 ────────────────────────────────────────────────────────────
#
#   FIX 6 (СРЕДНИЙ): NVENC параметры для screen content.
#     preset p4 → p5: чуть медленнее кодирование, заметно лучше качество
#       для UI/текста с большими статичными зонами.
#     cq=20 → cq=18: более низкий CQ = выше качество (диапазон 0-51).
#       Для мелкого шрифта разница отчётлива.
#     temporal-aq удалён: оптимизирует для движущегося видео, не для экрана.
#     spatial-aq остался: помогает тонким деталям интерфейса.
#     aq-strength 8 → 10: чуть сильнее → чёткость текстовых элементов.
#
#   FIX 7 (СРЕДНИЙ): Keyframe interval G=60 → G=30 (1 секунда @ 30fps).
#     При потере пакетов на RadminVPN зритель ждал до 2 сек до следующего
#     I-frame. При screen sharing IDR-кадр на статичных зонах весит мало,
#     поэтому уменьшение интервала не даёт значительного прироста битрейта.
#     Применено ко всем кодекам: NVENC, AMF, h264_mf, libx264.
#
#   FIX 8 (СРЕДНИЙ): x264 preset slow → medium.
#     slow — слишком медленный для realtime при 30fps на скромном CPU.
#     При 1080p и 30fps slow занимал >33ms на кадр → дропы.
#     medium = хороший баланс quality/speed для realtime screen capture.
#     crf=18 сохранён — он даёт качество, preset только скорость поиска.
#
#   FIX 9 (СРЕДНИЙ): patch_aiortc_nvenc() — явное предупреждение в v3.
#     В v3 стримером является Rust Media Engine (media-engine.exe).
#     aiortc в Python используется ТОЛЬКО у зрителя для ДЕКОДИРОВАНИЯ.
#     Патч H264Encoder для зрителя бессмысленен — декодер не кодирует.
#     Функция сохранена для обратной совместимости, но теперь возвращает
#     False и логирует WARNING если вызвана без явного флага force=True.
#     Убери вызов patch_aiortc_nvenc() из точки старта приложения.
#
#   FIX 10 (НИЗКИЙ): VideoReceiver — RTP PTS-based jitter buffer.
#     Было: синхронизация по wall-clock (time.monotonic() arrival time).
#     Это не настоящий jitter buffer — кадры планировались по времени
#     прихода, а не по временным меткам энкодера. При сетевых флуктуациях
#     несколько кадров могли прийти в burst → отображались почти одновременно.
#     Стало: используем frame.pts (RTP timestamp, clock 90000 Hz для H.264).
#     Первый кадр устанавливает PTS-якорь и wall-clock якорь.
#     Каждый следующий кадр планируется через pts_delta / 90000 секунд
#     от якоря + VIEWER_JITTER_BUFFER_MS задержка.
#     Результат: плавное воспроизведение независимо от сетевого jitter.

import asyncio
import collections
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
    VIEWER_JITTER_BUFFER_MS,
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

# cv2.INTER_AREA — лучший алгоритм downscale для UI/текста
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
# patch_aiortc_nvenc — DEPRECATED в v3 (только для обратной совместимости)
# =============================================================================

def patch_aiortc_nvenc(force: bool = False) -> bool:
    """
    DEPRECATED в v3: стримером является Rust Media Engine, не aiortc.
    aiortc в Python используется ТОЛЬКО у зрителя для декодирования.
    Патч H264Encoder для зрителя не нужен — там нет кодирования.

    FIX 9: функция теперь возвращает False и логирует WARNING по умолчанию.
    Установи force=True только если ты ТОЧНО знаешь что aiortc используется
    для кодирования (например, при откате с v3 на v2 архитектуру).

    Параметры:
      force: True → принудительно применить патч несмотря на предупреждение.
             False (default) → вернуть False с WARNING (рекомендуется).
    """
    if not force:
        print(
            "[Video] WARNING: patch_aiortc_nvenc() вызвана в v3, где Rust делает "
            "кодирование. aiortc у зрителя только декодирует — патч не нужен. "
            "Убери этот вызов или передай force=True если это намеренно."
        )
        return False

    if not (AV_AVAILABLE and AIORTC_AVAILABLE):
        return False

    hw_profiles = [
        {
            'codec': 'h264_nvenc',
            'name':  'NVIDIA NVENC',
            'options': {
                # FIX 6: preset p4 → p5 для screen content (UI/текст).
                # p5 = лучше quality/speed баланс при статичных зонах.
                # Для движущегося видео p4 быстрее, для экрана разница <2ms/frame.
                'preset':      'p5',
                'tune':        'hq',
                'rc':          'vbr',
                # FIX 6: cq=20 → cq=18. Ниже = лучше качество (диапазон 0-51).
                # Критично для мелкого текста и тонких UI-элементов.
                'cq':          '18',
                'bf':          '2',
                'profile':     'high',
                'spatial-aq':  '1',
                # FIX 6: temporal-aq УДАЛЁН. Оптимизирует движущееся видео,
                # для статичного экрана только тратит время энкодера.
                # FIX 6: aq-strength 8 → 10. Чуть сильнее → чётче детали UI.
                'aq-strength': '10',
                # FIX 7: g=60 → g=30. IDR каждые 1 сек вместо 2.
                # При потере пакетов зритель восстанавливается в 2× быстрее.
                # Screen content = много статики → IDR маленький по размеру.
                'g':           '30',
                'forced-idr':  '1',
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
                'rc':      'vbr_peak',
                # FIX 7: добавлен g=30 для AMF
                'g':       '30',
            },
        },
        {
            'codec': 'h264_mf',
            'name':  'Windows MF (GPU)',
            'options': {
                'scenario':         'livestreaming',
                'quality_vs_speed': '100',
                # FIX 7: добавлен g=30 для MF
                'g':                '30',
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
            # FIX 8: preset slow → medium для realtime screen capture.
            # slow занимает >33ms/frame при 1080p → дропы.
            # medium = хороший баланс quality/speed при 30fps.
            # crf=18 управляет качеством — preset влияет только на скорость.
            'preset':       'medium',
            'profile':      'high',
            'level':        '4.1',
            # FIX 7: g=60 → g=30
            'g':            '30',
            'sc_threshold': '40',
            'crf':          '18',
            'x264-params': (
                'rc-lookahead=30:'        # было 40; снижено под medium preset
                'bframes=3:'
                'b-adapt=2:'
                'no-fast-pskip=1:'
                'aq-mode=3:'
                'aq-strength=1.0:'
                'colormatrix=bt709:'
                'colorprim=bt709:'
                'transfer=bt709'
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
            codec_attr = next(
                (a for a in _CODEC_ATTRS
                 if hasattr(self_enc, a) and getattr(self_enc, a) is None),
                None,
            )
            if codec_attr is None and not _active_flag[0]:
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

                    if selected_profile['codec'] == 'libx264':
                        ctx.bit_rate = 0
                    else:
                        ctx.bit_rate = cur_bitrate * 2 // 3

                    ctx.open()
                    setattr(self_enc, codec_attr, ctx)

                    if not _active_flag[0]:
                        _active_flag[0] = True
                        mode = ("CRF 18"
                                if selected_profile['codec'] == 'libx264'
                                else f"{cur_bitrate // 1000} kbps")
                        print(
                            f"[Video] H264Encoder: {selected_profile['name']}, "
                            f"{mode}, {frame.width}×{frame.height}, "
                            f"limited-range bt709"
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
    В v3 не используется стримером (захват в Rust). Сохранён для совместимости.
    """

    kind = "video"
    _QUEUE_MAXSIZE = 4

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

        self._lq_track: 'DXCamTrackLQ | None' = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._loop    = loop
        self._queue   = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="dxcam-capture",
        )
        self._capture_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._lq_track is not None:
            self._lq_track.stop()
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None
        self._last_frame = None

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

    def _downscale_rgb(self, frame_np: np.ndarray, dst_w: int, dst_h: int) -> np.ndarray:
        if _CV2_AVAILABLE:
            return _cv2.resize(frame_np, (dst_w, dst_h),
                               interpolation=_cv2.INTER_AREA)
        return frame_np

    def _convert_to_yuv(self, frame_np: np.ndarray) -> 'av.VideoFrame | None':
        try:
            need_resize = (frame_np.shape[1] != self._width or
                           frame_np.shape[0] != self._height)

            if need_resize and _CV2_AVAILABLE:
                resized     = self._downscale_rgb(frame_np, self._width, self._height)
                av_frame    = av.VideoFrame.from_ndarray(resized, format='rgb24')
                need_resize = False
            else:
                av_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
            try:
                av_frame.colorspace = 1
            except (AttributeError, Exception):
                pass

            if need_resize:
                yuv_frame = av_frame.reformat(
                    width=self._width, height=self._height, format='yuv420p'
                )
            else:
                yuv_frame = av_frame.reformat(format='yuv420p')
            try:
                yuv_frame.colorspace = 1   # BT.709
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

        try:
            dxcam.clean_up()
            print("[DXCamTrack] dxcam singleton реестр очищен")
        except Exception:
            pass

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
# DXCamTrackLQ — LQ-трек для simulcast
# =============================================================================

class DXCamTrackLQ(_AiortcVideoStreamTrack):
    """
    LQ-версия видеотрека для simulcast (Discord-стиль).
    Получает raw RGB кадры от родительского DXCamTrack через _on_raw_frame().
    """

    kind = "video"
    _QUEUE_MAXSIZE = 4

    def __init__(self, hq_width: int, hq_height: int):
        super().__init__()
        lq_w, lq_h = get_lq_resolution(hq_width, hq_height)
        self._width  = lq_w
        self._height = lq_h

        self._queue: asyncio.Queue | None = None
        self._loop:  asyncio.AbstractEventLoop | None = None
        self._running    = False
        self._last_frame: 'av.VideoFrame | None' = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop    = loop
        self._queue   = asyncio.Queue(maxsize=self._QUEUE_MAXSIZE)
        self._running = True
        print(f"[DXCamTrackLQ] LQ-трек запущен: {self._width}×{self._height}")

    def stop(self) -> None:
        self._running    = False
        self._last_frame = None

    def _on_raw_frame(self, frame_np: np.ndarray) -> None:
        if not self._running or self._loop is None:
            return
        av_frame = self._convert_to_yuv_lq(frame_np)
        if av_frame is not None:
            self._loop.call_soon_threadsafe(self._enqueue_frame, av_frame)

    def _convert_to_yuv_lq(self, frame_np: np.ndarray) -> 'av.VideoFrame | None':
        """cv2.INTER_AREA downscale + BT.709 colorspace (LIMITED range)."""
        try:
            if _CV2_AVAILABLE:
                resized = _cv2.resize(frame_np, (self._width, self._height),
                                      interpolation=_cv2.INTER_AREA)
            else:
                h_src, w_src = frame_np.shape[:2]
                step_y = max(1, h_src // self._height)
                step_x = max(1, w_src // self._width)
                resized = frame_np[::step_y, ::step_x][:self._height, :self._width]
                if resized.shape[:2] != (self._height, self._width):
                    av_tmp = av.VideoFrame.from_ndarray(frame_np, format='rgb24')
                    yuv = av_tmp.reformat(
                        width=self._width, height=self._height, format='yuv420p'
                    )
                    try:
                        yuv.colorspace = 1
                    except Exception:
                        pass
                    return yuv

            av_frame = av.VideoFrame.from_ndarray(resized, format='rgb24')
            try:
                av_frame.colorspace = 1
            except Exception:
                pass
            yuv = av_frame.reformat(format='yuv420p')
            try:
                yuv.colorspace = 1
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

# RTP clock для H.264 видео (90000 Гц — стандарт RFC 6184)
_H264_RTP_CLOCK = 90000


class VideoReceiver(QObject):
    """
    Принимает один видеотрек от WebRTC и конвертирует кадры в QImage.

    Jitter Buffer (VIEWER_JITTER_BUFFER_MS):
      FIX 10: RTP PTS-based timing вместо wall-clock arrival time.

      Было (wall-clock):
        Кадры сортировались по времени прихода. При burst-доставке
        (несколько кадров пришли одновременно после задержки) они
        отображались почти мгновенно друг за другом — рывки.

      Стало (RTP PTS-based):
        Первый кадр устанавливает PTS-якорь (pts_anchor) и wall-clock якорь.
        Каждый следующий кадр получает display_at = wall_anchor +
        (frame.pts - pts_anchor) / RTP_CLOCK + buffer_ms/1000.
        При burst-доставке кадры всё равно показываются с правильным
        интервалом (33ms @ 30fps) — плавное воспроизведение.

      VIEWER_JITTER_BUFFER_MS = 0   → буфер отключён (немедленный показ)
      VIEWER_JITTER_BUFFER_MS = 700 → 700ms задержка (рекомендуется)
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

        # ── Jitter Buffer (FIX 10: RTP PTS-based) ────────────────────────
        self._buffer_ms = VIEWER_JITTER_BUFFER_MS

        # Буфер хранит (display_at: float, q_img: QImage).
        # display_at = wall time когда кадр должен быть показан.
        # Сортировка не нужна — RTP PTS гарантирует порядок от энкодера.
        self._jitter_buf: collections.deque = collections.deque()

        # PTS-якорь: устанавливается при первом кадре
        self._pts_anchor:  int   | None = None   # RTP timestamp первого кадра
        self._wall_anchor: float | None = None   # monotonic time первого кадра
        self._pts_clock:   int          = _H264_RTP_CLOCK  # 90000 для H264

        # Защита от wrap-around RTP timestamp (32-bit, переполняется ~13 часов)
        self._pts_prev: int | None = None

        # Playback thread
        self._playback_thread: threading.Thread | None = None
        if self._buffer_ms > 0:
            self._playback_thread = threading.Thread(
                target=self._playback_loop,
                daemon=True,
                name=f"jitter-playback-{uid}",
            )
            self._playback_thread.start()
            print(
                f"[VideoReceiver] uid={uid}: RTP PTS jitter buffer = "
                f"{self._buffer_ms} ms (clock={self._pts_clock} Hz)"
            )
        else:
            print(f"[VideoReceiver] uid={uid}: jitter buffer OFF")

        asyncio.run_coroutine_threadsafe(self._recv_loop(), loop)

    # Instance-level flag (не class-level как было — иначе все экземпляры делят один флаг)
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    _first_frame_logged: bool = False

    # ------------------------------------------------------------------
    # FIX 10: RTP PTS-based jitter buffer — push
    # ------------------------------------------------------------------

    def _buf_push(self, q_img: QImage, frame_pts: int | None) -> None:
        """
        Добавляет кадр в jitter buffer с правильным display_at временем.

        FIX 10: display_at вычисляется из RTP PTS а не wall-clock.
        Это гарантирует равномерное воспроизведение (33ms между кадрами
        при 30fps) независимо от сетевого jitter.

        frame_pts: RTP timestamp кадра (units = RTP clock, 90000 Гц для H264).
                   None → fallback на wall-clock (например, первый кадр без PTS).
        """
        now = time.monotonic()

        if frame_pts is None or not isinstance(frame_pts, int):
            # Fallback: PTS недоступен → используем wall-clock arrival
            display_at = now + self._buffer_ms / 1000.0
            self._jitter_buf.append((display_at, q_img))
            # Ограничиваем размер буфера
            while len(self._jitter_buf) > 200:
                self._jitter_buf.popleft()
            return

        # Первый кадр — устанавливаем якорь
        if self._pts_anchor is None:
            self._pts_anchor  = frame_pts
            self._wall_anchor = now
            self._pts_prev    = frame_pts
            display_at = now + self._buffer_ms / 1000.0
            self._jitter_buf.append((display_at, q_img))
            return

        # RTP wrap-around защита: 32-bit PTS переполняется примерно через 13 часов.
        # Если разница с предыдущим PTS отрицательна и большая — это wrap.
        pts_diff = frame_pts - self._pts_prev
        if pts_diff < -0x3FFFFFFF:
            # Wrap-around произошёл → сбрасываем якорь
            self._pts_anchor  = frame_pts
            self._wall_anchor = now
            self._pts_prev    = frame_pts
            display_at = now + self._buffer_ms / 1000.0
            self._jitter_buf.append((display_at, q_img))
            return

        self._pts_prev = frame_pts

        # Вычисляем когда кадр должен быть показан
        # pts_delta_secs = разница в секундах от первого кадра по энкодер-таймстампу
        pts_delta_secs = (frame_pts - self._pts_anchor) / self._pts_clock
        display_at = self._wall_anchor + pts_delta_secs + self._buffer_ms / 1000.0

        self._jitter_buf.append((display_at, q_img))

        # Ограничиваем размер буфера: 200 кадров ≈ ~6.7 сек @ 30fps
        while len(self._jitter_buf) > 200:
            self._jitter_buf.popleft()

    # ------------------------------------------------------------------
    # FIX 10: RTP PTS-based jitter buffer — playback
    # ------------------------------------------------------------------

    def _playback_loop(self) -> None:
        """
        Daemon thread: проверяет буфер с интервалом ~8ms (выше 30fps).
        Показывает все кадры у которых display_at <= now.

        FIX 10: логика основана на display_at из RTP PTS, не на arrival time.
        При корректном RTP stream кадры выходят из буфера равномерно (33ms).
        При burst-delivery (jitter) кадры сглаживаются автоматически.
        """
        # Опрашиваем в 2× быстрее чем fps чтобы не пропустить момент
        poll_interval = max(0.005, 0.5 / VIDEO_FPS)

        while self._running:
            time.sleep(poll_interval)
            if not self._running:
                break

            now = time.monotonic()
            frame_to_show: QImage | None = None

            # Достаём все созревшие кадры; показываем только последний
            # (если кадры накопились, показываем актуальный, не устаревший)
            while self._jitter_buf:
                display_at, img = self._jitter_buf[0]
                if display_at <= now:
                    self._jitter_buf.popleft()
                    frame_to_show = img
                else:
                    break

            if frame_to_show is not None:
                self.frame_received.emit(self.uid, frame_to_show)

    # ------------------------------------------------------------------
    # Asyncio recv loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:

        _loop = asyncio.get_running_loop()
        _use_buffer = self._buffer_ms > 0
        _rtp_diag_count = 0

        # FIX 10: Определяем RTP clock из первого кадра
        # H264 всегда 90000, но Opus/другие треки могут быть другими
        _clock_detected = False

        try:
            while self._running:
                try:
                    frame = await asyncio.wait_for(self._track.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: timeout 2s, жду... "
                              f"(decoded={_rtp_diag_count})")
                    continue
                except Exception as e:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: recv() error — {e}")
                    break

                _rtp_diag_count += 1

                try:
                    # FIX 10: извлекаем RTP PTS из frame
                    frame_pts: int | None = None
                    try:
                        if hasattr(frame, 'pts') and frame.pts is not None:
                            frame_pts = int(frame.pts)
                    except Exception:
                        pass

                    # Детектируем RTP clock из time_base (один раз)
                    if not _clock_detected and frame_pts is not None:
                        try:
                            if hasattr(frame, 'time_base') and frame.time_base:
                                tb = frame.time_base
                                # time_base = 1/90000 для H264 → clock = 90000
                                detected_clock = int(round(1.0 / float(tb)))
                                if 8000 <= detected_clock <= 120000:
                                    self._pts_clock = detected_clock
                                    _clock_detected = True
                                    print(
                                        f"[VideoReceiver] uid={self.uid}: "
                                        f"RTP clock={self._pts_clock} Hz"
                                    )
                        except Exception:
                            pass

                    # Colorspace hint для декодера
                    try:
                        if hasattr(frame, 'colorspace') and frame.colorspace == 0:
                            frame.colorspace = 1
                    except Exception:
                        pass

                    # Декодирование в numpy в executor (не блокируем event loop)
                    img_np = await _loop.run_in_executor(
                        None,
                        lambda f=frame: f.to_ndarray(format='rgb24')
                    )
                    img_np = np.ascontiguousarray(img_np)
                    h, w, _ = img_np.shape
                    bytes_per_line = img_np.strides[0]

                    q_img = QImage(
                        img_np.data, w, h, bytes_per_line,
                        QImage.Format.Format_RGB888
                    ).copy()

                    if _use_buffer:
                        # FIX 10: передаём RTP PTS для точного scheduling
                        self._buf_push(q_img, frame_pts)
                    else:
                        self.frame_received.emit(self.uid, q_img)

                    self._stats_decoded += 1
                    del img_np

                    if not self._first_frame_logged:
                        self._first_frame_logged = True
                        print(
                            f"[VideoReceiver] uid={self.uid}: ПЕРВЫЙ ВИДЕО КАДР "
                            f"{w}×{h} pts={frame_pts} "
                            f"(buffer={'RTP-PTS' if _use_buffer else 'OFF'})"
                        )

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
            print(
                f"[VideoReceiver] uid={self.uid}: recv_loop завершён "
                f"(всего декодировано: {_rtp_diag_count})"
            )

    def stop(self) -> None:
        self._running = False
        self._jitter_buf.clear()
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
    Менеджер WebRTC видео (v3).

    Стример (v3):
      Захват экрана — Rust WGC (media-engine.exe, ~2% CPU).
      Кодирование   — NVENC/AMF через FFmpeg (Rust).
      Отправка RTP  — webrtc-rs → Pion SFU (sidecar.exe).

    Зритель (v3):
      Pion SFU → aiortc PC → VideoReceiver → frame_received → VideoWindow.
      VideoReceiver использует RTP PTS-based jitter buffer (FIX 10).
    """

    frame_received       = pyqtSignal(int, QImage)
    stream_stats_updated = pyqtSignal(int, int, int)

    def __init__(self, net_client):
        super().__init__()
        self.net = net_client

        self._dxcam_track:    None = None
        self._dxcam_track_lq: None = None
        self._receivers: dict[int, VideoReceiver] = {}

        self._webrtc_loop: asyncio.AbstractEventLoop | None = None

    def set_webrtc_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._webrtc_loop = loop
        print("[VideoEngine] WebRTC asyncio loop установлен")

    def start_streaming(self, settings: dict | None = None) -> bool:
        """v3: Захват и кодирование выполняет Rust Media Engine. No-op."""
        print("[VideoEngine] start_streaming: v3 — захват в Rust, no-op")
        return True

    def stop_streaming(self) -> None:
        """v3: DXCamTrack отсутствует — GC + Windows heap trim."""
        self._dxcam_track    = None
        self._dxcam_track_lq = None

        gc.collect(0)
        gc.collect(1)
        gc.collect(2)
        gc.collect(0)

        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetProcessWorkingSetSizeEx(
                kernel32.GetCurrentProcess(),
                ctypes.c_size_t(0xFFFFFFFF),
                ctypes.c_size_t(0xFFFFFFFF),
                0,
            )
            print("[VideoEngine] stop_streaming: GC + Windows heap trim")
        except Exception:
            print("[VideoEngine] stop_streaming: GC выполнен")

    def get_dxcam_track(self):
        """v3: всегда None — захват в Rust."""
        return None

    def get_lq_track(self):
        """v3: всегда None — simulcast управляется Pion SFU."""
        return None

    def add_receiver(self, uid: int, track) -> VideoReceiver:
        """
        Создаёт VideoReceiver для входящего WebRTC трека зрителя.
        track — aiortc MediaStreamTrack от Pion SFU (через RTCPeerConnection).
        """
        if self._webrtc_loop is None:
            raise RuntimeError(
                "[VideoEngine] WebRTC loop не установлен. "
                "Вызовите set_webrtc_loop() первым."
            )

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

    # ── Заглушки для совместимости ────────────────────────────────────────────

    def process_incoming_packet(self, uid, data, is_lq: bool = False) -> None:
        pass

    def force_keyframe(self) -> None:
        pass

    def set_bitrate(self, new_bitrate: int) -> None:
        pass

    def set_lq_needed(self, needed: bool) -> None:
        pass

    def handle_retransmit(self, frame_id: int, chunk_idx: int) -> None:
        pass

    def cleanup_users(self, active_uids) -> None:
        for uid in list(self._receivers.keys()):
            if uid not in active_uids:
                self.stop_viewer_for_uid(uid)

    def shutdown(self) -> None:
        print("[VideoEngine] shutdown()")

        self._dxcam_track    = None
        self._dxcam_track_lq = None

        for uid in list(self._receivers.keys()):
            try:
                self._receivers[uid].stop()
            except Exception as e:
                print(f"[VideoEngine] VideoReceiver uid={uid} stop error: {e}")
        self._receivers.clear()
        print("[VideoEngine] shutdown: все VideoReceiver остановлены")
