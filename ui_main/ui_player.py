# ui_main/ui_player.py
import os

from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QPushButton,
                              QLabel, QSizePolicy, QWidget, QSlider)
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal, QPoint, QEvent
from PyQt6.QtGui import (QIcon, QPainter, QColor, QFontMetrics,
                          QLinearGradient, QPen, QFont, QGuiApplication)

from config import (
    resource_path,
    PLAYER_ICON_PREV, PLAYER_ICON_PLAY, PLAYER_ICON_PAUSE,
    PLAYER_ICON_NEXT, PLAYER_ICON_FOLDER,
)


def _find_icon(*names: str):
    """Ищет SVG в assets/icon по списку имён. Возвращает QIcon или None."""
    for name in names:
        if not name:
            continue
        path = resource_path(f"assets/icon/{name}")
        if os.path.exists(path):
            ico = QIcon(path)
            if not ico.isNull():
                return ico
    return None


class _MarqueeLabel(QWidget):
    """Подпись с бегущей строкой: если текст не влезает в ширину виджета,
    он плавно прокручивается по горизонтали. Фон прозрачный."""

    _GAP = 36

    def __init__(self, text: str = "", color: str = "#c8d0e0", parent=None):
        super().__init__(parent)
        self._text = text
        self._color = QColor(color)
        self._offset = 0
        self._text_w = 0
        self._needs_scroll = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._recalc()

    def setText(self, text: str):
        self._text = text or ""
        self._offset = 0
        self._recalc()
        self.update()

    def text(self) -> str:
        return self._text

    def _recalc(self):
        fm = QFontMetrics(self.font())
        self._text_w = fm.horizontalAdvance(self._text)
        self._needs_scroll = self.isVisible() and (self._text_w > self.width())
        if self._needs_scroll:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._offset = 0
            if self._timer.isActive():
                self._timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._recalc()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recalc()

    def _tick(self):
        self._offset += 1
        if self._offset >= self._text_w + self._GAP:
            self._offset = 0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(self._color)
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        y = (self.height() + fm.ascent() - fm.descent()) // 2

        if not self._needs_scroll:
            p.drawText(0, y, self._text)
            return

        x = -self._offset
        p.drawText(x, y, self._text)
        p.drawText(x + self._text_w + self._GAP, y, self._text)


class _SeekBar(QWidget):
    """
    Горизонтальная полоса перемотки. Клик/перетаскивание → перемотка
    к позиции (0.0 – 1.0 от длительности).
    """

    seeked = pyqtSignal(float)   # 0.0 – 1.0

    _H          = 6
    _HANDLE_R   = 6
    _TRACK_BG   = QColor(40, 42, 60, 200)
    _TRACK_FILL = QColor(91, 142, 245, 220)
    _HANDLE     = QColor(255, 255, 255)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(20)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        self._ratio   = 0.0
        self._dragging = False

    def set_ratio(self, r: float) -> None:
        r = max(0.0, min(1.0, float(r)))
        if self._dragging:
            return
        if abs(self._ratio - r) > 0.0005:
            self._ratio = r
            self.update()

    def set_interactive(self, enabled: bool) -> None:
        self.setEnabled(enabled)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled
            else Qt.CursorShape.ForbiddenCursor
        )

    def _x_to_ratio(self, x: int) -> float:
        w = max(self.width(), 1)
        return max(0.0, min(1.0, x / w))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._dragging = True
            self._ratio = self._x_to_ratio(e.position().toPoint().x())
            self.update()
            self.seeked.emit(self._ratio)
            e.accept()

    def mouseMoveEvent(self, e):
        if self._dragging and (e.buttons() & Qt.MouseButton.LeftButton):
            self._ratio = self._x_to_ratio(e.position().toPoint().x())
            self.update()
            self.seeked.emit(self._ratio)
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._dragging:
            self._dragging = False
            self.seeked.emit(self._ratio)
        e.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        cy = h // 2
        track_h = self._H
        track_y = cy - track_h // 2

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._TRACK_BG)
        p.drawRoundedRect(0, track_y, w, track_h, track_h // 2, track_h // 2)

        fill_w = int(w * self._ratio)
        if fill_w > 0:
            grad = QLinearGradient(0, track_y, fill_w, track_y)
            grad.setColorAt(0.0, QColor(70, 110, 220, 230))
            grad.setColorAt(1.0, QColor(120, 160, 255, 240))
            p.setBrush(grad)
            p.drawRoundedRect(0, track_y, fill_w, track_h, track_h // 2, track_h // 2)

        hx = fill_w
        p.setBrush(self._HANDLE)
        p.drawEllipse(
            hx - self._HANDLE_R, cy - self._HANDLE_R,
            self._HANDLE_R * 2, self._HANDLE_R * 2,
        )

        p.end()


class PlayerVolumePopup(QFrame):
    """
    Попап-слайдер громкости плеера. Сделано top-level окно (Qt.Tool +
    FramelessWindowHint + WindowStaysOnTopHint), чтобы не обрезалось
    главным окном и было видно поверх всех виджетов.
    """

    volume_changed = pyqtSignal(float)   # 0.0 – 2.0

    _FILL_COLOR   = QColor(88, 101, 242)
    _TRACK_COLOR  = QColor(60, 60, 80)
    _HANDLE_COLOR = QColor(255, 255, 255)
    _HANDLE_R     = 7

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)

        self.setFixedSize(36, 140)

        self.setStyleSheet("""
            PlayerVolumePopup {
                background-color: rgba(18, 18, 32, 240);
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,40);
            }
        """)

        self._value    = 0.70   # 0.0–2.0
        self._dragging = False
        self._pad_top    = 14
        self._pad_bottom = 14

    def set_value(self, v: float):
        self._value = max(0.0, min(2.0, float(v)))
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

        ratio   = self._value / 2.0
        fill_h  = int(track_h * ratio)
        fill_y  = track_bottom - fill_h

        grad = QLinearGradient(0, fill_y, 0, track_bottom)
        grad.setColorAt(0.0, QColor(120, 135, 255))
        grad.setColorAt(1.0, self._FILL_COLOR)
        p.setBrush(grad)
        p.drawRoundedRect(track_x, fill_y, track_w, fill_h, 3, 3)

        handle_y = fill_y - self._HANDLE_R
        p.setBrush(self._HANDLE_COLOR)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - self._HANDLE_R, handle_y,
                      self._HANDLE_R * 2, self._HANDLE_R * 2)

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
            self._value = self._y_to_value(int(event.position().y()))
            self.update()
            self.volume_changed.emit(self._value)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._value = self._y_to_value(int(event.position().y()))
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


class PlayerBar(QFrame):
    """Полоса общего плеера."""

    prev_clicked       = pyqtSignal()
    play_pause_clicked = pyqtSignal()
    next_clicked       = pyqtSignal()
    browse_clicked     = pyqtSignal()
    volume_changed     = pyqtSignal(float)   # 0.0 – 2.0
    seek_changed       = pyqtSignal(float)   # 0.0 – 1.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("playerBar")
        self.setFixedHeight(52)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        self.btn_prev = self._make_btn(
            "⏮", "Предыдущий трек",
            PLAYER_ICON_PREV, "prev.svg", "previous.svg",
            "back.svg", "left.svg", "player_prev.svg",
        )
        self.btn_play = self._make_btn(
            "▶", "Воспроизвести / Пауза",
            PLAYER_ICON_PLAY, "play.svg", "player_play.svg",
        )
        self.btn_next = self._make_btn(
            "⏭", "Следующий трек",
            PLAYER_ICON_NEXT, "next.svg", "forward.svg",
            "right.svg", "player_next.svg",
        )
        self.btn_vol = self._make_btn(
            "🔊", "Громкость плеера",
            "volume_on.svg", "player_volume.svg", "speaker.svg",
        )
        self.btn_browse = self._make_btn(
            "📂", "Выбрать папку с музыкой",
            PLAYER_ICON_FOLDER, "folder.svg", "open.svg",
            "browse.svg", "music.svg", "player_folder.svg",
        )

        # ── Попап громкости — top-level окно, БЕЗ родителя ─────────────
        # parent=None обязательно: Qt.Tool с родителем ведёт себя как
        # дочерний виджет (обрезается, скрывается вместе с родителем).
        self._vol_popup = PlayerVolumePopup(None)
        self._vol_popup.setVisible(False)
        self._vol_popup.volume_changed.connect(self._on_vol_changed)

        self._vol_hide_timer = QTimer(self)
        self._vol_hide_timer.setSingleShot(True)
        self._vol_hide_timer.setInterval(600)
        self._vol_hide_timer.timeout.connect(self._hide_vol_popup)

        self.btn_vol.clicked.connect(self._toggle_vol_popup)
        self.btn_vol.installEventFilter(self)
        self._vol_popup.installEventFilter(self)

        # ── Название трека + перемотка (столбиком) ─────────────────────
        mid = QWidget()
        mid_lay = QVBoxLayout(mid)
        mid_lay.setContentsMargins(4, 0, 4, 0)
        mid_lay.setSpacing(0)

        self.lbl_track = _MarqueeLabel("Трек не выбран", color="#c8d0e0")
        self.lbl_track.setStyleSheet(
            "font-size:12px; background:transparent; border:none;")
        mid_lay.addWidget(self.lbl_track)

        self.seek_bar = _SeekBar()
        self.seek_bar.seeked.connect(self._on_seek)
        mid_lay.addWidget(self.seek_bar)

        lay.addWidget(self.btn_prev)
        lay.addWidget(self.btn_play)
        lay.addWidget(self.btn_next)
        lay.addWidget(self.btn_vol)
        lay.addWidget(mid, stretch=1)
        lay.addWidget(self.btn_browse)

        self.btn_prev.clicked.connect(self.prev_clicked)
        self.btn_play.clicked.connect(self.play_pause_clicked)
        self.btn_next.clicked.connect(self.next_clicked)
        self.btn_browse.clicked.connect(self.browse_clicked)

        self._is_playing = False
        self._apply_styles()

    # ────────────────────────────────────────────────────────────────────
    def _make_btn(self, fallback: str, tooltip: str, *icon_names: str) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("playerBtn")
        btn.setFixedSize(38, 38)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        ico = _find_icon(*icon_names)
        if ico is not None:
            btn.setIcon(ico)
            btn.setIconSize(QSize(22, 22))
        else:
            btn.setText(fallback)
        return btn

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            QFrame#playerBar {
                background-color: rgba(10, 12, 22, 255);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px;
            }
            QPushButton#playerBtn {
                background-color: rgba(255,255,255,0.14);
                border: 1px solid rgba(255,255,255,0.20);
                border-radius: 8px;
                color: #c8d0e0;
                font-size: 14px;
                font-weight: 600;
                padding: 0;
            }
            QPushButton#playerBtn:hover {
                background-color: rgba(255,255,255,0.24);
                border-color: rgba(91,142,245,0.55);
            }
            QPushButton#playerBtn:pressed {
                background-color: rgba(91,142,245,0.35);
                border-color: rgba(91,142,245,0.75);
            }
            QPushButton#playerBtn:disabled {
                background-color: rgba(255,255,255,0.05);
                color: rgba(200,208,224,0.35);
                border-color: rgba(255,255,255,0.08);
            }
        """)

    # ── Публичное API ───────────────────────────────────────────────────
    def set_playing(self, playing: bool) -> None:
        if self._is_playing == playing:
            return
        self._is_playing = playing
        if playing:
            ico = _find_icon(PLAYER_ICON_PAUSE, "pause.svg", "player_pause.svg")
        else:
            ico = _find_icon(PLAYER_ICON_PLAY, "play.svg", "player_play.svg")
        if ico is not None:
            self.btn_play.setIcon(ico)
            self.btn_play.setText("")
        else:
            self.btn_play.setText('⏸' if playing else '▶')

    def set_track_name(self, name: str) -> None:
        self.lbl_track.setText(name or "Трек не выбран")
        self.lbl_track.setToolTip(name or "")

    def set_owner_mode(self, is_owner: bool) -> None:
        self.btn_prev.setEnabled(is_owner)
        self.btn_next.setEnabled(is_owner)
        self.btn_vol.setEnabled(True)
        self.seek_bar.set_interactive(is_owner)

    def set_volume(self, vol: float) -> None:
        """vol: 0.0 – 2.0 (0% – 200%)"""
        v = max(0.0, min(2.0, float(vol)))
        self._vol_popup.set_value(v)
        if v == 0.0:
            icon_name = "volume_off.svg"
        else:
            icon_name = "volume_on.svg"
        ico = _find_icon(icon_name, "volume_on.svg", "player_volume.svg")
        if ico is not None:
            self.btn_vol.setIcon(ico)
            self.btn_vol.setText("")
        else:
            self.btn_vol.setText("🔇" if v == 0.0 else "🔊")

    def set_position(self, ratio: float) -> None:
        self.seek_bar.set_ratio(ratio)

    # ── Внутренние слоты ────────────────────────────────────────────────
    def _toggle_vol_popup(self):
        print(f"[PlayerVolume] click; popup exists={self._vol_popup is not None}, "
              f"visible={self._vol_popup.isVisible() if self._vol_popup else None}")
        if self._vol_popup.isVisible():
            self._hide_vol_popup()
        else:
            self._show_vol_popup()

    def _show_vol_popup(self):
        self._vol_hide_timer.stop()

        try:
            btn_global = self.btn_vol.mapToGlobal(QPoint(0, 0))
        except Exception:
            btn_global = QPoint(0, 0)

        popup_w = self._vol_popup.width()
        popup_h = self._vol_popup.height()

        x = btn_global.x() + (self.btn_vol.width() - popup_w) // 2
        y = btn_global.y() - popup_h - 8

        screen = QGuiApplication.screenAt(btn_global)
        if screen:
            g = screen.availableGeometry()
            if y < g.top() + 4:
                y = btn_global.y() + self.btn_vol.height() + 8
            if x + popup_w > g.right() - 4:
                x = g.right() - popup_w - 4
            if x < g.left() + 4:
                x = g.left() + 4

        self._vol_popup.move(x, y)
        self._vol_popup.raise_()
        self._vol_popup.show()
        print(f"[PlayerVolume] show at ({x},{y}) size={popup_w}x{popup_h} "
              f"visible={self._vol_popup.isVisible()}")

    def _hide_vol_popup(self):
        self._vol_popup.setVisible(False)

    def _on_vol_changed(self, v: float):
        if v == 0.0:
            ico = _find_icon("volume_off.svg")
        else:
            ico = _find_icon("volume_on.svg", "player_volume.svg")
        if ico is not None:
            self.btn_vol.setIcon(ico)
        self.volume_changed.emit(v)

    def _on_seek(self, ratio: float):
        self.seek_changed.emit(ratio)

    def eventFilter(self, obj, event):
        t = event.type()
        if obj in (self.btn_vol, self._vol_popup):
            if t == QEvent.Type.Enter:
                self._vol_hide_timer.stop()
                if obj == self.btn_vol:
                    self._show_vol_popup()
            elif t == QEvent.Type.Leave:
                self._vol_hide_timer.start()
        return False