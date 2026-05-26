# -*- coding: utf-8 -*-
"""
ui_webcamera.py
===============

Круглое плавающее окно веб-камеры (Picture-in-Picture), по концепции
кружочков-видеосообщений Telegram.

Содержит ТОЛЬКО самостоятельные классы виджетов — никакой логики главного
окна здесь нет. Интеграция выполняется через `WebcamMixin` (см. ниже)
и точечные правки в `ui_main.py`, описанные в инструкции.

Возможности окна `CircularVideoWindow`:
  • круглое окно без рамок ОС;
  • всегда поверх остальных окон (WindowStaysOnTopHint);
  • свободное перетаскивание ЛКМ по всему экрану;
  • изменение размера при наведении на край окна (resize-кольцо);
  • центральный crop видеопотока 16:9 → 1:1 без растягивания;
  • кнопка «закрыть» (×) проявляется только при наведении (hover);
  • закрытие окна НЕ выключает камеру (статус в дереве остаётся активным).

PyQt6.
"""

from __future__ import annotations

import base64

from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import (
    Qt, QPoint, QRect, QSize, QTimer, QPointF, pyqtSignal,
)
from PyQt6.QtGui import (
    QImage, QPainter, QColor, QPen, QBrush, QPainterPath,
    QRegion, QPixmap, QCursor, QRadialGradient,
)

try:
    from .camera_capture import (
        CameraCaptureThread, decode_jpeg_to_qimage,
        CAM_SEND_SIZE, CAM_PREVIEW_FPS, CAM_SEND_FPS, CAM_JPEG_QUALITY,
        CAM_FPS_MIN, CAM_FPS_MAX, CAM_SIZE_MIN, CAM_SIZE_MAX,
        CAM_QUALITY_MIN, CAM_QUALITY_MAX,
    )
    _CAPTURE_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    CameraCaptureThread = None
    decode_jpeg_to_qimage = None
    CAM_SEND_SIZE, CAM_PREVIEW_FPS, CAM_SEND_FPS, CAM_JPEG_QUALITY = 320, 30, 24, 75
    CAM_FPS_MIN, CAM_FPS_MAX = 5, 60
    CAM_SIZE_MIN, CAM_SIZE_MAX = 160, 480
    CAM_QUALITY_MIN, CAM_QUALITY_MAX = 40, 95
    _CAPTURE_AVAILABLE = False
    print(f"[Camera] camera_capture недоступен: {_e}")


# ───────────────────────────────────────────────────────────────────────────
#  Цветовая палитра — совпадает с зелёным «активным» цветом mute/deafen кнопок
#  (#2ecc71), чтобы индикатор камеры визуально жил в той же системе.
# ───────────────────────────────────────────────────────────────────────────
_ACCENT       = QColor(46, 204, 113)        # #2ecc71 — зелёный «камера активна»
_ACCENT_DIM   = QColor(46, 204, 113, 90)
_RING_BG      = QColor(10, 12, 18, 230)
_CLOSE_BG     = QColor(20, 22, 30, 200)
_CLOSE_BG_HI  = QColor(231, 76, 60, 235)    # красный при наведении на ×
_CLOSE_FG     = QColor(236, 240, 241)
_PLACEHOLDER  = QColor(150, 158, 175)


class CircularVideoWindow(QWidget):
    """
    Плавающее круглое окно с видеопотоком одного пользователя.

    Кадры приходят как QImage через слот `update_frame(QImage)` — тот же
    формат, что и у обычного VideoWindow, поэтому окно подключается к
    существующему пайплайну `on_video_frame(uid, q_image)` без изменений
    в сетевом слое.
    """

    # uid пользователя, чьё окно закрыли (для очистки в главном окне).
    window_closed = pyqtSignal(int)

    # Размерные ограничения круга (диаметр в пикселях).
    MIN_DIAMETER = 140
    MAX_DIAMETER = 640
    DEFAULT_DIAMETER = 220

    # Толщина «горячей» зоны у края окна, в которой курсор переключается
    # в режим изменения размера.
    _RESIZE_EDGE = 14

    # Геометрия кнопки закрытия (диаметр кружка ×).
    _CLOSE_BTN_D = 30

    def __init__(self, uid: int, nick: str = "", parent=None):
        super().__init__(parent)

        self.uid = uid
        self.nick = nick or ""
        self._closing = False

        # Текущий кадр (уже обрезанный до квадрата мы НЕ храним —
        # crop делается на лету в paintEvent, чтобы при ресайзе картинка
        # всегда оставалась чёткой).
        self._frame: QImage | None = None

        # Состояния мыши.
        self._hovered = False           # курсор над окном (для показа ×)
        self._drag_active = False       # идёт перетаскивание
        self._drag_offset = QPoint()    # смещение курсора внутри окна
        self._resize_active = False     # идёт изменение размера
        self._resize_anchor = QPoint()  # глобальная точка старта ресайза
        self._resize_start_d = 0        # диаметр на старте ресайза
        self._resize_center = QPoint()  # экранный центр окна на старте ресайза
        self._close_hovered = False     # курсор над кнопкой ×

        self._init_window_flags()
        self._init_geometry()

        # Отслеживаем смену экрана (мониторы с разным DPI) → перерисовка круга.
        # Маски нет, поэтому форма не ломается; это лишь форсирует repaint.
        self._last_screen = None
        try:
            wh = self.windowHandle()
            if wh is not None:
                wh.screenChanged.connect(self._on_screen_changed)
        except Exception:
            pass

        # Лёгкая пульсация акцентного кольца, когда нет кадра (показывает,
        # что окно «живое» и ждёт видео).
        self._pulse = 0.0
        self._pulse_dir = 1
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(40)
        self._pulse_timer.timeout.connect(self._on_pulse_tick)
        self._pulse_timer.start()

        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # ── Инициализация окна ──────────────────────────────────────────────
    def _init_window_flags(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool                 # не светится в таскбаре
        )
        # Прозрачный фон + перерисовка по маске → круглая форма без
        # прямоугольных «ушей».
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def _init_geometry(self):
        d = self.DEFAULT_DIAMETER
        self.resize(d, d)
        # Стартовая позиция — правый нижний угол активного экрана,
        # с отступом, чтобы не лезть под системный трей.
        scr = self._current_screen_geometry()
        x = scr.right() - d - 40
        y = scr.bottom() - d - 80
        self.move(x, y)

    def _current_screen_geometry(self) -> QRect:
        scr = QApplication.screenAt(QCursor.pos())
        if scr is None:
            scr = QApplication.primaryScreen()
        return scr.availableGeometry()

    # ── Приём кадров ────────────────────────────────────────────────────
    def update_frame(self, q_img: QImage):
        """Слот для входящих кадров (QImage). Совместим с on_video_frame."""
        if self._closing or q_img is None or q_img.isNull():
            return
        self._frame = q_img
        self.update()

    def clear_frame(self):
        self._frame = None
        self.update()

    # ── Пульсация плейсхолдера ──────────────────────────────────────────
    def _on_pulse_tick(self):
        # Пульсируем только пока ждём первый кадр.
        if self._frame is not None:
            return
        self._pulse += 0.05 * self._pulse_dir
        if self._pulse >= 1.0:
            self._pulse, self._pulse_dir = 1.0, -1
        elif self._pulse <= 0.0:
            self._pulse, self._pulse_dir = 0.0, 1
        self.update()

    # ── Геометрические помощники ────────────────────────────────────────
    def _diameter(self) -> int:
        return min(self.width(), self.height())

    def _circle_rect(self) -> QRect:
        """Квадрат, в который вписан круг (с отступом под кольцо/тень)."""
        d = self._diameter()
        return QRect(0, 0, d, d)

    def _close_btn_rect(self) -> QRect:
        """Прямоугольник кнопки × — в правом верхнем секторе круга, но так,
        чтобы целиком оставаться внутри окружности."""
        d = self._diameter()
        bd = self._CLOSE_BTN_D
        import math
        cx = cy = d / 2.0
        r = d / 2.0
        # Половина диагонали кнопки + запас от края кольца.
        half_diag = (bd / 2.0) * math.sqrt(2)
        margin = 4.0
        # Максимальный радиус центра кнопки, чтобы её угол не вылез за круг.
        max_offset = r - half_diag - margin
        # Желаемое — почти у края (88% радиуса), но не больше допустимого.
        offset = min(r * 0.88, max_offset)
        if offset < 0:
            offset = 0
        ang = math.radians(45)   # вверх-вправо
        bx = cx + offset * math.cos(ang) - bd / 2.0
        by = cy - offset * math.sin(ang) - bd / 2.0
        return QRect(int(round(bx)), int(round(by)), bd, bd)

    def _dist_from_center(self, pos: QPoint) -> float:
        d = self._diameter()
        c = QPointF(d / 2.0, d / 2.0)
        dx = pos.x() - c.x()
        dy = pos.y() - c.y()
        return (dx * dx + dy * dy) ** 0.5

    def _on_resize_edge(self, pos: QPoint) -> bool:
        """True, если курсор в кольце у внешней границы круга."""
        r = self._diameter() / 2.0
        dist = self._dist_from_center(pos)
        return (r - self._RESIZE_EDGE) <= dist <= (r + 2)

    def _inside_circle(self, pos: QPoint) -> bool:
        return self._dist_from_center(pos) <= (self._diameter() / 2.0)

    # ── Маска окна (круглая зона кликов) ────────────────────────────────
    def _apply_mask(self):
        # Совместимость: маска больше не используется (см. ниже про DPI).
        return

    def resizeEvent(self, event):
        # КРИТИЧНО: окно должно всегда быть КВАДРАТНЫМ. При переносе между
        # мониторами с разным DPI Windows может пересчитать размер неравномерно
        # (width != height) → видео рисуется прямоугольником, а кольцо кругом
        # (как на скриншоте). Принудительно выравниваем в квадрат.
        w, h = self.width(), self.height()
        if w != h:
            side = min(w, h)
            self.resize(side, side)
            return
        self.update()
        super().resizeEvent(event)

    def moveEvent(self, event):
        # При перетаскивании на другой монитор может смениться DPI —
        # перевыставляем маску и проверяем смену экрана.
        self._maybe_handle_screen_change()
        super().moveEvent(event)

    def showEvent(self, event):
        try:
            wh = self.windowHandle()
            if wh is not None:
                try:
                    wh.screenChanged.disconnect(self._on_screen_changed)
                except (RuntimeError, TypeError):
                    pass
                wh.screenChanged.connect(self._on_screen_changed)
                self._last_screen = wh.screen()
        except Exception:
            pass
        self.update()
        super().showEvent(event)

    def _on_screen_changed(self, screen):
        # Экран сменился (другой DPI) — выравниваем в квадрат и перерисовываем.
        self._last_screen = screen
        QTimer.singleShot(0, self._ensure_square)
        QTimer.singleShot(60, self._ensure_square)
        QTimer.singleShot(0, self.update)
        QTimer.singleShot(60, self.update)

    def _maybe_handle_screen_change(self):
        try:
            wh = self.windowHandle()
            if wh is None:
                return
            scr = wh.screen()
            if scr is not self._last_screen:
                self._last_screen = scr
                QTimer.singleShot(0, self._ensure_square)
                QTimer.singleShot(60, self._ensure_square)
                QTimer.singleShot(0, self.update)
                QTimer.singleShot(60, self.update)
        except Exception:
            pass

    def _ensure_square(self):
        """Гарантирует квадратность окна (после DPI-перехода)."""
        w, h = self.width(), self.height()
        if w != h:
            self.resize(min(w, h), min(w, h))
        self.update()

    # ── Отрисовка ───────────────────────────────────────────────────────
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Без маски: гарантируем полностью прозрачный фон (чистые углы на
        # любом DPI/мониторе). CompositionMode_Source = перезапись альфой.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        d = self._diameter()
        rect = QRect(0, 0, d, d)

        # 1. Круглая клип-маска под видео.
        clip = QPainterPath()
        clip.addEllipse(2, 2, d - 4, d - 4)
        painter.save()
        painter.setClipPath(clip)

        if self._frame is not None and not self._frame.isNull():
            self._paint_cropped_frame(painter, d)
        else:
            self._paint_placeholder(painter, d)

        painter.restore()

        # 2. Акцентное кольцо-обводка по краю.
        self._paint_ring(painter, d)

        # 3. Кнопка закрытия — только при наведении на окно.
        if self._hovered:
            self._paint_close_button(painter)

        painter.end()

    def _paint_cropped_frame(self, painter: QPainter, d: int):
        """
        Центральный crop кадра до квадрата 1:1, затем вписывание в круг.
        Видео 16:9 → берём центральный квадрат высотой = высоте кадра.
        Никакого растяжения: соотношение сторон сохраняется.
        """
        img = self._frame
        iw, ih = img.width(), img.height()
        if iw <= 0 or ih <= 0:
            return

        side = min(iw, ih)
        sx = (iw - side) // 2          # центрируем по горизонтали (для 16:9)
        sy = (ih - side) // 2          # центрируем по вертикали (для 9:16)
        src = QRect(sx, sy, side, side)
        dst = QRect(0, 0, d, d)
        painter.drawImage(dst, img, src)

    def _paint_placeholder(self, painter: QPainter, d: int):
        # Мягкий радиальный градиент-фон + иконка/текст ожидания.
        grad = QRadialGradient(QPointF(d / 2, d / 2), d / 2)
        grad.setColorAt(0.0, QColor(28, 32, 44))
        grad.setColorAt(1.0, QColor(12, 14, 20))
        painter.fillRect(0, 0, d, d, QBrush(grad))

        painter.setPen(QPen(_PLACEHOLDER))
        f = painter.font()
        f.setPointSize(max(9, d // 22))
        painter.setFont(f)
        painter.drawText(QRect(0, 0, d, d),
                         Qt.AlignmentFlag.AlignCenter,
                         "Ожидание\nвидео…")

    def _paint_ring(self, painter: QPainter, d: int):
        # Двойное кольцо: тёмная подложка + акцентный зелёный контур.
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # подложка
        painter.setPen(QPen(_RING_BG, 5))
        painter.drawEllipse(3, 3, d - 6, d - 6)

        # акцент — при ожидании кадра пульсирует прозрачностью
        accent = QColor(_ACCENT)
        if self._frame is None:
            a = int(120 + 135 * self._pulse)
            accent.setAlpha(max(0, min(255, a)))
        painter.setPen(QPen(accent, 3))
        painter.drawEllipse(3, 3, d - 6, d - 6)

    def _paint_close_button(self, painter: QPainter):
        r = self._close_btn_rect()
        bg = _CLOSE_BG_HI if self._close_hovered else _CLOSE_BG
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.setBrush(QBrush(bg))
        painter.drawEllipse(r)

        # сам крестик
        painter.setPen(QPen(_CLOSE_FG, 2.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        m = r.width() * 0.30
        painter.drawLine(QPointF(r.left() + m, r.top() + m),
                         QPointF(r.right() - m, r.bottom() - m))
        painter.drawLine(QPointF(r.right() - m, r.top() + m),
                         QPointF(r.left() + m, r.bottom() - m))

    # ── Hover (наведение) ───────────────────────────────────────────────
    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._close_hovered = False
        if not self._drag_active and not self._resize_active:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        super().leaveEvent(event)

    # ── Мышь: перетаскивание / ресайз / закрытие ────────────────────────
    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)

        pos = event.position().toPoint()

        # 1) клик по кнопке × (только когда она видима — т.е. при hover).
        if self._hovered and self._close_btn_rect().contains(pos):
            self._request_close()
            return

        # 2) клик у края → старт изменения размера.
        if self._on_resize_edge(pos):
            self._resize_active = True
            self._resize_anchor = event.globalPosition().toPoint()
            self._resize_start_d = self._diameter()
            self._resize_center = self.frameGeometry().center()
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            return

        # 3) клик внутри круга → старт перетаскивания.
        if self._inside_circle(pos):
            self._drag_active = True
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        gpos = event.globalPosition().toPoint()

        # активный ресайз
        if self._resize_active:
            self._do_resize(gpos)
            return

        # активное перетаскивание
        if self._drag_active:
            self.move(gpos - self._drag_offset)
            return

        # пассивное движение — обновляем курсор и состояние кнопки ×
        on_close = self._hovered and self._close_btn_rect().contains(pos)
        if on_close != self._close_hovered:
            self._close_hovered = on_close
            self.update()

        if on_close:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif self._on_resize_edge(pos):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_drag = self._drag_active
            self._drag_active = False
            self._resize_active = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if was_drag:
                # После переноса (возможно, на другой монитор) — перерисовать.
                self._maybe_handle_screen_change()
                self.update()
        super().mouseReleaseEvent(event)

    def _do_resize(self, gpos: QPoint):
        """
        Диаметр меняем по расстоянию курсора от центра окна, удерживая
        центр окна на месте — круг «дышит» симметрично, без рывков.
        """
        cx = self._resize_center.x()
        cy = self._resize_center.y()
        dx = gpos.x() - cx
        dy = gpos.y() - cy
        dist = (dx * dx + dy * dy) ** 0.5
        new_d = int(max(self.MIN_DIAMETER,
                        min(self.MAX_DIAMETER, dist * 2)))
        if new_d == self._diameter():
            return
        self.resize(new_d, new_d)
        # пересчитываем top-left, чтобы центр остался на месте
        self.move(cx - new_d // 2, cy - new_d // 2)

    # ── Закрытие (камера остаётся активной) ─────────────────────────────
    def _request_close(self):
        self.close()

    def closeEvent(self, event):
        self._closing = True
        try:
            self._pulse_timer.stop()
        except Exception:
            pass
        self._frame = None
        # Сообщаем главному окну ТОЛЬКО о закрытии окна просмотра.
        # Главное окно НЕ должно гасить индикатор камеры в дереве —
        # это поведение по ТЗ (см. WebcamMixin.close_camera_window).
        if self.uid is not None:
            self.window_closed.emit(self.uid)
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════
#  WebcamMixin — логика главного окна, вынесенная в примесь.
#
#  Подмешивается к классу главного окна (см. инструкцию по интеграции).
#  Не содержит ничего, что меняло бы вёрстку: только методы-обработчики
#  + словарь активных круглых окон.
# ═══════════════════════════════════════════════════════════════════════════
class WebcamMixin:
    """
    Требует от хост-класса (главного окна) наличия:
      • self.net               — сетевой движок (для start/stop_watching при желании);
      • self.known_uids        — реестр {uid: {... 'is_cam': bool, 'item': QTreeWidgetItem}};
      • self.audio.my_uid      — собственный uid;
      • self.is_camera_on      — флаг своей камеры (создаётся ниже, если нет);
      • self.cam_windows       — dict (создаётся ниже, если нет).

    Метод `init_webcam_state()` нужно вызвать один раз в __init__ хоста.
    """

    # ── Состояние ───────────────────────────────────────────────────────
    def init_webcam_state(self):
        if not hasattr(self, "cam_windows"):
            self.cam_windows: dict[int, CircularVideoWindow] = {}
        if not hasattr(self, "is_camera_on"):
            self.is_camera_on = False
        if not hasattr(self, "_cam_capture"):
            self._cam_capture = None        # CameraCaptureThread (своя камера)

    def _read_camera_settings(self):
        """Читает параметры камеры из настроек с клампом в допустимые границы."""
        def _clamp(v, lo, hi, default):
            try:
                v = int(v)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        s = self.app_settings
        cam_index = _clamp(s.value("camera_index", 0), 0, 32, 0)
        send_fps  = _clamp(s.value("camera_send_fps", CAM_SEND_FPS),
                           CAM_FPS_MIN, CAM_FPS_MAX, CAM_SEND_FPS)
        # Превью держим не ниже отправки и не ниже дефолта — для плавности.
        preview_fps = max(send_fps, _clamp(s.value("camera_preview_fps", CAM_PREVIEW_FPS),
                                           CAM_FPS_MIN, CAM_FPS_MAX, CAM_PREVIEW_FPS))
        size    = _clamp(s.value("camera_send_size", CAM_SEND_SIZE),
                         CAM_SIZE_MIN, CAM_SIZE_MAX, CAM_SEND_SIZE)
        quality = _clamp(s.value("camera_jpeg_quality", CAM_JPEG_QUALITY),
                         CAM_QUALITY_MIN, CAM_QUALITY_MAX, CAM_JPEG_QUALITY)
        return cam_index, preview_fps, send_fps, size, quality

    def _make_camera_thread(self):
        """Создаёт настроенный поток захвата (параметры из настроек)."""
        cam_index, preview_fps, send_fps, size, quality = self._read_camera_settings()
        print(f"[Camera] поток: idx={cam_index} preview_fps={preview_fps} "
              f"send_fps={send_fps} size={size} q={quality}")
        th = CameraCaptureThread(
            camera_index=cam_index,
            preview_fps=preview_fps,
            send_fps=send_fps,
            send_size=size,
            jpeg_quality=quality,
        )
        th.frame_qimage.connect(self._on_local_cam_frame)
        th.frame_jpeg.connect(self._on_local_cam_jpeg)
        th.frame_bgr.connect(self._on_local_cam_bgr)
        th.opened.connect(self._on_cam_opened)
        th.error.connect(self._on_cam_error)
        return th

    # ── Кнопка камеры в нижней панели ───────────────────────────────────
    def toggle_camera(self):
        """
        Обработчик клика по кнопке веб-камеры (btn_cam).

        Кнопка checkable: checked == камера включена. Фон зелёный в :checked
        обеспечивается стилем #barBtnCam:checked.

        При включении:
          • открывается выбранная камера (поток CameraCaptureThread);
          • кадры идут в локальное превью (своё круглое окно);
          • JPEG-кадры отправляются на сервер ({"action":"camera_frame"}),
            который раздаёт их пользователям в комнате;
          • серверу шлётся {"action":"camera_start"} (поднимает флаг is_camera).
        """
        self.init_webcam_state()
        self.is_camera_on = self.btn_cam.isChecked()
        my_uid = getattr(self.audio, "my_uid", 0)

        if self.is_camera_on:
            cam_index = 0
            cam_name = ""
            try:
                cam_index = int(self.app_settings.value("camera_index", 0))
                cam_name = self.app_settings.value("camera_name", "") or ""
            except Exception:
                pass

            if not _CAPTURE_AVAILABLE:
                print("[Camera] Захват недоступен (нет OpenCV). Камера не запущена.")
                self.is_camera_on = False
                self.btn_cam.setChecked(False)
                return

            print(f"[Camera] Запуск камеры idx={cam_index} name={cam_name!r}")

            # Останавливаем прошлый поток, если вдруг остался.
            self._stop_camera_capture()

            self._cam_capture = self._make_camera_thread()
            self._cam_capture.start()

            # НОВЫЙ путь: поднимаем свой камера-SFU и публикуем H.264-трек.
            # Кадры в трек подаёт _on_local_cam_bgr (frame_bgr из захвата).
            cam_ok = False
            try:
                cam_ok = self.net.start_camera_stream()
            except Exception as e:
                print(f"[Camera] start_camera_stream error: {e}")

            if not cam_ok:
                # Фолбэк/ошибка: WebRTC-камера не поднялась. Выключаем кнопку.
                print("[Camera] камера-SFU/WebRTC не запустился — отмена.")
                self.is_camera_on = False
                try:
                    self.btn_cam.setChecked(False)
                except Exception:
                    pass
                self._stop_camera_capture()
                if my_uid in self.cam_windows:
                    self._destroy_cam_window(my_uid)
                return

            # Сообщаем серверу о включении камеры + порт нашего камера-SFU.
            try:
                cam_port = getattr(self.net, 'cam_sfu_port', 7820)
                self.net.send_json({
                    "action": "camera_start",
                    "camera_sfu_port": int(cam_port),
                })
            except Exception:
                pass

            # Сразу открываем своё круглое превью.
            self.open_camera_window(my_uid, "Моя камера")
        else:
            # Останавливаем захват.
            self._stop_camera_capture()
            # Останавливаем публикацию камеры в SFU.
            try:
                self.net.stop_camera_stream()
            except Exception:
                pass
            try:
                self.net.send_json({"action": "camera_stop"})
            except Exception:
                pass
            # Своё локальное превью закрываем вместе с камерой.
            if my_uid in self.cam_windows:
                self._destroy_cam_window(my_uid)

        # Обновляем индикатор в дереве для самого себя.
        if my_uid in self.known_uids:
            self.known_uids[my_uid]["is_cam"] = self.is_camera_on

        if hasattr(self, "refresh_ui"):
            self.refresh_ui()

    # ── Колбэки захвата своей камеры ─────────────────────────────────────
    def _on_cam_opened(self, ok: bool):
        if not ok:
            print("[Camera] Камера не открылась — выключаю кнопку.")
            self.is_camera_on = False
            try:
                self.btn_cam.setChecked(False)
            except Exception:
                pass
            self._stop_camera_capture()
            my_uid = getattr(self.audio, "my_uid", 0)
            if my_uid in self.cam_windows:
                self._destroy_cam_window(my_uid)
            if my_uid in self.known_uids:
                self.known_uids[my_uid]["is_cam"] = False
            try:
                self.net.send_json({"action": "camera_stop"})
            except Exception:
                pass
            if hasattr(self, "refresh_ui"):
                self.refresh_ui()

    def _on_cam_error(self, message: str):
        print(f"[Camera] Ошибка камеры: {message}")

    def _on_local_cam_frame(self, q_image: QImage):
        """Локальное превью своей камеры → своё круглое окно."""
        my_uid = getattr(self.audio, "my_uid", 0)
        win = self.cam_windows.get(my_uid)
        if win is not None and win.isVisible():
            win.update_frame(q_image)

    def _on_local_cam_jpeg(self, jpeg_bytes: bytes):
        """LEGACY: раньше слал JPEG на сервер. Теперь камера идёт через
        WebRTC/SFU, поэтому JPEG в сеть больше НЕ отправляем."""
        return

    def _on_local_cam_bgr(self, frame_bgr):
        """Сырой BGR-кадр своей камеры → в H.264-трек WebRTC (camera-SFU)."""
        if not self.is_camera_on:
            return
        try:
            self.net.push_camera_frame_bgr(frame_bgr)
        except Exception as e:
            print(f"[Camera] push_camera_frame_bgr error: {e}")

    def _stop_camera_capture(self):
        cap = getattr(self, "_cam_capture", None)
        if cap is None:
            return
        try:
            cap.frame_qimage.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            cap.frame_jpeg.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            cap.opened.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            cap.error.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            cap.stop()
        except Exception:
            pass
        self._cam_capture = None

    def restart_camera_capture(self):
        """
        Перезапускает захват с НОВЫМ источником из настроек, не выключая
        камеру (флаг is_camera_on остаётся, серверу camera_stop НЕ шлём).
        Используется при смене камеры в настройках «на лету».
        """
        if not getattr(self, "is_camera_on", False):
            return
        if not _CAPTURE_AVAILABLE:
            return

        print("[Camera] Перезапуск захвата с новыми параметрами/источником")
        self._stop_camera_capture()

        self._cam_capture = self._make_camera_thread()
        self._cam_capture.start()

        # Своё круглое превью держим открытым (если было закрыто — откроем).
        my_uid = getattr(self.audio, "my_uid", 0)
        if my_uid not in self.cam_windows:
            self.open_camera_window(my_uid, "Моя камера")

    # ── Открытие круглого окна с чужой/своей камерой ────────────────────
    def open_camera_window(self, uid: int, nick: str = ""):
        """
        Открывает (или поднимает на передний план) круглое PiP-окно с
        видеопотоком пользователя `uid`. Вызывается при клике на аватарку
        / иконку камеры в дереве.
        """
        self.init_webcam_state()

        win = self.cam_windows.get(uid)
        if win is not None and win.isVisible():
            win.raise_()
            win.activateWindow()
            return win

        win = CircularVideoWindow(uid, nick)
        win.window_closed.connect(self.close_camera_window)
        self.cam_windows[uid] = win
        win.show()
        win.raise_()

        # НОВЫЙ путь: если это ЧУЖАЯ камера — подписываемся через WebRTC.
        # Поток грузится ТОЛЬКО сейчас (при открытии кружка), не раньше.
        # Для своей камеры (локальное превью) подписка не нужна.
        my_uid = getattr(self.audio, "my_uid", 0)
        if uid != my_uid:
            try:
                self.net.start_watching_camera(uid)
            except Exception as e:
                print(f"[Camera] start_watching_camera({uid}) error: {e}")
        return win

    # ── Закрытие окна (камера ОСТАЁТСЯ включённой) ──────────────────────
    def close_camera_window(self, uid: int):
        """
        Закрывает круглое окно. По ТЗ статус камеры в дереве НЕ сбрасывается —
        мы лишь убираем окно из реестра. Индикатор продолжает гореть, пока
        пользователь действительно не выключит камеру.

        НОВЫЙ путь: отписываемся от WebRTC-потока чужой камеры → трафик
        перестаёт грузиться, как только кружок закрыт.
        """
        my_uid = getattr(self.audio, "my_uid", 0)
        if uid != my_uid:
            try:
                self.net.stop_watching_camera(uid)
            except Exception as e:
                print(f"[Camera] stop_watching_camera({uid}) error: {e}")
        self._destroy_cam_window(uid)
        # НАМЕРЕННО не трогаем known_uids[uid]['is_cam'] —
        # индикатор камеры остаётся активным.

    def _destroy_cam_window(self, uid: int):
        win = self.cam_windows.pop(uid, None)
        if win is None:
            return
        try:
            win.window_closed.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            win.clear_frame()
        except (RuntimeError, AttributeError):
            pass
        try:
            win.close()
            win.deleteLater()
        except RuntimeError:
            pass

    # ── Роутинг входящих кадров камеры ──────────────────────────────────
    def on_camera_frame(self, uid: int, q_image: QImage):
        """
        Доставка кадра камеры (QImage) в круглое окно. Используется как для
        кадров, пришедших через существующий on_video_frame, так и из
        декодированных JPEG (см. on_network_camera_frame).
        """
        win = self.cam_windows.get(uid)
        if win is not None and win.isVisible():
            win.update_frame(q_image)

    def on_network_camera_frame(self, uid: int, b64_data: str):
        """
        Входящий кадр камеры от сервера: base64(JPEG) → QImage → круглое окно.
        Вызывается из process_message сетевого клиента (см. правки core.py).
        Декодируем только если окно для этого uid открыто — экономим CPU.
        """
        if not _CAPTURE_AVAILABLE or decode_jpeg_to_qimage is None:
            return
        win = self.cam_windows.get(uid)
        if win is None or not win.isVisible():
            return
        try:
            jpeg = base64.b64decode(b64_data)
        except Exception:
            return
        q_img = decode_jpeg_to_qimage(jpeg)
        if q_img is not None and not q_img.isNull():
            win.update_frame(q_img)

    # ── Когда пользователь выключил камеру (пришло с сервера) ────────────
    def set_user_camera_state(self, uid: int, is_on: bool):
        """
        Вызывай при получении флага камеры от сервера для конкретного uid.
        Гасит индикатор и закрывает окно, если камера выключилась.
        """
        if uid in self.known_uids:
            self.known_uids[uid]["is_cam"] = bool(is_on)
        if not is_on and uid in self.cam_windows:
            # Чужая камера выключилась → отписываемся и закрываем окно.
            my_uid = getattr(self.audio, "my_uid", 0)
            if uid != my_uid:
                try:
                    self.net.stop_watching_camera(uid)
                except Exception:
                    pass
            self._destroy_cam_window(uid)
        if hasattr(self, "refresh_ui"):
            self.refresh_ui()
