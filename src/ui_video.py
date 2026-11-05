import time
import math
import random
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QSizePolicy, QPushButton, QFrame, QSlider,
                             QGraphicsOpacityEffect, QApplication, QMessageBox)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtCore import (Qt, pyqtSlot, QSize, QRect, pyqtSignal,
                          QTimer, QEvent, QPoint, QPointF, QPropertyAnimation,
                          QEasingCurve)
from PyQt6.QtGui import (QImage, QPainter, QColor, QFont, QIcon,
                         QLinearGradient, QPen, QPainterPath, QCursor)

from config import resource_path, DRAW_FADE_SEC

_HIDE_TIMEOUT_MS = 3000

_OVERLAY_H = 60

_TITLE_H = 36

_STROKE_PALETTE = [
    '#FF6B6B', '#FFD93D', '#6BCB77', '#4D96FF',
    '#FF922B', '#CC5DE8', '#20C997', '#F06595',
    '#74C0FC', '#A9E34B', '#FFA94D', '#E599F7',
]

class VideoGlassTitleBar(QWidget):

    def __init__(self, parent_window: QWidget, title: str = ''):
        super().__init__(parent_window)
        self._win      = parent_window
        self._drag_pos = None
        self._title    = title

        self.setFixedHeight(_TITLE_H)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName('videoTitleBar')
        self.setStyleSheet("""
            QWidget#videoTitleBar {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(26, 28, 44, 235),
                    stop:1 rgba(15, 16, 28, 245));
                border-bottom: 1px solid rgba(255,255,255,0.08);
            }
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(4)

        self._ico = QLabel()
        self._ico.setFixedSize(18, 18)
        self._ico.setStyleSheet('background:transparent; border:none;')
        try:
            self._ico.setPixmap(
                QIcon(resource_path('assets/icon/logo.ico')).pixmap(18, 18)
            )
        except Exception:
            pass
        lay.addWidget(self._ico)

        self._lbl = QLabel(title)
        self._lbl.setStyleSheet(
            'color:#cdd6f4; font-size:12px; font-weight:600;'
            'background:transparent; border:none; padding-left:4px;'
        )
        lay.addWidget(self._lbl, stretch=1)

        _btn_ss = (
            'QPushButton{'
            '  background:transparent; border:none; border-radius:5px;'
            '  color:#8890a0; font-size:13px;'
            '  min-width:28px; max-width:28px;'
            '  min-height:26px; max-height:26px;'
            '}'
            'QPushButton:hover{background:rgba(255,255,255,0.10);color:#cdd6f4;}'
        )
        _close_ss = (
            _btn_ss +
            'QPushButton#closeTitleBtn:hover{background:#c0392b;color:white;}'
        )

        self._btn_min = QPushButton('─')
        self._btn_min.setStyleSheet(_btn_ss)
        self._btn_min.clicked.connect(self._win.showMinimized)

        self._btn_max = QPushButton('□')
        self._btn_max.setStyleSheet(_btn_ss)
        self._btn_max.clicked.connect(self._toggle_max)

        self._btn_close = QPushButton('✕')
        self._btn_close.setObjectName('closeTitleBtn')
        self._btn_close.setStyleSheet(_close_ss)
        self._btn_close.clicked.connect(self._win.close)

        for b in (self._btn_min, self._btn_max, self._btn_close):
            lay.addWidget(b)

    def set_title(self, title: str):
        self._title = title
        self._lbl.setText(title)

    def update_max_icon(self):
        is_max = bool(self._win.windowState() & Qt.WindowState.WindowMaximized)
        self._btn_max.setText('❐' if is_max else '□')

    def _toggle_max(self):
        if self._win.windowState() & Qt.WindowState.WindowMaximized:
            self._win.showNormal()
        else:
            self._win.showMaximized()
        self.update_max_icon()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            )
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            if not (self._win.windowState() & Qt.WindowState.WindowMaximized):
                self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()
        super().mouseDoubleClickEvent(e)

class DrawCanvas(QWidget):

    stroke_ready = pyqtSignal(str, list, int)
    _MAX_STROKES = 64

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self._is_drawing_mode: bool = False
        self._current_stroke: list  = []
        self._strokes: list         = []
        self._my_color: str         = random.choice(_STROKE_PALETTE)
        self._my_width: int         = 3

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(50)
        self._fade_timer.timeout.connect(self._on_fade_tick)

        self._frame_rect: QRect = QRect()

    def set_drawing_mode(self, enabled: bool):
        self._is_drawing_mode = enabled
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not enabled)
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
            if not self._fade_timer.isActive():
                self._fade_timer.start()
        else:
            self.unsetCursor()
            self._current_stroke.clear()

    def set_color(self, color: str):
        self._my_color = color

    def set_width(self, w: int):
        self._my_width = max(1, min(8, w))

    def update_frame_rect(self, rect: QRect):
        self._frame_rect = rect

    def add_remote_stroke(self, color: str, norm_points: list, width: int):

        if not norm_points:
            return
        self._strokes.append({
            'norm': norm_points,
            'color': color,
            'width': width,
            'born': time.monotonic(),
            'local': False,
        })
        if len(self._strokes) > self._MAX_STROKES:
            self._strokes.pop(0)
        if not self._fade_timer.isActive():
            self._fade_timer.start()
        self.update()

    def clear_strokes(self):
        self._strokes.clear()
        self._current_stroke.clear()
        self.update()

    def mousePressEvent(self, e):
        if self._is_drawing_mode and e.button() == Qt.MouseButton.LeftButton:
            self._current_stroke = [e.position().toPoint()]
            if not self._fade_timer.isActive():
                self._fade_timer.start()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._is_drawing_mode and e.buttons() & Qt.MouseButton.LeftButton:
            p = e.position().toPoint()
            if self._current_stroke:
                last = self._current_stroke[-1]
                dx, dy = p.x() - last.x(), p.y() - last.y()
                if dx*dx + dy*dy >= 9:
                    self._current_stroke.append(p)
                    self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._is_drawing_mode and e.button() == Qt.MouseButton.LeftButton:
            if len(self._current_stroke) >= 2:
                norm = self._normalize_stroke(self._current_stroke)
                if norm:
                    self._strokes.append({
                        'norm':  norm,
                        'color': self._my_color,
                        'width': self._my_width,
                        'born':  time.monotonic(),
                        'local': True,
                    })
                    if len(self._strokes) > self._MAX_STROKES:
                        self._strokes.pop(0)
                    self.stroke_ready.emit(self._my_color, norm, self._my_width)
            self._current_stroke.clear()
            self.update()
        super().mouseReleaseEvent(e)

    def paintEvent(self, event):
        if not self._strokes and not self._current_stroke:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        now = time.monotonic()
        fade_start = DRAW_FADE_SEC - 1.0    # последняя 1 секунда — fade out

        for stroke in self._strokes:
            age     = now - stroke['born']
            if age >= DRAW_FADE_SEC:
                continue
            if age < fade_start:
                alpha = 255
            else:
                alpha = int(255 * (1.0 - (age - fade_start) / 1.0))
                alpha = max(0, min(255, alpha))

            pts = self._denormalize(stroke['norm'])
            if len(pts) < 2:
                continue
            color = QColor(stroke['color'])
            color.setAlpha(alpha)
            pen = QPen(color, stroke['width'],
                       Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(float(pts[0].x()), float(pts[0].y()))
            for pt in pts[1:]:
                path.lineTo(float(pt.x()), float(pt.y()))
            p.drawPath(path)

        if self._current_stroke and len(self._current_stroke) >= 2:
            color = QColor(self._my_color)
            color.setAlpha(255)
            pen = QPen(color, self._my_width,
                       Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(float(self._current_stroke[0].x()),
                        float(self._current_stroke[0].y()))
            for pt in self._current_stroke[1:]:
                path.lineTo(float(pt.x()), float(pt.y()))
            p.drawPath(path)

        p.end()

    def _on_fade_tick(self):
        now = time.monotonic()
        self._strokes = [s for s in self._strokes if now - s['born'] < DRAW_FADE_SEC]
        if not self._strokes and not self._current_stroke:
            self._fade_timer.stop()
        self.update()

    def _normalize_stroke(self, pixels: list) -> list:
        r = self._frame_rect
        if r.isEmpty():
            w, h = max(self.width(), 1), max(self.height(), 1)
            return [[p.x() / w, p.y() / h] for p in pixels]
        fw, fh = max(r.width(), 1), max(r.height(), 1)
        result = []
        for p in pixels:
            nx = (p.x() - r.x()) / fw
            ny = (p.y() - r.y()) / fh
            result.append([max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))])
        return result

    def _denormalize(self, norm_points: list) -> list:
        r = self._frame_rect
        if r.isEmpty():
            w, h = self.width(), self.height()
            return [QPoint(int(p[0] * w), int(p[1] * h)) for p in norm_points]
        return [
            QPoint(int(r.x() + p[0] * r.width()),
                   int(r.y() + p[1] * r.height()))
            for p in norm_points
        ]

class StreamerAnnotationOverlay(QWidget):

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.geometry())

        self._strokes: list = []
        self._MAX     = 64

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(50)
        self._fade_timer.timeout.connect(self._on_fade_tick)
        self._capture_rect: QRect = QRect()

    def set_capture_region(self, x: int, y: int, w: int, h: int):
        self._capture_rect = QRect(x, y, w, h)

    def add_stroke(self, nick: str, color: str, norm_points: list, width: int):
        if not norm_points:
            return
        self._strokes.append({
            'norm':  norm_points,
            'color': color,
            'width': width,
            'born':  time.monotonic(),
            'nick':  nick,
        })
        if len(self._strokes) > self._MAX:
            self._strokes.pop(0)
        if not self._fade_timer.isActive():
            self._fade_timer.start()
        self.update()

    def clear(self):
        self._strokes.clear()
        self.update()

    def _on_fade_tick(self):
        now = time.monotonic()
        self._strokes = [s for s in self._strokes if now - s['born'] < DRAW_FADE_SEC]
        if not self._strokes:
            self._fade_timer.stop()
        self.update()

    def paintEvent(self, event):
        if not self._strokes:
            return

        r = self._capture_rect
        if r.isEmpty():
            r = self.rect()

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        now         = time.monotonic()
        fade_start  = DRAW_FADE_SEC - 1.0
        font        = QFont('Segoe UI', 9, QFont.Weight.Bold)
        p.setFont(font)

        for stroke in self._strokes:
            age = now - stroke['born']
            if age >= DRAW_FADE_SEC:
                continue
            alpha = 255 if age < fade_start else max(
                0, int(255 * (1.0 - (age - fade_start)))
            )

            pts = [
                QPoint(int(r.x() + pt[0] * r.width()),
                       int(r.y() + pt[1] * r.height()))
                for pt in stroke['norm']
            ]
            if len(pts) < 2:
                continue

            color = QColor(stroke['color'])
            color.setAlpha(alpha)
            pen = QPen(color, stroke['width'],
                       Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path = QPainterPath()
            path.moveTo(float(pts[0].x()), float(pts[0].y()))
            for pt in pts[1:]:
                path.lineTo(float(pt.x()), float(pt.y()))
            p.drawPath(path)

            if stroke.get('nick'):
                lbl_color = QColor(stroke['color'])
                lbl_color.setAlpha(min(200, alpha))
                p.setPen(lbl_color)
                p.drawText(pts[0].x() + 6, pts[0].y() - 6, stroke['nick'])

        p.end()

class VideoSurface(QOpenGLWidget):
    fullscreen_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_image: QImage | None = None
        self._placeholder_text = "Ожидание видео..."
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)
        self._placeholder_font = QFont("Segoe UI", 20)

    def set_frame(self, q_img: QImage):
        self._current_image = q_img
        self.update()

    def initializeGL(self):
        pass

    def resizeGL(self, w: int, h: int):

        pass

    def paintGL(self):
        painter = QPainter(self)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(0, 0, 0))
        if self._current_image and not self._current_image.isNull():
            self._draw_frame(painter, w, h)
        else:
            self._draw_placeholder(painter, w, h)
        painter.end()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreen_requested.emit()
        super().mouseDoubleClickEvent(event)

    def _draw_frame(self, painter: QPainter, w: int, h: int):

        img = self._current_image
        img_w, img_h = img.width(), img.height()
        if img_w <= 0 or img_h <= 0:
            return
        scale  = min(w / img_w, h / img_h)
        dest_w = (int(img_w * scale) // 2) * 2   # чётный
        dest_h = (int(img_h * scale) // 2) * 2   # чётный
        dest_x = (w - dest_w) // 2
        dest_y = (h - dest_h) // 2
        painter.drawImage(QRect(dest_x, dest_y, dest_w, dest_h), img)

    def _draw_placeholder(self, painter: QPainter, w: int, h: int):
        painter.setFont(self._placeholder_font)
        painter.setPen(QColor(160, 160, 160))
        painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, self._placeholder_text)

class StreamVolumePopup(QFrame):

    volume_changed = pyqtSignal(float)

    _FILL_COLOR   = QColor(88, 101, 242)
    _TRACK_COLOR  = QColor(60, 60, 80)
    _HANDLE_COLOR = QColor(255, 255, 255)
    _HANDLE_R     = 7

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(36, 140)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)

        self.setStyleSheet("""
            StreamVolumePopup {
                background-color: rgba(18, 18, 32, 230);
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,40);
            }
        """)

        self._value   = 1.0   # 0.0–2.0
        self._dragging = False

        self._pad_top    = 14
        self._pad_bottom = 14

    def set_value(self, v: float):
        self._value = max(0.0, min(2.0, v))
        self.update()

    def get_value(self) -> float:
        return self._value

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        cx = w // 2
        track_x = cx - 3
        track_w = 6
        track_top    = self._pad_top
        track_bottom = self.height() - self._pad_bottom
        track_h      = track_bottom - track_top

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._TRACK_COLOR)
        p.drawRoundedRect(track_x, track_top, track_w, track_h, 3, 3)

        ratio      = self._value / 2.0
        fill_h     = int(track_h * ratio)
        fill_y     = track_bottom - fill_h

        grad = QLinearGradient(0, fill_y, 0, track_bottom)
        grad.setColorAt(0.0, QColor(120, 135, 255))
        grad.setColorAt(1.0, self._FILL_COLOR)
        p.setBrush(grad)
        p.drawRoundedRect(track_x, fill_y, track_w, fill_h, 3, 3)

        handle_y = fill_y - self._HANDLE_R
        p.setBrush(self._HANDLE_COLOR)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - self._HANDLE_R, handle_y, self._HANDLE_R * 2, self._HANDLE_R * 2)

        p.setPen(QColor(180, 180, 200))
        p.setFont(QFont("Segoe UI", 8))
        pct = int(self._value * 100)
        p.drawText(0, 0, w, self._pad_top, Qt.AlignmentFlag.AlignCenter, f"{pct}%")
        p.end()

    def _y_to_value(self, y: int) -> float:
        track_top    = self._pad_top
        track_bottom = self.height() - self._pad_bottom
        track_h      = track_bottom - track_top
        ratio = 1.0 - (y - track_top) / max(track_h, 1)
        return max(0.0, min(2.0, ratio * 2.0))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._value = self._y_to_value(event.pos().y())
            self.update()
            self.volume_changed.emit(self._value)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._value = self._y_to_value(event.pos().y())
            self.update()
            self.volume_changed.emit(self._value)

    def mouseReleaseEvent(self, event):
        self._dragging = False

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        step  = 0.05 if delta > 0 else -0.05
        self._value = max(0.0, min(2.0, self._value + step))
        self.update()
        self.volume_changed.emit(self._value)

class VideoOverlay(QFrame):

    mute_clicked       = pyqtSignal()
    deafen_clicked     = pyqtSignal()
    stop_watch_clicked = pyqtSignal()
    fullscreen_clicked = pyqtSignal()
    stream_volume_changed = pyqtSignal(float)   # 0.0–2.0
    draw_toggled = pyqtSignal(bool)   # True = рисование включено

    control_requested = pyqtSignal()
    control_released  = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)

        self.setStyleSheet("""
            VideoOverlay {
                background-color: rgba(15, 15, 30, 210);
                border-radius: 16px;
            }
        """)
        self.setFixedHeight(_OVERLAY_H)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(10)

        self.btn_mute = self._make_btn("assets/icon/mic_on.svg", "Заглушить микрофон")
        self.btn_mute.setCheckable(True)
        self.btn_mute.clicked.connect(self._on_mute_clicked)

        self.btn_deafen = self._make_btn("assets/icon/volume_on.svg", "Заглушить динамики")
        self.btn_deafen.setCheckable(True)
        self.btn_deafen.clicked.connect(self._on_deafen_clicked)

        self.btn_stop = self._make_btn("assets/icon/monitor_off.svg", "Прекратить просмотр")
        self.btn_stop.clicked.connect(self.stop_watch_clicked)

        self.btn_vol_stream = self._make_btn("assets/icon/volume_stream.svg", "Громкость стрима")
        self._vol_popup = StreamVolumePopup(parent.parent() if parent else self)
        self._vol_popup.setVisible(False)
        self._vol_popup.volume_changed.connect(self.stream_volume_changed)

        self._vol_hide_timer = QTimer(self)
        self._vol_hide_timer.setSingleShot(True)
        self._vol_hide_timer.setInterval(400)
        self._vol_hide_timer.timeout.connect(self._hide_vol_popup)

        self.btn_vol_stream.clicked.connect(self._toggle_vol_popup)
        self.btn_vol_stream.installEventFilter(self)
        self._vol_popup.installEventFilter(self)

        self.btn_draw = self._make_btn("assets/icon/draw.svg", "Рисовать на стриме (5 сек)")
        self.btn_draw.setCheckable(True)
        self.btn_draw.clicked.connect(self._on_draw_clicked)

        self.btn_control = self._make_btn("assets/icon/control.svg", "Взять управление")
        self.btn_control.setCheckable(True)
        self.btn_control.clicked.connect(self._on_control_clicked)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("QFrame { color: rgba(255,255,255,50); }")
        sep.setFixedWidth(2)
        sep.setFixedHeight(32)

        self.btn_fs = self._make_btn(None, "Полный экран / Оконный режим")
        self.btn_fs.setText("⛶")
        self.btn_fs.setFont(QFont("Segoe UI", 16))
        self.btn_fs.clicked.connect(self.fullscreen_clicked)

        layout.addWidget(self.btn_mute)
        layout.addWidget(self.btn_deafen)
        layout.addWidget(self.btn_stop)
        layout.addWidget(self.btn_vol_stream)
        layout.addWidget(self.btn_draw)
        layout.addWidget(self.btn_control)
        layout.addWidget(sep, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.btn_fs)

    def _make_btn(self, icon_path: str | None, tooltip: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(44, 44)
        btn.setToolTip(tooltip)
        btn.setMouseTracking(True)
        btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(60, 63, 65, 190);
                border: 1px solid rgba(255, 255, 255, 35);
                border-radius: 8px;
                padding: 4px;
                color: #e0e0e0;
            }
            QPushButton:hover {
                background-color: rgba(95, 100, 108, 230);
            }
            QPushButton:checked {
                background-color: rgba(231, 76, 60, 210);
                border: 1px solid rgba(231, 76, 60, 255);
            }
        """)
        if icon_path:
            btn.setIcon(QIcon(resource_path(icon_path)))
            btn.setIconSize(QSize(26, 26))
        return btn

    def _toggle_vol_popup(self):
        if self._vol_popup.isVisible():
            self._hide_vol_popup()
        else:
            self._show_vol_popup()

    def _show_vol_popup(self):
        self._vol_hide_timer.stop()
        btn_pos = self.btn_vol_stream.mapTo(self._vol_popup.parent(), QPoint(0, 0))
        popup_x = btn_pos.x() + (self.btn_vol_stream.width() - self._vol_popup.width()) // 2
        popup_y = btn_pos.y() - self._vol_popup.height() - 8
        self._vol_popup.move(popup_x, popup_y)
        self._vol_popup.raise_()
        self._vol_popup.setVisible(True)

    def _hide_vol_popup(self):
        self._vol_popup.setVisible(False)

    def eventFilter(self, obj, event):
        t = event.type()
        if obj in (self.btn_vol_stream, self._vol_popup):
            if t == QEvent.Type.Enter:
                self._vol_hide_timer.stop()
                if obj == self.btn_vol_stream:
                    self._show_vol_popup()
            elif t == QEvent.Type.Leave:
                self._vol_hide_timer.start()
        return False

    def _on_draw_clicked(self):

        is_drawing = self.btn_draw.isChecked()
        if is_drawing:
            self.btn_draw.setStyleSheet(
                self.btn_draw.styleSheet() +
                "QPushButton:checked {"
                "  background-color: rgba(88, 101, 242, 210);"
                "  border: 1px solid rgba(88, 101, 242, 255);"
                "}"
            )
        else:
            self.btn_draw.setStyleSheet("")
            self.btn_draw.setStyleSheet(self._make_btn_ss())
        self.draw_toggled.emit(is_drawing)

    def _make_btn_ss(self) -> str:
        return (
            "QPushButton {"
            "  background-color: rgba(60, 63, 65, 190);"
            "  border: 1px solid rgba(255, 255, 255, 35);"
            "  border-radius: 8px; padding: 4px; color: #e0e0e0;"
            "}"
            "QPushButton:hover { background-color: rgba(95, 100, 108, 230); }"
            "QPushButton:checked {"
            "  background-color: rgba(88, 101, 242, 210);"
            "  border: 1px solid rgba(88, 101, 242, 255);"
            "}"
        )

    def _on_mute_clicked(self):
        is_muted = self.btn_mute.isChecked()
        icon = "assets/icon/mic_off.svg" if is_muted else "assets/icon/mic_on.svg"
        self.btn_mute.setIcon(QIcon(resource_path(icon)))
        self.mute_clicked.emit()

    def _on_control_clicked(self):
        if self.btn_control.isChecked():
            # Подсветить синим (как кнопка Draw при активации)
            self.btn_control.setStyleSheet(
                self._make_btn_ss() +
                "QPushButton:checked {"
                "  background-color: rgba(88, 101, 242, 210);"
                "  border: 1px solid rgba(88, 101, 242, 255);"
                "}"
            )
            self.control_requested.emit()
        else:
            self.btn_control.setStyleSheet(self._make_btn_ss())
            self.control_released.emit()

    def set_control_active(self, active: bool):
        self.btn_control.blockSignals(True)
        self.btn_control.setChecked(active)
        if active:
            self.btn_control.setStyleSheet(
                self._make_btn_ss() +
                "QPushButton:checked {"
                "  background-color: rgba(88, 101, 242, 210);"
                "  border: 1px solid rgba(88, 101, 242, 255);"
                "}"
            )
        else:
            self.btn_control.setStyleSheet(self._make_btn_ss())
        self.btn_control.blockSignals(False)

    def _on_deafen_clicked(self):
        is_deafened = self.btn_deafen.isChecked()
        icon = "assets/icon/volume_off.svg" if is_deafened else "assets/icon/volume_on.svg"
        self.btn_deafen.setIcon(QIcon(resource_path(icon)))
        self.deafen_clicked.emit()

    def sync_mute_state(self, is_muted: bool):
        self.btn_mute.blockSignals(True)
        self.btn_mute.setChecked(is_muted)
        icon = "assets/icon/mic_off.svg" if is_muted else "assets/icon/mic_on.svg"
        self.btn_mute.setIcon(QIcon(resource_path(icon)))
        self.btn_mute.blockSignals(False)

    def sync_deafen_state(self, is_deafened: bool):
        self.btn_deafen.blockSignals(True)
        self.btn_deafen.setChecked(is_deafened)
        icon = "assets/icon/volume_off.svg" if is_deafened else "assets/icon/volume_on.svg"
        self.btn_deafen.setIcon(QIcon(resource_path(icon)))
        self.btn_deafen.blockSignals(False)

    def set_stream_volume_value(self, vol: float):

        self._vol_popup.set_value(vol)

    def set_fullscreen_icon(self, is_fullscreen: bool):
        self.btn_fs.setText("❐" if is_fullscreen else "⛶")

class _ViewerControlOverlay(QWidget):
    """
    Плавающая плашка «Управление активно» на стороне ЗРИТЕЛЯ.

    Это ОТДЕЛЬНОЕ top-level окно (Qt.Tool + FramelessWindowHint +
    WindowStaysOnTopHint), поэтому:
      • видно поверх всех окон, в т.ч. поверх фуллскрина трансляции;
      • её кнопку НЕ перехватывает eventFilter/grabKeyboard окна стрима
        (это другое окно — клик по «Отменить» доходит честно);
      • плашку можно перетаскивать мышью за тело.

    Сигнал stop_clicked → зритель просит остановить управление.
    """
    stop_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Не воровать фокус у окна стрима (иначе перехват клавиатуры собьётся)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._drag_offset = None

        outer = QFrame(self)
        outer.setObjectName("vcOuter")
        outer.setStyleSheet("""
            QFrame#vcOuter {
                background-color: rgba(231, 76, 60, 235);
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,70);
            }
        """)

        lay = QHBoxLayout(outer)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)

        self._grip = QLabel("⠿")
        self._grip.setStyleSheet(
            "background:transparent; border:none; color:#ffd; font-size:15px;")
        self._grip.setToolTip("Перетащить")
        lay.addWidget(self._grip)

        ico = QLabel("🖱️")
        ico.setStyleSheet("background:transparent; border:none; font-size:16px;")
        lay.addWidget(ico)

        col = QVBoxLayout()
        col.setSpacing(0)
        lbl = QLabel("Управление активно")
        lbl.setStyleSheet(
            "background:transparent; border:none; color:#fff;"
            "font-size:12px; font-weight:700;")
        hint = QLabel("3×ESC или кнопка")
        hint.setStyleSheet(
            "background:transparent; border:none; color:rgba(255,255,255,200);"
            "font-size:10px;")
        col.addWidget(lbl)
        col.addWidget(hint)
        lay.addLayout(col)

        btn = QPushButton("Отменить")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,45);
                border: 1px solid rgba(255,255,255,90);
                border-radius: 6px;
                color: #fff;
                font-size: 12px;
                font-weight: 600;
                padding: 5px 12px;
            }
            QPushButton:hover  { background: rgba(255,255,255,80); }
            QPushButton:pressed{ background: rgba(255,255,255,110); }
        """)
        btn.clicked.connect(self.stop_clicked)
        lay.addWidget(btn)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(outer)
        self.adjustSize()

    # ── Перетаскивание за тело плашки ────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_offset = None
        e.accept()

    def show_top_right_of(self, ref_widget):
        """Показать у правого-верхнего угла опорного виджета (окна стрима)."""
        self.adjustSize()
        try:
            if ref_widget is not None and ref_widget.isVisible():
                g = ref_widget.frameGeometry()
                x = g.right() - self.width() - 24
                y = g.top() + 24
            else:
                from PyQt6.QtGui import QGuiApplication
                scr = QGuiApplication.primaryScreen().availableGeometry()
                x = scr.right() - self.width() - 40
                y = scr.top() + 40
            self.move(max(0, x), max(0, y))
        except Exception:
            pass
        self.show()
        self.raise_()


class _ControlBanner(QFrame):

    stop_clicked = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setStyleSheet("""
            _ControlBanner {
                background-color: rgba(231, 76, 60, 220);
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,60);
            }
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(8)

        ico = QLabel("🖱️")
        ico.setStyleSheet("background:transparent; border:none; font-size:16px;")
        lay.addWidget(ico)

        lbl = QLabel("Управление активно")
        lbl.setStyleSheet(
            "background:transparent; border:none;"
            "color:#fff; font-size:12px; font-weight:600;"
        )
        lay.addWidget(lbl)

        btn = QPushButton("Отменить управление")
        btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,40);
                border: 1px solid rgba(255,255,255,80);
                border-radius: 6px;
                color: #fff;
                font-size: 12px;
                padding: 4px 10px;
            }
            QPushButton:hover { background: rgba(255,255,255,70); }
        """)
        btn.clicked.connect(self.stop_clicked)
        lay.addWidget(btn)

        self.adjustSize()

    def showEvent(self, event):
        super().showEvent(event)
        self.adjustSize()
        self.move(16, 16)
        self.raise_()

class VideoWindow(QWidget):

    window_closed = pyqtSignal(int)

    overlay_mute_toggled   = pyqtSignal()
    overlay_deafen_toggled = pyqtSignal()
    overlay_stop_watch     = pyqtSignal()
    overlay_stream_volume_changed = pyqtSignal(float)   # 0.0–2.0

    draw_stroke_ready = pyqtSignal(str, list, int)

    control_requested        = pyqtSignal()   # зритель нажал «Взять управление»
    control_released         = pyqtSignal()   # зритель отпустил / баннер у стримера
    remote_control_event_ready = pyqtSignal(dict)  # событие мыши/клавиш → на сервер

    def __init__(self, nick: str):
        super().__init__()
        self.uid: int | None = None
        self._nick = nick
        self._frame_count = 0
        self._fps_last_time = time.monotonic()   # для обновления Res/Frames раз в сек
        self._is_fullscreen = False
        self._closing = False        # флаг: окно в процессе закрытия
        self._net = None             # NetworkClient — устанавливается через set_net()
        self._sb_panel = None        # SoundboardPanel поверх стрима (toggle)
        self._control_mode = False   # True = этот зритель сейчас управляет стримером
        self._control_banner = None  # _ControlBanner у стримера
        self._viewer_ctrl_overlay = None  # плавающая плашка управления у зрителя

        self._draw_canvas: DrawCanvas | None = None

        self._setup_ui(nick)
        self._setup_hide_timer()

    def set_net(self, net):

        self._net = net

    def _setup_ui(self, nick: str):

        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.resize(1280, 720 + _TITLE_H + 28)
        self.setMinimumSize(640, 360 + _TITLE_H + 28)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setStyleSheet("""
            VideoWindow {
                background-color: #0e1018;
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 0px;
            }
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._title_bar = VideoGlassTitleBar(self, f"Стрим: {nick}")
        root.addWidget(self._title_bar)
        self._video_container = QWidget(self)
        self._video_container.setMouseTracking(True)
        self._video_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._video_container, stretch=1)
        self.surface = VideoSurface(self._video_container)
        self.surface.fullscreen_requested.connect(self.toggle_fullscreen)
        self._draw_canvas = DrawCanvas(self._video_container)
        self._draw_canvas.stroke_ready.connect(self.draw_stroke_ready)  # → MainWindow
        self.overlay = VideoOverlay(self._video_container)
        self.overlay.mute_clicked.connect(self.overlay_mute_toggled)
        self.overlay.deafen_clicked.connect(self.overlay_deafen_toggled)
        self.overlay.stop_watch_clicked.connect(self._on_overlay_stop)
        self.overlay.fullscreen_clicked.connect(self.toggle_fullscreen)
        self.overlay.stream_volume_changed.connect(self.overlay_stream_volume_changed)
        self.overlay.draw_toggled.connect(self._on_draw_toggled)
        self.overlay.control_requested.connect(self.control_requested)
        self.overlay.control_released.connect(self.control_released)
        self.overlay.hide()
        self._bar = QWidget(self)
        self._bar.setFixedHeight(28)
        self._bar.setStyleSheet("background: #10111e; border-top: 1px solid rgba(255,255,255,0.06);")
        bar_layout = QHBoxLayout(self._bar)
        bar_layout.setContentsMargins(8, 0, 8, 0)
        bar_layout.setSpacing(0)

        lbl_style = "color: #8888aa; padding: 0 10px; font-size: 11px;"

        self._lbl_fps      = QLabel("Net: —")
        self._lbl_res      = QLabel("Res: —")
        self._lbl_frames   = QLabel("Frames: 0")
        self._lbl_renderer = QLabel("🟢 OpenGL GPU")
        self._lbl_rtc      = QLabel("WebRTC: —")

        for lbl in (self._lbl_fps, self._lbl_res, self._lbl_frames,
                    self._lbl_renderer, self._lbl_rtc):
            lbl.setStyleSheet(lbl_style)
            bar_layout.addWidget(lbl)

        bar_layout.addStretch()
        root.addWidget(self._bar)

    def _setup_hide_timer(self):
        """Таймер авто-скрытия оверлея / курсора."""
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hide_timeout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_surface()
        self._reposition_draw_canvas()
        self._reposition_overlay()

        if hasattr(self, '_title_bar'):
            self._title_bar.update_max_icon()

    def _reposition_surface(self):
        c = self._video_container
        self.surface.setGeometry(0, 0, c.width(), c.height())

    def _reposition_draw_canvas(self):
        """DrawCanvas покрывает весь video_container — совпадает с surface."""
        if self._draw_canvas is None:
            return
        c = self._video_container
        self._draw_canvas.setGeometry(0, 0, c.width(), c.height())
        self._draw_canvas.raise_()      # поверх surface, но под overlay
        self._update_draw_canvas_frame_rect()

    def _update_draw_canvas_frame_rect(self):

        if self._draw_canvas is None:
            return
        img = self.surface._current_image
        c   = self._video_container
        cw, ch = c.width(), c.height()
        if img and not img.isNull() and img.width() > 0 and img.height() > 0:
            scale  = min(cw / img.width(), ch / img.height())
            dw = (int(img.width()  * scale) // 2) * 2
            dh = (int(img.height() * scale) // 2) * 2
            dx = (cw - dw) // 2
            dy = (ch - dh) // 2
            self._draw_canvas.update_frame_rect(QRect(dx, dy, dw, dh))
        else:
            self._draw_canvas.update_frame_rect(QRect(0, 0, cw, ch))

    def _reposition_overlay(self):

        c = self._video_container
        if c.width() <= 0 or c.height() <= 0:
            return
        ow = self.overlay.sizeHint().width()
        oh = _OVERLAY_H
        ox = (c.width() - ow) // 2
        oy = c.height() - oh - 24
        self.overlay.setFixedWidth(ow)
        self.overlay.setGeometry(ox, oy, ow, oh)
        self.overlay.raise_()   # оверлей поверх DrawCanvas

    def _show_overlay(self):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if not self.overlay.isVisible():
            self._reposition_overlay()
            self.overlay.show()
            self.overlay.raise_()
        self._hide_timer.start(_HIDE_TIMEOUT_MS)

    def _on_hide_timeout(self):
        """Скрыть оверлей по таймеру. В fullscreen — ещё и курсор."""
        self.overlay.hide()
        if self._is_fullscreen:
            self.setCursor(Qt.CursorShape.BlankCursor)

    def _on_draw_toggled(self, enabled: bool):
        """
        Переключает режим рисования на DrawCanvas.
        При включении overlay НЕ скрывается по таймеру — пользователь должен
        видеть кнопку Draw чтобы понять что режим активен.
        """
        if self._draw_canvas is None:
            return
        self._draw_canvas.set_drawing_mode(enabled)
        if enabled:
            self._hide_timer.stop()
            self.overlay.show()
            self.overlay.raise_()
        else:
            self._hide_timer.start(_HIDE_TIMEOUT_MS)

    def add_remote_stroke(self, color: str, norm_points: list, width: int):

        if self._draw_canvas is None:
            return
        self._draw_canvas.add_remote_stroke(color, norm_points, width)
        self._update_draw_canvas_frame_rect()

    def on_control_granted(self):
        self.overlay.set_control_active(True)
        self._control_mode = True
        self._rc_begin_capture()
        self._show_viewer_control_overlay()

    def on_control_denied(self):
        self.overlay.set_control_active(False)
        self._control_mode = False
        self._rc_end_capture()
        self._hide_viewer_control_overlay()

    def on_control_stopped(self):
        self.overlay.set_control_active(False)
        self._control_mode = False
        self._rc_end_capture()
        self._hide_viewer_control_overlay()

    def _show_viewer_control_overlay(self):
        """
        Плавающая плашка «Управление активно» у зрителя — отдельное top-level
        окно поверх всех, в т.ч. поверх фуллскрина. Двигается мышью, кнопка
        «Отменить» останавливает управление (её клик не съедается grab'ом).
        """
        if getattr(self, '_viewer_ctrl_overlay', None) is None:
            self._viewer_ctrl_overlay = _ViewerControlOverlay(None)
            self._viewer_ctrl_overlay.stop_clicked.connect(self._on_viewer_overlay_stop)
        self._viewer_ctrl_overlay.show_top_right_of(self)

    def _hide_viewer_control_overlay(self):
        ov = getattr(self, '_viewer_ctrl_overlay', None)
        if ov is not None:
            ov.hide()

    def _on_viewer_overlay_stop(self):
        """Зритель нажал «Отменить» на плавающей плашке."""
        self._hide_viewer_control_overlay()
        # Снимаем захват сразу, чтобы вернуть себе мышь/клавиатуру,
        # и просим остановить управление (stop уйдёт стримеру через сервер).
        self._control_mode = False
        self._rc_end_capture()
        self.overlay.set_control_active(False)
        self.control_released.emit()

    def _rc_begin_capture(self):
        """
        Включает захват ввода для удалённого управления.
        Клавиатурные события доставляются ТОЛЬКО виджету в фокусе, поэтому
        без grabKeyboard()+фокуса клавиатура не перехватывается вовсе.
        Фильтр вешаем и на само окно (self) — туда теперь приходят и клавиши.
        """
        try:
            self.installEventFilter(self)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.activateWindow()
            self.raise_()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            self.grabKeyboard()
            print(f"[RC-VIEWER] begin_capture: grabKeyboard OK, "
                  f"focusWidget={self.focusWidget()!r}, isActive={self.isActiveWindow()}")
        except Exception as e:
            print(f"[RemoteControl] begin_capture error: {e}")

    def _rc_end_capture(self):
        """Снимает захват клавиатуры."""
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        try:
            self.removeEventFilter(self)
        except Exception:
            pass

    def show_control_request_dialog(self, viewer_nick: str) -> bool:

        mb = QMessageBox(self)
        mb.setWindowTitle("Запрос управления")
        mb.setWindowIcon(self.windowIcon())
        mb.setText(
            f'Пользователь <b>{viewer_nick}</b> хочет взять управление '
            f'мышью и клавиатурой.<br><br>Разрешить?'
        )
        mb.setIcon(QMessageBox.Icon.Question)
        btn_yes = mb.addButton("Да",  QMessageBox.ButtonRole.AcceptRole)
        btn_no  = mb.addButton("Нет", QMessageBox.ButtonRole.RejectRole)
        mb.setDefaultButton(btn_no)
        mb.exec()
        return mb.clickedButton() == btn_yes

    def show_control_active_banner(self):

        if self._control_banner is None:
            self._control_banner = _ControlBanner(self._video_container)
            self._control_banner.stop_clicked.connect(self._on_control_banner_stop)
        self._control_banner.show()
        self._control_banner.raise_()

    def hide_control_active_banner(self):
        if self._control_banner is not None:
            self._control_banner.hide()

    def _on_control_banner_stop(self):
        self.hide_control_active_banner()
        self.control_released.emit()

    def _video_frame_rect(self):
        """
        Прямоугольник (dx, dy, dw, dh) — область внутри video_container, где
        реально нарисован кадр стрима (с letterbox/pillarbox по соотношению
        сторон). Совпадает с расчётом _draw_frame/_update_draw_canvas_frame_rect.
        Возвращает (dx, dy, dw, dh) в координатах video_container.
        """
        c = self._video_container
        cw, ch = max(c.width(), 1), max(c.height(), 1)
        img = self.surface._current_image
        if img and not img.isNull() and img.width() > 0 and img.height() > 0:
            scale = min(cw / img.width(), ch / img.height())
            dw = max(1, (int(img.width()  * scale) // 2) * 2)
            dh = max(1, (int(img.height() * scale) // 2) * 2)
            dx = (cw - dw) // 2
            dy = (ch - dh) // 2
            return dx, dy, dw, dh
        # Кадра ещё нет — считаем по всему контейнеру
        return 0, 0, cw, ch

    def _norm_pos(self, x: float, y: float):
        """
        Нормализует точку (в координатах video_container) к 0..1 ОТНОСИТЕЛЬНО
        реальной области кадра, а не всего контейнера. Это убирает рассинхрон
        при разных разрешениях/соотношениях сторон зрителя и стримера
        (учитываются чёрные полосы letterbox).
        """
        dx, dy, dw, dh = self._video_frame_rect()
        nx = (x - dx) / dw
        ny = (y - dy) / dh
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    def _norm_from_global(self, global_pt):
        """
        Переводит глобальную точку события в нормализованные координаты
        внутри video_container. Работает независимо от того, какой именно
        дочерний виджет (surface/overlay/canvas) принял событие.
        """
        c = self._video_container
        local = c.mapFromGlobal(global_pt.toPoint() if hasattr(global_pt, 'toPoint')
                                else global_pt)
        return self._norm_pos(local.x(), local.y())

    @staticmethod
    def _enum_int(v) -> int:
        """
        Безопасно приводит Qt-енам (MouseButton/KeyboardModifier/Key) к int.
        В PyQt6 int(enum) бросает TypeError — нужно брать .value.
        """
        if isinstance(v, int):
            return v
        try:
            return int(v.value)
        except AttributeError:
            try:
                return int(v)
            except Exception:
                return 0

    def _emit_control_event(self, event) -> bool:
        from PyQt6.QtCore import QEvent
        t = event.type()
        ev_dict = {}

        if t == QEvent.Type.MouseMove:
            nx, ny = self._norm_from_global(event.globalPosition())
            ev_dict = {'type': 'mouse_move', 'x': nx, 'y': ny}

        elif t == QEvent.Type.MouseButtonPress:
            nx, ny = self._norm_from_global(event.globalPosition())
            ev_dict = {'type': 'mouse_press',
                       'x': nx, 'y': ny,
                       'button': self._enum_int(event.button())}

        elif t == QEvent.Type.MouseButtonRelease:
            nx, ny = self._norm_from_global(event.globalPosition())
            ev_dict = {'type': 'mouse_release',
                       'x': nx, 'y': ny,
                       'button': self._enum_int(event.button())}

        elif t == QEvent.Type.MouseButtonDblClick:
            # Двойной клик шлём как press (стример доинъектит второй клик сам
            # по таймингу ОС); главное — не проглотить событие молча.
            nx, ny = self._norm_from_global(event.globalPosition())
            ev_dict = {'type': 'mouse_press',
                       'x': nx, 'y': ny,
                       'button': self._enum_int(event.button())}

        elif t == QEvent.Type.Wheel:
            ev_dict = {'type': 'mouse_scroll',
                       'delta': event.angleDelta().y()}

        elif t == QEvent.Type.KeyPress:
            ev_dict = {'type': 'key_press',
                       'key': self._enum_int(event.key()),
                       'modifiers': self._enum_int(event.modifiers()),
                       'text': event.text()}

        elif t == QEvent.Type.KeyRelease:
            ev_dict = {'type': 'key_release',
                       'key': self._enum_int(event.key()),
                       'modifiers': self._enum_int(event.modifiers()),
                       'text': event.text()}

        if ev_dict:
            et = ev_dict.get('type')
            if et != 'mouse_move':   # move не спамим
                try:
                    print(f"[RC-VIEWER] capture {et} "
                          f"key={ev_dict.get('key')} text={ev_dict.get('text')!r} "
                          f"btn={ev_dict.get('button')} x={ev_dict.get('x')}")
                except Exception:
                    pass
            self.remote_control_event_ready.emit(ev_dict)
            return True
        return False

    def showEvent(self, event):
        super().showEvent(event)
        widgets = [self.surface, self.overlay, self._video_container]
        if self._draw_canvas:
            widgets.append(self._draw_canvas)
        for w in widgets:
            w.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()

        if self._control_mode:
            # Кнопка «Отменить управление» в баннере должна оставаться кликабельной.
            if self._control_banner is not None:
                w = obj
                while w is not None:
                    if w is self._control_banner:
                        return False
                    w = w.parent() if hasattr(w, 'parent') else None

            if t in (QEvent.Type.MouseMove,
                     QEvent.Type.MouseButtonPress,
                     QEvent.Type.MouseButtonRelease,
                     QEvent.Type.MouseButtonDblClick,
                     QEvent.Type.Wheel,
                     QEvent.Type.KeyPress,
                     QEvent.Type.KeyRelease):
                # Фильтр висит на нескольких виджетах (surface/container/window),
                # одно и то же событие пролетает через несколько из них.
                # Дедуп по id(event), чтобы не отправить клик/клавишу 2-3 раза.
                eid = id(event)
                if eid == getattr(self, '_rc_last_event_id', None):
                    return True
                self._rc_last_event_id = eid
                self._emit_control_event(event)
                return True

        if t == QEvent.Type.MouseMove:
            if self._draw_canvas and self._draw_canvas._is_drawing_mode:
                return False
            self._show_overlay()
        return False

    def mouseMoveEvent(self, event):
        self._show_overlay()
        super().mouseMoveEvent(event)

    def toggle_fullscreen(self):
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        self._is_fullscreen = True
        self._bar.hide()
        self._title_bar.hide()
        self.overlay.set_fullscreen_icon(True)
        self.showFullScreen()

    def _exit_fullscreen(self):
        self._is_fullscreen = False
        self._bar.show()
        self._title_bar.show()
        self._title_bar.update_max_icon()
        self.overlay.set_fullscreen_icon(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.showNormal()

    def keyPressEvent(self, event):
        # Во время удалённого управления НЕ перехватываем клавиши под фуллскрин —
        # F/F11/Esc должны уходить на удалённую машину, а не дёргать окно.
        if self._control_mode:
            event.ignore()
            return
        key = event.key()
        if key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self._is_fullscreen:
            self._exit_fullscreen()
        else:
            super().keyPressEvent(event)

    def _on_overlay_stop(self):
        self.overlay_stop_watch.emit()
        self.close()

    def update_rtc_stats(self, rtt_ms: int, jitter_ms: int):

        if self._closing:
            return
        try:
            if rtt_ms <= 80:
                color, icon = "#2ecc71", "🟢"
            elif rtt_ms <= 200:
                color, icon = "#f1c40f", "🟡"
            else:
                color, icon = "#e74c3c", "🔴"
            self._lbl_rtc.setText(f"{icon} RTT: {rtt_ms} ms  Jitter: {jitter_ms} ms")
            self._lbl_rtc.setStyleSheet(
                f"color: {color}; padding: 0 10px; font-size: 11px;"
            )
        except RuntimeError:
            pass

    def update_stream_stats(self, fps: int, loss_pct: int):

        if self._closing:
            return
        try:
            if loss_pct < 5:
                color = "#2ecc71"   # зелёный
                icon  = "🟢"
            elif loss_pct < 15:
                color = "#f1c40f"   # жёлтый
                icon  = "🟡"
            else:
                color = "#e74c3c"   # красный
                icon  = "🔴"

            self._lbl_fps.setText(f"{icon} {fps} fps  Loss: {loss_pct}%")
            self._lbl_fps.setStyleSheet(
                f"color: {color}; padding: 0 10px; font-size: 11px;"
            )
        except RuntimeError:
            pass

    def sync_audio_state(self, is_muted: bool, is_deafened: bool):

        if self._closing:
            return
        try:
            self.overlay.sync_mute_state(is_muted)
            self.overlay.sync_deafen_state(is_deafened)
        except RuntimeError:
            pass

    @pyqtSlot(QImage)
    def update_frame(self, q_img: QImage):

        if q_img.isNull():
            return

        try:
            self.surface.set_frame(q_img)
            self._frame_count += 1
            now = time.monotonic()
            elapsed = now - self._fps_last_time
            if elapsed >= 1.0:
                self._fps_last_time = now
                self._lbl_res.setText(f"Res: {q_img.width()}×{q_img.height()}")
                self._lbl_frames.setText(f"Frames: {self._frame_count}")
                self._update_draw_canvas_frame_rect()

        except Exception as e:
            print(f"[VideoWindow] Error updating frame: {e}")

    def closeEvent(self, event):
        self._closing = True
        self._hide_timer.stop()

        # Если окно закрывают во время управления — снять захват и плашку,
        # иначе у зрителя останется grabKeyboard, а у стримера — активная сессия.
        if self._control_mode:
            self._control_mode = False
            self._rc_end_capture()
            self.control_released.emit()
        ov = getattr(self, '_viewer_ctrl_overlay', None)
        if ov is not None:
            try:
                ov.close()
            except RuntimeError:
                pass
            self._viewer_ctrl_overlay = None

        try:
            if self._draw_canvas is not None:
                self._draw_canvas.set_drawing_mode(False)
                self._draw_canvas._fade_timer.stop()
        except (RuntimeError, AttributeError):
            pass

        try:
            if self._sb_panel is not None:
                self._sb_panel.close()
                self._sb_panel = None
        except (RuntimeError, AttributeError):
            self._sb_panel = None

        if hasattr(self, 'surface') and self.surface is not None:
            try:
                self.surface._current_image = None
            except RuntimeError:
                pass

        if hasattr(self, 'overlay') and self.overlay is not None:
            try:
                self.overlay._vol_hide_timer.stop()
            except (RuntimeError, AttributeError):
                pass

        if self._is_fullscreen:
            self._exit_fullscreen()
        if self.uid is not None:
            self.window_closed.emit(self.uid)
        super().closeEvent(event)

    def sizeHint(self) -> QSize:
        return QSize(1280, 720 + _TITLE_H + 28)