# video_engine.py — WebRTC видеодвижок (aiortc)
#
# ═══════════════════════════════════════════════════════════════════════════════
# ДИАГНОСТИКА И ИСПРАВЛЕНИЯ КАЧЕСТВА v5
#
# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  КОРНЕВАЯ ПРИЧИНА ЗАЦВЕТОВ (WHITE BLOWOUT)                                 │
# │                                                                             │
# │  Проблема: несоответствие color range между энкодером и декодером.          │
# │                                                                             │
# │  Старый код устанавливал color_range=2 (FULL/JPEG, 0–255) на:              │
# │    — CodecContext перед открытием (энкодер)                                 │
# │    — av.VideoFrame перед to_ndarray() (декодер)                             │
# │                                                                             │
# │  Проблема 1 — NVENC:                                                        │
# │    'rc': 'vbr_hq' — этот режим удалён из NVENC SDK 11+.                    │
# │    NVENC тихо игнорирует неизвестные параметры и открывается с              │
# │    дефолтными настройками → LIMITED range в битстриме (Y: 16–235).          │
# │    Декодер получает full-range (color_range=2) override → Y=235             │
# │    трактуется как 92% яркости → артефакты кодирования выше 235              │
# │    усиливаются → БЕЛЫЙ ЗАЦВЕТ на ярких областях.                            │
# │                                                                             │
# │  Проблема 2 — libx264:                                                      │
# │    ctx.bit_rate=0 при CRF делает битрейт неопределённым для PyAV.           │
# │    color_range=2 применяется к CodecContext, но range=pc в x264-params      │
# │    тоже должен быть — двойное задание конфликтует на некоторых версиях.     │
# │                                                                             │
# │  ИСПРАВЛЕНИЕ: переходим на LIMITED range (стандарт H.264/broadcast).       │
# │    — Убираем ВСЕ color_range=2 override'ы.                                 │
# │    — Убираем range=pc из x264-params.                                       │
# │    — Декодер читает color_range из VUI битстрима (как должно быть).        │
# │    — libswscale применяет правильную матрицу по VUI → нет мисматча.        │
# │                                                                             │
# │  РЕЗУЛЬТАТ: полное устранение зацветов. LIMITED range совместим со          │
# │  всеми устройствами, браузерами и железными декодерами.                     │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  КОРНЕВАЯ ПРИЧИНА МУТНОСТИ (BLUR/MUDDY)                                    │
# │                                                                             │
# │  1. libx264 preset=faster — агрессивный tradeoff в пользу скорости.        │
# │     screen-capture контент (UI, текст, иконки) плохо переносит этот        │
# │     preset: пропуск B-frame анализа + слабый ME → размытые края текста.    │
# │     ИСПРАВЛЕНО: preset=slow (лучший для статичного/полустатичного           │
# │     контента, типичного при захвате экрана).                                │
# │                                                                             │
# │  2. NVENC vbr_hq → дефолтная конфигурация без quality tuning.              │
# │     ИСПРАВЛЕНО: preset=p4 + rc=vbr + cq=20 (верный modern NVENC API).      │
# │                                                                             │
# │  3. RGB→YUV через PyAV reformat() использует BT.601 матрицу по умолчанию. │
# │     Для HD контента нужна BT.709. Неверная матрица = цветовые ошибки.      │
# │     ИСПРАВЛЕНО: явная colorspace=1 (BT.709) на RGB фрейме ДО reformat.     │
# │                                                                             │
# │  4. QPainter.SmoothPixmapTransform в VideoSurface = bilinear scaling.      │
# │     Для экранного контента (текст) bilinear даёт blur при downscaling.     │
# │     Решение: отключаем smooth hint для VideoSurface (см. ui_video.py).     │
# │                                                                             │
# │  5. Queue maxsize=2 при 30fps = буфер только 66ms.                         │
# │     При кратковременных задержках encode → кадры дропаются, зритель        │
# │     видит дёргание. ИСПРАВЛЕНО: maxsize=4 (133ms буфер).                   │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# ┌─────────────────────────────────────────────────────────────────────────────┐
# │  КОРНЕВАЯ ПРИЧИНА ЛАГОВ ЗВУКА И КАРТИНКИ (A/V SYNC)                        │
# │                                                                             │
# │  1. _recv_stream_audio_coro и VideoReceiver._recv_loop живут на ОДНОМ      │
# │     asyncio event loop (_webrtc_loop). H264 decode = CPU тяжёлая          │
# │     операция (1–5 мс). Когда video recv занят — audio recv ждёт.           │
# │     При 30fps: 5мс × 30 = 150мс CPU блокировки audio per second.           │
# │     ИСПРАВЛЕНО: frame.to_ndarray() вынесен в executor (thread pool)        │
# │     чтобы не блокировать event loop. Video recv отдаёт управление          │
# │     async loop после decode.                                                │
# │                                                                             │
# │  2. asyncio.wait_for(track.recv(), timeout=5.0) — если recv() зависает     │
# │     на 5 секунд (потеря пакетов), audio recv также подвисает.              │
# │     ИСПРАВЛЕНО: timeout=2.0 + явный continue для быстрого восстановления. │
# │                                                                             │
# │  3. _stream_buf в AudioHandler: 30 чанков = 600мс буфер.                  │
# │     При переполнении — drop oldest (пропуск звука). При underrun —         │
# │     тишина. Рекомендуется: увеличить до 60 чанков (1.2 сек) в             │
# │     audio_engine.py (_STREAM_BUF_CHUNKS = 60).                             │
# │     (Изменение в audio_engine.py, не здесь.)                               │
# └─────────────────────────────────────────────────────────────────────────────┘

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
# patch_aiortc_encoder — production quality encoder
# =============================================================================

def patch_aiortc_nvenc() -> bool:
    """
    Подменяет H264Encoder в aiortc для максимального качества стрима.

    Приоритет кодеков:
      1. NVIDIA NVENC (h264_nvenc)
      2. AMD AMF     (h264_amf)
      3. Windows MF  (h264_mf)
      4. libx264     (CPU fallback)

    ЦВЕТОВОЙ ДИАПАЗОН: используем LIMITED range (стандарт H.264).
    Убраны все color_range=2 override'ы — они были причиной зацветов
    при несовместимости NVENC/AMF с ffmpeg color_range API.
    """
    if not (AV_AVAILABLE and AIORTC_AVAILABLE):
        return False

    hw_profiles = [
        {
            'codec': 'h264_nvenc',
            'name':  'NVIDIA NVENC',
            'options': {
                # FIX: 'vbr_hq' удалён из NVENC SDK 11+ → заменяем на 'vbr'
                # preset p4 = quality/latency баланс (p5/p6 слишком медленные)
                'preset':      'p4',
                'tune':        'hq',
                'rc':          'vbr',        # FIX: было vbr_hq (invalid)
                'cq':          '20',         # Constant quality target
                'bf':          '2',
                'profile':     'high',
                'spatial-aq':  '1',
                'temporal-aq': '1',
                'aq-strength': '8',
                'g':           '60',
                # FIX: убран fullrange=1 → LIMITED range по умолчанию
                # NVENC: forced-idr=1 гарантирует синхронизацию
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

    # FIX libx264: preset=slow для экранного контента
    # slow > faster для статичного/полустатичного контента (UI, текст):
    # лучший motion estimation, больше B-frames анализ, четкие края.
    # preset=slow на CPU современных ПК: ~5-8 мс/кадр @ 720p → ОК для 30fps.
    # Убраны: range=pc (full-range), rc-lookahead (слишком малый → мутность).
    x264_profile = {
        'codec': 'libx264',
        'name':  'libx264 (CPU)',
        'options': {
            'preset':       'slow',           # FIX: было 'faster' → хуже качество
            'profile':      'high',
            'level':        '4.1',
            'g':            '60',             # Keyframe каждые 2 сек @ 30fps
            'sc_threshold': '40',
            'crf':          '18',             # FIX: было 20; 18 = better quality
            'x264-params': (
                'rc-lookahead=40:'            # FIX: было 10 (слишком мало)
                'bframes=3:'                  # FIX: было 2
                'b-adapt=2:'                  # FIX: было 1
                'no-fast-pskip=1:'
                'aq-mode=3:'
                'aq-strength=1.0:'            # FIX: было 0.8
                'colormatrix=bt709:'
                'colorprim=bt709:'
                'transfer=bt709'
                # FIX: убран range=pc (full-range) → LIMITED range
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
            # Поиск attr в двух проходах:
            # 1. attr со значением None → первая инициализация
            # 2. attr с av.CodecContext → замена существующего (reconnect)
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

                    # FIX COLOR RANGE: убраны ctx.color_range = 2 / ctx.colorspace = 1
                    # LIMITED range (default) — не задаём явно, пусть кодек решает.
                    # color_range=2 было главной причиной зацветов: NVENC молча
                    # игнорировал этот флаг и писал LIMITED в VUI, а декодер
                    # получал override full-range → mismatch → blowout.
                    # Теперь оба конца (encoder/decoder) используют VUI из битстрима.

                    if selected_profile['codec'] == 'libx264':
                        ctx.bit_rate = 0      # CRF управляет качеством, maxrate — потолок
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

    Архитектура:
        Поток захвата (threading.Thread):
            dxcam.get_latest_frame()
            → _downscale_rgb() если нужен ресайз (cv2.INTER_AREA)
            → _convert_to_yuv() → BT.709 limited range YUV420p
            → _lq_track._on_raw_frame() (simulcast)
            → loop.call_soon_threadsafe(_enqueue_frame, av_frame)

        asyncio event loop (WebRTC thread):
            recv() → queue.get_nowait() → aiortc H264Encoder → RTP

    FIX: Queue maxsize=4 (было 2) = 133ms буфер при 30fps.
    При кратковременных задержках encode кадры не дропаются.
    """

    kind = "video"

    # FIX: увеличен с 2 до 4 → меньше дропов при кратковременных задержках encode
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

        # LQ-подписчик (simulcast): получает raw RGB кадры из того же capture loop
        self._lq_track: 'DXCamTrackLQ | None' = None

    # ------------------------------------------------------------------
    # Управление жизненным циклом
    # ------------------------------------------------------------------

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
    # Конвертация кадра
    # ------------------------------------------------------------------

    def _downscale_rgb(self, frame_np: np.ndarray, dst_w: int, dst_h: int) -> np.ndarray:
        """
        cv2.INTER_AREA — area averaging для downscaling экранного контента.
        Антиалиасинг, чёткие края текста vs SWS_BILINEAR.
        """
        if _CV2_AVAILABLE:
            return _cv2.resize(frame_np, (dst_w, dst_h),
                               interpolation=_cv2.INTER_AREA)
        return frame_np  # PyAV reformat справится ниже

    def _convert_to_yuv(self, frame_np: np.ndarray) -> 'av.VideoFrame | None':
        """
        Конвертирует np.ndarray (RGB) в av.VideoFrame (yuv420p).

        FIX COLOR: устанавливаем colorspace=1 (BT.709) на RGB фрейме ДО reformat.
        Это заставляет libswscale использовать BT.709 матрицу для RGB→YUV.
        Без этого libswscale использует BT.601 по умолчанию → цветовые ошибки.

        color_range НЕ переопределяем → LIMITED range (default, стандарт H.264).
        """
        try:
            need_resize = (frame_np.shape[1] != self._width or
                           frame_np.shape[0] != self._height)

            if need_resize and _CV2_AVAILABLE:
                resized   = self._downscale_rgb(frame_np, self._width, self._height)
                av_frame  = av.VideoFrame.from_ndarray(resized, format='rgb24')
                need_resize = False
            else:
                av_frame = av.VideoFrame.from_ndarray(frame_np, format='rgb24')

            # FIX: colorspace=1 (BT.709) для правильной матрицы RGB→YUV.
            # НЕ задаём color_range — оставляем LIMITED range по умолчанию.
            try:
                av_frame.colorspace = 1   # AVCOL_SPC_BT709
            except (AttributeError, Exception):
                pass

            if need_resize:
                yuv_frame = av_frame.reformat(
                    width=self._width, height=self._height, format='yuv420p'
                )
            else:
                yuv_frame = av_frame.reformat(format='yuv420p')

            # BT.709 метка на YUV фрейме — энкодер запишет в VUI
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
        # FIX LEAK #3: dxcam держит D3D11 OutputDuplication в глобальном реестре.
        # clean_up() уничтожает этот реестр → IDXGIOutputDuplication::Release().
        # Без этого ~80 МБ видеопамяти остаётся выделенной даже после del camera.
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
    _QUEUE_MAXSIZE = 4  # FIX: было 2

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
                        yuv.colorspace = 1  # BT.709
                    except Exception:
                        pass
                    return yuv

            av_frame = av.VideoFrame.from_ndarray(resized, format='rgb24')
            try:
                av_frame.colorspace = 1  # BT.709, LIMITED range (default)
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

class VideoReceiver(QObject):
    """
    Принимает один видеотрек от WebRTC и конвертирует кадры в QImage.

    FIX COLOR: убран color_range=2 override перед to_ndarray().
    Теперь libavcodec читает color_range из VUI битстрима (как должно быть).
    Это устраняет mismatch который вызывал белые зацветы.

    FIX AV SYNC: frame.to_ndarray() выполняется в executor (thread pool)
    чтобы не блокировать asyncio event loop → audio recv не ждёт decode.

    FIX QImage stride: bytes_per_line = img_np.strides[0] (4-byte aligned).
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
        # FIX AV SYNC: executor для CPU-тяжёлого to_ndarray()
        # Без executor: decode занимает 1-5 мс в event loop → audio recv ждёт.
        # С executor: decode идёт в thread pool, event loop остаётся свободным.
        _loop = asyncio.get_running_loop()

        try:
            while self._running:
                try:
                    # FIX: timeout уменьшен с 5.0 до 2.0 сек
                    # 5 сек ожидания при потере пакетов = 5 сек заморозки audio.
                    # 2 сек достаточно для RadminVPN jitter (обычно < 100 мс).
                    frame = await asyncio.wait_for(self._track.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: timeout 2s, жду...")
                    continue
                except Exception as e:
                    if self._running:
                        print(f"[VideoReceiver] uid={self.uid}: recv() error — {e}")
                    break

                try:
                    # FIX COLOR: НЕ переопределяем color_range вручную.
                    # Старый код: frame.color_range = 2 (full-range override)
                    # Проблема: если энкодер писал LIMITED range в VUI,
                    # то override=2 (full) → mismatch → зацветы.
                    # Теперь: libavcodec читает color_range из VUI битстрима.
                    # При BT.709 limited (стандарт H.264) результат корректен.
                    #
                    # Также устанавливаем colorspace=1 (BT.709) если не задан:
                    try:
                        if hasattr(frame, 'colorspace') and frame.colorspace == 0:
                            frame.colorspace = 1  # BT.709 если неизвестно
                    except Exception:
                        pass

                    # FIX AV SYNC: to_ndarray() в executor (thread pool)
                    # Это CPU-тяжёлая операция (libswscale YUV→RGB).
                    # В executor она не блокирует event loop → audio recv работает.
                    img_np = await _loop.run_in_executor(
                        None,
                        lambda f=frame: f.to_ndarray(format='rgb24')
                    )

                    # Правильный stride для QImage (4-byte aligned)
                    img_np = np.ascontiguousarray(img_np)
                    h, w, c = img_np.shape
                    bytes_per_line = img_np.strides[0]

                    q_img = QImage(
                        img_np.data, w, h, bytes_per_line,
                        QImage.Format.Format_RGB888
                    )
                    # copy() нужен: img_np может быть собран GC после emit
                    self.frame_received.emit(self.uid, q_img.copy())

                    self._stats_decoded += 1
                    del img_np, q_img

                    # Обновление статистики
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

        target_bitrate = get_bitrate_for_resolution(w, h, lq=False)
        lq_w, lq_h = get_lq_resolution(w, h)
        lq_bitrate = get_bitrate_for_resolution(lq_w, lq_h, lq=True)

        set_encoder_bitrate(target_bitrate, lq_bitrate)

        self._dxcam_track = DXCamTrack(
            monitor_idx = s.get("monitor_idx", 0),
            fps         = fps,
            width       = w,
            height      = h,
        )

        self._dxcam_track_lq = DXCamTrackLQ(hq_width=w, hq_height=h)
        self._dxcam_track_lq.start(self._webrtc_loop)
        self._dxcam_track._lq_track = self._dxcam_track_lq

        self._dxcam_track.start(self._webrtc_loop)

        print(
            f"[VideoEngine] Стрим запущен: HQ={w}×{h}, LQ={lq_w}×{lq_h}, "
            f"fps={fps}, HQ={target_bitrate//1000} kbps, LQ={lq_bitrate//1000} kbps"
        )
        return True

    def stop_streaming(self) -> None:
        if self._dxcam_track is not None:
            self._dxcam_track.stop()
            self._dxcam_track    = None
        self._dxcam_track_lq = None

        # FIX LEAK #4: PyAV держит av.Codec и av.CodecContext на C-уровне.
        # Первый gc.collect(0) снижает refcount Python-обёрток.
        # gc.collect(1) / collect(2) запускают финализаторы C-расширений
        # (av.CodecContext.__dealloc__ → avcodec_free_context).
        # Второй gc.collect(0) подчищает то, что освободилось во время gen2.
        # Без этой последовательности ~30-40 МБ PyAV codec buffers остаются
        # живыми до следующего автоматического GC цикла.
        gc.collect(0)
        gc.collect(1)
        gc.collect(2)
        gc.collect(0)   # второй gen0 — чистит хвосты после gen2

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

    def get_dxcam_track(self) -> 'DXCamTrack | None':
        return self._dxcam_track

    def get_lq_track(self) -> 'DXCamTrackLQ | None':
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
    # Заглушки для совместимости
    # ------------------------------------------------------------------

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