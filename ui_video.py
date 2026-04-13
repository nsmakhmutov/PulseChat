# ui_video.py — GPU-accelerated видеоплеер на QOpenGLWidget
#
# Архитектура:
#   VideoSurface             — QOpenGLWidget, рендерит кадры через OpenGL текстуры (GPU)
#   VideoGlassTitleBar       — стеклянный кастомный тайтлбар (перетаскивание, мин/макс/закрыть)
#   DrawCanvas               — прозрачный QWidget поверх video_container; рисование зрителем
#   StreamerAnnotationOverlay— прозрачное топ-окно на экране стримера (видит чужие мазки)
#   VideoOverlay             — QFrame-оверлей с панелью управления (авто-скрытие по мышке)
#   VideoWindow              — QWidget-обёртка: склеивает всё вместе
#
# Публичный API (совместим со старым кодом):
#   VideoWindow(nick)           — создать окно
#   window.uid                  — UID стримера (устанавливается снаружи)
#   window.update_frame(QImage) — слот для приёма нового кадра
#   window.add_remote_stroke()  — принять мазок от сервера (зритель/стример)
#
# Новые сигналы VideoWindow (подключать в MainWindow при необходимости):
#   overlay_mute_toggled   () — зритель нажал кнопку mic в оверлее
#   overlay_deafen_toggled () — зритель нажал кнопку volume в оверлее
#   overlay_stop_watch     () — зритель нажал «Прекратить просмотр»
#   draw_stroke_ready(uid, nick, color, points, width) — зритель закончил мазок
#
# Полноэкранный режим:
#   — Кнопка ⛶ в оверлее / двойной клик / F / F11 → переключить fullscreen
#   — Escape → выйти из fullscreen

import time
import math
import random
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QSizePolicy, QPushButton, QFrame, QSlider,
                             QGraphicsOpacityEffect, QApplication)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtCore import (Qt, pyqtSlot, QSize, QRect, pyqtSignal,
                          QTimer, QEvent, QPoint, QPointF, QPropertyAnimation,
                          QEasingCurve)
from PyQt6.QtGui import (QImage, QPainter, QColor, QFont, QIcon,
                         QLinearGradient, QPen, QPainterPath, QCursor)

from config import resource_path, DRAW_FADE_SEC

# ВАЖНО: QSurfaceFormat.setDefaultFormat() вызывается в client_main.py
# ДО создания QApplication. Здесь его быть НЕ должно — иначе краш 0xC0000409.

# Таймаут авто-скрытия оверлея и курсора (мс)
_HIDE_TIMEOUT_MS = 3000

# Высота оверлей-панели
_OVERLAY_H = 60

# Высота стеклянного тайтлбара плеера
_TITLE_H = 36

# Палитра цветов мазков — 12 хорошо различимых цветов (назначается случайно при старте)
_STROKE_PALETTE = [
    '#FF6B6B', '#FFD93D', '#6BCB77', '#4D96FF',
    '#FF922B', '#CC5DE8', '#20C997', '#F06595',
    '#74C0FC', '#A9E34B', '#FFA94D', '#E599F7',
]

# ---------------------------------------------------------------------------
# VideoGlassTitleBar — стеклянный кастомный тайтлбар окна плеера
# ---------------------------------------------------------------------------
class VideoGlassTitleBar(QWidget):
    """
    Кастомный title bar в стиле стеклянного интерфейса приложения.
    Заменяет стандартный системный заголовок окна.
    Поддерживает перетаскивание, двойной клик → maximize/restore,
    кнопки: свернуть, развернуть/восстановить, закрыть.
    """
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

        # Иконка приложения
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

        # Заголовок
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

        # Свернуть
        self._btn_min = QPushButton('─')
        self._btn_min.setStyleSheet(_btn_ss)
        self._btn_min.clicked.connect(self._win.showMinimized)

        # Максимизировать / восстановить
        self._btn_max = QPushButton('□')
        self._btn_max.setStyleSheet(_btn_ss)
        self._btn_max.clicked.connect(self._toggle_max)

        # Закрыть
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
            # В fullscreen перетаскивание не нужно
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


# ---------------------------------------------------------------------------
# DrawCanvas — прозрачный QWidget для рисования поверх видео (зритель)
# ---------------------------------------------------------------------------
class DrawCanvas(QWidget):
    """
    Прозрачный виджет-«стекло» поверх video_container.
    В режиме рисования (is_drawing=True) перехватывает мышь и рисует мазки.
    В обычном режиме — полностью прозрачен для событий мыши.

    Хранит до 64 мазков (локальных + удалённых).
    Каждый мазок: {'points': [(x,y)...], 'color': str, 'width': int, 'born': float}
    Координаты — пиксельные (абсолют. виджета). При рендере нормализуем к кадру.
    Удалённые мазки хранятся нормализованными (0.0–1.0) и денормализуются при paint.

    Сигнал stroke_ready испускается при отпускании кнопки мыши.
    points в сигнале нормализованы 0.0–1.0 относительно области кадра.
    """

    stroke_ready = pyqtSignal(str, list, int)   # (color, norm_points, width)

    _MAX_STROKES = 64

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self._is_drawing_mode: bool = False
        self._current_stroke: list  = []     # текущий мазок (пиксели)
        self._strokes: list         = []     # все мазки (нормализованные, для render)
        self._my_color: str         = random.choice(_STROKE_PALETTE)
        self._my_width: int         = 3

        # Таймер перерисовки для затухания
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(50)      # 20 Гц — достаточно для fade
        self._fade_timer.timeout.connect(self._on_fade_tick)

        # Область кадра внутри виджета (letterbox rect) — обновляется из VideoSurface
        self._frame_rect: QRect = QRect()

    # ── Публичный API ────────────────────────────────────────────────────────

    def set_drawing_mode(self, enabled: bool):
        """Переключает режим рисования. True — захватываем мышь."""
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
        """Вызывается VideoWindow при каждом resize/paintGL чтобы знать где кадр."""
        self._frame_rect = rect

    def add_remote_stroke(self, color: str, norm_points: list, width: int):
        """
        Добавляет мазок от удалённого зрителя или ретранслированный сервером.
        norm_points — нормализованные [[x,y], ...] 0.0–1.0 относительно кадра.
        """
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

    # ── Мышь ────────────────────────────────────────────────────────────────

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
                # Сглаживание: не добавляем точку ближе 3px к предыдущей
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
                    # Добавляем локальный мазок
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

    # ── Рендер ──────────────────────────────────────────────────────────────

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
            # Прозрачность: полная до fade_start, потом линейное затухание
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

        # Текущий мазок (в процессе рисования) — всегда непрозрачен
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

    # ── Вспомогательное ──────────────────────────────────────────────────────

    def _on_fade_tick(self):
        now = time.monotonic()
        # Убираем истёкшие мазки
        self._strokes = [s for s in self._strokes if now - s['born'] < DRAW_FADE_SEC]
        if not self._strokes and not self._current_stroke:
            self._fade_timer.stop()
        self.update()

    def _normalize_stroke(self, pixels: list) -> list:
        """Конвертирует пиксельные координаты виджета в нормализованные 0.0–1.0 кадра."""
        r = self._frame_rect
        if r.isEmpty():
            # Fallback: весь виджет
            w, h = max(self.width(), 1), max(self.height(), 1)
            return [[p.x() / w, p.y() / h] for p in pixels]
        fw, fh = max(r.width(), 1), max(r.height(), 1)
        result = []
        for p in pixels:
            nx = (p.x() - r.x()) / fw
            ny = (p.y() - r.y()) / fh
            # Зажимаем в [0, 1] — рисование за пределами кадра бессмысленно
            result.append([max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))])
        return result

    def _denormalize(self, norm_points: list) -> list:
        """Конвертирует нормализованные 0.0–1.0 обратно в пиксели виджета."""
        r = self._frame_rect
        if r.isEmpty():
            w, h = self.width(), self.height()
            return [QPoint(int(p[0] * w), int(p[1] * h)) for p in norm_points]
        return [
            QPoint(int(r.x() + p[0] * r.width()),
                   int(r.y() + p[1] * r.height()))
            for p in norm_points
        ]


# ---------------------------------------------------------------------------
# StreamerAnnotationOverlay — оверлей аннотаций на экране СТРИМЕРА
# ---------------------------------------------------------------------------
class StreamerAnnotationOverlay(QWidget):
    """
    Полностью прозрачное frameless top-level окно поверх всего экрана стримера.
    Отображает мазки зрителей поверх захватываемого DXCam-контента.
    Стример видит кто именно рисует (ник + цветная точка рядом с мазком).

    Архитектура:
      — WA_TranslucentBackground + WA_NoSystemBackground → прозрачный фон
      — WindowType.Tool | FramelessWindowHint | WindowStaysOnTopHint →
        поверх всех окон, не попадает в taskbar, не мешает DXCam capture
        (DXCam захватывает рабочий стол под оверлеем, т.к. overlay — отдельное окно)
      — setWindowFlag(X11BypassWindowManagerHint) — только если нужно

    Вызывать show() при старте стрима, hide()/close() при остановке.
    """

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

        # Растягиваем на весь primary screen
        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.geometry())

        self._strokes: list = []    # {'norm', 'color', 'width', 'born', 'nick'}
        self._MAX     = 64

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(50)
        self._fade_timer.timeout.connect(self._on_fade_tick)

        # Захват области стрима: если DXCam снимает не весь экран, а регион —
        # нормализация происходит в VideoEngine. Здесь храним последний известный
        # DXCam-регион для правильного отображения. По умолчанию — весь экран.
        self._capture_rect: QRect = QRect()

    def set_capture_region(self, x: int, y: int, w: int, h: int):
        """Обновляет координаты захватываемой области DXCam на экране."""
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

        # Определяем область на экране (capture rect или весь виджет)
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

            # Ник рядом с первой точкой мазка
            if stroke.get('nick'):
                lbl_color = QColor(stroke['color'])
                lbl_color.setAlpha(min(200, alpha))
                p.setPen(lbl_color)
                p.drawText(pts[0].x() + 6, pts[0].y() - 6, stroke['nick'])

        p.end()


# ---------------------------------------------------------------------------
# VideoSurface — "холст" OpenGL, отвечает только за рендеринг кадров
# ---------------------------------------------------------------------------
class VideoSurface(QOpenGLWidget):
    """
    QOpenGLWidget, который принимает QImage и рисует его через QPainter
    поверх OpenGL-контекста. QPainter на QOpenGLWidget использует GPU
    (OpenGL paint engine), поэтому масштабирование и блиттинг идут без CPU.

    Почему QPainter, а не голые glTexImage2D-вызовы?
      — Полная совместимость с PyQt6 без PyOpenGL/OpenGL32 зависимостей.
      — Qt автоматически загружает QImage как GL-текстуру и делает
        texSubImage при обновлении, что даёт те же преимущества GPU.
      — В дальнейшем сюда легко добавить шейдеры через QOpenGLShaderProgram.
    """

    # Двойной клик → запрос переключения fullscreen
    fullscreen_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_image: QImage | None = None
        self._placeholder_text = "Ожидание видео..."
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)
        self._placeholder_font = QFont("Segoe UI", 20)

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------
    def set_frame(self, q_img: QImage):
        """Принять новый кадр. Вызывать только из GUI-потока."""
        self._current_image = q_img
        self.update()  # запросить перерисовку (не блокирует)

    # ------------------------------------------------------------------
    # Переопределения QOpenGLWidget
    # ------------------------------------------------------------------
    def initializeGL(self):
        """QPainter управляет контекстом самостоятельно — ручные glClear не нужны."""
        pass  # Не вызываем context().functions() — это конфликтует с QPainter

    def resizeGL(self, w: int, h: int):
        """QPainter сам обновляет viewport при каждом paintGL()."""
        pass

    def paintGL(self):
        """Главный рендер-цикл на GPU."""
        painter = QPainter(self)
        # FIX BLUR: убран SmoothPixmapTransform для экранного контента.
        # SmoothPixmapTransform = bilinear interpolation при масштабировании.
        # Для видео с экрана (UI, текст, иконки) bilinear добавляет размытость
        # при downscaling (когда окно меньше исходника) — текст теряет чёткость.
        # Qt на QOpenGLWidget использует GPU-accelerated drawImage в любом случае.
        # Antialiasing оставляем для корректного subpixel-позиционирования рамок.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # НЕ устанавливаем SmoothPixmapTransform — Qt выберет nearest/linear
        # автоматически в зависимости от scale factor (sharp для 1:1, linear для других).
        w, h = self.width(), self.height()
        painter.fillRect(0, 0, w, h, QColor(0, 0, 0))
        if self._current_image and not self._current_image.isNull():
            self._draw_frame(painter, w, h)
        else:
            self._draw_placeholder(painter, w, h)
        painter.end()

    def mouseDoubleClickEvent(self, event):
        """Двойной клик ЛКМ — переключить полноэкранный режим."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.fullscreen_requested.emit()
        super().mouseDoubleClickEvent(event)

    # ------------------------------------------------------------------
    # Приватные методы рисования
    # ------------------------------------------------------------------
    def _draw_frame(self, painter: QPainter, w: int, h: int):
        """
        Рисует кадр с сохранением пропорций (letterbox / pillarbox).

        Масштабирование всегда билинейное (SmoothPixmapTransform установлен
        в paintGL). Qt использует GPU-path при рендере в QOpenGLWidget,
        поэтому quality ≈ бесплатна.

        dest_w/dest_h округляются до чётных пикселей: YUV420p нативно
        чётный субдискрет, нечётные размеры вызывают субпиксельный сдвиг
        при drawImage → лёгкое размытие горизонтальных линий.
        """
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
        """Рисует заглушку 'Ожидание видео...' по центру."""
        painter.setFont(self._placeholder_font)
        painter.setPen(QColor(160, 160, 160))
        painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, self._placeholder_text)


# ---------------------------------------------------------------------------
# StreamVolumePopup — всплывающий вертикальный слайдер громкости стрима
# ---------------------------------------------------------------------------
class StreamVolumePopup(QFrame):
    """
    Всплывающий вертикальный слайдер громкости стрима в стиле Discord.
    Появляется над кнопкой volume_stream при hover/клике.
    Закрашиваемая область снизу вверх (как уровень заполнения).
    """

    volume_changed = pyqtSignal(float)  # 0.0 – 2.0

    _FILL_COLOR   = QColor(88, 101, 242)   # Discord-синий
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

        # Геометрия трека (с отступами сверху и снизу)
        self._pad_top    = 14
        self._pad_bottom = 14

    # --- Публичный API ---
    def set_value(self, v: float):
        self._value = max(0.0, min(2.0, v))
        self.update()

    def get_value(self) -> float:
        return self._value

    # --- Рисование ---
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

        # Трек (фон)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._TRACK_COLOR)
        p.drawRoundedRect(track_x, track_top, track_w, track_h, 3, 3)

        # Заполненная часть (снизу вверх)
        # value 0.0 → y=track_bottom (пусто), value 2.0 → y=track_top (полно)
        ratio      = self._value / 2.0
        fill_h     = int(track_h * ratio)
        fill_y     = track_bottom - fill_h

        grad = QLinearGradient(0, fill_y, 0, track_bottom)
        grad.setColorAt(0.0, QColor(120, 135, 255))
        grad.setColorAt(1.0, self._FILL_COLOR)
        p.setBrush(grad)
        p.drawRoundedRect(track_x, fill_y, track_w, fill_h, 3, 3)

        # Ручка
        handle_y = fill_y - self._HANDLE_R
        p.setBrush(self._HANDLE_COLOR)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - self._HANDLE_R, handle_y, self._HANDLE_R * 2, self._HANDLE_R * 2)

        # Текст процентов
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


# ---------------------------------------------------------------------------
# VideoOverlay — плавающая панель управления поверх видео
# ---------------------------------------------------------------------------
class VideoOverlay(QFrame):
    """
    Полупрозрачная панель с кнопками управления.
    Располагается снизу-по-центру поверх VideoSurface.
    Появляется при движении мыши, скрывается через _HIDE_TIMEOUT_MS.

    Кнопки (слева направо):
        🎤  Заглушить микрофон    (mic_on / mic_off)
        🔊  Заглушить динамики   (volume_on / volume_off)
        🛑  Прекратить просмотр  (stop_stream_watch)
      | sep |
        ⛶   Полный экран         (справа)
    """

    # Сигналы — чистые клики, состояние хранит VideoWindow
    mute_clicked       = pyqtSignal()
    deafen_clicked     = pyqtSignal()
    stop_watch_clicked = pyqtSignal()
    fullscreen_clicked = pyqtSignal()
    stream_volume_changed = pyqtSignal(float)   # 0.0–2.0
    # Soundboard убран из оверлея зрителя (доступен только в главном окне).
    # Кнопка Draw: зажата → режим рисования включён
    draw_toggled = pyqtSignal(bool)   # True = рисование включено

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

        # --- Микрофон ---
        self.btn_mute = self._make_btn("assets/icon/mic_on.svg", "Заглушить микрофон")
        self.btn_mute.setCheckable(True)
        self.btn_mute.clicked.connect(self._on_mute_clicked)

        # --- Динамики ---
        self.btn_deafen = self._make_btn("assets/icon/volume_on.svg", "Заглушить динамики")
        self.btn_deafen.setCheckable(True)
        self.btn_deafen.clicked.connect(self._on_deafen_clicked)

        # --- Прекратить просмотр ---
        self.btn_stop = self._make_btn("assets/icon/monitor_off.svg", "Прекратить просмотр")
        self.btn_stop.clicked.connect(self.stop_watch_clicked)

        # --- Громкость стрима ---
        self.btn_vol_stream = self._make_btn("assets/icon/volume_stream.svg", "Громкость стрима")
        self._vol_popup = StreamVolumePopup(parent.parent() if parent else self)
        self._vol_popup.setVisible(False)
        self._vol_popup.volume_changed.connect(self.stream_volume_changed)

        # Таймер скрытия попапа после потери фокуса мышки
        self._vol_hide_timer = QTimer(self)
        self._vol_hide_timer.setSingleShot(True)
        self._vol_hide_timer.setInterval(400)
        self._vol_hide_timer.timeout.connect(self._hide_vol_popup)

        self.btn_vol_stream.clicked.connect(self._toggle_vol_popup)
        self.btn_vol_stream.installEventFilter(self)
        self._vol_popup.installEventFilter(self)

        # --- Soundboard убран из оверлея зрителя ---
        # Звуковая панель доступна только в главном окне (btn_sb).
        # В окне просмотра стрима она отвлекает и не нужна зрителю.

        # --- Draw (рисование поверх стрима) ---
        self.btn_draw = self._make_btn("assets/icon/draw.svg", "Рисовать на стриме (5 сек)")
        self.btn_draw.setCheckable(True)
        self.btn_draw.clicked.connect(self._on_draw_clicked)

        # --- Разделитель ---
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("QFrame { color: rgba(255,255,255,50); }")
        sep.setFixedWidth(2)
        sep.setFixedHeight(32)

        # --- Fullscreen ---
        self.btn_fs = self._make_btn(None, "Полный экран / Оконный режим")
        self.btn_fs.setText("⛶")
        self.btn_fs.setFont(QFont("Segoe UI", 16))
        self.btn_fs.clicked.connect(self.fullscreen_clicked)

        layout.addWidget(self.btn_mute)
        layout.addWidget(self.btn_deafen)
        layout.addWidget(self.btn_stop)
        layout.addWidget(self.btn_vol_stream)
        layout.addWidget(self.btn_draw)
        layout.addWidget(sep, alignment=Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.btn_fs)

    # ------------------------------------------------------------------
    # Фабричный метод кнопки в стиле главного меню
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Громкость стрима: попап
    # ------------------------------------------------------------------
    def _toggle_vol_popup(self):
        if self._vol_popup.isVisible():
            self._hide_vol_popup()
        else:
            self._show_vol_popup()

    def _show_vol_popup(self):
        """Позиционировать и показать попап над кнопкой."""
        self._vol_hide_timer.stop()
        # Координаты кнопки в родительском виджете (VideoWindow/_video_container)
        btn_pos = self.btn_vol_stream.mapTo(self._vol_popup.parent(), QPoint(0, 0))
        popup_x = btn_pos.x() + (self.btn_vol_stream.width() - self._vol_popup.width()) // 2
        popup_y = btn_pos.y() - self._vol_popup.height() - 8
        self._vol_popup.move(popup_x, popup_y)
        self._vol_popup.raise_()
        self._vol_popup.setVisible(True)

    def _hide_vol_popup(self):
        self._vol_popup.setVisible(False)

    def eventFilter(self, obj, event):
        """Скрываем попап при уходе мышки с кнопки или самого попапа."""
        t = event.type()
        if obj in (self.btn_vol_stream, self._vol_popup):
            if t == QEvent.Type.Enter:
                self._vol_hide_timer.stop()
                if obj == self.btn_vol_stream:
                    self._show_vol_popup()
            elif t == QEvent.Type.Leave:
                self._vol_hide_timer.start()
        return False

    # ------------------------------------------------------------------
    # Обработка кликов с обновлением иконок
    # ------------------------------------------------------------------
    def _on_draw_clicked(self):
        """
        Переключает режим рисования.
        Кнопка checkable: checked=True → рисование включено (карандаш подсвечен).
        Цвет кнопки при active — Discord-синий вместо стандартного красного,
        чтобы не путать с «заглушить».
        """
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
            # Сбрасываем на дефолтный стиль (перестройкой не трогаем остальные кнопки)
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

    def _on_deafen_clicked(self):
        is_deafened = self.btn_deafen.isChecked()
        icon = "assets/icon/volume_off.svg" if is_deafened else "assets/icon/volume_on.svg"
        self.btn_deafen.setIcon(QIcon(resource_path(icon)))
        self.deafen_clicked.emit()

    # ------------------------------------------------------------------
    # Публичные методы синхронизации состояния (вызываются из VideoWindow)
    # ------------------------------------------------------------------
    def sync_mute_state(self, is_muted: bool):
        """Обновить иконку/состояние кнопки без эмита сигнала."""
        self.btn_mute.blockSignals(True)
        self.btn_mute.setChecked(is_muted)
        icon = "assets/icon/mic_off.svg" if is_muted else "assets/icon/mic_on.svg"
        self.btn_mute.setIcon(QIcon(resource_path(icon)))
        self.btn_mute.blockSignals(False)

    def sync_deafen_state(self, is_deafened: bool):
        """Обновить иконку/состояние кнопки без эмита сигнала."""
        self.btn_deafen.blockSignals(True)
        self.btn_deafen.setChecked(is_deafened)
        icon = "assets/icon/volume_off.svg" if is_deafened else "assets/icon/volume_on.svg"
        self.btn_deafen.setIcon(QIcon(resource_path(icon)))
        self.btn_deafen.blockSignals(False)

    def set_stream_volume_value(self, vol: float):
        """
        Синхронизирует ползунок попапа с текущей громкостью AudioHandler.
        Вызывается из MainWindow.open_video_window() после подключения сигнала.
        Не эмитит stream_volume_changed — только обновляет визуальное состояние.
        """
        self._vol_popup.set_value(vol)

    def set_fullscreen_icon(self, is_fullscreen: bool):
        """Переключить иконку кнопки fullscreen."""
        self.btn_fs.setText("❐" if is_fullscreen else "⛶")


# ---------------------------------------------------------------------------
# VideoWindow — окно-контейнер: поверхность + оверлей + тулбар статистики
# ---------------------------------------------------------------------------
class VideoWindow(QWidget):
    """
    Полноценное окно воспроизведения стрима.

    Публичный API (совместим со старым кодом):
        window.uid                  (int)   — UID стримера, ставится снаружи
        window.update_frame(img)    (slot)  — принять QImage от VideoEngine
        window.window_closed        (signal, int uid) — испускается при закрытии окна

    Новые сигналы (опционально подключать в MainWindow):
        overlay_mute_toggled   () — зритель переключил микрофон
        overlay_deafen_toggled () — зритель переключил динамики
        overlay_stop_watch     () — зритель нажал «Прекратить просмотр»

    Публичные методы:
        sync_audio_state(muted, deafened) — синхронизировать иконки оверлея
    """

    # --- Совместимый сигнал ---
    window_closed = pyqtSignal(int)

    # --- Новые сигналы от оверлея ---
    overlay_mute_toggled   = pyqtSignal()
    overlay_deafen_toggled = pyqtSignal()
    overlay_stop_watch     = pyqtSignal()
    overlay_stream_volume_changed = pyqtSignal(float)   # 0.0–2.0

    # --- Рисование: зритель закончил мазок → MainWindow отправит на сервер ---
    # (color: str, norm_points: list, width: int)
    draw_stroke_ready = pyqtSignal(str, list, int)

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

        # DrawCanvas — инициализируется после _setup_ui (нужен _video_container)
        self._draw_canvas: DrawCanvas | None = None

        self._setup_ui(nick)
        self._setup_hide_timer()
        # ABR-таймер удалён: WebRTC управляет битрейтом через TWCC автоматически.
        # _lbl_rtc в тулбаре будет подключён к pc.getStats() в следующей итерации.

    # ------------------------------------------------------------------
    # Публичный метод: передать NetworkClient для soundboard в оверлее
    # ------------------------------------------------------------------
    def set_net(self, net):
        """
        Вызывается из MainWindow.open_video_window() после создания окна.
        Сохраняет ссылку на NetworkClient для soundboard в оверлее стрима.
        """
        self._net = net

    # ------------------------------------------------------------------
    # Soundboard поверх стрима
    # ------------------------------------------------------------------
    # open_soundboard убран — кнопка удалена из оверлея зрителя.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Построение UI
    # ------------------------------------------------------------------
    def _setup_ui(self, nick: str):
        # ── Безрамочное окно со стеклянным тайтлбаром ───────────────────────
        # WindowType.Window даёт полноценное окно (taskbar + Alt-Tab),
        # FramelessWindowHint убирает системный заголовок и рамку.
        # Стеклянный тайтлбар рисуется нами (VideoGlassTitleBar).
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
        # Стиль окна: тёмный фон + тонкая рамка как у других окон приложения
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

        # ── Стеклянный тайтлбар ──────────────────────────────────────────────
        self._title_bar = VideoGlassTitleBar(self, f"Стрим: {nick}")
        root.addWidget(self._title_bar)

        # --- Контейнер для видео + оверлея (нужен для абсолютного позиционирования) ---
        self._video_container = QWidget(self)
        self._video_container.setMouseTracking(True)
        self._video_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._video_container, stretch=1)

        # OpenGL-поверхность заполняет весь контейнер
        self.surface = VideoSurface(self._video_container)
        self.surface.fullscreen_requested.connect(self.toggle_fullscreen)

        # ── DrawCanvas поверх video_container ───────────────────────────────
        # Создаём здесь, после video_container. Размер синхронизируем в resizeEvent.
        self._draw_canvas = DrawCanvas(self._video_container)
        self._draw_canvas.stroke_ready.connect(self.draw_stroke_ready)  # → MainWindow

        # Оверлей поверх видео (абсолютное позиционирование внутри контейнера)
        self.overlay = VideoOverlay(self._video_container)
        self.overlay.mute_clicked.connect(self.overlay_mute_toggled)
        self.overlay.deafen_clicked.connect(self.overlay_deafen_toggled)
        self.overlay.stop_watch_clicked.connect(self._on_overlay_stop)
        self.overlay.fullscreen_clicked.connect(self.toggle_fullscreen)
        self.overlay.stream_volume_changed.connect(self.overlay_stream_volume_changed)
        # Кнопка Draw → включить/выключить режим рисования на DrawCanvas
        self.overlay.draw_toggled.connect(self._on_draw_toggled)
        self.overlay.hide()  # скрыт по умолчанию

        # --- Тулбар статистики (снизу) ---
        self._bar = QWidget(self)
        self._bar.setFixedHeight(28)
        self._bar.setStyleSheet("background: #10111e; border-top: 1px solid rgba(255,255,255,0.06);")
        bar_layout = QHBoxLayout(self._bar)
        bar_layout.setContentsMargins(8, 0, 8, 0)
        bar_layout.setSpacing(0)

        lbl_style = "color: #8888aa; padding: 0 10px; font-size: 11px;"

        self._lbl_fps      = QLabel("Net: —")     # FPS декодера + % потерь пакетов
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

    # ------------------------------------------------------------------
    # Геометрия: surface и overlay обновляются при каждом resizeEvent
    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_surface()
        self._reposition_draw_canvas()  # Сначала обновляем и поднимаем холст
        self._reposition_overlay()  # Оверлей поднимаем В САМОМ КОНЦЕ, чтобы он был поверх холста

        # Обновляем title bar при смене состояния окна (maximize/restore)
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
        # Сообщаем DrawCanvas актуальный letterbox-прямоугольник кадра
        self._update_draw_canvas_frame_rect()

    def _update_draw_canvas_frame_rect(self):
        """
        Вычисляет letterbox-прямоугольник кадра внутри video_container
        и передаёт его в DrawCanvas для корректной нормализации координат.
        """
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
            # Нет кадра — считаем весь контейнер кадром
            self._draw_canvas.update_frame_rect(QRect(0, 0, cw, ch))

    def _reposition_overlay(self):
        """
        Центрировать оверлей горизонтально.
        Прижать к нижнему краю контейнера с отступом 24 px.
        Ширина подстраивается под содержимое (sizeHint).
        """
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

    # ------------------------------------------------------------------
    # Авто-показ / авто-скрытие
    # ------------------------------------------------------------------
    def _show_overlay(self):
        """Показать оверлей и перезапустить таймер скрытия."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if not self.overlay.isVisible():
            self._reposition_overlay()   # пересчитать позицию — на случай resize
            self.overlay.show()
            self.overlay.raise_()        # поверх surface
        self._hide_timer.start(_HIDE_TIMEOUT_MS)

    def _on_hide_timeout(self):
        """Скрыть оверлей по таймеру. В fullscreen — ещё и курсор."""
        self.overlay.hide()
        if self._is_fullscreen:
            self.setCursor(Qt.CursorShape.BlankCursor)

    # ------------------------------------------------------------------
    # Рисование: режим Draw
    # ------------------------------------------------------------------
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
            # Останавливаем авто-скрытие пока режим рисования активен
            self._hide_timer.stop()
            self.overlay.show()
            self.overlay.raise_()
        else:
            # Возобновляем авто-скрытие
            self._hide_timer.start(_HIDE_TIMEOUT_MS)

    def add_remote_stroke(self, color: str, norm_points: list, width: int):
        """
        Принимает мазок от удалённого зрителя (ретранслированный сервером).
        Вызывается из MainWindow при получении draw_stroke_received.
        Также вызывается на стороне зрителя для эха своего же мазка
        (сервер возвращает всем включая отправителя — для консистентности).
        """
        if self._draw_canvas is None:
            return
        self._draw_canvas.add_remote_stroke(color, norm_points, width)
        # После нового мазка обновляем letterbox-прямоугольник (кадр мог смениться)
        self._update_draw_canvas_frame_rect()

    # ------------------------------------------------------------------
    # Перехват mouseMoveEvent со всех дочерних виджетов через eventFilter
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        # Устанавливаем фильтр на все виджеты, которые могут «поглощать» move
        widgets = [self.surface, self.overlay, self._video_container]
        if self._draw_canvas:
            widgets.append(self._draw_canvas)
        for w in widgets:
            w.installEventFilter(self)

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.Type.MouseMove:
            # В режиме рисования не перезапускаем таймер скрытия
            if self._draw_canvas and self._draw_canvas._is_drawing_mode:
                return False
            self._show_overlay()
        return False  # не поглощаем — пусть Qt продолжает обработку

    def mouseMoveEvent(self, event):
        self._show_overlay()
        super().mouseMoveEvent(event)

    # ------------------------------------------------------------------
    # Полноэкранный режим
    # ------------------------------------------------------------------
    def toggle_fullscreen(self):
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        self._is_fullscreen = True
        self._bar.hide()
        self._title_bar.hide()   # в fullscreen тайтлбар скрываем
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

    # ------------------------------------------------------------------
    # Клавиатура
    # ------------------------------------------------------------------
    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_F, Qt.Key.Key_F11):
            self.toggle_fullscreen()
        elif key == Qt.Key.Key_Escape and self._is_fullscreen:
            self._exit_fullscreen()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Кнопка «Прекратить просмотр»: сигнал + закрытие окна
    # ------------------------------------------------------------------
    def _on_overlay_stop(self):
        self.overlay_stop_watch.emit()
        self.close()

    def update_rtc_stats(self, rtt_ms: int, jitter_ms: int):
        """
        Обновляет WebRTC-метку в тулбаре (RTT + jitter из pc.getStats()).
        Будет вызываться из VideoEngine после подключения к WebRTC pc.getStats()
        в следующей итерации рефакторинга.

        Цветовая схема по RTT:
          ≤ 80 мс  → зелёный  (отличная сеть)
          ≤ 200 мс → жёлтый   (допустимо)
          > 200 мс → красный  (высокая задержка)
        """
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
        """
        Обновляет HUD-метку с FPS декодера и процентом потерь пакетов.
        Вызывается из MainWindow каждые 2 сек (VideoEngine.stream_stats_updated).

        Цветовая схема по loss%:
          0-4%   → зелёный  (отличная сеть)
          5-14%  → жёлтый   (небольшие потери, видео может рябить)
          ≥15%   → красный  (серьёзные потери, артефакты неизбежны)

        Показываем FPS и Loss вместо исходного «FPS: N»:
          «28 fps  Loss: 0%»   → хорошая сеть
          «15 fps  Loss: 22%» → плохая сеть (красный)
        """
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

    # ------------------------------------------------------------------
    # Публичный метод синхронизации состояния аудио с иконками оверлея
    # ------------------------------------------------------------------
    def sync_audio_state(self, is_muted: bool, is_deafened: bool):
        """
        Вызывать из MainWindow при изменении AudioHandler.is_muted / is_deafened,
        чтобы иконки в оверлее отражали реальное состояние.
        """

        if self._closing:
            return
        try:
            self.overlay.sync_mute_state(is_muted)
            self.overlay.sync_deafen_state(is_deafened)
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # Публичный слот (совместим со старым кодом в ui_main.py)
    # ------------------------------------------------------------------
    @pyqtSlot(QImage)
    def update_frame(self, q_img: QImage):
        """
        Потокобезопасный слот для приёма кадра от VideoEngine.
        VideoEngine эмитит frame_received(int uid, QImage) —
        MainWindow подключает сигнал к этому слоту.
        """
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

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        """Перехватываем закрытие окна, испускаем сигнал до уничтожения объекта."""
        self._closing = True         # блокируем sync_audio_state от внешних сигналов
        self._hide_timer.stop()      # останавливаем таймер авто-скрытия

        # Останавливаем DrawCanvas fade-таймер и выключаем режим рисования
        try:
            if self._draw_canvas is not None:
                self._draw_canvas.set_drawing_mode(False)
                self._draw_canvas._fade_timer.stop()
        except (RuntimeError, AttributeError):
            pass

        # Закрываем SoundboardPanel если открыта
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

        # Останавливаем таймер попапа громкости
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
        # 720px видео + 36px стеклянный тайтлбар + 28px статус-бар
        return QSize(1280, 720 + _TITLE_H + 28)