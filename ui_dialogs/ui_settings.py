import os
import json
import wave
import sounddevice as sd
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
                             QWidget, QLabel, QSlider, QTabWidget, QComboBox, QFrame,
                             QGroupBox, QSizePolicy, QFileDialog, QMessageBox,
                             QLineEdit, QCheckBox, QProgressBar, QListWidget, QListWidgetItem,
                             QAbstractItemView)
from PyQt6.QtCore import (Qt, QSize, QSettings, QTimer, pyqtSignal, QObject, QPoint)
from PyQt6.QtGui import QIcon, QPainter, QColor, QPen, QStandardItem, QPolygon

from config import (resource_path, USER_CONFIG_PATH, KNOWN_USERS_PATH)
from audio_engine import PYRNNOISE_AVAILABLE
from version import APP_VERSION, APP_NAME, ABOUT_TEXT, GITHUB_REPO
from .ui_dialogs import (
    _DialogTitleBar,
    AvatarSelector,
    CUSTOM_SOUND_MAX_BYTES,
    CUSTOM_SOUND_SLOTS,
)

class MicVadWidget(QWidget):
    threshold_changed = pyqtSignal(int)  # slider_val 1-50

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0
        self._smooth_level = 0.0
        self._threshold_pos = 10
        self._dragging = False
        self.setMinimumHeight(32)
        self.setMaximumHeight(32)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_level(self, val: int):
        target = max(0, min(100, val))
        if target > self._smooth_level:
            self._smooth_level = self._smooth_level * 0.3 + target * 0.7
        else:
            self._smooth_level = self._smooth_level * 0.85 + target * 0.15
        self._level = int(self._smooth_level)
        self.update()

    def set_threshold(self, slider_val: int):
        self._threshold_pos = max(0, min(100, slider_val * 2))
        self.update()

    def _pos_to_slider(self, x: int) -> int:
        ratio = max(0.0, min(1.0, x / max(1, self.width())))
        return max(1, min(50, int(ratio * 50)))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            sv = self._pos_to_slider(int(e.position().x()))
            self.set_threshold(sv)
            self.threshold_changed.emit(sv)

    def mouseMoveEvent(self, e):
        if self._dragging:
            sv = self._pos_to_slider(int(e.position().x()))
            self.set_threshold(sv)
            self.threshold_changed.emit(sv)

    def mouseReleaseEvent(self, e):
        self._dragging = False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.setBrush(QColor(35, 38, 52))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, w, h, 6, 6)

        bar_w = int(self._level / 100.0 * w)
        if bar_w > 0:
            if self._level < self._threshold_pos:
                bar_color = QColor(180, 80, 70, 180)
            else:
                bar_color = QColor(80, 180, 120, 200)
            p.setBrush(bar_color)
            p.drawRoundedRect(0, 0, bar_w, h, 6, 6)

        tx = int(self._threshold_pos / 100.0 * w)
        pen = QPen(QColor(255, 100, 80, 200), 2)
        p.setPen(pen)
        p.drawLine(tx, 4, tx, h - 4)
        p.setBrush(QColor(255, 100, 80))
        p.setPen(Qt.PenStyle.NoPen)
        tri = [QPoint(tx - 4, 0), QPoint(tx + 4, 0), QPoint(tx, 6)]
        p.drawPolygon(QPolygon(tri))

        p.end()

class HotkeyCaptureEdit(QLineEdit):

    _WAIT_SS = (
        "QLineEdit {"
        "  background: rgba(100,60,200,0.22);"
        "  border: 1px solid rgba(130,80,230,0.70);"
        "  border-radius: 6px;"
        "  color: #c8b0ff;"
        "  padding: 4px 8px;"
        "}"
    )
    _FILLED_SS = (
        "QLineEdit {"
        "  background: rgba(46,204,113,0.12);"
        "  border: 1px solid rgba(46,204,113,0.45);"
        "  border-radius: 6px;"
        "  color: #82e0aa;"
        "  padding: 4px 8px;"
        "}"
    )
    _EMPTY_SS = (
        "QLineEdit {"
        "  background: rgba(255,255,255,0.06);"
        "  border: 1px solid rgba(255,255,255,0.13);"
        "  border-radius: 6px;"
        "  color: #c8d0e0;"
        "  padding: 4px 8px;"
        "}"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._capturing = False
        self._prev_value = ""
        self.setReadOnly(True)
        self.setPlaceholderText("Кликни для задания клавиши")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(self._EMPTY_SS)
        self.setMinimumWidth(180)
        self.setFixedHeight(30)

    def set_hotkey(self, text: str):
        self._prev_value = text
        self.setText(text)
        self.setStyleSheet(self._FILLED_SS if text else self._EMPTY_SS)

    def get_hotkey(self) -> str:
        return self.text()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_capture()
        super().mousePressEvent(event)

    def _start_capture(self):
        self._prev_value = self.text()
        self._capturing = True
        self.setText("")
        self.setPlaceholderText("Нажми клавишу…")
        self.setStyleSheet(self._WAIT_SS)
        self.setFocus()

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return

        key = event.key()

        if key == Qt.Key.Key_Escape:
            self._capturing = False
            self.setText(self._prev_value)
            self.setPlaceholderText("Кликни для задания клавиши")
            self.setStyleSheet(self._FILLED_SS if self._prev_value else self._EMPTY_SS)
            self.clearFocus()
            return

        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
                   Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return

        mods = event.modifiers()
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("shift")

        key_name = self._key_to_str(key)
        if key_name:
            parts.append(key_name)

        combo = "+".join(parts) if parts else ""
        self._capturing = False
        self.setText(combo)
        self.setPlaceholderText("Кликни для задания клавиши")
        self.setStyleSheet(self._FILLED_SS if combo else self._EMPTY_SS)
        self.clearFocus()

    def focusOutEvent(self, event):
        if self._capturing:
            self._capturing = False
            self.setText(self._prev_value)
            self.setPlaceholderText("Кликни для задания клавиши")
            self.setStyleSheet(self._FILLED_SS if self._prev_value else self._EMPTY_SS)
        super().focusOutEvent(event)

    @staticmethod
    def _key_to_str(key: int) -> str:
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key).lower()
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            return chr(key)
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            n = key - Qt.Key.Key_F1 + 1
            return f"f{n}"
        _MAP = {
            Qt.Key.Key_Space:       "space",
            Qt.Key.Key_Return:      "enter",
            Qt.Key.Key_Enter:       "enter",
            Qt.Key.Key_Tab:         "tab",
            Qt.Key.Key_Backspace:   "backspace",
            Qt.Key.Key_Delete:      "delete",
            Qt.Key.Key_Insert:      "insert",
            Qt.Key.Key_Home:        "home",
            Qt.Key.Key_End:         "end",
            Qt.Key.Key_PageUp:      "page up",
            Qt.Key.Key_PageDown:    "page down",
            Qt.Key.Key_Left:        "left",
            Qt.Key.Key_Right:       "right",
            Qt.Key.Key_Up:          "up",
            Qt.Key.Key_Down:        "down",
            Qt.Key.Key_BracketLeft:  "[",
            Qt.Key.Key_BracketRight: "]",
            Qt.Key.Key_Semicolon:   ";",
            Qt.Key.Key_Apostrophe:  "'",
            Qt.Key.Key_Comma:       ",",
            Qt.Key.Key_Period:      ".",
            Qt.Key.Key_Slash:       "/",
            Qt.Key.Key_Backslash:   "\\",
            Qt.Key.Key_Minus:       "-",
            Qt.Key.Key_Equal:       "=",
            Qt.Key.Key_QuoteLeft:   "`",
            Qt.Key.Key_NumLock:     "num lock",
            Qt.Key.Key_ScrollLock:  "scroll lock",
            Qt.Key.Key_CapsLock:    "caps lock",
            Qt.Key.Key_Print:       "print screen",
            Qt.Key.Key_Pause:       "pause",
        }
        return _MAP.get(key, "")

class SettingsDialog(QDialog):
    def __init__(self, audio_engine, parent):
        super().__init__(parent)
        self.audio = audio_engine
        self.mw = parent
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Настройки")
        self.resize(780, 660)
        self.setMinimumSize(480, 520)

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self._card = QFrame(self)
        self._card.setObjectName("settingsCard")
        self._card.setStyleSheet("""
            QFrame#settingsCard {
                background-color: rgba(26, 28, 38, 252);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }
            QLabel {
                color: #c8d0e0;
                background: transparent;
                border: none;
            }
            QGroupBox {
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                color: #8899bb;
                font-weight: bold;
            }
            QComboBox {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.13);
                border-radius: 6px;
                padding: 5px 10px;
                color: #c8d0e0;
            }
            QComboBox QAbstractItemView {
                background-color: #1e2130;
                color: #c8d0e0;
                border: 1px solid #333648;
                selection-background-color: #2c3252;
                selection-color: #ffffff;
                outline: none;
            }
            QComboBox::drop-down { border: none; }
            QLineEdit {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.13);
                border-radius: 6px;
                padding: 5px 10px;
                color: #c8d0e0;
            }
            QCheckBox { color: #c8d0e0; background: transparent; }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border: 1px solid rgba(255,255,255,0.20);
                border-radius: 4px;
                background: rgba(255,255,255,0.06);
            }
            QCheckBox::indicator:checked {
                background: #5b8ef5;
                border-color: #5b8ef5;
            }
            QSlider::groove:horizontal {
                height: 5px;
                background: rgba(255,255,255,0.12);
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 14px; height: 14px;
                margin: -5px 0;
                background: #5b8ef5;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal {
                background: #5b8ef5;
                border-radius: 2px;
            }
            QTabWidget::pane {
                border: 1px solid rgba(255,255,255,0.10);
                background-color: rgba(255,255,255,0.03);
                border-radius: 6px;
            }
            QTabBar::tab {
                background-color: rgba(255,255,255,0.05);
                color: #8899bb;
                padding: 8px 16px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                margin-right: 2px;
                border: 1px solid rgba(255,255,255,0.07);
                border-bottom: none;
            }
            QTabBar::tab:selected {
                background-color: rgba(255,255,255,0.10);
                color: #cdd6f4;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background-color: rgba(255,255,255,0.08);
                color: #aabbcc;
            }
            QTabBar::scroller { width: 20px; }
            QTabBar QToolButton {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 4px;
                color: #cccccc;
            }
            QTabBar QToolButton:hover { background: rgba(255,255,255,0.14); }
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 6px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.13);
                border-color: rgba(255,255,255,0.22);
            }
            QPushButton:checked {
                background-color: rgba(220,60,60,0.35);
                border-color: rgba(220,60,60,0.6);
                color: #ff9090;
            }
            #btn_nr { background-color: rgba(214,93,78,0.30); color: #ff9090; }
            #btn_nr:checked { background-color: rgba(39,174,96,0.30); color: #82e0aa; }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.04);
                width: 6px; border-radius: 3px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 3px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: rgba(255,255,255,0.04);
                height: 6px; border-radius: 3px; margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: rgba(255,255,255,0.18);
                border-radius: 3px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
            QScrollArea { background: transparent; border: none; }
            QFrame[frameShape="4"], QFrame[frameShape="5"] {
                background: rgba(255,255,255,0.08);
                border: none;
                max-height: 1px;
            }
            QProgressBar {
                background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 4px;
                color: #c8d0e0;
                text-align: center;
            }
            QProgressBar::chunk {
                background: #5b8ef5;
                border-radius: 3px;
            }
        """)
        root_lay.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        self._title_bar = _DialogTitleBar(self, "⚙  Настройки")
        card_lay.addWidget(self._title_bar)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setFixedHeight(1)
        _sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(_sep)

        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        content_lay = QVBoxLayout(content_w)
        content_lay.setContentsMargins(16, 14, 16, 14)
        content_lay.setSpacing(10)
        card_lay.addWidget(content_w, stretch=1)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)

        self.setup_profile_tab()

        self.setup_audio_tab()

        self.setup_personalization_tab()

        self.setup_soundboard_tab()

        self.setup_version_tab()

        content_lay.addWidget(self.tabs)

        btn_save = QPushButton("✔  Сохранить")
        btn_save.setStyleSheet("""
            QPushButton {
                background-color: rgba(46,204,113,0.25);
                color: #82e0aa;
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 7px;
                padding: 8px 20px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(46,204,113,0.40);
                border-color: rgba(46,204,113,0.75);
                color: #ffffff;
            }
        """)
        btn_save.clicked.connect(self.save_all)
        content_lay.addWidget(btn_save)

        QTimer.singleShot(0, self._fix_combo_popups)

    def setup_profile_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)
        top_row.setContentsMargins(0, 0, 0, 0)

        av_col = QVBoxLayout()
        av_col.setSpacing(6)
        av_col.setContentsMargins(0, 0, 0, 0)

        self.av_lbl = QLabel()
        self.av_lbl.setFixedSize(96, 96)
        self.av_lbl.setStyleSheet(
            "border: 2px solid rgba(255,255,255,0.18); "
            "border-radius: 10px; background: rgba(0,0,0,0.20);"
        )
        self.av_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cur_av = self.mw.avatar
        self.upd_av_preview()
        av_col.addWidget(self.av_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        btn_ch = QPushButton("Изменить")
        btn_ch.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ch.setFixedWidth(96)
        btn_ch.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.06);
                color: #d0d8f0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(91,142,245,0.18);
                border-color: rgba(91,142,245,0.45);
                color: #ffffff;
            }
        """)
        btn_ch.clicked.connect(self.open_av_sel)
        av_col.addWidget(btn_ch, alignment=Qt.AlignmentFlag.AlignHCenter)
        av_col.addStretch()

        top_row.addLayout(av_col)

        nick_col = QVBoxLayout()
        nick_col.setSpacing(6)
        nick_col.setContentsMargins(0, 2, 0, 0)

        lbl_nick = QLabel("Никнейм")
        lbl_nick.setStyleSheet("color: rgba(200,200,210,0.8); font-size: 12px;")
        nick_col.addWidget(lbl_nick)

        self.ed_nick = QLineEdit(self.mw.nick)
        self.ed_nick.setStyleSheet("""
            QLineEdit {
                background-color: rgba(0,0,0,0.25);
                color: #e8ecf5;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 6px;
                padding: 7px 10px;
                font-size: 13px;
                selection-background-color: rgba(91,142,245,0.45);
            }
            QLineEdit:focus {
                border-color: rgba(91,142,245,0.70);
                background-color: rgba(0,0,0,0.35);
            }
        """)
        nick_col.addWidget(self.ed_nick)
        nick_col.addStretch()

        top_row.addLayout(nick_col, 1)

        lay.addLayout(top_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none; max-height: 1px;")
        sep.setMaximumHeight(1)
        lay.addWidget(sep)

        lbl_server = QLabel("Сервер")
        lbl_server.setStyleSheet(
            "color: rgba(200,200,210,0.8); font-size: 12px;"
        )
        lay.addWidget(lbl_server)

        from ui_dialogs.ui_dialogs import NudgeHoldButton
        btn_clear_cache = NudgeHoldButton("🗑  Удерживайте 3 сек — очистить кеш")
        btn_clear_cache.setToolTip("Удаляет локальную историю чата (SQLite)")
        try:
            from config import resource_path
            btn_clear_cache.set_hold_sound(resource_path("assets/music/bubble_progress.wav"))
        except Exception:
            pass
        btn_clear_cache.setStyleSheet("""
            QPushButton {
                background-color: rgba(231,76,60,0.15);
                color: #e88;
                border: 1px solid rgba(231,76,60,0.30);
                border-radius: 7px;
                padding: 8px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: rgba(231,76,60,0.30);
                border-color: rgba(231,76,60,0.60);
                color: #fff;
            }
        """)
        btn_clear_cache.hold_complete.connect(self._on_clear_server_cache)
        lay.addWidget(btn_clear_cache)

        self._ban_section_widgets: list = []

        self._ban_title = QLabel("Забаненные участники")
        self._ban_title.setStyleSheet(
            "color: rgba(200,200,210,0.8); font-size: 12px; margin-top: 4px;"
        )
        lay.addWidget(self._ban_title)
        self._ban_section_widgets.append(self._ban_title)

        self._ban_list_widget = QListWidget()
        self._ban_list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._ban_list_widget.setStyleSheet("""
            QListWidget {
                background-color: rgba(0,0,0,0.25);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 6px;
                color: #d0d8f0;
                padding: 4px;
                outline: none;
            }
            QListWidget::item {
                padding: 5px 6px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: rgba(91,142,245,0.30);
                color: #ffffff;
            }
            QListWidget::item:hover {
                background-color: rgba(255,255,255,0.05);
            }
        """)
        self._ban_list_widget.setMinimumHeight(96)
        self._ban_list_widget.setMaximumHeight(160)
        lay.addWidget(self._ban_list_widget)
        self._ban_section_widgets.append(self._ban_list_widget)
        lay.addSpacing(2)
        self._btn_unban = QPushButton("Разбанить выделенных")
        self._btn_unban.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_unban.setStyleSheet("""
            QPushButton {
                background-color: rgba(91,142,245,0.18);
                color: #8ab4f8;
                border: 1px solid rgba(91,142,245,0.35);
                border-radius: 6px;
                padding: 7px 14px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(91,142,245,0.32);
                border-color: rgba(91,142,245,0.70);
                color: #ffffff;
            }
            QPushButton:disabled {
                color: rgba(200,200,210,0.35);
                background-color: rgba(255,255,255,0.04);
                border-color: rgba(255,255,255,0.08);
            }
        """)
        self._btn_unban.clicked.connect(self._on_unban_selected_clicked)
        lay.addWidget(self._btn_unban)
        self._ban_section_widgets.append(self._btn_unban)

        self._refresh_ban_group_visibility()
        try:
            self.mw.net.ban_list_updated.connect(self._on_ban_list_updated)
        except Exception as _e:
            print(f"[Settings] ban_list_updated connect error: {_e}")

        if self._is_local_host():
            try:
                self.mw.net.request_ban_list()
            except Exception:
                pass

        lay.addStretch()
        self.tabs.addTab(tab, "Главное")

    def _is_local_host(self) -> bool:
        try:
            my_uid = getattr(self.mw.audio, 'my_uid', 0)
            srv_uid = getattr(self.mw.net, '_server_host_uid', 0)
            return bool(my_uid and my_uid == srv_uid)
        except Exception:
            return False

    def _refresh_ban_group_visibility(self) -> None:
        visible = self._is_local_host()
        for w in getattr(self, '_ban_section_widgets', ()):
            try:
                w.setVisible(visible)
            except Exception:
                pass

    def _on_ban_list_updated(self, entries: list) -> None:

        if not hasattr(self, '_ban_list_widget'):
            return
        self._ban_list_widget.clear()
        if not entries:
            placeholder = QListWidgetItem("— нет забаненных —")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)  # не выделяется
            self._ban_list_widget.addItem(placeholder)
            self._btn_unban.setEnabled(False)
            return

        self._btn_unban.setEnabled(True)
        for entry in entries:
            ip   = str(entry.get('ip',   '')).strip()
            nick = str(entry.get('nick', '')).strip()
            if not ip:
                continue
            display_nick = nick or '(без ника)'
            item = QListWidgetItem(f"{display_nick}    ·    {ip}")
            item.setData(Qt.ItemDataRole.UserRole, (ip, nick))
            self._ban_list_widget.addItem(item)

    def _on_unban_selected_clicked(self) -> None:
        selected = self._ban_list_widget.selectedItems()
        if not selected:
            return
        pairs: list = []
        for item in selected:
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                ip, nick = data
                if isinstance(ip, str) and ip:
                    pairs.append((ip, nick or ''))
        if not pairs:
            return
        try:
            for ip, nick in pairs:
                self.mw.net.send_host_unban(ip, nick)
            print(f"[Settings] Разбан: {len(pairs)} запис(ь/и)")
        except Exception as e:
            print(f"[Settings] Unban error: {e}")

    def _on_clear_server_cache(self):
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            srv = getattr(mgr, '_server', None)
            chat_db = getattr(srv, '_chat_db', None) if srv else None

            if chat_db is not None:
                chat_db.clear()
            else:
                from config import CHAT_DB_PATH
                import os
                for path in [CHAT_DB_PATH, CHAT_DB_PATH + '-wal', CHAT_DB_PATH + '-shm']:
                    if os.path.exists(path):
                        os.remove(path)
            print("[Settings] Кеш сервера очищен")
        except Exception as e:
            print(f"[Settings] Ошибка очистки: {e}")

    def setup_audio_tab(self):
        aud_tab = QWidget()
        aud_lay = QVBoxLayout(aud_tab)

        self.cb_in = QComboBox()
        self.cb_out = QComboBox()
        self.refresh_devices_list()

        nr_label = QLabel("🎛  Шумоподавление микрофона:")
        nr_label.setStyleSheet("font-weight: bold; font-size: 12px; margin-top: 6px;")

        self.cb_nr = QComboBox()
        self.cb_nr.setObjectName("cb_nr")
        self.cb_nr.setToolTip(
            "Выкл — без обработки\n"
            "RNNoise — лёгкое NN-подавление (низкая нагрузка)\n"
            "DeepFilterNet — глубокая фильтрация (средняя нагрузка)"
        )

        self.cb_nr.addItem("🔇  Выкл", 0)

        rnn_item_text = "🟢  RNNoise" if PYRNNOISE_AVAILABLE else "🔴  RNNoise (модуль не установлен)"
        self.cb_nr.addItem(rnn_item_text, 1)
        if not PYRNNOISE_AVAILABLE:
            model = self.cb_nr.model()
            item = model.item(1)
            if item:
                item.setEnabled(False)

        dfn_ok = getattr(self.audio, 'dfn_available', False)
        dfn_item_text = "🔵  DeepFilterNet" if dfn_ok else "🔴  DeepFilterNet (dll не найдена)"
        self.cb_nr.addItem(dfn_item_text, 2)
        if not dfn_ok:
            model = self.cb_nr.model()
            item = model.item(2)
            if item:
                item.setEnabled(False)

        saved_nr = getattr(self.audio, 'nr_mode', 0)
        idx = self.cb_nr.findData(saved_nr)
        if idx != -1:
            self.cb_nr.setCurrentIndex(idx)

        self.cb_nr.currentIndexChanged.connect(self._on_nr_mode_changed)

        aud_lay.addWidget(QLabel("Качество звука (Битрейт):"))
        self.cb_bitrate = QComboBox()
        bitrate_options = {
            "24 kbps (Рация)": 24,
            "48 kbps (Стандарт)": 48,
            "64 kbps (Хорошее)": 64,
            "128 kbps (Наилучшее)": 128
        }
        for text, val in bitrate_options.items():
            self.cb_bitrate.addItem(text, val)
        current_bitrate = int(self.app_settings.value("audio_bitrate", 64000)) // 1000
        index = self.cb_bitrate.findData(current_bitrate)
        if index != -1:
            self.cb_bitrate.setCurrentIndex(index)
        self.cb_bitrate.currentIndexChanged.connect(
            lambda: self.audio.set_bitrate(self.cb_bitrate.currentData())
        )
        aud_lay.addWidget(self.cb_bitrate)

        aud_lay.addWidget(QLabel("Ввод:"))
        aud_lay.addWidget(self.cb_in)
        aud_lay.addWidget(QLabel("Вывод:"))
        aud_lay.addWidget(self.cb_out)
        aud_lay.addWidget(nr_label)
        aud_lay.addWidget(self.cb_nr)

        aud_lay.addSpacing(10)
        mic_group = QGroupBox("🎙  Микрофон и порог активации (VAD)")
        mic_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        mic_lay = QVBoxLayout(mic_group)

        hint_lbl = QLabel(
            "Установите VAD ниже полосы голоса, чтобы передать ваш изумительный голос."
        )
        hint_lbl.setStyleSheet("font-size: 11px; color: #aaa; font-weight: normal;")
        hint_lbl.setWordWrap(True)
        mic_lay.addWidget(hint_lbl)

        self.mic_vad = MicVadWidget()
        self.audio.volume_level_signal.connect(self.mic_vad.set_level)
        mic_lay.addWidget(self.mic_vad)

        vad_slider_val = int(self.app_settings.value("vad_threshold_slider", 5))
        self._current_vad_val = vad_slider_val
        self.mic_vad.set_threshold(vad_slider_val)
        self.mic_vad.threshold_changed.connect(self._on_vad_slider_changed)

        aud_lay.addWidget(mic_group)

        aud_lay.addSpacing(8)

        sys_vol = int(self.app_settings.value("system_sound_volume", 30))
        self.lbl_sys = QLabel(f"Системные звуки: {sys_vol}%")
        self.sl_sys = QSlider(Qt.Orientation.Horizontal)
        self.sl_sys.setRange(0, 100)
        self.sl_sys.setValue(sys_vol)
        self.sl_sys.valueChanged.connect(lambda v: self.lbl_sys.setText(f"Системные звуки: {v}%"))
        aud_lay.addWidget(self.lbl_sys)
        aud_lay.addWidget(self.sl_sys)

        aud_lay.addStretch()
        self.tabs.addTab(aud_tab, "Аудио")

    def setup_personalization_tab(self):

        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setSpacing(10)
        outer.setContentsMargins(16, 16, 16, 16)

        hk_group = QGroupBox("🎹  Горячие клавиши")
        hk_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        hk_group_lay = QVBoxLayout(hk_group)
        hk_group_lay.setSpacing(8)
        hk_group_lay.setContentsMargins(10, 14, 10, 10)
        outer.addWidget(hk_group, stretch=1)

        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(4, 0, 36, 0)   # 36 = ширина кнопки «✕»
        hdr_row.setSpacing(8)
        lbl_func = QLabel("Действие")
        lbl_func.setStyleSheet("font-weight: bold; font-size: 12px;")
        lbl_key  = QLabel("Горячая клавиша (кликни для записи)")
        lbl_key.setStyleSheet("font-weight: bold; font-size: 12px;")
        hdr_row.addWidget(lbl_func, stretch=4)
        hdr_row.addWidget(lbl_key,  stretch=5)
        hk_group_lay.addLayout(hdr_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._hk_rows_container = QWidget()
        self._hk_rows_container.setStyleSheet("background: transparent;")
        self._hk_rows_layout = QVBoxLayout(self._hk_rows_container)
        self._hk_rows_layout.setSpacing(5)
        self._hk_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._hk_rows_layout.addStretch()   # прижимаем строки сверху
        scroll.setWidget(self._hk_rows_container)
        hk_group_lay.addWidget(scroll, stretch=1)

        self._btn_hk_add = QPushButton("＋  Добавить назначение")
        self._btn_hk_add.setStyleSheet("""
            QPushButton {
                background: rgba(88,101,242,0.20);
                color: #a0b0ff;
                border: 1px solid rgba(88,101,242,0.50);
                border-radius: 7px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background: rgba(88,101,242,0.38);
                color: #ffffff;
            }
            QPushButton:disabled {
                background: rgba(255,255,255,0.04);
                color: #555;
                border-color: rgba(255,255,255,0.08);
            }
        """)
        self._btn_hk_add.clicked.connect(self._add_hk_row)
        outer.addWidget(self._btn_hk_add, alignment=Qt.AlignmentFlag.AlignLeft)

        self._hk_rows: list[dict] = []

        self._load_hk_rows()

        self.tabs.addTab(tab, "Персонализация")

    def _build_function_options(self) -> list[tuple[str, str, str]]:

        opts: list[tuple[str, str, str]] = [
            ("— не задано —",                  "none",     ""),
            ("🎙  Замутить микрофон",           "mute_mic", ""),
            ("🔇  Замутить динамики (Deafen)",  "deafen",   ""),
        ]

        try:
            if os.path.exists(KNOWN_USERS_PATH):
                with open(KNOWN_USERS_PATH, "r", encoding="utf-8") as f:
                    registry: dict = json.load(f)
                users = sorted(
                    ((v.get("nick", ""), ip)
                     for ip, v in registry.items() if v.get("nick", "")),
                    key=lambda x: x[0].lower()
                )
                for nick, ip in users:
                    opts.append((f"🤫  Шёпот → {nick}", "whisper", ip))
        except Exception:
            pass

        s = self.app_settings
        for i in range(CUSTOM_SOUND_SLOTS):
            name = s.value(f"custom_sound_{i}_name", "")
            if name:
                opts.append((f"🎵  Звук: {name}", "sound", name))

        return opts

    def _add_hk_row(self, func_type: str = "none", func_data: str = "",
                    hotkey: str = "", anonymous: bool = False) -> None:
        MAX_ROWS = 7
        if len(self._hk_rows) >= MAX_ROWS:
            self._btn_hk_add.setEnabled(False)
            return

        opts = self._build_function_options()

        frame = QFrame()
        frame.setStyleSheet("""
            QFrame {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.09);
                border-radius: 8px;
            }
        """)
        row_lay = QHBoxLayout(frame)
        row_lay.setContentsMargins(8, 5, 8, 5)
        row_lay.setSpacing(8)

        cb = QComboBox()
        cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for text, ftype, fdata in opts:
            cb.addItem(text, (ftype, fdata))

        selected_idx = 0
        for j in range(cb.count()):
            d = cb.itemData(j)
            if d and d[0] == func_type and d[1] == func_data:
                selected_idx = j
                break
        cb.setCurrentIndex(selected_idx)

        def _fix_this_cb_popup(combo=cb):
            try:
                v = combo.view()
                v.setStyleSheet(
                    "QAbstractItemView {"
                    "  background-color: #1e2130;"
                    "  color: #c8d0e0;"
                    "  selection-background-color: #2c3252;"
                    "  selection-color: #ffffff;"
                    "  border: 1px solid #333648;"
                    "  outline: none;"
                    "}"
                )
                win = v.window()
                win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                win.setStyleSheet("background-color: #1e2130;")
            except Exception:
                pass
        QTimer.singleShot(0, _fix_this_cb_popup)

        hk_edit = HotkeyCaptureEdit()
        hk_edit.set_hotkey(hotkey)

        chk_anon = QCheckBox("👤")
        chk_anon.setChecked(bool(anonymous))
        chk_anon.setCursor(Qt.CursorShape.PointingHandCursor)
        chk_anon.setToolTip(
            "Анонимный шёпот: получатель не увидит твой ник в баннере и оверлее.\n"
            "Хост сервера знает отправителя — это ограничение client-server архитектуры."
        )
        chk_anon.setFixedWidth(38)
        chk_anon.setStyleSheet("""
            QCheckBox {
                color: #c8b0ff;
                font-size: 14px;
                background: transparent;
                border: none;
                spacing: 2px;
                padding: 0;
            }
            QCheckBox::indicator {
                width: 13px; height: 13px;
                border: 1px solid rgba(130,100,220,0.55);
                border-radius: 3px;
                background: rgba(255,255,255,0.04);
            }
            QCheckBox::indicator:hover {
                border-color: rgba(160,130,240,0.85);
            }
            QCheckBox::indicator:checked {
                background: #7b52d4;
                border-color: #9b72f4;
            }
        """)

        def _sync_anon_visibility(_ignored=None, _combo=cb, _chk=chk_anon):
            data = _combo.currentData()
            is_whisper = bool(data and data[0] == "whisper")
            _chk.setVisible(is_whisper)
            if not is_whisper and _chk.isChecked():
                _chk.setChecked(False)
        _sync_anon_visibility()
        cb.currentIndexChanged.connect(_sync_anon_visibility)

        btn_del = QPushButton("✕")
        btn_del.setFixedSize(28, 28)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setStyleSheet("""
            QPushButton {
                background: rgba(220,60,60,0.15);
                color: #e87070;
                border: 1px solid rgba(220,60,60,0.35);
                border-radius: 6px;
                font-size: 13px;
                padding: 0;
            }
            QPushButton:hover {
                background: rgba(220,60,60,0.35);
                color: #ffffff;
            }
        """)

        row_lay.addWidget(cb, stretch=4)
        row_lay.addWidget(hk_edit, stretch=5)
        row_lay.addWidget(chk_anon)
        row_lay.addWidget(btn_del)

        slot = {"cb": cb, "hk": hk_edit, "anon": chk_anon, "frame": frame}
        self._hk_rows.append(slot)

        stretch_idx = self._hk_rows_layout.count() - 1
        self._hk_rows_layout.insertWidget(stretch_idx, frame)

        self._btn_hk_add.setEnabled(len(self._hk_rows) < MAX_ROWS)

        def _remove(checked: bool = False, _slot=slot):
            if _slot not in self._hk_rows:
                return
            self._hk_rows.remove(_slot)
            _slot["frame"].setParent(None)
            _slot["frame"].deleteLater()
            if not self._hk_rows:
                self._add_hk_row()
            self._btn_hk_add.setEnabled(len(self._hk_rows) < MAX_ROWS)

        btn_del.clicked.connect(_remove)

    def _load_hk_rows(self) -> None:

        s = self.app_settings
        count = s.value("hk_table_count", None)

        if count is None or int(count) == 0:
            self._add_hk_row()
            return

        for i in range(int(count)):
            ftype = s.value(f"hk_table_{i}_type", "none")
            fdata = s.value(f"hk_table_{i}_data", "")
            fhk   = s.value(f"hk_table_{i}_key",  "")

            fanon = s.value(f"hk_table_{i}_anon", "false") == "true"
            self._add_hk_row(ftype, fdata, fhk, anonymous=fanon)

    def setup_soundboard_tab(self):

        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setSpacing(14)
        lay.setContentsMargins(16, 16, 16, 16)

        vol_group = QGroupBox("🔊  Громкость Soundboard")
        vol_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        vol_lay = QVBoxLayout(vol_group)

        sb_vol = int(self.app_settings.value("soundboard_volume", 40))
        self.lbl_sb = QLabel(f"Soundboard: {sb_vol}%")
        self.sl_sb = QSlider(Qt.Orientation.Horizontal)
        self.sl_sb.setRange(0, 100)
        self.sl_sb.setValue(sb_vol)
        self.sl_sb.valueChanged.connect(lambda v: self.lbl_sb.setText(f"Soundboard: {v}%"))
        vol_lay.addWidget(self.lbl_sb)
        vol_lay.addWidget(self.sl_sb)
        lay.addWidget(vol_group)

        cust_group = QGroupBox("🎵  Мои звуки")
        cust_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        cust_lay = QVBoxLayout(cust_group)

        desc = QLabel(
            "Добавьте собственные звуки (.mp3 / .wav), максимум 1 МБ (~7 сек)."
        )
        desc.setStyleSheet("font-size: 11px; color: #aaa; font-weight: normal;")
        desc.setWordWrap(True)
        cust_lay.addWidget(desc)

        self._custom_sound_rows: list[dict] = []   # список виджетов каждого слота

        for i in range(CUSTOM_SOUND_SLOTS):
            saved_path = self.app_settings.value(f"custom_sound_{i}_path", "")
            saved_name = self.app_settings.value(f"custom_sound_{i}_name", "")
            self._add_custom_sound_row(cust_lay, i, saved_path, saved_name)

        lay.addWidget(cust_group)
        lay.addStretch()
        self.tabs.addTab(tab, "SoundBoard")

    def _add_custom_sound_row(self, parent_lay: QVBoxLayout, idx: int,
                               saved_path: str = "", saved_name: str = ""):
        row_frame = QFrame()
        row_frame.setStyleSheet("""
            QFrame {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.09);
                border-radius: 8px;
            }
        """)
        row_lay = QHBoxLayout(row_frame)
        row_lay.setContentsMargins(10, 7, 10, 7)
        row_lay.setSpacing(8)

        num_lbl = QLabel(f"#{idx + 1}")
        num_lbl.setFixedWidth(24)
        num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num_lbl.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #888; "
            "background: transparent; border: none;"
        )
        row_lay.addWidget(num_lbl)

        name_lbl = QLabel(saved_name if saved_name else "— не выбрано —")
        name_lbl.setStyleSheet(
            "font-size: 12px; color: #ccc; background: transparent; border: none;"
        )
        name_lbl.setMinimumWidth(160)
        name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        name_lbl.setToolTip(saved_path)
        row_lay.addWidget(name_lbl, stretch=1)

        btn_browse = QPushButton("📂  Выбрать")
        btn_browse.setFixedHeight(28)
        btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_browse.setStyleSheet("""
            QPushButton {
                background: rgba(88,101,242,0.25);
                color: #a0b0ff;
                border: 1px solid rgba(88,101,242,0.55);
                border-radius: 6px;
                padding: 0 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background: rgba(88,101,242,0.45);
                color: #ffffff;
            }
        """)
        row_lay.addWidget(btn_browse)

        btn_del = QPushButton("✕")
        btn_del.setFixedSize(28, 28)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setEnabled(bool(saved_path))
        btn_del.setStyleSheet("""
            QPushButton {
                background: rgba(220,60,60,0.15);
                color: #e87070;
                border: 1px solid rgba(220,60,60,0.35);
                border-radius: 6px;
                font-size: 13px;
                padding: 0;
            }
            QPushButton:hover {
                background: rgba(220,60,60,0.35);
                color: #ffffff;
            }
            QPushButton:disabled {
                background: transparent;
                color: #555;
                border-color: rgba(255,255,255,0.08);
            }
        """)
        row_lay.addWidget(btn_del)

        slot = {"path": saved_path, "name": saved_name,
                "name_lbl": name_lbl, "btn_del": btn_del}
        self._custom_sound_rows.append(slot)

        def _on_browse(checked=False, _idx=idx, _slot=slot):
            path, _ = QFileDialog.getOpenFileName(
                self, f"Выбрать звук для слота #{_idx + 1}",
                "", "Аудио файлы (*.mp3 *.wav)"
            )
            if not path:
                return
            try:
                fsize = os.path.getsize(path)
            except OSError:
                fsize = 0
            if fsize > CUSTOM_SOUND_MAX_BYTES:
                QMessageBox.warning(
                    self, "Файл слишком большой",
                    f"Максимальный размер — 1 МБ (~7 сек).\n"
                    f"Выбранный файл: {fsize // 1024} КБ."
                )
                return
            if path.lower().endswith(".wav"):
                try:
                    with wave.open(path, 'rb') as wf:
                        dur = wf.getnframes() / wf.getframerate()
                    if dur > 7.5:
                        QMessageBox.warning(
                            self, "Звук слишком длинный",
                            f"Максимальная длительность — 7 секунд.\n"
                            f"Длительность файла: {dur:.1f} сек."
                        )
                        return
                except Exception:
                    pass

            name = os.path.splitext(os.path.basename(path))[0]
            _slot["path"] = path
            _slot["name"] = name
            _slot["name_lbl"].setText(name)
            _slot["name_lbl"].setToolTip(path)
            _slot["name_lbl"].setStyleSheet(
                "font-size: 12px; color: #7ecf8e; background: transparent; border: none;"
            )
            _slot["btn_del"].setEnabled(True)
            self.app_settings.setValue(f"custom_sound_{_idx}_path", path)
            self.app_settings.setValue(f"custom_sound_{_idx}_name", name)
            self._rebuild_sb_panel_if_open()

        def _on_delete(checked=False, _idx=idx, _slot=slot):
            _slot["path"] = ""
            _slot["name"] = ""
            _slot["name_lbl"].setText("— не выбрано —")
            _slot["name_lbl"].setToolTip("")
            _slot["name_lbl"].setStyleSheet(
                "font-size: 12px; color: #ccc; background: transparent; border: none;"
            )
            _slot["btn_del"].setEnabled(False)
            self.app_settings.setValue(f"custom_sound_{_idx}_path", "")
            self.app_settings.setValue(f"custom_sound_{_idx}_name", "")
            self._rebuild_sb_panel_if_open()

        btn_browse.clicked.connect(_on_browse)
        btn_del.clicked.connect(_on_delete)

        parent_lay.addWidget(row_frame)

    def _rebuild_sb_panel_if_open(self):
        try:
            mw = self.mw
            if hasattr(mw, '_sb_panel') and mw._sb_panel is not None:
                try:
                    if mw._sb_panel.isVisible():
                        mw._sb_panel.rebuild()
                except RuntimeError:
                    pass
        except Exception:
            pass

    def setup_version_tab(self):
        class _Bridge(QObject):
            sig_found    = pyqtSignal(str, str)   # version, download_url
            sig_no_upd   = pyqtSignal()
            sig_error    = pyqtSignal(str)
            sig_progress = pyqtSignal(int)
            sig_done     = pyqtSignal()

        self._upd_bridge = _Bridge()
        self._upd_bridge.sig_found.connect(self._slot_update_found)
        self._upd_bridge.sig_no_upd.connect(self._slot_no_update)
        self._upd_bridge.sig_error.connect(self._slot_update_error)
        self._upd_bridge.sig_progress.connect(self._slot_progress)
        self._upd_bridge.sig_done.connect(self._slot_download_done)

        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_path = resource_path("assets/icon/app_icon.ico")
        if os.path.exists(icon_path):
            icon_lbl.setPixmap(QIcon(icon_path).pixmap(64, 64))
        lay.addWidget(icon_lbl)

        about_lbl = QLabel(ABOUT_TEXT)
        about_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        about_lbl.setWordWrap(True)
        about_lbl.setStyleSheet("font-size: 13px; line-height: 1.6;")
        lay.addWidget(about_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep)

        self._ver_status_lbl = QLabel("Обновления не проверялись")
        self._ver_status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ver_status_lbl.setWordWrap(True)
        lay.addWidget(self._ver_status_lbl)

        self._btn_check_update = QPushButton("🔍  Проверить обновления")
        self._btn_check_update.setFixedHeight(36)
        self._btn_check_update.clicked.connect(self._on_check_update_clicked)
        lay.addWidget(self._btn_check_update)

        self._btn_install_update = QPushButton("⬇  Скачать и установить")
        self._btn_install_update.setFixedHeight(36)
        self._btn_install_update.setVisible(False)
        self._btn_install_update.setStyleSheet(
            "background-color: #2ecc71; color: white; font-weight: bold;"
        )
        self._btn_install_update.clicked.connect(self._on_install_update_clicked)
        lay.addWidget(self._btn_install_update)

        self._ver_progress = QProgressBar()
        self._ver_progress.setVisible(False)
        self._ver_progress.setTextVisible(True)
        lay.addWidget(self._ver_progress)

        if not GITHUB_REPO:
            self._btn_check_update.setEnabled(False)
            self._ver_status_lbl.setText("⚠ GITHUB_REPO не задан в version.py")

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep2)

        btn_logs = QPushButton("📂  Открыть папку с логами")
        btn_logs.setFixedHeight(34)
        btn_logs.setToolTip("Открывает папку с файлами логов в Проводнике")
        btn_logs.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_logs.setStyleSheet("""
            QPushButton {
                background-color: rgba(91,142,245,0.18);
                color: #a0c0ff;
                border: 1px solid rgba(91,142,245,0.45);
                border-radius: 7px;
                font-size: 13px;
                padding: 0 14px;
            }
            QPushButton:hover {
                background-color: rgba(91,142,245,0.32);
                border-color: rgba(91,142,245,0.70);
                color: #ffffff;
            }
        """)
        btn_logs.clicked.connect(self._on_open_logs_folder)
        lay.addWidget(btn_logs)

        lay.addStretch()
        self.tabs.addTab(tab, "Версия")

    def _slot_update_found(self, version: str, download_url: str):
        self._pending_download_url = download_url
        self._ver_status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._ver_status_lbl.setText(
            f"🎉 Доступна новая версия: <b>v{version}</b>"
            f"<br><small>Текущая: v{APP_VERSION}</small>"
        )
        self._btn_install_update.setVisible(True)
        self._btn_check_update.setEnabled(True)

    def _slot_no_update(self):
        self._ver_status_lbl.setText(f"✅ Версия актуальна  (v{APP_VERSION})")
        self._btn_check_update.setEnabled(True)

    def _slot_update_error(self, message: str):
        self._ver_status_lbl.setText(f"❌ {message}")
        self._btn_check_update.setEnabled(True)

    def _slot_progress(self, pct: int):
        self._ver_progress.setValue(pct)

    def _slot_download_done(self):
        self._ver_status_lbl.setText("✅ Загрузка завершена. Перезапуск...")
        from PyQt6.QtWidgets import QApplication
        QTimer.singleShot(1500, QApplication.instance().quit)

    def _on_open_logs_folder(self):
        import subprocess
        _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
        logs_dir = os.path.join(_appdata, 'InPulse', 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        try:
            if os.name == 'nt':
                os.startfile(logs_dir)
            else:
                subprocess.Popen(['xdg-open', logs_dir])
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Ошибка",
                f"Не удалось открыть папку логов:\n{logs_dir}\n\n{e}"
            )

    def _on_check_update_clicked(self):
        from core.updater import check_for_updates_async
        self._btn_check_update.setEnabled(False)
        self._btn_install_update.setVisible(False)
        self._ver_progress.setVisible(False)
        self._ver_status_lbl.setText("⏳ Проверяю...")
        bridge = self._upd_bridge
        check_for_updates_async(
            on_update_found=lambda v, url: bridge.sig_found.emit(v, url),
            on_no_update=lambda: bridge.sig_no_upd.emit(),
            on_error=lambda msg: bridge.sig_error.emit(msg),
        )

    def _on_install_update_clicked(self):
        from core.updater import download_and_apply
        self._btn_install_update.setEnabled(False)
        self._btn_check_update.setEnabled(False)
        self._ver_progress.setVisible(True)
        self._ver_progress.setValue(0)
        self._ver_status_lbl.setText("⬇ Загружаю обновление...")
        bridge = self._upd_bridge
        download_url = getattr(self, "_pending_download_url", None)
        if not download_url:
            bridge.sig_error.emit("URL для скачивания не найден. Нажмите «Проверить обновления» ещё раз.")
            self._btn_install_update.setEnabled(True)
            self._btn_check_update.setEnabled(True)
            return
        download_and_apply(
            download_url,
            on_progress=lambda pct: bridge.sig_progress.emit(pct),
            on_done=lambda: bridge.sig_done.emit(),
            on_error=lambda msg: bridge.sig_error.emit(msg),
        )

    def open_av_sel(self):
        d = AvatarSelector(self)
        if d.exec():
            self.cur_av = d.selected_avatar
            self.upd_av_preview()

    def upd_av_preview(self):
        p = resource_path(f"assets/avatars/{self.cur_av}")
        self.av_lbl.setPixmap(QIcon(p).pixmap(80, 80) if os.path.exists(p) else QIcon().pixmap(0, 0))

    def refresh_devices_list(self):
        devs = sd.query_devices()
        apis = sd.query_hostapis()

        wasapi_idx = next((i for i, a in enumerate(apis) if 'WASAPI' in a['name']), None)
        target_api_idx = wasapi_idx if wasapi_idx is not None else sd.default.hostapi

        self.cb_in.clear()
        self.cb_out.clear()
        u_in, u_out = set(), set()

        s_in = self.app_settings.value("device_in_name", "")
        s_out = self.app_settings.value("device_out_name", "")

        for i, d in enumerate(devs):
            if d['hostapi'] != target_api_idx:
                continue

            api_name = apis[d['hostapi']]['name']
            dn = f"{d['name']} ({api_name})"

            if d['max_input_channels'] > 0 and dn not in u_in:
                self.cb_in.addItem(dn)
                u_in.add(dn)
            if d['max_output_channels'] > 0 and dn not in u_out:
                self.cb_out.addItem(dn)
                u_out.add(dn)

        if not s_in and target_api_idx is not None:
            def_in_idx = apis[target_api_idx]['default_input_device']
            s_in = f"{devs[def_in_idx]['name']} ({apis[target_api_idx]['name']})"

        if not s_out and target_api_idx is not None:
            def_out_idx = apis[target_api_idx]['default_output_device']
            s_out = f"{devs[def_out_idx]['name']} ({apis[target_api_idx]['name']})"

        self.cb_in.setCurrentText(s_in)
        self.cb_out.setCurrentText(s_out)

    def _on_vad_slider_changed(self, val: int):
        self._current_vad_val = val
        self.audio.set_vad_threshold(val)
        self.mic_vad.set_threshold(val)

    def _on_nr_mode_changed(self, index: int):
        mode = self.cb_nr.currentData()
        if mode is None:
            return
        self.audio.set_nr_mode(mode)

    def _fix_combo_popups(self):

        from PyQt6.QtWidgets import QComboBox as _QCB
        _VIEW_SS = (
            "QAbstractItemView {"
            "  background-color: #1e2130;"
            "  color: #c8d0e0;"
            "  selection-background-color: #2c3252;"
            "  selection-color: #ffffff;"
            "  border: 1px solid #333648;"
            "}"
        )
        for cb in self.findChildren(_QCB):
            try:
                v = cb.view()
                v.setStyleSheet(_VIEW_SS)
                win = v.window()
                win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                win.setStyleSheet("background-color: #1e2130;")
            except Exception:
                pass

    def get_devices(self):
        return self.cb_in.currentText(), self.cb_out.currentText()

    def done(self, result: int):
        try:
            self.audio.volume_level_signal.disconnect(self.mic_vad.set_level)
        except (RuntimeError, TypeError):
            pass
        super().done(result)

    def save_all(self):
        s = self.app_settings
        s.setValue("device_in_name", self.cb_in.currentText())
        s.setValue("device_out_name", self.cb_out.currentText())
        s.setValue("system_sound_volume", self.sl_sys.value())
        s.setValue("soundboard_volume", self.sl_sb.value())
        s.setValue("vad_threshold_slider", self._current_vad_val)

        s.setValue("hk_table_count", len(self._hk_rows))
        whisper_slot_idx = 0

        s.setValue("hk_mute", "")
        s.setValue("hk_deafen", "")

        for i in range(8):
            s.setValue(f"whisper_slot_{i}_nick", "")
            s.setValue(f"whisper_slot_{i}_ip",   "")
            s.setValue(f"whisper_slot_{i}_hk",   "")
            s.setValue(f"whisper_slot_{i}_anon", "false")

        for i, row in enumerate(self._hk_rows):
            data = row["cb"].currentData()

            # FIX: переменная hk раньше нигде не определялась → NameError в
            # save_all → диалог настроек не закрывался по «Сохранить».
            try:
                hk = row["hk"].get_hotkey()
            except Exception:
                hk = ""

            anon_checked = False
            try:
                anon_checked = bool(row["anon"].isChecked())
            except Exception:
                pass
            ftype = data[0] if data else "none"
            fdata = data[1] if data else ""

            s.setValue(f"hk_table_{i}_type", ftype)
            s.setValue(f"hk_table_{i}_data", fdata)
            s.setValue(f"hk_table_{i}_key",  hk)
            s.setValue(f"hk_table_{i}_anon",
                       "true" if (anon_checked and ftype == "whisper") else "false")

            if ftype == "mute_mic" and not s.value("hk_mute", ""):
                s.setValue("hk_mute", hk)
            elif ftype == "deafen" and not s.value("hk_deafen", ""):
                s.setValue("hk_deafen", hk)
            elif ftype == "whisper" and whisper_slot_idx < 8 and hk:
                nick = ""
                try:
                    if os.path.exists(KNOWN_USERS_PATH):
                        with open(KNOWN_USERS_PATH, "r", encoding="utf-8") as f:
                            reg = json.load(f)
                        nick = reg.get(fdata, {}).get("nick", "")
                except Exception:
                    pass
                s.setValue(f"whisper_slot_{whisper_slot_idx}_ip",   fdata)
                s.setValue(f"whisper_slot_{whisper_slot_idx}_nick", nick)
                s.setValue(f"whisper_slot_{whisper_slot_idx}_hk",   hk)

                s.setValue(f"whisper_slot_{whisper_slot_idx}_anon",
                           "true" if anon_checked else "false")
                whisper_slot_idx += 1

        self.mw.nick = self.ed_nick.text()
        self.mw.avatar = self.cur_av
        self.mw.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.mw.nick}")
        if hasattr(self.mw, 'net'):
            self.mw.net.update_user_info(self.mw.nick, self.mw.avatar)

        new_icon = self.app_settings.value("my_status_icon", "")
        new_text = self.app_settings.value("my_status_text", "")
        if hasattr(self.mw, '_my_status_icon'):
            self.mw._my_status_icon = new_icon
            self.mw._my_status_text = new_text
        if hasattr(self.mw, 'net'):
            self.mw.net.send_presence_update(new_icon, new_text)

        if os.path.exists(USER_CONFIG_PATH):
            try:
                with open(USER_CONFIG_PATH, 'r') as f:
                    d = json.load(f)
                d['nick'] = self.mw.nick
                d['avatar'] = self.mw.avatar
                with open(USER_CONFIG_PATH, 'w') as f:
                    json.dump(d, f)
            except Exception:
                pass
        self.accept()