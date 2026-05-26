# -*- coding: utf-8 -*-
"""
camera_webrtc.py
================

Правильная трансляция веб-камеры — по той же модели, что и трансляция экрана.

╔══════════════════════════════════════════════════════════════════════════╗
║  БЫЛО (слабая реализация):                                                 ║
║    камера → OpenCV → JPEG → base64 → JSON по сигнальному TCP →             ║
║    сервер РАССЫЛАЕТ каждый кадр ВСЕМ в комнате (даже тем, кто не открыл     ║
║    кружок) → клиент декодирует только если окно открыто.                   ║
║    Трафик растёт как N×N, сервер — обязательный ретранслятор, грузит даже  ║
║    тех, кто не смотрит.                                                     ║
║                                                                            ║
║  СТАЛО (эта реализация):                                                    ║
║    Владелец камеры поднимает СВОЙ локальный SFU (отдельный sidecar.exe на   ║
║    своём порту) и пушит в него H.264-трек камеры через aiortc — РОВНО как   ║
║    экран. Зритель подключается WebRTC-viewer'ом к SFU владельца ТОЛЬКО      ║
║    когда нажал на кружок аватарки. Не нажал — ни одного байта потока.       ║
║    Кодируется один раз, SFU фанаутит подписавшимся. Сервер только          ║
║    проксирует SDP (как для экрана), медиа идёт P2P/LAN мимо сервера.        ║
╚══════════════════════════════════════════════════════════════════════════╝

Поток данных
------------
ВЛАДЕЛЕЦ КАМЕРЫ (uid=A):
    CameraCaptureThread (BGR-кадры)
        → CameraVideoTrack (av.VideoFrame, H.264)
        → aiortc RTCPeerConnection (sendonly)
        → POST /streamer/offer  на СВОЙ камера-SFU (127.0.0.1:cam_port)
    + серверу: {action: camera_start, camera_sfu_port: cam_port}

ЗРИТЕЛЬ (uid=B нажал кружок A):
    серверу: {action: camera_watch_start, streamer_uid: A}
        ← сервер: {action: camera_webrtc_offer, role: viewer, streamer_uid: A}
    aiortc RTCPeerConnection (recvonly)
        → серверу: {action: camera_webrtc_offer, role: viewer_offer, sdp}
        ← сервер проксирует на SFU владельца A → answer
    on track → decode H.264 → QImage → CircularVideoWindow(A)

Этот модуль ВЕШАЕТСЯ на NetworkClient как примесь (CameraWebRTCMixin), рядом с
WebRTCMixin (экран). Своя asyncio-петля переиспользуется из WebRTCMixin
(_webrtc_loop), отдельный SFU-bridge — свой.
"""

from __future__ import annotations

import asyncio
import fractions
import threading
import time

import numpy as np

try:
    import av  # PyAV — нужен для av.VideoFrame
    _AV_OK = True
except Exception:  # pragma: no cover
    av = None
    _AV_OK = False

try:
    from aiortc import (
        RTCPeerConnection, RTCSessionDescription, RTCConfiguration,
    )
    from aiortc.mediastreams import MediaStreamTrack
    _AIORTC_OK = True
except Exception:  # pragma: no cover
    RTCPeerConnection = RTCSessionDescription = RTCConfiguration = None
    MediaStreamTrack = object
    _AIORTC_OK = False

from config import (
    CMD_WEBRTC_OFFER, CMD_WEBRTC_ANSWER,
    CAM_SFU_PORT, CAM_STREAM_BITRATE, CAM_STREAM_BITRATE_BIG,
    CAM_STREAM_FPS, CAM_STREAM_SIZE, CAM_STREAM_SIZE_BIG,
    CAM_H264_MIN_BITRATE, CAM_H264_MAX_BITRATE,
)
from .sdp_utils import normalize_sdp_ice


# ── Снимаем заводской пол bitrate H.264-энкодера aiortc (500 кбит/с).
# Без этого один поток камеры физически не опускается ниже ~670–900 кбит/с,
# что и было узким местом при 5–6 зрителях. Делаем это один раз при импорте.
def _patch_aiortc_bitrate_floor():
    """ЖЁСТКО ограничиваем bitrate H.264-энкодера aiortc для камеры.

    Главное: помимо понижения пола, мы понижаем ПОТОЛОК MAX_BITRATE до
    CAM_H264_MAX_BITRATE. Setter target_bitrate в aiortc клампит значение в
    [MIN, MAX] — значит даже если pion-SFU пришлёт REMB с большим значением,
    энкодер физически НЕ сможет разогнаться выше нашего потолка. Это и есть
    причина прошлых 9 Мбит/с: без потолка REMB/дефолт разгонял поток.
    """
    try:
        import aiortc.codecs.h264 as _h264
        _h264.MIN_BITRATE = CAM_H264_MIN_BITRATE
        _h264.MAX_BITRATE = CAM_H264_MAX_BITRATE
        # DEFAULT тоже опускаем — это стартовое значение до любого REMB.
        if hasattr(_h264, "DEFAULT_BITRATE"):
            _h264.DEFAULT_BITRATE = CAM_STREAM_BITRATE
        print(f"[CamSFU] H264 bitrate clamp: MIN={_h264.MIN_BITRATE} "
              f"MAX={_h264.MAX_BITRATE} DEFAULT={getattr(_h264,'DEFAULT_BITRATE','?')}")
    except Exception as e:
        print(f"[CamSFU] не удалось ограничить bitrate: {e}")


if _AIORTC_OK:
    _patch_aiortc_bitrate_floor()


# Действия камеры в сигнальном протоколе (новые, рядом со старыми).
CAM_ACT_START        = 'camera_start'
CAM_ACT_STOP         = 'camera_stop'
CAM_ACT_WATCH_START  = 'camera_watch_start'
CAM_ACT_WATCH_STOP   = 'camera_watch_stop'
# Отдельный канал SDP под камеру, чтобы не пересекаться с экраном (CMD_WEBRTC_*).
CAM_ACT_WEBRTC_OFFER  = 'camera_webrtc_offer'
CAM_ACT_WEBRTC_ANSWER = 'camera_webrtc_answer'

_VIEWER_ICE_TIMEOUT = 5.0
_H264_CLOCK = 90000


# ───────────────────────────────────────────────────────────────────────────
#  CameraVideoTrack — источник H.264 для aiortc.
#  Берёт BGR-кадры из CameraCaptureThread (через push_frame) и отдаёт
#  av.VideoFrame в нужном темпе. aiortc сам кодирует в H.264.
# ───────────────────────────────────────────────────────────────────────────
class CameraVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, fps: int = CAM_STREAM_FPS, size: int = CAM_STREAM_SIZE):
        super().__init__()
        self._fps = max(1, int(fps))
        self._size = int(size)
        self._interval = 1.0 / self._fps

        # Последний полученный квадратный RGB-кадр (numpy uint8 HxWx3).
        self._latest_rgb: np.ndarray | None = None
        self._lock = threading.Lock()

        self._pts = 0
        self._time_base = fractions.Fraction(1, _H264_CLOCK)
        self._start_ts: float | None = None
        self._stopped = False

    def set_size(self, size: int) -> None:
        """Меняет целевое разрешение (кружок ↔ развёрнутое окно)."""
        self._size = int(size)

    def push_bgr(self, frame_bgr: np.ndarray) -> None:
        """Вызывается из CameraCaptureThread на каждый кадр (BGR OpenCV)."""
        if self._stopped or frame_bgr is None:
            return
        try:
            import cv2
            h, w = frame_bgr.shape[:2]
            side = min(h, w)
            y0 = (h - side) // 2
            x0 = (w - side) // 2
            sq = frame_bgr[y0:y0 + side, x0:x0 + side]
            if sq.shape[0] != self._size:
                sq = cv2.resize(sq, (self._size, self._size),
                                interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(sq, cv2.COLOR_BGR2RGB)
            rgb = np.ascontiguousarray(rgb)
        except Exception:
            return
        with self._lock:
            self._latest_rgb = rgb

    async def recv(self):
        if self._start_ts is None:
            self._start_ts = time.monotonic()

        # Фиксированный целевой FPS (статичный cap, без адаптивности).
        target = self._start_ts + (self._pts / _H264_CLOCK)
        now = time.monotonic()
        wait = target + self._interval - now
        if wait > 0:
            await asyncio.sleep(wait)

        with self._lock:
            rgb = self._latest_rgb
        if rgb is None:
            rgb = np.zeros((self._size, self._size, 3), dtype=np.uint8)
        elif rgb.shape[0] != self._size:
            try:
                import cv2
                rgb = cv2.resize(rgb, (self._size, self._size),
                                 interpolation=cv2.INTER_AREA)
                rgb = np.ascontiguousarray(rgb)
            except Exception:
                pass

        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        self._pts += int(_H264_CLOCK / self._fps)
        frame.pts = self._pts
        frame.time_base = self._time_base
        return frame

    def stop(self):
        self._stopped = True
        try:
            super().stop()
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────────────
#  CameraWebRTCMixin — примесь к NetworkClient.
# ───────────────────────────────────────────────────────────────────────────
class CameraWebRTCMixin:

    def _init_camera_webrtc_attrs(self):
        # SFU-bridge под камеру (свой инстанс sidecar.exe на CAM_SFU_PORT+).
        self._cam_sfu_bridge = None
        if _AIORTC_OK and _AV_OK:
            try:
                from .sfu_bridge import SfuBridge
                self._cam_sfu_bridge = SfuBridge(
                    port=None,  # _find_free_port подберёт; см. ниже override базы
                    on_log=lambda s: print(f"[CamSFU] {s}"),
                    on_exit=lambda c: print(f"[CamSFU] завершён (code={c})"),
                )
                # Перенацеливаем поиск свободного порта на камера-диапазон.
                self._cam_sfu_bridge._port = self._find_cam_sfu_port()
                self._cam_sfu_bridge._base_url = (
                    f"http://127.0.0.1:{self._cam_sfu_bridge._port}"
                )
            except Exception as e:
                print(f"[CamSFU] init error: {e}")
                self._cam_sfu_bridge = None
        else:
            print("[CamSFU] aiortc/av недоступны — камера-WebRTC отключён")

        # Состояние ВЛАДЕЛЬЦА камеры.
        self._cam_streamer_pc = None
        self._cam_video_track: CameraVideoTrack | None = None
        self._cam_is_streaming = False
        self._cam_sender = None
        self._cam_target_bitrate = CAM_STREAM_BITRATE

        # Состояние ЗРИТЕЛЯ: uid стримера → RTCPeerConnection.
        self._cam_viewer_pcs: dict[int, object] = {}
        # uid стримера → future для answer SDP.
        self._cam_answer_futures: dict[int, object] = {}
        self._cam_lock = threading.Lock()

    @staticmethod
    def _find_cam_sfu_port() -> int:
        import socket
        from config import CAM_SFU_PORT as base, CAM_SFU_PORT_RANGE as rng
        for port in range(base, base + rng):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("0.0.0.0", port))
                s.close()
                return port
            except OSError:
                s.close()
        return base

    @property
    def cam_sfu_port(self) -> int:
        if self._cam_sfu_bridge is not None:
            return self._cam_sfu_bridge.port
        return CAM_SFU_PORT

    # ── ВЛАДЕЛЕЦ: запуск/остановка трансляции своей камеры ──────────────────
    def start_camera_stream(self) -> bool:
        """
        Поднимает камера-SFU и публикует в него H.264-трек камеры.
        Возвращает True при успехе. Кадры в трек подаёт камера через
        push_camera_frame_bgr().
        """
        if not (_AIORTC_OK and _AV_OK):
            print("[CamSFU] aiortc/av недоступны — стрим камеры невозможен")
            return False
        if self._cam_sfu_bridge is None:
            print("[CamSFU] bridge не инициализирован")
            return False

        if not self._cam_sfu_bridge.is_running():
            print("[CamSFU] запускаем камера-SFU...")
            if not self._cam_sfu_bridge.start():
                print("[CamSFU] не удалось запустить камера-SFU")
                return False

        self._start_webrtc_loop()  # переиспользуем петлю экрана (из WebRTCMixin)

        self._cam_video_track = CameraVideoTrack(
            fps=CAM_STREAM_FPS, size=CAM_STREAM_SIZE
        )
        fut = self._run_in_webrtc_loop(self._cam_publish_coro())
        try:
            ok = fut.result(timeout=15.0) if fut is not None else False
        except Exception as e:
            print(f"[CamSFU] publish ошибка: {e}")
            ok = False

        if ok:
            self._cam_is_streaming = True
        return ok

    async def _cam_publish_coro(self) -> bool:
        from aiortc.rtcrtpsender import RTCRtpSender

        if self._cam_streamer_pc is not None:
            try:
                await self._cam_streamer_pc.close()
            except Exception:
                pass
            self._cam_streamer_pc = None

        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._cam_streamer_pc = pc

        sender = pc.addTrack(self._cam_video_track)

        # Принудительно H.264 (как у экрана), фильтруем кодеки на трансивере.
        try:
            for tr in pc.getTransceivers():
                if tr.sender is sender and tr.kind == "video":
                    caps = RTCRtpSender.getCapabilities("video")
                    h264 = [c for c in caps.codecs if "H264" in c.mimeType]
                    if h264:
                        tr.setCodecPreferences(h264)
                    break
        except Exception as e:
            print(f"[CamSFU] setCodecPreferences warn: {e}")

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._cam_wait_ice(pc)
            offer_sdp = normalize_sdp_ice(pc.localDescription.sdp)

            loop = asyncio.get_running_loop()
            answer_sdp = await loop.run_in_executor(
                None, self._cam_sfu_bridge.post_streamer_offer, offer_sdp
            )
            await asyncio.wait_for(
                pc.setRemoteDescription(
                    RTCSessionDescription(sdp=answer_sdp, type="answer")
                ),
                timeout=10.0,
            )
            print(f"[CamSFU] камера опубликована в SFU (порт={self.cam_sfu_port}) "
                  f"| {CAM_STREAM_SIZE}px @ {CAM_STREAM_FPS}fps, "
                  f"cap={self._cam_target_bitrate // 1000} кбит/с")
            self._cam_sender = sender
            # Энкодер создаётся лениво на первом кадре — ставим cap чуть позже.
            asyncio.ensure_future(self._cam_apply_bitrate_loop(sender))
            return True
        except Exception as e:
            import traceback
            print(f"[CamSFU] publish EXCEPTION: {e}\n{traceback.format_exc()}")
            try:
                await pc.close()
            except Exception:
                pass
            self._cam_streamer_pc = None
            return False

    def push_camera_frame_bgr(self, frame_bgr) -> None:
        """Вызывается из CameraCaptureThread.frame_bgr → в H.264-трек."""
        tr = self._cam_video_track
        if tr is not None and self._cam_is_streaming:
            tr.push_bgr(frame_bgr)

    async def _cam_apply_bitrate_loop(self, sender) -> None:
        """Жёстко и ПОСТОЯННО удерживаем target_bitrate энкодера.

        Две защиты:
          1) Периодически (каждые 0.3с, без срока годности) выставляем target
             на наш cap. Энкодер создаётся лениво на первом кадре, поэтому
             ждём его появления.
          2) Как только энкодер появился — подменяем сам класс-сеттер
             target_bitrate, чтобы НИКАКОЙ REMB не мог поднять выше cap.
             Это страховка от разгона из-за обратной связи SFU.
        """
        patched = False
        while True:
            if self._cam_streamer_pc is None or not self._cam_is_streaming:
                return
            try:
                enc = getattr(sender, "_RTCRtpSender__encoder", None)
                if enc is not None and hasattr(enc, "target_bitrate"):
                    want = int(self._cam_target_bitrate)
                    # (1) держим значение
                    try:
                        if int(getattr(enc, "target_bitrate", 0)) != want:
                            enc.target_bitrate = want
                    except Exception:
                        pass
                    # (2) одноразовая подмена сеттера на классе энкодера —
                    #     клампим любые попытки задрать битрейт выше cap.
                    if not patched:
                        patched = self._hijack_encoder_setter(type(enc))
            except Exception:
                pass
            await asyncio.sleep(0.3)

    def _hijack_encoder_setter(self, enc_cls) -> bool:
        """Подменяет property target_bitrate у класса H264Encoder так, чтобы
        значение никогда не превышало текущий cap (self._cam_target_bitrate)."""
        try:
            if getattr(enc_cls, "_inpulse_capped", False):
                return True
            mixin = self
            orig = enc_cls.target_bitrate  # property

            def _getter(self_enc):
                return getattr(self_enc, "_inpulse_tb", mixin._cam_target_bitrate)

            def _setter(self_enc, value):
                cap = int(mixin._cam_target_bitrate)
                # Что бы ни прислал REMB — не выше cap и не ниже разумного.
                v = min(int(value), cap)
                v = max(v, CAM_H264_MIN_BITRATE)
                self_enc._inpulse_tb = v
                try:
                    self_enc.codec.bit_rate = v
                except Exception:
                    pass

            enc_cls.target_bitrate = property(_getter, _setter)
            enc_cls._inpulse_capped = True
            print(f"[CamSFU] encoder setter перехвачен — cap {self._cam_target_bitrate} bps")
            return True
        except Exception as e:
            print(f"[CamSFU] hijack setter failed: {e}")
            return False

    def set_camera_quality(self, big: bool) -> None:
        """Переключение качества: big=True при разворачивании окна на полэкрана."""
        if big:
            self._cam_target_bitrate = CAM_STREAM_BITRATE_BIG
            sz = CAM_STREAM_SIZE_BIG
        else:
            self._cam_target_bitrate = CAM_STREAM_BITRATE
            sz = CAM_STREAM_SIZE
        if self._cam_video_track is not None:
            self._cam_video_track.set_size(sz)
        # Применяем к живому энкодеру немедленно.
        sender = self._cam_sender
        if sender is not None:
            enc = getattr(sender, "_RTCRtpSender__encoder", None)
            if enc is not None and hasattr(enc, "target_bitrate"):
                try:
                    enc.target_bitrate = self._cam_target_bitrate
                except Exception:
                    pass
        print(f"[CamSFU] качество → {'BIG' if big else 'small'} "
              f"({sz}px, {self._cam_target_bitrate // 1000} кбит/с)")

    def stop_camera_stream(self) -> None:
        self._cam_is_streaming = False

        if self._cam_video_track is not None:
            try:
                self._cam_video_track.stop()
            except Exception:
                pass
            self._cam_video_track = None

        if self._cam_streamer_pc is not None:
            self._run_in_webrtc_loop(self._close_pc_coro(self._cam_streamer_pc))
            self._cam_streamer_pc = None

        if self._cam_sfu_bridge is not None and self._cam_sfu_bridge.is_running():
            try:
                self._cam_sfu_bridge.delete_streamer()
            except Exception as e:
                print(f"[CamSFU] delete_streamer error: {e}")

    # ── ЗРИТЕЛЬ: смотреть/перестать смотреть камеру друга ───────────────────
    def start_watching_camera(self, streamer_uid: int) -> None:
        """Нажали на кружок аватарки → подписываемся на камеру streamer_uid."""
        if not (_AIORTC_OK and _AV_OK):
            print("[CamView] aiortc/av недоступны")
            return
        self.send_json({
            'action': CAM_ACT_WATCH_START,
            'streamer_uid': streamer_uid,
        })
        print(f"[CamView] start_watching_camera → uid={streamer_uid}")

    def stop_watching_camera(self, streamer_uid: int) -> None:
        """Закрыли кружок → отписываемся, поток больше не грузится."""
        with self._cam_lock:
            pc = self._cam_viewer_pcs.pop(streamer_uid, None)
            self._cam_answer_futures.pop(streamer_uid, None)
        if pc is not None:
            self._run_in_webrtc_loop(self._close_pc_coro(pc))
        self.send_json({
            'action': CAM_ACT_WATCH_STOP,
            'streamer_uid': streamer_uid,
        })
        print(f"[CamView] stop_watching_camera → uid={streamer_uid}")

    async def _cam_viewer_offer_coro(self, streamer_uid: int) -> None:
        # ИДЕМПОТЕНТНОСТЬ: если уже есть живое (или подключающееся) соединение
        # к этой камере — НЕ пересоздаём его. Иначе дубликат camera_watch_start
        # (гонка в UI, фокус окна, реконнект) убивал бы рабочий поток —
        # ровно тот баг, когда камера «зависала» через ~5 секунд.
        with self._cam_lock:
            existing = self._cam_viewer_pcs.get(streamer_uid)
        if existing is not None:
            st = getattr(existing, "connectionState", None)
            if st in ("new", "connecting", "connected"):
                print(f"[CamView] uid={streamer_uid}: уже подключён ({st}), "
                      f"повторный offer пропущен")
                return
            # Соединение мертво (failed/closed) — корректно закрываем и пересоздаём.
            try:
                await existing.close()
            except Exception:
                pass
            with self._cam_lock:
                if self._cam_viewer_pcs.get(streamer_uid) is existing:
                    self._cam_viewer_pcs.pop(streamer_uid, None)

        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        with self._cam_lock:
            self._cam_viewer_pcs[streamer_uid] = pc

        pc.addTransceiver("video", direction="recvonly")

        @pc.on("track")
        def on_track(track):
            if track.kind == "video":
                asyncio.ensure_future(
                    self._cam_recv_loop(track, streamer_uid)
                )

        @pc.on("connectionstatechange")
        async def on_state():
            st = pc.connectionState
            if st in ("failed", "closed"):
                with self._cam_lock:
                    if self._cam_viewer_pcs.get(streamer_uid) is pc:
                        self._cam_viewer_pcs.pop(streamer_uid, None)
                try:
                    await pc.close()
                except Exception:
                    pass

        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            await self._cam_wait_ice(pc)
            offer_sdp = normalize_sdp_ice(pc.localDescription.sdp)

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            with self._cam_lock:
                self._cam_answer_futures[streamer_uid] = fut

            self.send_json({
                'action': CAM_ACT_WEBRTC_OFFER,
                'role': 'viewer_offer',
                'streamer_uid': streamer_uid,
                'sdp': offer_sdp,
                'type': 'offer',
            })

            try:
                answer_sdp = await asyncio.wait_for(fut, timeout=15.0)
            except asyncio.TimeoutError:
                print(f"[CamView] uid={streamer_uid}: answer таймаут")
                with self._cam_lock:
                    if self._cam_viewer_pcs.get(streamer_uid) is pc:
                        self._cam_viewer_pcs.pop(streamer_uid, None)
                await pc.close()
                return
            finally:
                with self._cam_lock:
                    self._cam_answer_futures.pop(streamer_uid, None)

            await asyncio.wait_for(
                pc.setRemoteDescription(
                    RTCSessionDescription(sdp=answer_sdp, type="answer")
                ),
                timeout=10.0,
            )
            print(f"[CamView] uid={streamer_uid}: подключён к камера-SFU")
        except Exception as e:
            import traceback
            print(f"[CamView] uid={streamer_uid} EXCEPTION: {e}\n{traceback.format_exc()}")
            with self._cam_lock:
                if self._cam_viewer_pcs.get(streamer_uid) is pc:
                    self._cam_viewer_pcs.pop(streamer_uid, None)
            try:
                await pc.close()
            except Exception:
                pass

    async def _cam_recv_loop(self, track, streamer_uid: int) -> None:
        """Принимает H.264-кадры → QImage → круглое окно (через сигнал)."""
        from PyQt6.QtGui import QImage
        print(f"[CamView] recv loop старт uid={streamer_uid}")
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"[CamView] recv error uid={streamer_uid}: {e}")
                    break
                try:
                    rgb = frame.to_ndarray(format="rgb24")
                    h, w, _ = rgb.shape
                    rgb = np.ascontiguousarray(rgb)
                    qimg = QImage(rgb.data, w, h, rgb.strides[0],
                                  QImage.Format.Format_RGB888).copy()
                    # Сигнал в UI-поток (как обычный кадр камеры).
                    if hasattr(self, 'camera_qframe_received'):
                        self.camera_qframe_received.emit(streamer_uid, qimg)
                except Exception as e:
                    print(f"[CamView] decode error uid={streamer_uid}: {e}")
        finally:
            print(f"[CamView] recv loop конец uid={streamer_uid}")

    # ── Обработка сигнального ответа от сервера ─────────────────────────────
    def _process_camera_webrtc_message(self, msg: dict, act: str) -> bool:
        """Возвращает True если сообщение относится к камера-WebRTC."""
        if act == CAM_ACT_WEBRTC_OFFER:
            role = msg.get('role', '')
            streamer_uid = int(msg.get('streamer_uid', 0) or 0)
            if role == 'viewer' and streamer_uid:
                # Сервер просит зрителя начать offer к камере streamer_uid.
                self._start_webrtc_loop()
                self._run_in_webrtc_loop(
                    self._cam_viewer_offer_coro(streamer_uid)
                )
            return True

        elif act == CAM_ACT_WEBRTC_ANSWER:
            streamer_uid = int(msg.get('streamer_uid', 0) or 0)
            sdp = msg.get('sdp', '')
            if streamer_uid and sdp:
                with self._cam_lock:
                    fut = self._cam_answer_futures.get(streamer_uid)
                if fut is not None and self._webrtc_loop and not self._webrtc_loop.is_closed():
                    def _resolve(f=fut, s=sdp):
                        if not f.done():
                            f.set_result(s)
                    self._webrtc_loop.call_soon_threadsafe(_resolve)
            return True

        return False

    @staticmethod
    async def _cam_wait_ice(pc, timeout: float = _VIEWER_ICE_TIMEOUT) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while pc.iceGatheringState != "complete":
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.05)

    def _shutdown_camera_webrtc(self) -> None:
        self.stop_camera_stream()
        with self._cam_lock:
            uids = list(self._cam_viewer_pcs.keys())
        for uid in uids:
            with self._cam_lock:
                pc = self._cam_viewer_pcs.pop(uid, None)
            if pc is not None:
                self._run_in_webrtc_loop(self._close_pc_coro(pc))
        if self._cam_sfu_bridge is not None and self._cam_sfu_bridge.is_running():
            try:
                self._cam_sfu_bridge.stop()
            except Exception:
                pass
