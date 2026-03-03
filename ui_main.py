import os
import gc
import json
import sounddevice as sd
import soundfile as sf
import winsound
import keyboard
import time
from video_engine import VideoEngine
from ui_video import VideoWindow
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QHeaderView, QMessageBox, QStackedWidget,
                             QFrame, QSizeGrip)
from PyQt6.QtCore import Qt, QTimer, QSize, QSettings, QRect, QPoint
from PyQt6.QtGui import QIcon, QFont, QFontDatabase, QBrush, QColor

from config import *
from audio_engine import AudioHandler
from network_engine import NetworkClient
from ui_dialogs import UserOverlayPanel, SettingsDialog, SoundboardDialog, WhisperSystemOverlay, SelfStatusOverlayPanel
from version import APP_VERSION, APP_NAME, GITHUB_REPO


class CustomTitleBar(QWidget):
    """Кастомный title bar для безрамочного окна.

    Поддерживает перетаскивание, сворачивание, maximize/restore, закрытие.
    """

    def __init__(self, parent_window, title=""):
        super().__init__(parent_window)
        self._win = parent_window
        self._drag_pos = None
        self.setFixedHeight(40)
        self.setObjectName("customTitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(6)

        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(22, 22)
        self._icon_lbl.setPixmap(
            QIcon(resource_path("assets/icon/logo.ico")).pixmap(22, 22)
        )
        layout.addWidget(self._icon_lbl)

        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("titleBarText")
        layout.addWidget(self._title_lbl, stretch=1)

        self._btn_min = QPushButton("─")
        self._btn_min.setObjectName("titleBtnMin")
        self._btn_min.setFixedSize(34, 30)
        self._btn_min.clicked.connect(parent_window.showMinimized)

        self._btn_max = QPushButton("□")
        self._btn_max.setObjectName("titleBtnMax")
        self._btn_max.setFixedSize(34, 30)
        self._btn_max.clicked.connect(self._toggle_maximize)

        self._btn_close = QPushButton("✕")
        self._btn_close.setObjectName("titleBtnClose")
        self._btn_close.setFixedSize(34, 30)
        self._btn_close.clicked.connect(parent_window.close)

        layout.addWidget(self._btn_min)
        layout.addWidget(self._btn_max)
        layout.addWidget(self._btn_close)

    def set_title(self, title: str):
        """Обновить текст заголовка.

        :param title: новый заголовок окна
        """
        self._title_lbl.setText(title)

    def _toggle_maximize(self):
        if self._win.isMaximized():
            self._win.showNormal()
            self._btn_max.setText("□")
        else:
            self._win.showMaximized()
            self._btn_max.setText("❐")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            if self._win.isMaximized():
                self._win.showNormal()
                self._btn_max.setText("□")
                self._drag_pos = QPoint(self._win.width() // 2, 20)
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()


class MainWindow(QMainWindow):
    def __init__(self, ip, nick, avatar):
        super().__init__()
        font_path = resource_path("assets/font/MyFont.ttf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        self.custom_font_family = QFontDatabase.applicationFontFamilies(font_id)[0] if font_id != -1 else "Segoe UI"

        self.ip, self.nick, self.avatar = ip, nick, avatar
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.known_uids = {}
        self.current_room = "General"
        self.default_rooms = ["General", "Gaming", "Music", "Work"]
        self.sound_files = {
            "self_move":  resource_path("assets/music/user_join.wav"),
            "other_join": resource_path("assets/music/user_join.wav"),
            "other_exit": resource_path("assets/music/disconnected.wav"),
            "mute":       resource_path("assets/music/mute.wav"),
            "unmute":     resource_path("assets/music/unmute.wav"),
            "stream_on":  resource_path("assets/music/stream_on.wav"),
            "stream_off": resource_path("assets/music/stream_off.wav"),
        }
        self.prev_room_uids: set = set()
        self.prev_streaming_uids: set = set()

        self.audio = AudioHandler()
        self.net = NetworkClient(self.audio)

        self._resize_margin = 6
        self._resize_direction: str | None = None
        self._resize_start_pos: QPoint | None = None
        self._resize_start_geom: QRect | None = None
        self.setMouseTracking(True)

        # Предзагрузка звуков — один раз при старте
        self._loaded_sounds: dict = {}
        for key, path in self.sound_files.items():
            if os.path.exists(path):
                try:
                    data, sr = sf.read(path, dtype='float32')
                    self._loaded_sounds[key] = (data, sr)
                except Exception as ex:
                    print(f"[UI] Не удалось загрузить звук {key}: {ex}")

        self.video = VideoEngine(self.net)
        self.net.set_video_engine(self.video)
        self.stream_windows = {}

        self.setup_ui()
        self.apply_theme(self.app_settings.value("theme", "Светлая"))
        self.net.connected.connect(self.on_connected)
        self.net.global_state_update.connect(self.update_user_tree)
        self.net.error_occurred.connect(self.on_connection_error)
        self.net.connection_lost.connect(self.on_connection_lost)
        self.net.connection_restored.connect(self.on_connection_restored)
        self.net.reconnect_failed.connect(self.on_reconnect_failed)

        self.audio.status_changed.connect(self.on_audio_status_changed)
        self.audio.status_changed.connect(self.net.send_status_update)
        self.audio.whisper_received.connect(self._on_whisper_received)
        self.audio.user_volume_zero.connect(self._on_user_volume_zero)
        self.video.frame_received.connect(self.on_video_frame)

        self.net.soundboard_played.connect(self._on_soundboard_played)
        self.net.nudge_received.connect(self._on_nudge_received)
        self.net.nudge_triggered.connect(self._on_nudge_triggered)

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(100)

        self.setup_hotkeys()
        self.net.connect_to_server(self.ip, self.nick, self.avatar)
        self.is_streaming = False
        self._sb_panel = None

        # Тост soundboard — поверх главного окна
        self._sb_toast = QLabel(self)
        self._sb_toast.setStyleSheet("""
            QLabel {
                background-color: rgba(20, 22, 30, 215);
                color: #f5c518;
                font-size: 13px;
                font-weight: bold;
                border: 1px solid rgba(245,197,24,0.45);
                border-radius: 8px;
                padding: 5px 14px;
            }
        """)
        self._sb_toast.setVisible(False)
        self._sb_toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._sb_toast_timer = QTimer(self)
        self._sb_toast_timer.setSingleShot(True)
        self._sb_toast_timer.setInterval(3500)
        self._sb_toast_timer.timeout.connect(lambda: self._sb_toast.setVisible(False))

        # Таймер завершения шёпота: >1.5 с без пакетов — скрываем баннер
        self._whisper_end_timer = QTimer()
        self._whisper_end_timer.setSingleShot(True)
        self._whisper_end_timer.setInterval(1500)
        self._whisper_end_timer.timeout.connect(self._on_whisper_ended)

        self._whisper_overlay = WhisperSystemOverlay()

        self._start_silent_update_check()

        # Кэш цветов и кистей для refresh_ui() — пересоздаётся только при смене темы
        self._cache_theme = self.app_settings.value("theme", "Светлая")
        self._theme_dirty = False
        self._c_talk   = QColor("#2ecc71")
        self._c_mute   = QColor("#e74c3c")
        self._c_stream = QColor("#3498db")
        self._c_def    = QColor("#ecf0f1") if self._cache_theme == "Темная" else QColor("#444444")
        self._icon_size = QSize(26, 26)

        self._br_talk   = QBrush(self._c_talk)
        self._br_mute   = QBrush(self._c_mute)
        self._br_stream = QBrush(self._c_stream)
        self._br_def    = QBrush(self._c_def)
        self._br_gray   = QBrush(QColor("#888888"))

        # Кэш пиксмапов иконок — не пересоздаём при каждом refresh_ui()
        self._px_live      = QIcon(resource_path("assets/icon/live.svg")).pixmap(25, 25)
        self._px_vol_off   = QIcon(resource_path("assets/icon/volume_off.svg")).pixmap(self._icon_size)
        self._px_mic_off   = QIcon(resource_path("assets/icon/mic_off.svg")).pixmap(self._icon_size)
        self._px_ban       = QIcon(resource_path("assets/icon/ban.svg")).pixmap(self._icon_size)

        self._status_px_cache: dict = {}
        self._avatar_cache: dict = {}  # <--- ДОБАВЛЕНО: Кэш для аватарок пользователей

        self._my_status_icon: str = self.app_settings.value("my_status_icon", "")
        self._my_status_text: str = self.app_settings.value("my_status_text", "")

        self._font_room    = QFont(self.custom_font_family, 12)
        self._font_room.setBold(True)
        self._font_user    = QFont(self.custom_font_family, 14)
        self._font_watcher = QFont(self.custom_font_family, 11)

    def setup_ui(self):
        """Построить структуру главного окна."""
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.nick}")
        self.setMinimumSize(450, 600)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        _root = QWidget()
        _root.setObjectName("windowRoot")
        _root_layout = QVBoxLayout(_root)
        _root_layout.setContentsMargins(0, 0, 0, 0)
        _root_layout.setSpacing(0)

        self._title_bar = CustomTitleBar(self, f"{APP_NAME} v{APP_VERSION} — {self.nick}")
        _root_layout.addWidget(self._title_bar)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setObjectName("titleSeparator")
        _sep.setFixedHeight(1)
        _root_layout.addWidget(_sep)

        self._stack = QStackedWidget()
        _root_layout.addWidget(self._stack, stretch=1)

        self.setCentralWidget(_root)

        main_page = QWidget()
        main_page.setObjectName("centralWidget")
        layout = QVBoxLayout(main_page)
        layout.setContentsMargins(12, 10, 12, 0)
        layout.setSpacing(8)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Ник", "", "", "", ""])
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(32, 32))
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 30)
        header.resizeSection(2, 35)
        header.resizeSection(3, 35)
        header.resizeSection(4, 35)
        header.hide()

        self.tree.itemDoubleClicked.connect(self.on_tree_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.tree, stretch=1)

        self._update_banner = QPushButton()
        self._update_banner.setObjectName("updateBanner")
        self._update_banner.setVisible(False)
        self._update_banner.clicked.connect(self.open_settings)
        layout.addWidget(self._update_banner)

        self._whisper_banner = QLabel()
        self._whisper_banner.setVisible(False)
        self._whisper_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._whisper_banner.setStyleSheet(
            "background-color: rgba(52, 73, 94, 220); color: #ecf0f1; "
            "border: 1px solid #5dade2; border-radius: 8px; "
            "padding: 8px 12px; font-weight: bold; font-size: 15px;"
        )
        self._whisper_banner.setFixedHeight(40)
        layout.addWidget(self._whisper_banner)

        self._bottom_bar = QFrame()
        self._bottom_bar.setObjectName("bottomBar")
        self._bottom_bar.setFixedHeight(72)

        btns = QHBoxLayout(self._bottom_bar)
        btns.setContentsMargins(12, 0, 12, 0)
        btns.setSpacing(8)

        self.btn_mute = QPushButton()
        self.btn_mute.setCheckable(True)
        self.btn_mute.setFixedSize(46, 46)
        self.btn_mute.setObjectName("barBtn")
        self.btn_mute.setIcon(QIcon(resource_path("assets/icon/mic_on.svg")))
        self.btn_mute.setIconSize(QSize(26, 26))
        self.btn_mute.clicked.connect(self.toggle_mute)

        self.btn_deafen = QPushButton()
        self.btn_deafen.setCheckable(True)
        self.btn_deafen.setFixedSize(46, 46)
        self.btn_deafen.setObjectName("barBtn")
        self.btn_deafen.setIcon(QIcon(resource_path("assets/icon/volume_on.svg")))
        self.btn_deafen.setIconSize(QSize(26, 26))
        self.btn_deafen.clicked.connect(self.toggle_deafen)

        self.btn_sb = QPushButton()
        self.btn_sb.setFixedSize(46, 46)
        self.btn_sb.setObjectName("barBtn")
        self.btn_sb.setIcon(QIcon(resource_path("assets/icon/bells.svg")))
        self.btn_sb.setIconSize(QSize(26, 26))
        self.btn_sb.clicked.connect(self.open_soundboard)

        self.btn_stream = QPushButton()
        self.btn_stream.setFixedSize(46, 46)
        self.btn_stream.setObjectName("btnStream")
        self.btn_stream.setIconSize(QSize(26, 26))
        self.btn_stream.setIcon(QIcon(resource_path("assets/icon/stream_off.svg")))
        self.btn_stream.setCheckable(True)
        self.btn_stream.clicked.connect(self.toggle_stream)

        self.ping_lbl = QLabel("0 ms")
        self.ping_lbl.setObjectName("pingLabel")
        self.ping_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_set = QPushButton()
        btn_set.setFixedSize(46, 46)
        btn_set.setObjectName("barBtn")
        btn_set.setIcon(QIcon(resource_path("assets/icon/settings.svg")))
        btn_set.setIconSize(QSize(26, 26))
        btn_set.clicked.connect(self.open_settings)

        btns.addWidget(self.btn_mute)
        btns.addWidget(self.btn_deafen)
        btns.addWidget(self.btn_sb)
        btns.addWidget(self.btn_stream)
        btns.addStretch()
        btns.addWidget(self.ping_lbl)
        btns.addWidget(btn_set)

        layout.addWidget(self._bottom_bar)
        self._stack.addWidget(main_page)

        # Экран потери соединения
        lost_page = QWidget()
        lost_page.setObjectName("centralWidget")
        lost_layout = QVBoxLayout(lost_page)
        lost_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lost_layout.setSpacing(20)

        self._lost_icon_lbl = QLabel()
        self._lost_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_path = resource_path("assets/icon/lost_connection.svg")
        if os.path.exists(icon_path):
            self._lost_icon_lbl.setPixmap(QIcon(icon_path).pixmap(QSize(96, 96)))
        else:
            self._lost_icon_lbl.setText("⚠")
            self._lost_icon_lbl.setStyleSheet("font-size: 64px;")
        lost_layout.addWidget(self._lost_icon_lbl)

        self._lost_title_lbl = QLabel("Сервер недоступен")
        self._lost_title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lost_title_lbl.setStyleSheet("font-size: 22px; font-weight: bold;")
        lost_layout.addWidget(self._lost_title_lbl)

        self._lost_status_lbl = QLabel("Попытка переподключения...")
        self._lost_status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lost_status_lbl.setStyleSheet("font-size: 15px; color: #888888;")
        lost_layout.addWidget(self._lost_status_lbl)

        self._btn_reconnect = QPushButton("Переподключиться")
        self._btn_reconnect.setFixedHeight(45)
        self._btn_reconnect.setStyleSheet(
            "background-color: #2ecc71; color: white; font-size: 16px; font-weight: bold; border-radius: 8px;"
        )
        self._btn_reconnect.clicked.connect(self._on_manual_reconnect_clicked)
        lost_layout.addWidget(self._btn_reconnect)

        self._stack.addWidget(lost_page)

    def on_connection_error(self, error_msg: str):
        """Показать экран ошибки подключения.

        :param error_msg: текст ошибки
        """
        print(f"[UI] Connection error: {error_msg}")
        self._lost_title_lbl.setText("Сервер недоступен")
        self._lost_status_lbl.setText(f"Не удалось подключиться:\n{error_msg}")
        self._btn_reconnect.setEnabled(True)
        self._stack.setCurrentIndex(1)

    def on_connection_lost(self):
        """Показать экран потери соединения с авто-переподключением."""
        print("[UI] Connection lost — showing reconnect screen")
        self._lost_title_lbl.setText("Соединение потеряно")
        self._lost_status_lbl.setText("Автоматическое переподключение...")
        self._btn_reconnect.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def on_connection_restored(self):
        """Вернуть главный экран после восстановления соединения."""
        print("[UI] Connection restored — returning to main screen")
        self._stack.setCurrentIndex(0)
        self._btn_reconnect.setEnabled(True)

    def on_reconnect_failed(self):
        """Все попытки авто-переподключения провалились — разблокировать кнопку."""
        print("[UI] All silent reconnect attempts failed")
        self._lost_title_lbl.setText("Нет соединения")
        self._lost_status_lbl.setText("Не удалось переподключиться автоматически.\nНажмите кнопку ниже или проверьте сеть.")
        self._btn_reconnect.setEnabled(True)

    def _on_manual_reconnect_clicked(self):
        """Запустить ручное переподключение по нажатию кнопки."""
        self._lost_status_lbl.setText("Попытка переподключения...")
        self._btn_reconnect.setEnabled(False)
        self.net.manual_reconnect()

    def setWindowTitle(self, title: str):
        """Переопределяем — синхронно обновляем кастомный title bar.

        :param title: заголовок окна
        """
        super().setWindowTitle(title)
        if hasattr(self, '_title_bar'):
            self._title_bar.set_title(title)

    # ── Edge-resize для безрамочного окна ────────────────────────────────────

    _EDGE_CURSORS = {
        "top-left":     Qt.CursorShape.SizeFDiagCursor,
        "top-right":    Qt.CursorShape.SizeBDiagCursor,
        "bottom-left":  Qt.CursorShape.SizeBDiagCursor,
        "bottom-right": Qt.CursorShape.SizeFDiagCursor,
        "left":         Qt.CursorShape.SizeHorCursor,
        "right":        Qt.CursorShape.SizeHorCursor,
        "top":          Qt.CursorShape.SizeVerCursor,
        "bottom":       Qt.CursorShape.SizeVerCursor,
    }

    def _edge_at(self, pos: QPoint) -> str | None:
        """Определить край окна по позиции курсора.

        :param pos: позиция курсора в координатах окна
        :return: название края или None
        """
        m = self._resize_margin
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        l, r, t, b = x <= m, x >= w - m, y <= m, y >= h - m
        if t and l:   return "top-left"
        if t and r:   return "top-right"
        if b and l:   return "bottom-left"
        if b and r:   return "bottom-right"
        if l:         return "left"
        if r:         return "right"
        if t:         return "top"
        if b:         return "bottom"
        return None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            edge = self._edge_at(e.pos())
            if edge:
                self._resize_direction = edge
                self._resize_start_pos = e.globalPosition().toPoint()
                self._resize_start_geom = QRect(self.geometry())
                e.accept()
                return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if (self._resize_direction
                and e.buttons() == Qt.MouseButton.LeftButton
                and self._resize_start_pos is not None
                and self._resize_start_geom is not None):
            delta = e.globalPosition().toPoint() - self._resize_start_pos
            g = QRect(self._resize_start_geom)
            d = self._resize_direction
            if "right"  in d: g.setRight(g.right()   + delta.x())
            if "bottom" in d: g.setBottom(g.bottom() + delta.y())
            if "left"   in d: g.setLeft(g.left()     + delta.x())
            if "top"    in d: g.setTop(g.top()       + delta.y())
            if g.width() >= self.minimumWidth() and g.height() >= self.minimumHeight():
                self.setGeometry(g)
            e.accept()
            return
        if not self.isMaximized():
            edge = self._edge_at(e.pos())
            self.setCursor(self._EDGE_CURSORS[edge]) if edge else self.unsetCursor()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._resize_direction = None
        self._resize_start_pos = None
        self._resize_start_geom = None
        self.unsetCursor()
        super().mouseReleaseEvent(e)

    def apply_theme(self, theme_name: str):
        """Применить тему оформления к главному окну.

        :param theme_name: 'Темная' или 'Светлая'
        """
        font_f = self.custom_font_family
        is_dark = (theme_name == "Темная")

        if is_dark:
            win_bg       = "rgba(28, 32, 50, 255)"
            surface      = "rgba(255,255,255,0.10)"
            surface_solid= "#252840"
            text         = "#eaeef8"
            text_dim     = "#8898bb"
            border       = "rgba(255,255,255,0.13)"
            border_solid = "#3d4260"
            hover        = "rgba(255,255,255,0.12)"
            hover_solid  = "#363a58"
            accent       = "#5b8ef5"
            accent_red   = "#e74c3c"
            title_bg     = "rgba(16, 18, 32, 255)"
            title_text   = "#cdd6f4"
            title_sep    = "rgba(255,255,255,0.09)"
            win_border   = "rgba(255,255,255,0.13)"
            bottom_bg    = "rgba(0,0,0,0.28)"
            bottom_sep   = "rgba(255,255,255,0.09)"
            btn_bg       = "rgba(255,255,255,0.16)"
            btn_hover    = "rgba(255,255,255,0.26)"
            btn_border   = "rgba(255,255,255,0.24)"
            scrollbar    = "rgba(255,255,255,0.22)"
            sb_track     = "rgba(255,255,255,0.07)"
            tree_room_bg = "rgba(255,255,255,0.07)"
        else:
            win_bg       = "rgba(210, 215, 225, 255)"
            surface      = "rgba(0,0,0,0.04)"
            surface_solid= "#e8eaee"
            text         = "#1a1e2a"
            text_dim     = "#667088"
            border       = "rgba(0,0,0,0.12)"
            border_solid = "#b8bcc8"
            hover        = "rgba(0,0,0,0.07)"
            hover_solid  = "#c8cad4"
            accent       = "#3a6fd8"
            accent_red   = "#d32f2f"
            title_bg     = "rgba(30, 42, 55, 255)"
            title_text   = "#dce6f0"
            title_sep    = "rgba(0,0,0,0.15)"
            win_border   = "rgba(0,0,0,0.20)"
            bottom_bg    = "rgba(0,0,0,0.10)"
            bottom_sep   = "rgba(0,0,0,0.12)"
            btn_bg       = "rgba(255,255,255,0.45)"
            btn_hover    = "rgba(255,255,255,0.70)"
            btn_border   = "rgba(0,0,0,0.15)"
            scrollbar    = "rgba(0,0,0,0.25)"
            sb_track     = "rgba(0,0,0,0.06)"
            tree_room_bg = "rgba(0,0,0,0.05)"

        self.setStyleSheet(f"""
            * {{ font-family: '{font_f}'; font-size: 15px; color: {text}; }}

            #windowRoot {{
                background-color: {win_bg};
                border: 1px solid {win_border};
                border-radius: 10px;
            }}

            #customTitleBar {{
                background-color: {title_bg};
                border: none;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }}
            #customTitleBar QLabel {{
                background: transparent;
                border: none;
            }}
            #titleBarText {{
                color: {title_text};
                font-size: 13px;
                font-weight: bold;
                letter-spacing: 0.5px;
                background: transparent;
                border: none;
            }}
            #titleBtnMin, #titleBtnMax {{
                background: transparent;
                border: none;
                border-radius: 5px;
                color: {title_text};
                font-size: 15px;
            }}
            #titleBtnMin:hover, #titleBtnMax:hover {{
                background: rgba(255,255,255,0.12);
            }}
            #titleBtnClose {{
                background: transparent;
                border: none;
                border-radius: 5px;
                color: {title_text};
                font-size: 15px;
            }}
            #titleBtnClose:hover {{
                background: #e74c3c;
                color: white;
            }}
            #titleSeparator {{
                background-color: {title_sep};
                border: none;
            }}

            QMainWindow, #centralWidget {{
                background-color: transparent;
            }}

            QTreeWidget {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 8px;
                outline: none;
                padding: 0px;
            }}
            QTreeWidget::item {{
                outline: none;
                border: none;
                border-radius: 0px;
                padding-left: 4px;
            }}
            QTreeWidget::item:!has-children {{
                height: 44px;
            }}
            QTreeWidget::item:has-children {{
                height: 30px;
                background-color: transparent;
                border-radius: 0px;
                color: {text_dim};
                font-size: 12px;
                font-weight: bold;
                letter-spacing: 0.5px;
            }}
            QTreeWidget::item:selected {{
                background-color: transparent;
                color: {text};
            }}
            QTreeWidget::item:hover {{
                background-color: {hover_solid};
                border-radius: 0px;
            }}
            QTreeWidget::item:selected:hover {{
                background-color: {hover_solid};
                border-radius: 0px;
            }}
            QTreeWidget::branch {{
                background: transparent;
                border-radius: 0px;
            }}
            QTreeWidget QToolTip {{
                background-color: transparent;
                border: none;
                color: {text};
                font-size: 12px;
                padding: 0px;
            }}
            QTreeWidget QScrollBar:vertical {{
                background: {sb_track};
                width: 5px;
                border-radius: 2px;
                margin: 0;
            }}
            QTreeWidget QScrollBar::handle:vertical {{
                background: {scrollbar};
                border-radius: 2px;
            }}
            QTreeWidget QScrollBar::add-line:vertical,
            QTreeWidget QScrollBar::sub-line:vertical {{ height: 0; }}

            #bottomBar {{
                background-color: {bottom_bg};
                border-top: 1px solid {bottom_sep};
                border-bottom-left-radius: 9px;
                border-bottom-right-radius: 9px;
            }}

            #barBtn {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 4px;
            }}
            #barBtn:hover {{
                background-color: {btn_hover};
                border-color: {accent};
            }}
            #barBtn:checked {{
                background-color: rgba(231,76,60,0.45);
                border-color: rgba(231,76,60,0.75);
            }}

            #btnStream {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 4px;
            }}
            #btnStream:hover {{
                background-color: {btn_hover};
                border-color: {accent};
            }}

            #pingLabel {{
                font-size: 12px;
                color: {text_dim};
                background: transparent;
                border: none;
            }}

            QPushButton#updateBanner {{
                background-color: rgba(46,204,113,0.20);
                color: #82e0aa;
                font-weight: bold;
                border: 1px solid rgba(46,204,113,0.45);
                border-radius: 7px;
                padding: 6px;
                text-align: center;
            }}
            QPushButton#updateBanner:hover {{
                background-color: rgba(46,204,113,0.35);
            }}

            QPushButton {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 8px;
                padding: 5px 10px;
                color: {text};
            }}
            QPushButton:hover {{
                background-color: {btn_hover};
                border-color: {accent};
            }}
            QPushButton:checked {{
                background-color: rgba(231,76,60,0.30);
                border-color: rgba(231,76,60,0.55);
                color: #ff9090;
            }}

            #btn_reconnect_green {{
                background-color: rgba(46,204,113,0.25);
                color: #82e0aa;
                font-size: 16px;
                font-weight: bold;
                border-radius: 8px;
                border: 1px solid rgba(46,204,113,0.50);
            }}
            #btn_reconnect_green:hover {{
                background-color: rgba(46,204,113,0.40);
            }}

            QDialog {{ background: transparent; }}
        """)

        # Обновляем кэш цветов при смене темы
        self._cache_theme = theme_name
        self._theme_dirty = True
        self._c_talk   = QColor("#2ecc71")
        self._c_mute   = QColor("#e74c3c")
        self._c_stream = QColor("#3498db")
        self._c_def    = QColor("#d4d8e8") if is_dark else QColor("#1a1e2a")
        self._br_talk   = QBrush(self._c_talk)
        self._br_mute   = QBrush(self._c_mute)
        self._br_stream = QBrush(self._c_stream)
        self._br_def    = QBrush(self._c_def)
        self._br_gray   = QBrush(QColor("#6e7a96") if is_dark else QColor("#8090a8"))

    def setup_hotkeys(self):
        """Зарегистрировать глобальные горячие клавиши.

        Регистрирует mute/deafen, PTT-шёпот (5 слотов) и звуки soundboard.
        PTT-release реализован через raw key-hook — надёжнее чем trigger_on_release.
        """
        try:
            keyboard.unhook_all()

            m = self.app_settings.value("hk_mute",   "alt+[")
            d = self.app_settings.value("hk_deafen", "alt+]")
            if m:
                try:
                    keyboard.add_hotkey(m, lambda: self.btn_mute.click())
                except Exception as e:
                    print(f"[HK] mute hotkey error: {e}")
            if d:
                try:
                    keyboard.add_hotkey(d, lambda: self.btn_deafen.click())
                except Exception as e:
                    print(f"[HK] deafen hotkey error: {e}")

            for i in range(5):
                ip   = self.app_settings.value(f"whisper_slot_{i}_ip",   "")
                nick = self.app_settings.value(f"whisper_slot_{i}_nick", "")
                hk   = self.app_settings.value(f"whisper_slot_{i}_hk",   "")
                if (not ip and not nick) or not hk:
                    continue

                def _make_ptt(target_ip: str, target_nick: str, hotkey_str: str):
                    active = [False]
                    trigger_key = hotkey_str.replace(" ", "").split("+")[-1].lower()

                    def _press():
                        if active[0]:
                            return
                        uid = None
                        if target_ip:
                            with self.audio.users_lock:
                                for u_uid, u_ip in self.audio.uid_to_ip.items():
                                    if u_ip == target_ip:
                                        uid = u_uid
                                        break
                        if uid is None and target_nick:
                            for u_uid, data in self.known_uids.items():
                                try:
                                    if data['item'].text(0).strip() == target_nick:
                                        uid = u_uid
                                        break
                                except Exception:
                                    pass
                        if uid is not None:
                            active[0] = True
                            self.audio.start_whisper(uid)
                            display = target_nick or target_ip
                            print(f"[HK] Whisper PTT START → {display} (uid={uid})")
                        else:
                            display = target_nick or target_ip
                            print(f"[HK] Whisper PTT: '{display}' не найден онлайн")

                    def _raw_key_up(e):
                        if (active[0]
                                and e.event_type == 'up'
                                and e.name
                                and e.name.lower() == trigger_key):
                            active[0] = False
                            self.audio.stop_whisper()
                            display = target_nick or target_ip
                            print(f"[HK] Whisper PTT STOP  ← {display}")

                    return _press, _raw_key_up

                _press, _raw_key_up = _make_ptt(ip, nick, hk)
                try:
                    keyboard.add_hotkey(hk, _press, trigger_on_release=False, suppress=False)
                    keyboard.hook(_raw_key_up, suppress=False)
                    print(f"[HK] Whisper slot {i}: ip='{ip}' nick='{nick}' → '{hk}' (trigger_key='{hk.replace(' ','').split('+')[-1].lower()}')")
                except Exception as e:
                    print(f"[HK] Whisper slot {i} error ({hk!r}): {e}")

            hk_count = int(self.app_settings.value("hk_table_count", 0))
            for i in range(hk_count):
                ftype = self.app_settings.value(f"hk_table_{i}_type", "none")
                if ftype != "sound":
                    continue
                fdata = self.app_settings.value(f"hk_table_{i}_data", "")
                hk    = self.app_settings.value(f"hk_table_{i}_key",  "")
                if not fdata or not hk:
                    continue

                sound_path = ""
                for j in range(10):
                    n = self.app_settings.value(f"custom_sound_{j}_name", "")
                    p = self.app_settings.value(f"custom_sound_{j}_path", "")
                    if n == fdata and p:
                        sound_path = p
                        break

                if not sound_path:
                    print(f"[HK] Sound hk slot {i}: файл для '{fdata}' не найден")
                    continue

                def _make_sound_hk(path: str, name: str):
                    def _play():
                        try:
                            import os, base64
                            from config import CMD_SOUNDBOARD
                            fsize = os.path.getsize(path)
                            if fsize > 1 * 1024 * 1024:
                                return
                            with open(path, 'rb') as f:
                                raw = f.read()
                            b64 = base64.b64encode(raw).decode('ascii')
                            self.net.send_json({
                                "action":   CMD_SOUNDBOARD,
                                "file":     f"__custom__:{name}",
                                "data_b64": b64,
                            })
                            print(f"[HK] Sound fired: '{name}'")
                        except Exception as ex:
                            print(f"[HK] Sound play error '{name}': {ex}")
                    return _play

                try:
                    keyboard.add_hotkey(hk, _make_sound_hk(sound_path, fdata),
                                        trigger_on_release=False, suppress=False)
                    print(f"[HK] Sound slot {i}: name='{fdata}' hk='{hk}'")
                except Exception as e:
                    print(f"[HK] Sound hk slot {i} error ({hk!r}): {e}")

        except Exception as e:
            print(f"[HK] setup_hotkeys error: {e}")

    def play_notification(self, stype: str = "self_move"):
        """Воспроизвести звук уведомления.

        :param stype: ключ звука из sound_files
        """
        raw = int(self.app_settings.value("system_sound_volume", 30)) / 100.0
        vol = raw ** 2  # перцептивно равномерная шкала
        entry = self._loaded_sounds.get(stype)
        if entry is not None:
            try:
                data, sr = entry
                sd.play(data * vol, sr)
            except Exception:
                pass
        else:
            if vol > 0:
                winsound.Beep(600 if stype == "self_move" else 400, 150)

    def _on_whisper_received(self, sender_uid: int):
        """Обработать входящий пакет шёпота.

        Вызывается на каждый пакет (~50/сек). UI обновляется только при
        смене отправителя или первом появлении — для исключения мерцания.

        :param sender_uid: UID отправителя шёпота
        """
        self._whisper_end_timer.start()

        if sender_uid == getattr(self, '_current_whisper_uid', None) \
                and self._whisper_banner.isVisible():
            return

        self._current_whisper_uid = sender_uid

        nick = "Кто-то"
        for uid, data in self.known_uids.items():
            if uid == sender_uid:
                try:
                    raw = data['item'].text(0).strip()
                    if raw:
                        nick = raw
                except Exception:
                    pass
                break

        self._whisper_banner.setText(f"🤫  {nick} шепчет вам...")
        self._whisper_banner.setVisible(True)
        self._whisper_overlay.show_for(nick)

    def _on_whisper_ended(self):
        """Скрыть баннер и системный оверлей по истечении таймера шёпота."""
        self._current_whisper_uid = None
        self._whisper_banner.setVisible(False)
        self._whisper_overlay.hide_overlay()

    def _on_user_volume_zero(self, uid: int, is_zero: bool):
        """Мгновенно обновить иконку и цвет ника при изменении громкости пользователя.

        :param uid: UID пользователя
        :param is_zero: True — громкость выставлена в 0
        """
        data = self.known_uids.get(uid)
        if data is None:
            return

        item = data['item']

        try:
            with self.audio.users_lock:
                u_audio = self.audio.remote_users.get(uid)
                is_muted_btn = u_audio.is_locally_muted if u_audio else False
                is_m_remote  = data.get('is_m', False)

            if is_zero or is_muted_btn:
                item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_ban)
            elif is_m_remote:
                item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_mic_off)
            else:
                item.setData(4, Qt.ItemDataRole.DecorationRole, None)

            if is_zero or is_muted_btn:
                item.setForeground(0, self._br_mute)
            else:
                item.setForeground(0, self._br_def)

        except RuntimeError:
            pass

    def toggle_mute(self):
        """Переключить состояние микрофона."""
        self.audio.is_muted = self.btn_mute.isChecked()
        ico = "assets/icon/mic_off.svg" if self.audio.is_muted else "assets/icon/mic_on.svg"
        self.btn_mute.setIcon(QIcon(resource_path(ico)))
        self.play_notification("mute" if self.audio.is_muted else "unmute")

    def toggle_deafen(self):
        """Переключить состояние звука (deaf). При включении также мьютит микрофон."""
        is_d = self.btn_deafen.isChecked()
        self.audio.is_deafened = is_d
        ico = "assets/icon/volume_off.svg" if is_d else "assets/icon/volume_on.svg"
        self.btn_deafen.setIcon(QIcon(resource_path(ico)))

        if is_d and not self.audio.is_muted:
            self.btn_mute.setChecked(True)
            self.toggle_mute()
        else:
            self.play_notification("mute" if is_d else "unmute")

    def on_connected(self, msg: dict):
        """Обработать успешное подключение к серверу.

        :param msg: ответ сервера с полем 'uid'
        """
        try:
            self.audio.my_uid = msg['uid']

            self.audio.start(
                self.app_settings.value("device_in_name"),
                self.app_settings.value("device_out_name")
            )

            self.play_notification("self_move")
            self._stack.setCurrentIndex(0)
            self._btn_reconnect.setEnabled(True)

            if self._my_status_icon:
                self.net.send_presence_update(self._my_status_icon, self._my_status_text)

        except Exception as e:
            import traceback
            print(f"[on_connected] EXCEPTION:\n{traceback.format_exc()}", flush=True)

    def on_video_frame(self, uid: int, q_image):
        """Передать кадр в окно просмотра стрима.

        :param uid: UID стримера
        :param q_image: QImage кадр
        """
        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_frame(q_image)

    def update_user_tree(self, users_map: dict):
        """Полностью перестроить дерево пользователей по данным с сервера.

        :param users_map: словарь {room: [user_dict, ...]}
        """
        user_rooms: dict = {}
        all_active_uids = set()

        for r, u_list in users_map.items():
            for u in u_list:
                user_rooms[u['uid']] = r
                all_active_uids.add(u['uid'])

        my_new_room = user_rooms.get(self.audio.my_uid, self.current_room)
        room_changed = (my_new_room != self.current_room)
        if room_changed:
            self.current_room = my_new_room
            self.play_notification("self_move")

        current_room_uids = {
            u['uid']
            for u in users_map.get(self.current_room, [])
            if u['uid'] != self.audio.my_uid
        }
        current_streaming_uids = {
            u['uid']
            for u in users_map.get(self.current_room, [])
            if u.get('is_streaming', False)
        }

        if not room_changed and self.audio.my_uid != 0:
            if current_room_uids - self.prev_room_uids:
                self.play_notification("other_join")
            if self.prev_room_uids - current_room_uids:
                self.play_notification("other_exit")
            if current_streaming_uids - self.prev_streaming_uids:
                self.play_notification("stream_on")
            stopped_streams = self.prev_streaming_uids - current_streaming_uids
            if stopped_streams:
                self.play_notification("stream_off")
                for uid in stopped_streams:
                    if uid in self.stream_windows:
                        self._on_stream_window_closed(uid)

        self.audio.cleanup_users(all_active_uids)
        self.video.cleanup_users(all_active_uids)

        self.prev_room_uids = current_room_uids
        self.prev_streaming_uids = current_streaming_uids

        self.tree.clear()
        self.known_uids.clear()

        font_r = self._font_room
        font_u = self._font_user

        all_rooms = sorted(list(set(users_map.keys()).union(set(self.default_rooms))),
                           key=lambda x: (x not in self.default_rooms, x))

        for room in all_rooms:
            cl_room = room.replace("#", "").strip()
            item_r = QTreeWidgetItem(self.tree, [f"# {cl_room.upper()}", "", "", ""])
            item_r.setFirstColumnSpanned(True)
            item_r.setFont(0, font_r)
            item_r.setForeground(0, self._br_gray)
            item_r.setFlags(item_r.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item_r.setData(0, Qt.ItemDataRole.UserRole, "ROOM_HEADER")
            item_r.setData(1, Qt.ItemDataRole.UserRole, room)

            for u in users_map.get(room, []):
                uid = u['uid']
                ip_addr = u.get('ip', '')
                if hasattr(self.audio, 'register_ip_mapping'):
                    self.audio.register_ip_mapping(uid, ip_addr)

                item_u = QTreeWidgetItem(item_r, [f"  {u['nick']}", "", "", "", ""])

                # --- ДОБАВЛЕНО: Кэширование SVG аватарок ---
                avatar_file = u.get('avatar', '1.svg')
                if avatar_file not in self._avatar_cache:
                    # Если аватарки еще нет в кэше, грузим с диска и сохраняем
                    avatar_path = resource_path(f"assets/avatars/{avatar_file}")
                    self._avatar_cache[avatar_file] = QIcon(avatar_path)

                # Берем готовую иконку из памяти (0 нагрузки на CPU)
                item_u.setIcon(0, self._avatar_cache[avatar_file])
                # -------------------------------------------

                item_u.setFont(0, font_u)
                item_u.setData(0, Qt.ItemDataRole.UserRole, uid)

                status_icon = u.get('status_icon', '')
                status_text = u.get('status_text', '')
                if status_icon:
                    if status_icon not in self._status_px_cache:
                        icon_path = resource_path(f"assets/status/{status_icon}")
                        px = QIcon(icon_path).pixmap(20, 20)
                        self._status_px_cache[status_icon] = px
                    item_u.setData(1, Qt.ItemDataRole.DecorationRole, self._status_px_cache[status_icon])
                    if status_text:
                        item_u.setToolTip(1, status_text)
                else:
                    item_u.setData(1, Qt.ItemDataRole.DecorationRole, None)

                item_u.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
                item_u.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)
                item_u.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter)

                self.known_uids[uid] = {
                    'item':        item_u,
                    'is_m':        u.get('mute', False),
                    'is_d':        u.get('deaf', False),
                    'is_s':        u.get('is_streaming', False),
                    'status_icon': status_icon,
                    'status_text': status_text,
                }

                watchers = u.get('watchers', [])
                if u.get('is_streaming', False) and watchers:
                    for watcher in watchers:
                        w_nick = watcher.get('nick', '?')
                        watcher_item = QTreeWidgetItem(item_u, [f"    {w_nick}", "", "", ""])
                        watcher_item.setFont(0, self._font_watcher)
                        watcher_item.setForeground(0, self._br_gray)
                        watcher_item.setFlags(watcher_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        watcher_item.setData(0, Qt.ItemDataRole.UserRole, None)

        self.tree.expandAll()
        self._update_known_users_registry(users_map)

    def refresh_ui(self):
        """Обновить визуальное состояние дерева (пинг, иконки, цвета ников).
        ОПТИМИЗИРОВАНО: UI дергается только при реальном изменении значений.
        """
        try:
            ping = self.net.current_ping
            # Оптимизация 1: Не парсим CSS 10 раз в секунду, если пинг не изменился
            if getattr(self, '_last_ping_val', -1) != ping:
                self._last_ping_val = ping
                self.ping_lbl.setText(f"Ping: {ping} ms")
                col = "#2ecc71" if ping < 60 else "#f1c40f" if ping < 150 else "#e74c3c"
                self.ping_lbl.setStyleSheet(f"color: {col}; font-weight: bold; font-size: 13px; margin-right: 10px;")

            now = time.time()

            if self._theme_dirty:
                self._theme_dirty = False
                self._c_def = QColor("#ecf0f1") if self._cache_theme == "Темная" else QColor("#444444")
                self._br_def = QBrush(self._c_def)

            with self.audio.users_lock:
                me_talk = (now - self.audio.last_voice_time < 0.3) and not self.audio.is_muted
                remote_snapshot = {
                    uid: (
                        u.last_packet_time,
                        u.is_locally_muted,
                        u.volume_zero,
                    )
                    for uid, u in self.audio.remote_users.items()
                }
                my_uid = self.audio.my_uid
                is_muted = self.audio.is_muted
                is_deafened = self.audio.is_deafened

            for uid, data in self.known_uids.items():
                item = data['item']
                is_m = data['is_m']
                is_d = data['is_d']
                is_s = data['is_s']

                curr_s = self.is_streaming if uid == my_uid else is_s
                curr_d = is_deafened if uid == my_uid else is_d

                # Кешируем состояние UI конкретного юзера, чтобы не дергать движок Qt вхолостую
                state = data.setdefault('ui_state', {})

                # Иконка стрима
                s_deco = self._px_live if curr_s else None
                if state.get('deco2') != s_deco:
                    item.setData(2, Qt.ItemDataRole.DecorationRole, s_deco)
                    state['deco2'] = s_deco

                # Иконка звука (deafen)
                d_deco = self._px_vol_off if curr_d else None
                if state.get('deco3') != d_deco:
                    item.setData(3, Qt.ItemDataRole.DecorationRole, d_deco)
                    state['deco3'] = d_deco

                # Иконка микрофона/бана
                m_deco = None
                talk = False
                if uid == my_uid:
                    talk = me_talk
                    if is_muted:
                        m_deco = self._px_mic_off
                else:
                    u_vals = remote_snapshot.get(uid)
                    talk = (now - u_vals[0] < 0.3) if u_vals else False
                    is_locally_muted = u_vals[1] if u_vals else False
                    is_vol_zero = u_vals[2] if u_vals else False

                    if is_locally_muted or is_vol_zero:
                        m_deco = self._px_ban
                    elif is_m:
                        m_deco = self._px_mic_off

                if state.get('deco4') != m_deco:
                    item.setData(4, Qt.ItemDataRole.DecorationRole, m_deco)
                    state['deco4'] = m_deco

                # Цвет ника (говорит/стримит/мут/обычный)
                if talk:
                    fg = self._br_talk
                elif curr_s:
                    fg = self._br_stream
                elif curr_d or is_m or (uid != my_uid and u_vals and (is_locally_muted or is_vol_zero)):
                    fg = self._br_mute
                else:
                    fg = self._br_def

                if state.get('fg') != fg:
                    item.setForeground(0, fg)
                    state['fg'] = fg

        except Exception as _e:
            pass  # Убрал печать исключения каждую миллисекунду, чтобы не забивать консоль при закрытии

    def on_tree_double_click(self, item, col):
        """Войти в комнату по двойному клику на заголовке.

        :param item: элемент дерева
        :param col: колонка клика
        """
        if item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
            self.net.send_json({"action": CMD_JOIN_ROOM, "room": item.data(1, Qt.ItemDataRole.UserRole)})

    def show_context_menu(self, pos):
        """Показать контекстный оверлей по правому клику на пользователе.

        :param pos: позиция клика в координатах дерева
        """
        item = self.tree.itemAt(pos)
        if not item:
            return

        uid = item.data(0, Qt.ItemDataRole.UserRole)
        if not uid or uid == "ROOM_HEADER":
            return

        if uid == self.audio.my_uid:
            from ui_dialogs import SelfStatusOverlayPanel
            item_rect = self.tree.visualItemRect(item)
            global_pos = self.tree.viewport().mapToGlobal(item_rect.bottomLeft())

            def _on_status_save(icon: str, text: str):
                self._my_status_icon = icon
                self._my_status_text = text
                self.app_settings.setValue("my_status_icon", icon)
                self.app_settings.setValue("my_status_text", text)
                self.net.send_presence_update(icon, text)

            SelfStatusOverlayPanel(
                self._my_status_icon,
                self._my_status_text,
                global_pos,
                on_save=_on_status_save,
                parent=self,
            ).show()
            return

        nick = item.text(0).strip()

        current_vol = 1.0
        with self.audio.users_lock:
            u = self.audio.remote_users.get(uid)
            if u is not None:
                current_vol = u.volume
            else:
                ip = self.audio.uid_to_ip.get(uid, '')
                if ip:
                    current_vol = float(self.audio.settings.value(f"vol_ip_{ip}", 1.0))

        item_rect = self.tree.visualItemRect(item)
        global_pos = self.tree.viewport().mapToGlobal(item_rect.bottomLeft())

        user_data = self.known_uids.get(uid)
        is_streaming = user_data.get('is_s', False) if user_data else False

        watch_cb = None
        if is_streaming:
            _uid = uid
            _nick_txt = item.text(0)
            watch_cb = lambda: self.open_video_window(_uid, _nick_txt)

        UserOverlayPanel(
            nick, current_vol, uid, self.audio, global_pos,
            parent=self,
            is_streaming=is_streaming,
            on_watch_stream=watch_cb,
            net=self.net,
        ).show()

    def open_video_window(self, uid: int, nick: str):
        """Открыть или поднять окно просмотра трансляции.

        :param uid: UID стримера
        :param nick: ник стримера
        """
        if uid not in self.stream_windows or not self.stream_windows[uid].isVisible():
            w = VideoWindow(nick)
            w.uid = uid
            w.set_net(self.net)
            w.window_closed.connect(self._on_stream_window_closed)

            w.overlay_mute_toggled.connect(lambda: self.btn_mute.click())
            w.overlay_deafen_toggled.connect(lambda: self.btn_deafen.click())
            w.overlay_stop_watch.connect(lambda _uid=uid: self._on_stream_window_closed(_uid))

            self.audio.status_changed.connect(w.sync_audio_state)
            w.sync_audio_state(self.audio.is_muted, self.audio.is_deafened)

            w.overlay_stream_volume_changed.connect(self.audio.set_stream_volume)
            w.overlay._vol_popup.set_value(self.audio.stream_volume)

            w.quality_changed.connect(
                lambda sf, _uid=uid: self.net.send_quality_request(sf)
            )
            w.viewer_keyframe_needed.connect(
                lambda _uid=uid: self.net.request_viewer_keyframe(_uid)
            )

            w.show()
            self.stream_windows[uid] = w
            self.net.send_json({"action": "stream_watch_start", "streamer_uid": uid})
        else:
            self.stream_windows[uid].raise_()
            self.stream_windows[uid].activateWindow()

    def _on_stream_window_closed(self, uid: int):
        """Очистить ресурсы при закрытии окна трансляции.

        :param uid: UID стримера
        """
        w = self.stream_windows.get(uid)
        if w is not None:
            try:
                self.audio.status_changed.disconnect(w.sync_audio_state)
            except (RuntimeError, TypeError):
                pass
        self.stream_windows.pop(uid, None)
        self.net.send_json({"action": "stream_watch_stop", "streamer_uid": uid})

        self.video.stop_viewer_for_uid(uid)

        if w is not None:
            try:
                w.deleteLater()
            except RuntimeError:
                pass

        gc.collect()

        def _deferred_cleanup():
            gc.collect()
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetProcessWorkingSetSizeEx(
                    kernel32.GetCurrentProcess(),
                    ctypes.c_size_t(0xFFFFFFFF),
                    ctypes.c_size_t(0xFFFFFFFF),
                    0,
                )
            except Exception:
                pass
        QTimer.singleShot(2500, _deferred_cleanup)

    def open_settings(self):
        """Открыть диалог настроек и перезагрузить хоткеи/аудио при применении."""
        dlg = SettingsDialog(self.audio, self)
        if dlg.exec():
            self.setup_hotkeys()
            self.audio.start(self.app_settings.value("device_in_name"), self.app_settings.value("device_out_name"))
        dlg.deleteLater()
        dlg = None  # <--- Убираем локальную ссылку

        # Заставляем Python удалить все циклические ссылки от интерфейса
        import gc
        QTimer.singleShot(200, gc.collect)

    def open_status_dialog(self):
        """Открыть диалог выбора статуса пользователя (резервный метод).

        Основной UX — правый клик по своему нику в дереве.
        """
        from ui_dialogs import StatusDialog
        dlg = StatusDialog(self._my_status_icon, self._my_status_text, parent=self)
        if dlg.exec():
            icon, text = dlg.get_result()
            self._my_status_icon = icon
            self._my_status_text = text
            self.app_settings.setValue("my_status_icon", icon)
            self.app_settings.setValue("my_status_text", text)
            self.net.send_presence_update(icon, text)

    def open_soundboard(self):
        """Открыть или закрыть панель soundboard (toggle)."""
        from ui_dialogs import SoundboardPanel

        try:
            if self._sb_panel is not None:
                if self._sb_panel.isVisible():
                    self._sb_panel.close()
                    self._sb_panel = None
                    return
                else:
                    self._sb_panel.deleteLater()
                    self._sb_panel = None
        except RuntimeError:
            self._sb_panel = None

        panel = SoundboardPanel(self.net, self)
        self._sb_panel = panel
        panel.show_above(self.btn_sb)

    def _on_soundboard_played(self, from_nick: str):
        """Показать жёлтый тост и обновить метку автора в открытых soundboard-панелях.

        :param from_nick: ник пользователя, нажавшего кнопку soundboard
        """
        self._sb_toast.setText(f"🎵  {from_nick}  включил звук")
        self._sb_toast.adjustSize()
        tw = self._sb_toast.width()
        tx = (self.width() - tw) // 2
        ty = self._bottom_bar.y() - self._sb_toast.height() - 8
        self._sb_toast.move(tx, max(4, ty))
        self._sb_toast.raise_()
        self._sb_toast.setVisible(True)
        self._sb_toast_timer.start()

        try:
            if self._sb_panel is not None and self._sb_panel.isVisible():
                self._sb_panel.flash_from_nick(from_nick)
        except (RuntimeError, AttributeError):
            pass

        for w in list(self.stream_windows.values()):
            try:
                if w.isVisible() and w._sb_panel is not None and w._sb_panel.isVisible():
                    w._sb_panel.flash_from_nick(from_nick)
            except (RuntimeError, AttributeError):
                pass

    def _on_nudge_received(self):
        """Нас пнули — показать красный тост."""
        self._show_nudge_toast("👟  Тебя пнули!")

    def _on_nudge_triggered(self, target_nick: str, voter_nick: str):
        """Broadcast: кого-то пнули в комнате — показать информационный тост.

        :param target_nick: ник того, кого пнули
        :param voter_nick: ник того, кто пнул
        """
        self._show_nudge_toast(f"👟  {voter_nick} пнул {target_nick}!")

    def _show_nudge_toast(self, text: str):
        """Показать красный тост в правом нижнем углу на 4 секунды.

        Lazy-init: объект создаётся один раз при первом вызове.

        :param text: текст тоста
        """
        if not hasattr(self, '_nudge_toast_lbl'):
            lbl = QLabel(self)
            lbl.setObjectName("nudgeToast")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lbl.setStyleSheet("""
                QLabel#nudgeToast {
                    background-color: rgba(180, 30, 30, 0.88);
                    color: #ffffff;
                    border: 1px solid rgba(255, 80, 80, 0.60);
                    border-radius: 8px;
                    padding: 6px 14px;
                    font-size: 13px;
                    font-weight: bold;
                }
            """)
            lbl.hide()
            self._nudge_toast_lbl = lbl
            self._nudge_toast_timer = QTimer(self)
            self._nudge_toast_timer.setSingleShot(True)
            self._nudge_toast_timer.timeout.connect(lbl.hide)

        self._nudge_toast_lbl.setText(text)
        self._nudge_toast_lbl.adjustSize()
        mw = self.width()
        mh = self.height()
        tw = self._nudge_toast_lbl.width()
        th = self._nudge_toast_lbl.height()
        self._nudge_toast_lbl.move(mw - tw - 14, mh - th - 60)
        self._nudge_toast_lbl.raise_()
        self._nudge_toast_lbl.show()
        self._nudge_toast_timer.stop()
        self._nudge_toast_timer.start(4000)

    def _update_known_users_registry(self, users_map: dict):
        """Обновить реестр известных пользователей (known_users.json).

        Кэш загружается с диска один раз при первом вызове,
        запись на диск — только при реальных изменениях данных.

        :param users_map: словарь {room: [user_dict, ...]}
        """
        REGISTRY_FILE = "known_users.json"

        if not hasattr(self, '_known_users_cache'):
            try:
                if os.path.exists(REGISTRY_FILE):
                    with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
                        self._known_users_cache = json.load(f)
                else:
                    self._known_users_cache = {}
            except Exception:
                self._known_users_cache = {}

        changed = False
        now_str = time.strftime("%Y-%m-%d %H:%M")

        for room, u_list in users_map.items():
            for u in u_list:
                ip = u.get('ip', '')
                nick = u.get('nick', '')
                if not ip:
                    continue
                entry = self._known_users_cache.get(ip, {})
                if entry.get('nick') != nick or entry.get('last_seen') != now_str:
                    self._known_users_cache[ip] = {
                        'nick': nick,
                        'first_seen': entry.get('first_seen', now_str),
                        'last_seen': now_str,
                    }
                    changed = True

        if changed:
            try:
                with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self._known_users_cache, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[UI] known_users.json write error: {e}")

    def _start_silent_update_check(self):
        """Запустить фоновую проверку обновлений без всплывающих окон."""
        if not GITHUB_REPO:
            return

        from updater import check_for_updates_async

        def _on_found(version: str, url: str):
            QTimer.singleShot(0, lambda: self._show_update_banner(version, url))

        check_for_updates_async(on_update_found=_on_found)

    def _show_update_banner(self, version: str, url: str):
        """Показать зелёный баннер с сообщением о доступном обновлении.

        :param version: номер новой версии
        :param url: ссылка на релиз
        """
        self._update_banner.setText(
            f"🎉 Доступна новая версия v{version}  —  нажмите чтобы обновить"
        )
        self._update_banner.setVisible(True)

    def closeEvent(self, e):
        """Корректно завершить приложение при закрытии окна."""
        try:
            self._whisper_overlay.hide_overlay()
            self._whisper_overlay.deleteLater()
        except Exception:
            pass
        self.audio.stop()
        self.net.running = False
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()
        e.accept()

    def on_audio_status_changed(self, mute: bool, deaf: bool):
        """Отправить обновлённый статус аудио на сервер.

        :param mute: состояние микрофона
        :param deaf: состояние звука
        """
        self.net.send_status_update(mute, deaf)

    def update_stream_button_icon(self):
        """Обновить иконку и стиль кнопки трансляции согласно текущему состоянию."""
        if self.is_streaming:
            path = resource_path("assets/icon/stream_on.svg")
            self.btn_stream.setIcon(QIcon(path))
            self.btn_stream.setStyleSheet(
                "background-color: rgba(46,204,113,0.28); "
                "border: 1px solid rgba(46,204,113,0.65); "
                "border-radius: 10px;"
            )
        else:
            path = resource_path("assets/icon/stream_off.svg")
            self.btn_stream.setIcon(QIcon(path))
            self.btn_stream.setStyleSheet("")

    def toggle_stream(self):
        """Запустить или остановить трансляцию экрана."""
        from ui_dialogs import StreamSettingsDialog
        if not self.is_streaming:
            dialog = StreamSettingsDialog(self)
            if dialog.exec():
                settings = dialog.get_settings()
                self.audio.set_stream_audio_enabled(settings.get("stream_audio", False))
                success = self.video.start_streaming(settings)
                if success:
                    self.is_streaming = True
                else:
                    self.audio.set_stream_audio_enabled(False)
                    self.btn_stream.setChecked(False)
                    dialog.deleteLater()
                    dialog = None
                    import gc;
                    QTimer.singleShot(200, gc.collect)  # <--- СЮДА
                    return
            else:
                self.btn_stream.setChecked(False)
                dialog.deleteLater()
                dialog = None
                import gc;
                QTimer.singleShot(200, gc.collect)  # <--- И СЮДА
                return

            dialog.deleteLater()
            dialog = None
            import gc;
            QTimer.singleShot(200, gc.collect)  # <--- И СЮДА (ПРИ УСПЕХЕ)
        else:
            self.video.stop_streaming()
            self.audio.set_stream_audio_enabled(False)
            self.is_streaming = False

        self.update_stream_button_icon()

        action = CMD_STREAM_START if self.is_streaming else CMD_STREAM_STOP
        self.net.send_json({"action": action})
        self.refresh_ui()