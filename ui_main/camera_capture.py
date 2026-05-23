# -*- coding: utf-8 -*-
"""
camera_capture.py
=================

Полный Python-пайплайн веб-камеры, НЕ зависящий от Rust/SFU стека стрима
экрана. Сделан для маленьких круглых видеосообщений (Telegram-style):
небольшое разрешение + JPEG + умеренный FPS → нагрузка на TCP-канал мала.

Содержит:
  • CameraCaptureThread — поток захвата выбранной камеры через OpenCV.
        Эмитит:
          - frame_qimage(QImage)  — для локального превью (своя камера);
          - frame_jpeg(bytes)     — JPEG для отправки в сеть (уже квадрат,
                                    отцентрован и уменьшен).
  • encode_frame_to_jpeg(...)     — bgr→квадрат→resize→JPEG (используется внутри).
  • decode_jpeg_to_qimage(bytes)  — JPEG→QImage (для входящих кадров).

Зависимости: cv2 (есть в проекте), numpy (есть), PyQt6.
"""

from __future__ import annotations

import time
import threading

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    print("[Camera] ОШИБКА: OpenCV (cv2) не установлен — захват камеры недоступен.")


# ── Параметры по умолчанию (баланс качество/трафик для кружка) ─────────────
CAM_SEND_SIZE = 320        # сторона квадрата, который улетает в сеть (px)
CAM_PREVIEW_FPS = 30       # частота кадров локального превью
CAM_SEND_FPS = 24          # частота отправки в сеть (выше → плавнее, больше трафик)
CAM_JPEG_QUALITY = 75      # 0..100; 75 — хороший компромисс

# Допустимые диапазоны (используются в настройках и при чтении из QSettings).
CAM_FPS_MIN, CAM_FPS_MAX = 5, 60
CAM_SIZE_MIN, CAM_SIZE_MAX = 160, 480
CAM_QUALITY_MIN, CAM_QUALITY_MAX = 40, 95


def _center_square_crop(frame_bgr: np.ndarray) -> np.ndarray:
    """Центральный crop кадра до квадрата (без растяжения)."""
    h, w = frame_bgr.shape[:2]
    side = min(h, w)
    y0 = (h - side) // 2
    x0 = (w - side) // 2
    return frame_bgr[y0:y0 + side, x0:x0 + side]


def encode_frame_to_jpeg(frame_bgr: np.ndarray,
                         size: int = CAM_SEND_SIZE,
                         quality: int = CAM_JPEG_QUALITY) -> bytes | None:
    """
    BGR-кадр OpenCV → квадрат `size`×`size` → JPEG bytes.
    Возвращает None при ошибке.
    """
    if not CV2_AVAILABLE or frame_bgr is None:
        return None
    try:
        sq = _center_square_crop(frame_bgr)
        if sq.shape[0] != size:
            sq = cv2.resize(sq, (size, size), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(
            ".jpg", sq, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        )
        if not ok:
            return None
        return buf.tobytes()
    except Exception as e:
        print(f"[Camera] encode_frame_to_jpeg error: {e}")
        return None


def decode_jpeg_to_qimage(jpeg_bytes: bytes) -> QImage | None:
    """JPEG bytes → QImage (RGB888). Возвращает None при ошибке."""
    if not jpeg_bytes:
        return None
    try:
        # Сначала пробуем напрямую через QImage (без cv2 — быстрее, без BGR↔RGB).
        img = QImage()
        if img.loadFromData(jpeg_bytes, "JPG") and not img.isNull():
            return img.convertToFormat(QImage.Format.Format_RGB888)
    except Exception:
        pass
    # Fallback через cv2 (если QImage не смог).
    if CV2_AVAILABLE:
        try:
            arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is None:
                return None
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            rgb = np.ascontiguousarray(rgb)
            h, w, _ = rgb.shape
            return QImage(rgb.data, w, h, rgb.strides[0],
                          QImage.Format.Format_RGB888).copy()
        except Exception as e:
            print(f"[Camera] decode_jpeg_to_qimage error: {e}")
    return None


def _bgr_to_qimage(frame_bgr: np.ndarray) -> QImage | None:
    """BGR ndarray → QImage (для локального превью, без JPEG-петли)."""
    try:
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        rgb = np.ascontiguousarray(rgb)
        h, w, _ = rgb.shape
        return QImage(rgb.data, w, h, rgb.strides[0],
                      QImage.Format.Format_RGB888).copy()
    except Exception as e:
        print(f"[Camera] _bgr_to_qimage error: {e}")
        return None


class CameraCaptureThread(QThread):
    """
    Поток захвата камеры. Открывает cv2.VideoCapture(camera_index), читает
    кадры и эмитит:
      • frame_qimage(QImage) — для локального превью (своя камера в кружке);
      • frame_jpeg(bytes)    — JPEG для отправки в сеть (throttled до send_fps).

    Останов: вызвать stop() и подождать (поток daemon-подобный, проверяет флаг).
    """

    frame_qimage = pyqtSignal(QImage)
    frame_jpeg   = pyqtSignal(bytes)
    opened       = pyqtSignal(bool)     # True если камера успешно открылась
    error        = pyqtSignal(str)

    def __init__(self, camera_index: int = 0,
                 preview_fps: int = CAM_PREVIEW_FPS,
                 send_fps: int = CAM_SEND_FPS,
                 send_size: int = CAM_SEND_SIZE,
                 jpeg_quality: int = CAM_JPEG_QUALITY,
                 parent=None):
        super().__init__(parent)
        self.camera_index = int(camera_index)
        self.preview_fps = max(1, int(preview_fps))
        self.send_fps = max(1, int(send_fps))
        self.send_size = int(send_size)
        self.jpeg_quality = int(jpeg_quality)
        self._running = False
        self._cap = None

    def run(self):
        # Внешняя защита: что бы ни случилось внутри (включая cv2.error —
        # "Unknown C++ exception from OpenCV code"), исключение НЕ должно
        # покидать QThread.run(), иначе подвисает/падает весь Qt event loop.
        try:
            self._run_impl()
        except Exception as e:
            print(f"[Camera] run() fatal: {e}")
            try:
                self.error.emit(f"Сбой камеры: {e}")
            except Exception:
                pass
            try:
                self.opened.emit(False)
            except Exception:
                pass
        finally:
            # Гарантированно освобождаем устройство при любом исходе.
            self._running = False
            cap = self._cap
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            self._cap = None

    def _run_impl(self):
        if not CV2_AVAILABLE:
            self.error.emit("OpenCV не установлен")
            self.opened.emit(False)
            return

        # На Windows CAP_DSHOW открывается заметно быстрее и стабильнее.
        cap = None
        try:
            backend = getattr(cv2, "CAP_DSHOW", 0)
            cap = cv2.VideoCapture(self.camera_index, backend)
            if not cap.isOpened():
                # fallback на дефолтный backend
                cap.release()
                cap = cv2.VideoCapture(self.camera_index)
        except Exception as e:
            self.error.emit(f"Не удалось открыть камеру: {e}")
            self.opened.emit(False)
            return

        if cap is None or not cap.isOpened():
            self.error.emit(f"Камера #{self.camera_index} недоступна")
            self.opened.emit(False)
            return

        # Запоминаем cap сразу — чтобы finally в run() мог его освободить,
        # даже если что-то упадёт на этапе конфигурации ниже.
        self._cap = cap

        # Просим у камеры разрешение и FPS. MJPG-кодек на многих камерах
        # позволяет выдавать высокий FPS (30/60), тогда как сырой YUY2 часто
        # ограничен ~10-15 fps на 640×480.
        try:
            try:
                fourcc = cv2.VideoWriter_fourcc(*"MJPG")
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)
            except Exception:
                pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            # Запрашиваем максимум из превью/отправки, чтобы камера давала
            # достаточно кадров для самого частого потребителя.
            cap.set(cv2.CAP_PROP_FPS, max(self.preview_fps, self.send_fps))
            # Минимальная буферизация → меньше задержка.
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
        except Exception:
            pass

        self._running = True
        self.opened.emit(True)
        print(f"[Camera] Камера #{self.camera_index} открыта")

        preview_interval = 1.0 / self.preview_fps
        send_interval = 1.0 / self.send_fps
        last_send = 0.0

        # Сколько подряд неудачных/сбойных чтений терпим, прежде чем считать
        # камеру потерянной и корректно выйти (вместо зависания/краша).
        MAX_CONSECUTIVE_FAILURES = 60   # ~ 0.6 c при паузе 0.01 на сбой
        fail_count = 0

        while self._running:
            t0 = time.perf_counter()

            # cap.read() на некоторых драйверах/при отключении камеры умеет
            # бросать cv2.error ("Unknown C++ exception from OpenCV code").
            # Этот блок ловит ВСЁ (включая cv2.error), чтобы один сбой чтения
            # не валил QThread и не подвешивал весь Qt event loop.
            try:
                ok, frame = cap.read()
            except Exception as e:
                ok, frame = False, None
                print(f"[Camera] cap.read() exception: {e}")

            if not ok or frame is None:
                fail_count += 1
                if fail_count >= MAX_CONSECUTIVE_FAILURES:
                    # Камера, похоже, отвалилась окончательно — выходим аккуратно.
                    print("[Camera] Слишком много сбоев чтения — закрываю камеру")
                    try:
                        self.error.emit("Камера перестала отвечать")
                    except Exception:
                        pass
                    break
                # одиночный сбой — не падаем, ждём чуть-чуть
                time.sleep(0.01)
                continue

            # Успешный кадр — сбрасываем счётчик сбоев.
            fail_count = 0

            # Локальное превью (каждый кадр). Кодирование/конвертация тоже
            # под защитой, т.к. encode/cvtColor могут бросить на битом кадре.
            try:
                qimg = _bgr_to_qimage(frame)
                if qimg is not None:
                    self.frame_qimage.emit(qimg)

                # Отправка в сеть — throttle до send_fps.
                now = time.perf_counter()
                if now - last_send >= send_interval:
                    last_send = now
                    jpeg = encode_frame_to_jpeg(
                        frame, self.send_size, self.jpeg_quality
                    )
                    if jpeg is not None:
                        self.frame_jpeg.emit(jpeg)
            except Exception as e:
                # Сбой обработки одного кадра не должен ронять поток.
                print(f"[Camera] frame processing error: {e}")

            # Держим частоту превью.
            dt = time.perf_counter() - t0
            sleep_left = preview_interval - dt
            if sleep_left > 0:
                time.sleep(sleep_left)

        self._running = False
        try:
            cap.release()
        except Exception:
            pass
        self._cap = None
        print(f"[Camera] Камера #{self.camera_index} закрыта")

    def stop(self):
        self._running = False
        # Дать run() выйти из цикла и release().
        self.wait(1500)
