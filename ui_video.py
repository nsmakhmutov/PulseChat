# ui_video.py — GPU-accelerated видеоплеер на QOpenGLWidget
#
# Архитектура:
#   VideoSurface  — QOpenGLWidget, рендерит кадры через OpenGL текстуры (GPU)
#   VideoOverlay  — QFrame-оверлей с панелью управления (авто-скрытие по мышке)
#   VideoWindow   — QWidget-обёртка: склеивает VideoSurface + VideoOverlay
#
# Публичный API (совместим со старым кодом):
#   VideoWindow(nick)           — создать окно
#   window.uid                  — UID стримера (устанавливается снаружи)
#   window.update_frame(QImage) — слот для приёма нового кадра
#
# Новые сигналы VideoWindow (подключать в MainWindow при необходимости):
#   overlay_mute_toggled   () — зритель нажал кнопку mic в оверлее
#   overlay_deafen_toggled () — зритель нажал кнопку volume в оверлее
#   overlay_stop_watch     () — зритель нажал «Прекратить просмотр»
#
# Полноэкранный режим:
#   — Кнопка ⛶ в оверлее / двойной клик / F / F11 → переключить fullscreen
#   — Escape → выйти из fullscreen

import time
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QSizePolicy, QPushButton, QFrame, QSlider,
                             QGraphicsOpacityEffect)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from PyQt6.QtCore import (Qt, pyqtSlot, QSize, QRect, pyqtSignal,
                          QTimer, QEvent, QPoint, QPropertyAnimation,
                          QEasingCurve)
from PyQt6.QtGui import QImage, QPainter, QColor, QFont, QIcon, QLinearGradient

from config import resource_path

# ВАЖНО: QSurfaceFormat.setDefaultFormat() вызывается в client_main.py
# ДО создания QApplication. Здесь его быть НЕ должно — иначе краш 0xC0000409.

# Таймаут авто-скрытия оверлея и курсора (мс)
_HIDE_TIMEOUT_MS = 3000

# Высота оверлей-панели
_OVERLAY_H = 60


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
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
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
        """Рисует кадр с сохранением пропорций (letterbox / pillarbox)."""
        img = self._current_image
        img_w, img_h = img.width(), img.height()
        if img_w <= 0 or img_h <= 0:
            return
        scale = min(w / img_w, h / img_h)
        dest_w = int(img_w * scale)
        dest_h = int(img_h * scale)
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
    quality_changed    = pyqtSignal(int)         # skip_factor: 1, 2, 4

    # Циклические уровни качества: (skip_factor, emoji-метка, tooltip)
    _QUALITY_LEVELS = [
        (1, "🎯", "Высокое HD (1280×720, ~30fps)"),
        (2, "⚡", "Среднее SD (640×360, ~15fps, меньше трафика)"),
        (4, "📉", "Низкое SD (640×360, ~15fps, минимум трафика)"),
    ]

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setMouseTracking(True)
        self._quality_idx = 0   # текущий индекс в _QUALITY_LEVELS (по умолчанию HIGH)

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
        self.btn_stop = self._make_btn("assets/icon/stream_off.svg", "Прекратить просмотр")
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

        # --- Разделитель ---
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("QFrame { color: rgba(255,255,255,50); }")
        sep.setFixedWidth(2)
        sep.setFixedHeight(32)

        # --- Качество видео (цикличная кнопка: HIGH → MEDIUM → LOW → HIGH) ---
        self.btn_quality = self._make_btn(None, self._QUALITY_LEVELS[0][2])
        self.btn_quality.setText(self._QUALITY_LEVELS[0][1])
        self.btn_quality.setFont(QFont("Segoe UI", 14))
        self.btn_quality.clicked.connect(self._cycle_quality)

        # --- Fullscreen ---
        self.btn_fs = self._make_btn(None, "Полный экран / Оконный режим")
        self.btn_fs.setText("⛶")
        self.btn_fs.setFont(QFont("Segoe UI", 16))
        self.btn_fs.clicked.connect(self.fullscreen_clicked)

        layout.addWidget(self.btn_mute)
        layout.addWidget(self.btn_deafen)
        layout.addWidget(self.btn_stop)
        layout.addWidget(self.btn_vol_stream)
        layout.addWidget(self.btn_quality)
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

    def set_fullscreen_icon(self, is_fullscreen: bool):
        """Переключить иконку кнопки fullscreen."""
        self.btn_fs.setText("❐" if is_fullscreen else "⛶")

    def _cycle_quality(self):
        """Циклически переключает качество HIGH → MEDIUM → LOW → HIGH и эмитит сигнал."""
        self._quality_idx = (self._quality_idx + 1) % len(self._QUALITY_LEVELS)
        skip, emoji, tip = self._QUALITY_LEVELS[self._quality_idx]
        self.btn_quality.setText(emoji)
        self.btn_quality.setToolTip(tip)
        self.quality_changed.emit(skip)

    def set_quality_by_skip(self, skip_factor: int):
        """Синхронизировать иконку кнопки с внешне заданным skip_factor."""
        for idx, (sf, emoji, tip) in enumerate(self._QUALITY_LEVELS):
            if sf == skip_factor:
                self._quality_idx = idx
                self.btn_quality.setText(emoji)
                self.btn_quality.setToolTip(tip)
                break


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

    # --- Качество видео ---
    # Эмитит skip_factor при смене пользователем качества (1/2/4).
    # MainWindow подключает к net.send_quality_request().
    quality_changed        = pyqtSignal(int)

    # Эмитит при необходимости получить свежий IDR-кадр (в low-quality режиме).
    # MainWindow подключает к net.request_viewer_keyframe(streamer_uid).
    viewer_keyframe_needed = pyqtSignal()

    def __init__(self, nick: str):
        super().__init__()
        self.uid: int | None = None
        self._nick = nick
        self._frame_count = 0
        self._fps_count = 0
        self._fps_last_time = time.monotonic()
        self._current_fps = 0.0
        self._is_fullscreen = False
        self._closing = False        # флаг: окно в процессе закрытия
        self._quality_skip = 1       # текущий skip_factor (1=HIGH, 2=MED, 4=LOW)

        self._setup_ui(nick)
        self._setup_hide_timer()

        # IDR-таймер: в режимах MEDIUM/LOW периодически запрашиваем I-frame,
        # чтобы P-frame артефакты от пропущенных кадров очищались регулярно.
        # При HIGH (skip=1) таймер не запущен — пакеты не пропускаются.
        from config import VIDEO_LOW_QUALITY_IDR_INTERVAL_MS
        self._idr_timer = QTimer(self)
        self._idr_timer.setSingleShot(False)
        self._idr_timer.setInterval(VIDEO_LOW_QUALITY_IDR_INTERVAL_MS)
        self._idr_timer.timeout.connect(self.viewer_keyframe_needed)

    # ------------------------------------------------------------------
    # Построение UI
    # ------------------------------------------------------------------
    def _setup_ui(self, nick: str):
        self.setWindowTitle(f"Стрим: {nick}")
        self.resize(1280, 720)
        self.setMinimumSize(640, 360)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Контейнер для видео + оверлея (нужен для абсолютного позиционирования) ---
        self._video_container = QWidget(self)
        self._video_container.setMouseTracking(True)
        self._video_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self._video_container, stretch=1)

        # OpenGL-поверхность заполняет весь контейнер
        self.surface = VideoSurface(self._video_container)
        self.surface.fullscreen_requested.connect(self.toggle_fullscreen)

        # Оверлей поверх видео (абсолютное позиционирование внутри контейнера)
        self.overlay = VideoOverlay(self._video_container)
        self.overlay.mute_clicked.connect(self.overlay_mute_toggled)
        self.overlay.deafen_clicked.connect(self.overlay_deafen_toggled)
        self.overlay.stop_watch_clicked.connect(self._on_overlay_stop)
        self.overlay.fullscreen_clicked.connect(self.toggle_fullscreen)
        self.overlay.stream_volume_changed.connect(self.overlay_stream_volume_changed)
        self.overlay.quality_changed.connect(self._on_quality_changed)
        self.overlay.hide()  # скрыт по умолчанию

        # --- Тулбар статистики (снизу) ---
        self._bar = QWidget(self)
        self._bar.setFixedHeight(28)
        self._bar.setStyleSheet("background: #1a1a2e;")
        bar_layout = QHBoxLayout(self._bar)
        bar_layout.setContentsMargins(8, 0, 8, 0)
        bar_layout.setSpacing(0)

        lbl_style = "color: #8888aa; padding: 0 10px; font-size: 11px;"

        self._lbl_fps      = QLabel("FPS: —")
        self._lbl_res      = QLabel("Res: —")
        self._lbl_frames   = QLabel("Frames: 0")
        self._lbl_renderer = QLabel("🟢 OpenGL GPU")
        self._lbl_quality  = QLabel("Качество: 🎯 HD")

        for lbl in (self._lbl_fps, self._lbl_res, self._lbl_frames,
                    self._lbl_renderer, self._lbl_quality):
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
        self._reposition_overlay()

    def _reposition_surface(self):
        c = self._video_container
        self.surface.setGeometry(0, 0, c.width(), c.height())

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
    # Перехват mouseMoveEvent со всех дочерних виджетов через eventFilter
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        # Устанавливаем фильтр на все виджеты, которые могут «поглощать» move
        for w in (self.surface, self.overlay, self._video_container):
            w.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseMove:
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
        self.overlay.set_fullscreen_icon(True)
        self.showFullScreen()

    def _exit_fullscreen(self):
        self._is_fullscreen = False
        self._bar.show()
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

    # ------------------------------------------------------------------
    # Управление качеством видео
    # ------------------------------------------------------------------
    def _on_quality_changed(self, skip_factor: int):
        """
        Вызывается при нажатии кнопки качества в оверлее.
        Обновляет внутреннее состояние, метку тулбара, IDR-таймер,
        затем пробрасывает skip_factor наружу через quality_changed.
        """
        self._quality_skip = skip_factor
        labels = {1: "🎯 HD", 2: "⚡ SD", 4: "📉 LQ"}
        self._lbl_quality.setText(f"Качество: {labels.get(skip_factor, str(skip_factor))}")

        # IDR-таймер: включаем при любом снижении качества
        if skip_factor > 1:
            self._idr_timer.start()
        else:
            self._idr_timer.stop()

        # Немедленный IDR-запрос: зритель сразу получит чистый I-frame
        self.viewer_keyframe_needed.emit()
        # Уведомляем MainWindow → net.send_quality_request()
        self.quality_changed.emit(skip_factor)

    # ------------------------------------------------------------------
    # Публичный метод синхронизации состояния аудио с иконками оверлея
    # ------------------------------------------------------------------
    def sync_audio_state(self, is_muted: bool, is_deafened: bool):
        """
        Вызывать из MainWindow при изменении AudioHandler.is_muted / is_deafened,
        чтобы иконки в оверлее отражали реальное состояние.
        """
        # Защита от гонки: сигнал может прийти уже после того, как Qt
        # уничтожил C++ объекты (окно закрыто, но Python-лямбда ещё жива).
        if self._closing:
            return
        try:
            self.overlay.sync_mute_state(is_muted)
            self.overlay.sync_deafen_state(is_deafened)
        except RuntimeError:
            # C++ объект уже удалён — игнорируем
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
            self._fps_count += 1

            now = time.monotonic()
            elapsed = now - self._fps_last_time
            if elapsed >= 1.0:
                self._current_fps = self._fps_count / elapsed
                self._fps_count = 0
                self._fps_last_time = now

                self._lbl_fps.setText(f"FPS: {self._current_fps:.1f}")
                self._lbl_res.setText(f"Res: {q_img.width()}×{q_img.height()}")
                self._lbl_frames.setText(f"Frames: {self._frame_count}")

        except Exception as e:
            print(f"[VideoWindow] Error updating frame: {e}")

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        """Перехватываем закрытие окна, испускаем сигнал до уничтожения объекта."""
        self._closing = True         # блокируем sync_audio_state от внешних сигналов
        self._idr_timer.stop()       # останавливаем IDR-таймер
        self._hide_timer.stop()      # останавливаем таймер авто-скрытия

        # FIX MEM: явно очищаем последний кадр (QImage = ~3.7 МБ для 1280×720 RGB).
        # Без этого _current_image держался до уничтожения VideoSurface объекта,
        # что при deleteLater() может произойти не сразу.
        # После очистки OpenGL текстура тоже освобождается при следующем paintGL().
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
        return QSize(1280, 748)  # 720 + 28px тулбар