import os
import base64
import gc
import json
import sounddevice as sd
import soundfile as sf
import winsound
import keyboard
import time

try:
    import pyautogui
    pyautogui.FAILSAFE = False
    _PYAUTOGUI_AVAILABLE = True
    try:
        print(f"[RC-INIT] pyautogui OK, version={getattr(pyautogui,'__version__','?')}")
    except Exception:
        pass
except ImportError as _e:
    _PYAUTOGUI_AVAILABLE = False
    print(f"[RC-INIT] pyautogui ImportError: {_e!r}")

# WinAPI SendInput-бэкенд для надёжной инъекции (Unicode-текст, модификаторы).
try:
    from core import win_input as _win_input
    _WIN_INPUT_AVAILABLE = _win_input.WIN_INPUT_AVAILABLE
    print(f"[RC-INIT] win_input available={_WIN_INPUT_AVAILABLE}")
except Exception as _e:
    _win_input = None
    _WIN_INPUT_AVAILABLE = False
    print(f"[RC-INIT] win_input import failed: {_e!r}")
from .video_engine import VideoEngine
from .ui_video import VideoWindow, StreamerAnnotationOverlay
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QHeaderView, QMessageBox, QStackedWidget,
                             QFrame, QSizeGrip, QFileDialog, QLineEdit,
                             QScrollArea, QDialog, QCheckBox,
                             QMenu, QApplication, QSystemTrayIcon)
from PyQt6.QtCore import Qt, QTimer, QSize, QSettings, QRect, QPoint, QEvent, QThread, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QIcon, QFont, QFontDatabase, QBrush, QColor, QCursor, QFontMetrics

from config import (
    resource_path,
    KNOWN_USERS_PATH,
    QUICK_MSG_MAX_LEN,
    CMD_JOIN_ROOM, CMD_STREAM_START, CMD_STREAM_STOP,
    CMD_SOUNDBOARD, CMD_SERVER_TRANSFER, CMD_SERVER_MIGRATE,
    CMD_FORCE_MUTED,
    CHAT_MSG_MAX_LEN,
    CMD_DRAW_STROKE,
    RC_ESC_STOP_COUNT, RC_ESC_WINDOW_SEC,
    is_anonymous_uid,
)
from audio_engine import AudioHandler
from network_engine import NetworkClient
from ui_dialogs import (UserOverlayPanel, WhisperSystemOverlay, SelfStatusOverlayPanel,
                        FileReceiverWorker, FileTransferProgressWidget,
                        _show_float_widget, _format_size)
from ui_dialogs.ui_settings import SettingsDialog
from ui_dialogs.ui_soundboard import SoundboardPanel, SoundboardDialog
from .ui_widgets import QuickMsgBubble, CustomTitleBar, ChatPanel
from .ui_channel import _CreateChannelDialog, _ChannelPasswordDialog
from .ui_webcamera import CircularVideoWindow, WebcamMixin
from .avatar_ring import (
    make_avatar_with_pulse_ring, pulse_phase, quantize_phase,
)
from version import APP_VERSION, APP_NAME, GITHUB_REPO

class HoldToDisconnectButton(QPushButton):

    HOLD_MS = 1000
    TICK_MS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self._callback = None
        self._progress = 0.0
        self._hold_sound_path: str | None = None

        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._on_hold_complete)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._tick)

    def set_hold_callback(self, callback):
        self._callback = callback

    def set_hold_sound(self, path: str) -> None:
        import os
        resolved = os.path.normpath(os.path.abspath(path))
        if os.path.isfile(resolved):
            self._hold_sound_path = resolved
        else:
            print(f"[HoldToDisconnectButton] hold_sound НЕ НАЙДЕН: {resolved}")
            self._hold_sound_path = None

    def _start_hold_sound(self) -> None:
        if not self._hold_sound_path:
            return
        try:
            import winsound
            winsound.PlaySound(
                self._hold_sound_path,
                winsound.SND_FILENAME | winsound.SND_ASYNC
                | winsound.SND_LOOP | winsound.SND_NODEFAULT,
            )
        except Exception as ex:
            print(f"[HoldToDisconnectButton] PlaySound error: {ex}")

    def _stop_hold_sound(self) -> None:
        if not self._hold_sound_path:
            return
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except Exception:
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._progress = 0.0
            self._hold_timer.start(self.HOLD_MS)
            self._tick_timer.start()
            self._start_hold_sound()
            self._apply_style()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._cancel()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self._cancel()
        super().leaveEvent(event)

    def _tick(self):
        self._progress = min(self._progress + self.TICK_MS / self.HOLD_MS, 1.0)
        self._apply_style()

    def _cancel(self):
        if self._hold_timer.isActive() or self._tick_timer.isActive():
            self._hold_timer.stop()
            self._tick_timer.stop()
            self._stop_hold_sound()
            self._progress = 0.0
            self.setStyleSheet("")          # вернуть стиль из родительского QSS

    def _on_hold_complete(self):
        self._tick_timer.stop()
        self._stop_hold_sound()
        self._progress = 1.0
        self._apply_style()
        if self._callback:
            QTimer.singleShot(80, self._callback)
        QTimer.singleShot(120, lambda: self.setStyleSheet(""))

    def _apply_style(self):
        p = self._progress
        bg_empty  = "rgba(255,255,255,0.14)"
        bg_fill   = "rgba(192,57,43,0.75)"
        border_c  = "rgba(231,76,60,0.70)"

        if p <= 0.0:
            gradient = bg_empty
        elif p >= 1.0:
            gradient = bg_fill
        else:
            lo = f"{max(p - 0.005, 0.0):.4f}"
            hi = f"{min(p + 0.005, 1.0):.4f}"
            gradient = (
                f"qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                f"stop:0 {bg_fill},"
                f"stop:{lo} {bg_fill},"
                f"stop:{hi} {bg_empty},"
                f"stop:1 {bg_empty})"
            )

        self.setStyleSheet(
            f"QPushButton#barBtnDisconnect {{"
            f"  background: {gradient};"
            f"  border: 1px solid {border_c};"
            f"  border-radius: 10px;"
            f"  padding: 4px;"
            f"}}"
        )


class MainWindow(WebcamMixin, QMainWindow):
    # Эмитится из потока библиотеки keyboard, когда стример нажал ESC нужное
    # число раз. Доставляется в Qt-поток через очередь сигналов.
    _rc_esc_triggered = pyqtSignal()

    def __init__(self, ip, nick, avatar):
        super().__init__()
        font_path = resource_path("assets/font/MyFont.ttf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        self.custom_font_family = QFontDatabase.applicationFontFamilies(font_id)[0] if font_id != -1 else "Segoe UI"

        self.ip, self.nick, self.avatar = ip, nick, avatar
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.known_uids = {}
        self._icon_size = 24
        self._my_status_icon = None
        from PyQt6.QtGui import QFont
        self._font_room = QFont()
        self._font_user = QFont()
        self.current_room = "General"
        self.default_rooms = ["General"]
        self._channel_list: list = [
            {'name': 'General', 'has_password': False, 'permanent': True}
        ]
        self._pending_channel_join: str | None = None
        self.sound_files = {
            "self_move":  resource_path("assets/music/user_join.wav"),
            "other_join": resource_path("assets/music/user_join.wav"),
            "other_exit": resource_path("assets/music/disconnected.wav"),
            "mute":       resource_path("assets/music/mute.wav"),
            "unmute":     resource_path("assets/music/unmute.wav"),
            "stream_on":      resource_path("assets/music/stream_on.wav"),
            "stream_off":     resource_path("assets/music/stream_off.wav"),
            "quick_msg":      resource_path("assets/music/message.wav"),
            "friend_connect": resource_path("assets/music/friend_connect.wav"),
            "file_received":  resource_path("assets/music/file.wav"),
            "chat_msg_in":    resource_path("assets/music/message.wav"),
            "chat_msg_out":   resource_path("assets/music/message_send.wav"),
        }
        self.prev_room_uids: set = set()
        self.prev_streaming_uids: set = set()

        self.prev_all_uids: set = set()
        self._server_users_initialized: bool = False

        self.audio = AudioHandler()
        self.net = NetworkClient(self.audio)

        self._resize_margin = 6
        self._resize_direction: str | None = None
        self._resize_start_pos: QPoint | None = None
        self._resize_start_geom: QRect | None = None
        self.setMouseTracking(True)

        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

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
        self.apply_theme(self.app_settings.value("theme", "Темная"))

        self._tray_icon = QSystemTrayIcon(
            QIcon(resource_path("assets/icon/logo.ico")), self
        )
        _tray_menu = QMenu()
        _act_show = _tray_menu.addAction("Показать InPulse")
        _act_show.triggered.connect(self._tray_show)
        _tray_menu.addSeparator()
        _act_quit = _tray_menu.addAction("Выйти")
        _act_quit.triggered.connect(self._tray_quit)
        self._tray_icon.setContextMenu(_tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.setToolTip(f"{APP_NAME} — {self.nick}")
        self._tray_icon.show()
        self._force_quit = False
        self._returning_to_lobby = False
        self._chat_panel.set_my_uid(0)
        self._chat_panel.message_sent.connect(self._on_chat_panel_send)
        self._chat_panel.media_send_requested.connect(self._on_chat_media_requested)
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
        self.video.stream_stats_updated.connect(self.on_stream_stats_updated)
        self.net.soundboard_played.connect(self._on_soundboard_played)
        self.net.nudge_received.connect(self._on_nudge_received)
        self.net.nudge_triggered.connect(self._on_nudge_triggered)
        self.net.force_muted.connect(self._on_force_muted)
        self.net.kicked.connect(self._on_kicked_by_host)
        self.net.banned.connect(self._on_banned_by_host)
        self.net.remote_control_requested.connect(self._on_rc_request_global)
        self.net.remote_control_event.connect(self._on_rc_event_received)
        # 3×ESC у стримера → остановка управления (сигнал из keyboard-потока)
        self._rc_esc_triggered.connect(self._on_rc_esc_stop)
        self._rc_esc_times: list = []     # ts последних нажатий ESC
        self._rc_esc_hook = None          # хендл keyboard-хука (None = не активен)
        self.net.file_offer_received.connect(self._on_file_offer_received)
        self.net.quick_msg_received.connect(self._on_quick_msg_received)
        self._quick_bubbles: dict[int, tuple] = {}
        self.net.chat_msg_received.connect(self._on_chat_msg_received)
        self.net.chat_history_received.connect(self._on_chat_history_received)
        self.net.chat_media_received.connect(self._on_chat_media_received)
        self.net.typing_received.connect(self._on_typing_received)
        self.net.camera_frame_received.connect(self.on_network_camera_frame)
        self._chat_panel.typing_started.connect(self.net.send_typing)
        self.net.become_host.connect(self._on_become_host)
        self.net.server_migrating.connect(self._on_server_migrating)
        self.net.channel_created.connect(self._on_channel_created)
        self.net.channel_deleted.connect(self._on_channel_deleted)
        self.net.join_room_denied.connect(self._on_join_room_denied)
        self.net.channel_auth_ok.connect(self._on_channel_auth_ok)
        self.net.channel_list_updated.connect(self._on_channel_list_updated)
        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(100)
        self.setup_hotkeys()
        self.net.connect_to_server(self.ip, self.nick, self.avatar)
        self.is_streaming = False
        # ── Веб-камера: реестр круглых окон + флаг своей камеры ──────────
        self.init_webcam_state()
        # Кэш аватарок с пульсирующим кольцом: ключ (avatar_name, phase_step)
        self._avatar_ring_cache: dict = {}
        # Время старта — для расчёта фазы пульсации по ui_timer.
        self._cam_pulse_t0 = time.perf_counter()
        self._sb_panel = None
        self._streamer_draw_overlay: StreamerAnnotationOverlay | None = None
        self.net.draw_stroke_received.connect(self._on_draw_stroke_received)
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
        self._whisper_end_timer = QTimer()
        self._whisper_end_timer.setSingleShot(True)
        self._whisper_end_timer.setInterval(1500)
        self._whisper_end_timer.timeout.connect(self._on_whisper_ended)
        self._whisper_overlay = WhisperSystemOverlay()
        self._start_silent_update_check()
        self._cache_theme = "Темная"
        self._theme_dirty = False
        self._c_talk   = QColor("#2ecc71")
        self._c_mute   = QColor("#e74c3c")
        self._c_stream = QColor("#3498db")
        self._c_def    = QColor("#ecf0f1")
        self._icon_size = QSize(26, 26)
        self._br_talk   = QBrush(self._c_talk)
        self._br_mute   = QBrush(self._c_mute)
        self._br_stream = QBrush(self._c_stream)
        self._br_def    = QBrush(self._c_def)
        self._br_gray   = QBrush(QColor("#888888"))   # для заголовков комнат и watchers
        self._br_gold   = QBrush(QColor("#f5c518"))   # золотой — для ника хоста
        self._px_live      = QIcon(resource_path("assets/icon/live.svg")).pixmap(25, 25)
        self._px_vol_off   = QIcon(resource_path("assets/icon/volume_off.svg")).pixmap(self._icon_size)
        self._px_mic_off   = QIcon(resource_path("assets/icon/mic_off.svg")).pixmap(self._icon_size)
        self._px_ban       = QIcon(resource_path("assets/icon/ban.svg")).pixmap(self._icon_size)
        self._px_cam       = QIcon(resource_path("assets/icon/webcamera.svg")).pixmap(self._icon_size)
        self._status_px_cache: dict = {}
        self._my_status_icon: str = self.app_settings.value("my_status_icon", "")
        self._my_status_text: str = self.app_settings.value("my_status_text", "")
        self._font_room    = QFont(self.custom_font_family, 12)
        self._font_room.setBold(True)
        self._font_user    = QFont(self.custom_font_family, 14)
        self._font_watcher = QFont(self.custom_font_family, 11)

    def setup_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.nick}")
        self.setMinimumSize(440, 500)
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
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Ник", "", "", "", ""])
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(32, 32))
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.setRootIsDecorated(False)

        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)  # статус дела
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)  # live/stream
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # deaf/vol_off
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)  # mute/mic_off
        header.resizeSection(1, 30)   # статус — чуть уже (иконка 20×20)
        header.resizeSection(2, 35)
        header.resizeSection(3, 35)
        header.resizeSection(4, 35)
        header.hide()

        self.tree.itemDoubleClicked.connect(self.on_tree_double_click)
        self.tree.itemClicked.connect(self._on_tree_clicked_cam)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)

        layout.addWidget(self.tree, stretch=1)

        self._update_banner = QPushButton()
        self._update_banner.setObjectName("updateBanner")
        self._update_banner.setVisible(False)
        self._update_banner.clicked.connect(self.open_settings)  # откроет вкладку Версия
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

        # Баннер «Управление активно» — встроен в дерево над кнопкой Чат
        self._rc_access_banner = QFrame()
        self._rc_access_banner.setObjectName("rcAccessBanner")
        self._rc_access_banner.setVisible(False)
        self._rc_access_banner.setFixedHeight(40)
        self._rc_access_banner.setStyleSheet("""
            QFrame#rcAccessBanner {
                background-color: rgba(231, 76, 60, 200);
                border-radius: 8px;
                border: 1px solid rgba(255,255,255,60);
            }
        """)
        _rc_banner_lay = QHBoxLayout(self._rc_access_banner)
        _rc_banner_lay.setContentsMargins(10, 4, 10, 4)
        _rc_banner_lay.setSpacing(8)
        _rc_ico = QLabel("🖱️")
        _rc_ico.setStyleSheet("background:transparent; border:none; font-size:14px;")
        _rc_lbl = QLabel("Управление активно")
        _rc_lbl.setStyleSheet(
            "background:transparent; border:none; color:#fff; font-size:12px; font-weight:600;"
        )
        _rc_stop_btn = QPushButton("Отменить")
        _rc_stop_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,35);
                border: 1px solid rgba(255,255,255,70);
                border-radius: 6px;
                color: #fff;
                font-size: 11px;
                padding: 3px 8px;
            }
            QPushButton:hover { background: rgba(255,255,255,65); }
        """)
        _rc_stop_btn.clicked.connect(self._hide_rc_streamer_banner)
        _rc_banner_lay.addWidget(_rc_ico)
        _rc_banner_lay.addWidget(_rc_lbl, stretch=1)
        _rc_banner_lay.addWidget(_rc_stop_btn)
        layout.addWidget(self._rc_access_banner)

        self._btn_chat_main = QPushButton("💬  Чат")
        self._btn_chat_main.setCheckable(True)
        self._btn_chat_main.setFixedHeight(36)
        self._btn_chat_main.setObjectName("btnChatMain")
        self._btn_chat_main.setStyleSheet("""
            QPushButton#btnChatMain {
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                color: #8899bb;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.5px;
                margin: 4px 0px 2px 0px;
            }
            QPushButton#btnChatMain:hover {
                background-color: rgba(255, 255, 255, 0.08);
                border-color: rgba(255, 255, 255, 0.15);
                color: #c8d0e0;
            }
            QPushButton#btnChatMain:checked {
                background-color: rgba(91, 142, 245, 0.12);
                border-color: rgba(91, 142, 245, 0.35);
                color: #5b8ef5;
            }
        """)
        self._btn_chat_main.clicked.connect(
            lambda checked: self._toggle_chat_panel(checked)
        )
        self._chat_has_unread = False

        self._chat_badge = QLabel("● новое", self._btn_chat_main)
        self._chat_badge.setStyleSheet("""
            QLabel {
                color: #e74c3c;
                font-size: 10px;
                font-weight: bold;
                background: transparent;
                border: none;
                padding: 0px;
            }
        """)
        self._chat_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._chat_badge.setVisible(False)

        self._chat_blink_timer = QTimer(self)
        self._chat_blink_timer.setInterval(900)
        self._chat_blink_timer.timeout.connect(self._on_chat_badge_blink)
        self._chat_blink_state = True

        layout.addWidget(self._btn_chat_main)

        # ── «Завершить» — текстом под кнопкой «Чат» (бывшая lobby.svg) ──
        self._btn_leave_text = QPushButton("Завершить")
        self._btn_leave_text.setFixedHeight(34)
        self._btn_leave_text.setObjectName("btnLeaveText")
        self._btn_leave_text.setStyleSheet("""
            QPushButton#btnLeaveText {
                background-color: rgba(231, 76, 60, 0.10);
                border: 1px solid rgba(231, 76, 60, 0.30);
                border-radius: 8px;
                color: #e07a6e;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.5px;
                margin: 0px 0px 2px 0px;
            }
            QPushButton#btnLeaveText:hover {
                background-color: rgba(231, 76, 60, 0.20);
                border-color: rgba(231, 76, 60, 0.50);
                color: #ff8c7a;
            }
        """)
        self._btn_leave_text.clicked.connect(self._disconnect_and_show_lobby)
        layout.addWidget(self._btn_leave_text)

        self._bottom_bar = QFrame()
        self._bottom_bar.setObjectName("bottomBar")
        self._bottom_bar.setFixedHeight(72)

        btns = QHBoxLayout(self._bottom_bar)
        btns.setContentsMargins(12, 0, 12, 0)
        btns.setSpacing(8)

        self.btn_mute = QPushButton()
        self.btn_mute.setCheckable(True)
        self.btn_mute.setFixedSize(46, 46)
        self.btn_mute.setObjectName("barBtnMic")
        self.btn_mute.setIcon(QIcon(resource_path("assets/icon/mic_on.svg")))
        self.btn_mute.setIconSize(QSize(26, 26))
        self.btn_mute.clicked.connect(self.toggle_mute)

        self.btn_deafen = QPushButton()
        self.btn_deafen.setCheckable(True)
        self.btn_deafen.setFixedSize(46, 46)
        self.btn_deafen.setObjectName("barBtnDeafen")
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

        # ── Кнопка веб-камеры (на месте бывшей lobby.svg) ──────────────
        self.btn_cam = QPushButton()
        self.btn_cam.setCheckable(True)
        self.btn_cam.setFixedSize(46, 46)
        self.btn_cam.setObjectName("barBtnCam")
        self.btn_cam.setIcon(QIcon(resource_path("assets/icon/webcamera.svg")))
        self.btn_cam.setIconSize(QSize(26, 26))
        self.btn_cam.setToolTip("Веб-камера")
        self.btn_cam.clicked.connect(self.toggle_camera)

        self._stream_conn_lbl = QLabel()
        self._stream_conn_lbl.setFixedSize(22, 22)
        self._stream_conn_lbl.setScaledContents(True)
        self._stream_conn_lbl.setVisible(False)
        self._stream_conn_lbl.setPixmap(
            QIcon(resource_path("assets/icon/connection_bad.svg")).pixmap(QSize(22, 22))
        )

        self._latency_btn = QPushButton("-- мс")
        self._latency_btn.setFixedSize(64, 46)
        self._latency_btn.setObjectName("barBtn")
        self._latency_btn.setStyleSheet(
            "QPushButton#barBtn { font-size: 11px; font-weight: bold;"
            " color: #8899aa; padding: 4px; }"
        )

        btn_set = QPushButton()
        btn_set.setFixedSize(46, 46)
        btn_set.setObjectName("barBtn")
        btn_set.setIcon(QIcon(resource_path("assets/icon/settings.svg")))
        btn_set.setIconSize(QSize(26, 26))
        btn_set.clicked.connect(self.open_settings)

        btns.addWidget(self.btn_mute)
        btns.addWidget(self.btn_deafen)
        btns.addWidget(self.btn_cam)
        btns.addWidget(self.btn_sb)
        btns.addWidget(self.btn_stream)
        btns.addWidget(self._stream_conn_lbl)
        btns.addStretch()
        btns.addWidget(self._latency_btn)
        btns.addWidget(btn_set)

        layout.addWidget(self._bottom_bar)

        self._chat_panel = ChatPanel(
            my_uid=0,
            current_room_fn=lambda: self.current_room,
        )
        self._chat_panel.setVisible(False)

        _main_row = QWidget()
        _main_row.setObjectName("mainRow")
        _mr_lay = QHBoxLayout(_main_row)
        _mr_lay.setContentsMargins(0, 0, 0, 0)
        _mr_lay.setSpacing(0)
        _mr_lay.addWidget(main_page, stretch=1)
        _mr_lay.addWidget(self._chat_panel)

        self._stack.addWidget(_main_row)

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
        print(f"[UI] Connection error: {error_msg}")
        self._lost_title_lbl.setText("Сервер недоступен")
        self._lost_status_lbl.setText(f"Не удалось подключиться:\n{error_msg}")
        self._btn_reconnect.setEnabled(True)
        self._stack.setCurrentIndex(1)

    def on_connection_lost(self):
        print("[UI] Connection lost — showing reconnect screen")
        self._lost_title_lbl.setText("Соединение потеряно")
        self._lost_status_lbl.setText("Автоматическое переподключение...")
        self._btn_reconnect.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def on_connection_restored(self):
        print("[UI] Connection restored — returning to main screen")
        self._stack.setCurrentIndex(0)
        self._btn_reconnect.setEnabled(True)

    def on_reconnect_failed(self):
        print("[UI] All silent reconnect attempts failed")
        self._lost_title_lbl.setText("Нет соединения")
        self._lost_status_lbl.setText("Не удалось переподключиться автоматически.\nНажмите кнопку ниже или проверьте сеть.")
        self._btn_reconnect.setEnabled(True)

    def _on_manual_reconnect_clicked(self):
        self._lost_status_lbl.setText("Попытка переподключения...")
        self._btn_reconnect.setEnabled(False)
        self.net.manual_reconnect()


    def _on_become_host(self):

        print("[UI] _on_become_host: запускаем встроенный сервер")

        self._lost_title_lbl.setText("Переключение хоста")
        self._lost_status_lbl.setText(
            "Запускаем сервер на вашем ПК...\nОстальные подключатся автоматически."
        )
        self._btn_reconnect.setEnabled(False)
        self._stack.setCurrentIndex(1)

        try:
            from server import EmbeddedServerManager
            from client_main.ui_login import load_server_name
            from network_engine.server_discovery import get_local_radmin_ip
            host_ip     = get_local_radmin_ip()
            server_name = load_server_name()
            EmbeddedServerManager.get().start(host_ip, self.nick, server_name=server_name)
        except Exception as e:
            print(f"[UI] _on_become_host error: {e}")
            self._lost_status_lbl.setText(
                f"Ошибка запуска сервера:\n{e}\n\nПопробуйте перезапустить."
            )
            self._btn_reconnect.setEnabled(True)
            try:
                from network_engine.core import _PHASE_IDLE
                with self.net._recovery_lock:
                    self.net._recovery_state = _PHASE_IDLE
            except Exception:
                pass
            return

        try:
            self.net.send_migrate_ready()
        except Exception as e:
            print(f"[UI] send_migrate_ready error (ok): {e}")

        def _reconnect_to_self():

            self.net.fast_switch_to(host_ip)

        QTimer.singleShot(200, _reconnect_to_self)

    def _on_server_migrating(self, new_host_ip: str):

        print(f"[UI] _on_server_migrating: новый хост {new_host_ip}")

        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                print("[UI] _on_server_migrating: stop_silent (освобождаем ресурсы)")
                mgr.stop_silent()
        except Exception as e:
            print(f"[UI] _on_server_migrating stop error: {e}")

        self._lost_title_lbl.setText("Смена хоста")
        self._lost_status_lbl.setText(
            f"Сервер переезжает...\nНовый хост: {new_host_ip}\n"
            "Переподключение автоматически (до 10 секунд)."
        )
        self._btn_reconnect.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def setWindowTitle(self, title: str):
        super().setWindowTitle(title)
        if hasattr(self, '_title_bar'):
            self._title_bar.set_title(title)

    _EDGE_CURSORS = {
        "right":        Qt.CursorShape.SizeHorCursor,
        "bottom":       Qt.CursorShape.SizeVerCursor,
        "bottom-left":  Qt.CursorShape.SizeBDiagCursor,
        "bottom-right": Qt.CursorShape.SizeFDiagCursor,
    }

    def _edge_at(self, pos: QPoint) -> str | None:
        m = self._resize_margin
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        on_right  = x >= w - m
        on_bottom = y >= h - m
        if on_bottom and x <= m:       return "bottom-left"
        if on_bottom and on_right:     return "bottom-right"
        if on_bottom:                  return "bottom"
        if on_right:                   return "right"
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
            orig  = self._resize_start_geom
            g     = QRect(orig)
            min_w = self.minimumWidth()
            min_h = self.minimumHeight()
            d     = self._resize_direction

            if d in ("bottom", "bottom-left", "bottom-right"):
                new_bottom = orig.bottom() + delta.y()
                g.setBottom(max(new_bottom, orig.top() + min_h))
            if d in ("right", "bottom-right"):
                new_right = orig.right() + delta.x()
                g.setRight(max(new_right, orig.left() + min_w))

            self.setGeometry(g)
            e.accept()
            return

        if not self.isMaximized():
            edge = self._edge_at(e.pos())
            self.setCursor(self._EDGE_CURSORS[edge]) if edge else self.unsetCursor()
        super().mouseMoveEvent(e)

    def eventFilter(self, obj, event):

        if (event.type() == QEvent.Type.MouseMove
                and not self._resize_direction
                and not self.isMaximized()):
            pos = self.mapFromGlobal(QCursor.pos())
            edge = self._edge_at(pos)
            if edge:
                self.setCursor(self._EDGE_CURSORS[edge])
            else:
                self.unsetCursor()
        return False

    def mouseReleaseEvent(self, e):
        self._resize_direction = None
        self._resize_start_pos = None
        self._resize_start_geom = None

        self.unsetCursor()
        super().mouseReleaseEvent(e)

    def moveEvent(self, e):
        super().moveEvent(e)
        if hasattr(self, '_quick_bubbles') and self._quick_bubbles:
            self._reposition_quick_bubbles()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, '_quick_bubbles') and self._quick_bubbles:
            self._reposition_quick_bubbles()

    def apply_theme(self, theme_name):
        font_f = self.custom_font_family

        win_bg       = "rgba(22, 25, 40, 255)"
        surface      = "rgba(255,255,255,0.07)"
        surface_solid= "#1e2240"
        text         = "#e8edf8"
        text_dim     = "#7888aa"
        border       = "rgba(255,255,255,0.11)"
        border_solid = "#2e3458"
        hover        = "rgba(255,255,255,0.09)"
        hover_solid  = "#2a2f50"
        accent       = "#5b8ef5"
        accent_red   = "#e74c3c"
        title_bg     = "rgba(10, 12, 22, 255)"
        title_text   = "#c8d4f0"
        title_sep    = "rgba(255,255,255,0.08)"
        win_border   = "rgba(91,142,245,0.22)"
        bottom_bg    = "rgba(10, 12, 22, 255)"
        bottom_sep   = "rgba(255,255,255,0.08)"
        btn_bg       = "rgba(255,255,255,0.14)"
        btn_hover    = "rgba(255,255,255,0.24)"
        btn_border   = "rgba(255,255,255,0.20)"
        scrollbar    = "rgba(255,255,255,0.20)"
        sb_track     = "rgba(255,255,255,0.05)"
        tree_room_bg = "rgba(255,255,255,0.05)"

        self.setStyleSheet(f"""
            * {{ font-family: '{font_f}'; font-size: 15px; color: {text}; }}

            /* ════════════════════════════════════════════════════════════════
               Корневой контейнер окна — матовое стекло
            ════════════════════════════════════════════════════════════════ */
            #windowRoot {{
                background-color: {win_bg};
                border: 1px solid {win_border};
                border-radius: 12px;
            }}

            /* ════════════════════════════════════════════════════════════════
               Кастомный title bar — тёмная подложка, тонкий сепаратор
            ════════════════════════════════════════════════════════════════ */
            #customTitleBar {{
                background-color: {title_bg};
                border: none;
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
            }}
            #customTitleBar QLabel {{
                background: transparent;
                border: none;
            }}
            #titleBarText {{
                color: {title_text};
                font-size: 13px;
                font-weight: bold;
                letter-spacing: 0.8px;
                background: transparent;
                border: none;
            }}
            #titleBtnMin {{
                background: transparent;
                border: none;
                border-radius: 6px;
                color: {title_text};
                font-size: 14px;
            }}
            #titleBtnMin:hover {{
                background: rgba(255,255,255,0.10);
                color: #ffffff;
            }}
            #titleBtnClose {{
                background: transparent;
                border: none;
                border-radius: 6px;
                color: {title_text};
                font-size: 14px;
            }}
            #titleBtnClose:hover {{
                background: rgba(231,76,60,0.85);
                color: white;
            }}
            #titleSeparator {{
                background-color: {title_sep};
                border: none;
            }}
            #titleBtnSep {{
                background-color: {title_sep};
                border: none;
            }}

            /* ════════════════════════════════════════════════════════════════
               Главная область
            ════════════════════════════════════════════════════════════════ */
            QMainWindow, #centralWidget {{
                background-color: transparent;
            }}

            /* ════════════════════════════════════════════════════════════════
               Дерево пользователей — «стеклянная» панель
            ════════════════════════════════════════════════════════════════ */
            QTreeWidget {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                border-radius: 10px;
                outline: none;
                padding: 2px 0;
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
                height: 28px;
                background-color: transparent;
                border-radius: 0px;
                color: {text_dim};
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1.2px;
                text-transform: uppercase;
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
                background-color: rgba(14,16,28,230);
                border: 1px solid rgba(91,142,245,0.35);
                border-radius: 6px;
                color: {text};
                font-size: 12px;
                padding: 4px 8px;
            }}
            QTreeWidget QScrollBar:vertical {{
                background: {sb_track};
                width: 4px;
                border-radius: 2px;
                margin: 0;
            }}
            QTreeWidget QScrollBar::handle:vertical {{
                background: {scrollbar};
                border-radius: 2px;
            }}
            QTreeWidget QScrollBar::add-line:vertical,
            QTreeWidget QScrollBar::sub-line:vertical {{ height: 0; }}

            /* ════════════════════════════════════════════════════════════════
               Нижняя панель — «матовая» подложка
            ════════════════════════════════════════════════════════════════ */
            #bottomBar {{
                background-color: {bottom_bg};
                border: 1px solid {bottom_sep};
                border-radius: 14px;
            }}
            #quickChatBar {{
                background-color: {bottom_bg};
                border: 1px solid {bottom_sep};
                border-radius: 14px;
            }}

            /* ── Кнопки в bottomBar ────────────────────────────────────── */
            #barBtn {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 4px;
            }}
            #barBtn:hover {{
                background-color: {btn_hover};
                border-color: rgba(91,142,245,0.55);
            }}
            #barBtn:checked {{
                background-color: rgba(231,76,60,0.35);
                border-color: rgba(231,76,60,0.65);
            }}
            /* ── Микрофон: зелёный когда работает, красный когда замьючен ── */
            #barBtnMic {{
                background-color: rgba(46,204,113,0.25);
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 10px;
                padding: 4px;
            }}
            #barBtnMic:hover {{
                background-color: rgba(46,204,113,0.40);
                border-color: rgba(46,204,113,0.70);
            }}
            #barBtnMic:checked {{
                background-color: rgba(231,76,60,0.35);
                border-color: rgba(231,76,60,0.65);
            }}
            #barBtnMic:checked:hover {{
                background-color: rgba(231,76,60,0.50);
                border-color: rgba(231,76,60,0.80);
            }}
            /* ── Динамики: зелёный когда работают, красный когда замьючены ── */
            #barBtnDeafen {{
                background-color: rgba(46,204,113,0.25);
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 10px;
                padding: 4px;
            }}
            #barBtnDeafen:hover {{
                background-color: rgba(46,204,113,0.40);
                border-color: rgba(46,204,113,0.70);
            }}
            #barBtnDeafen:checked {{
                background-color: rgba(231,76,60,0.35);
                border-color: rgba(231,76,60,0.65);
            }}
            #barBtnDeafen:checked:hover {{
                background-color: rgba(231,76,60,0.50);
                border-color: rgba(231,76,60,0.80);
            }}
            #barBtnDisconnect {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 4px;
            }}
            #barBtnDisconnect:hover {{
                background-color: rgba(192,57,43,0.30);
                border-color: rgba(231,76,60,0.55);
            }}
            #btnStream {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 4px;
            }}
            #btnStream:hover {{
                background-color: {btn_hover};
                border-color: rgba(91,142,245,0.55);
            }}
            /* ── Веб-камера: нейтральный фон выкл, зелёный когда вкл ── */
            #barBtnCam {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 4px;
            }}
            #barBtnCam:hover {{
                background-color: {btn_hover};
                border-color: rgba(91,142,245,0.55);
            }}
            #barBtnCam:checked {{
                background-color: rgba(46,204,113,0.30);
                border: 1px solid rgba(46,204,113,0.60);
            }}
            #barBtnCam:checked:hover {{
                background-color: rgba(46,204,113,0.45);
                border-color: rgba(46,204,113,0.80);
            }}

            /* ════════════════════════════════════════════════════════════════
               Баннер обновления
            ════════════════════════════════════════════════════════════════ */
            QPushButton#updateBanner {{
                background-color: rgba(46,204,113,0.18);
                color: #82e0aa;
                font-weight: bold;
                border: 1px solid rgba(46,204,113,0.40);
                border-radius: 8px;
                padding: 6px;
                text-align: center;
            }}
            QPushButton#updateBanner:hover {{
                background-color: rgba(46,204,113,0.30);
                border-color: rgba(46,204,113,0.65);
            }}

            /* ════════════════════════════════════════════════════════════════
               Fallback QPushButton
            ════════════════════════════════════════════════════════════════ */
            QPushButton {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 8px;
                padding: 5px 10px;
                color: {text};
            }}
            QPushButton:hover {{
                background-color: {btn_hover};
                border-color: rgba(91,142,245,0.55);
            }}
            QPushButton:checked {{
                background-color: rgba(231,76,60,0.28);
                border-color: rgba(231,76,60,0.55);
                color: #ff9090;
            }}

            /* Кнопка переподключения */
            #btn_reconnect_green {{
                background-color: rgba(46,204,113,0.22);
                color: #82e0aa;
                font-size: 16px;
                font-weight: bold;
                border-radius: 10px;
                border: 1px solid rgba(46,204,113,0.45);
            }}
            #btn_reconnect_green:hover {{
                background-color: rgba(46,204,113,0.38);
            }}

            QDialog {{ background: transparent; }}

            /* ════════════════════════════════════════════════════════════════
               Быстрый чат
            ════════════════════════════════════════════════════════════════ */
            #quickChatInput {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 0 12px;
                color: {text};
                font-size: 13px;
                selection-background-color: rgba(91,142,245,0.40);
            }}
            #quickChatInput:focus {{
                border-color: rgba(91,142,245,0.60);
                background-color: {btn_hover};
            }}
            #quickChatInput::placeholder {{
                color: {text_dim};
            }}
            #quickChatSendBtn {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                color: {accent};
                font-size: 16px;
                font-weight: bold;
                padding: 0;
            }}
            #quickChatSendBtn:hover {{
                background-color: rgba(91,142,245,0.22);
                border-color: rgba(91,142,245,0.55);
            }}
            #quickChatSendBtn:pressed {{
                background-color: rgba(91,142,245,0.35);
            }}

            /* ════════════════════════════════════════════════════════════════
               ChatPanel — стеклянная боковая панель
            ════════════════════════════════════════════════════════════════ */
            #chatPanel {{
                background-color: {win_bg};
                border-left: 1px solid {border};
                border-radius: 0 12px 12px 0;
            }}
            #chatPanelHeader {{
                background-color: {title_bg};
                border-bottom: 1px solid {title_sep};
                border-top-right-radius: 12px;
            }}
            #chatPanelTitle {{
                color: {title_text};
                font-size: 13px;
                font-weight: bold;
                letter-spacing: 0.5px;
                background: transparent;
                border: none;
            }}
            #chatPanelClose {{
                background: transparent;
                border: none;
                border-radius: 5px;
                color: {text_dim};
                font-size: 12px;
                padding: 0;
            }}
            #chatPanelClose:hover {{
                background: rgba(231,76,60,0.75);
                color: white;
            }}
            #chatPanelSep {{
                background-color: {title_sep};
                border: none;
            }}
            #chatScrollArea {{
                background: transparent;
                border: none;
            }}
            #chatScrollArea QScrollBar:vertical {{
                background: {sb_track};
                width: 4px;
                border-radius: 2px;
                margin: 0;
            }}
            #chatScrollArea QScrollBar::handle:vertical {{
                background: {scrollbar};
                border-radius: 2px;
                min-height: 20px;
            }}
            #chatScrollArea QScrollBar::add-line:vertical,
            #chatScrollArea QScrollBar::sub-line:vertical {{ height: 0; }}
            #chatMsgContainer, #chatMsgWidget {{
                background: transparent;
            }}
            /* Чужие и свои пузыри — base-стиль (border-radius переопределяется
               напрямую на каждом QFrame в ChatMessageWidget по позиции в группе) */
            #chatBubbleOther {{
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }}
            #chatBubbleOwn {{
                background-color: rgba(91,142,245,0.16);
                border: 1px solid rgba(91,142,245,0.28);
                border-radius: 12px;
            }}
            #chatInputBar {{
                background-color: {bottom_bg};
                border: none;
                border-bottom-right-radius: 12px;
            }}
            #chatInput {{
                background-color: {btn_bg};
                border: 1px solid {btn_border};
                border-radius: 10px;
                padding: 0 10px;
                color: {text};
                font-size: 13px;
                selection-background-color: rgba(91,142,245,0.40);
            }}
            #chatInput:focus {{
                border-color: rgba(91,142,245,0.60);
                background-color: {btn_hover};
            }}
            #chatSendBtn, #chatAttachBtn {{
                background: transparent;
                border: none;
                border-radius: 8px;
                color: {accent};
                font-size: 15px;
                padding: 0;
            }}
            #chatSendBtn:hover, #chatAttachBtn:hover {{
                background: rgba(91,142,245,0.18);
            }}
            #chatSendBtn:pressed, #chatAttachBtn:pressed {{
                background: rgba(91,142,245,0.32);
            }}
        """)

        self._cache_theme = "Темная"
        self._theme_dirty = True
        self._c_talk   = QColor("#2ecc71")
        self._c_mute   = QColor("#e74c3c")
        self._c_stream = QColor("#3498db")
        self._c_def    = QColor("#d4d8e8")
        self._br_talk   = QBrush(self._c_talk)
        self._br_mute   = QBrush(self._c_mute)
        self._br_stream = QBrush(self._c_stream)
        self._br_def    = QBrush(self._c_def)
        self._br_gray   = QBrush(QColor("#6e7a96"))

    def setup_hotkeys(self):

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

            try:
                keyboard.add_hotkey("ctrl+t", lambda: self._toggle_chat_panel())
            except Exception as e:
                print(f"[HK] chat hotkey error: {e}")

            for i in range(5):
                ip   = self.app_settings.value(f"whisper_slot_{i}_ip",   "")
                nick = self.app_settings.value(f"whisper_slot_{i}_nick", "")
                hk   = self.app_settings.value(f"whisper_slot_{i}_hk",   "")

                anon = self.app_settings.value(f"whisper_slot_{i}_anon", "false") == "true"
                if (not ip and not nick) or not hk:
                    continue

                def _make_ptt(target_ip: str, target_nick: str, hotkey_str: str,
                              anonymous: bool = False):
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
                            self.audio.start_whisper(uid, anonymous=anonymous)
                            display = target_nick or target_ip
                            mark = " [anon]" if anonymous else ""
                            print(f"[HK] Whisper PTT START → {display} (uid={uid}){mark}")
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

                _press, _raw_key_up = _make_ptt(ip, nick, hk, anonymous=anon)
                try:
                    keyboard.add_hotkey(hk, _press, trigger_on_release=False, suppress=False)
                    keyboard.hook(_raw_key_up, suppress=False)
                    print(f"[HK] Whisper slot {i}: ip='{ip}' nick='{nick}' anon={anon} "
                          f"→ '{hk}' (trigger_key='{hk.replace(' ','').split('+')[-1].lower()}')")
                except Exception as e:
                    print(f"[HK] Whisper slot {i} error ({hk!r}): {e}")

            hk_count = int(self.app_settings.value("hk_table_count", 0))
            for i in range(hk_count):
                ftype = self.app_settings.value(f"hk_table_{i}_type", "none")
                if ftype != "sound":
                    continue
                fdata = self.app_settings.value(f"hk_table_{i}_data", "")  # имя звука
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

        # setup_hotkeys() начинается с keyboard.unhook_all() — это сносит и наш
        # ESC-хук. Если управление сейчас активно, вешаем его заново.
        if self._rc_viewer_uid_get() is not None:
            self._rc_esc_hook = None      # старый хендл уже невалиден
            self._rc_start_esc_watch()

    def play_notification(self, stype="self_move"):

        raw = int(self.app_settings.value("system_sound_volume", 30)) / 100.0
        vol = raw ** 2
        entry = self._loaded_sounds.get(stype)
        if entry is not None:
            try:
                data, sr = entry

                if hasattr(self.audio, 'play_internal_sound') and self.audio.stream:
                    self.audio.play_internal_sound(data, sr, vol)
                else:
                    sd.play(data * vol, sr)  # fallback если поток не запущен
            except Exception:
                pass
        else:
            if vol > 0:
                winsound.Beep(600 if stype == "self_move" else 400, 150)

    def _on_whisper_received(self, sender_uid: int):
        self._whisper_end_timer.stop()
        self._whisper_end_timer.start()

        if sender_uid == getattr(self, '_current_whisper_uid', None) \
                and self._whisper_banner.isVisible():
            return

        self._current_whisper_uid = sender_uid

        if is_anonymous_uid(sender_uid):
            nick = "Аноним"
        else:
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
        self._current_whisper_uid = None
        self._whisper_banner.setVisible(False)
        self._whisper_overlay.hide_overlay()

    def _send_quick_msg(self):
        pass

    def _position_chat_badge(self) -> None:
        btn = self._btn_chat_main
        badge = self._chat_badge
        badge.adjustSize()
        margin = 8
        x = btn.width() - badge.width() - margin
        y = (btn.height() - badge.height()) // 2
        badge.move(x, y)

    def _show_chat_badge(self) -> None:
        if self._chat_has_unread:
            return  # уже показан
        self._chat_has_unread = True
        self._position_chat_badge()
        self._chat_badge.setVisible(True)
        self._chat_blink_state = True
        self._chat_blink_timer.start()
        self._btn_chat_main.setStyleSheet("""
            QPushButton#btnChatMain {
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(231, 76, 60, 0.45);
                border-radius: 8px;
                color: #8899bb;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.5px;
                margin: 4px 0px 2px 0px;
            }
            QPushButton#btnChatMain:hover {
                background-color: rgba(255, 255, 255, 0.08);
                border-color: rgba(231, 76, 60, 0.65);
                color: #c8d0e0;
            }
            QPushButton#btnChatMain:checked {
                background-color: rgba(91, 142, 245, 0.12);
                border-color: rgba(91, 142, 245, 0.35);
                color: #5b8ef5;
            }
        """)

    def _hide_chat_badge(self) -> None:
        self._chat_has_unread = False
        self._chat_blink_timer.stop()
        self._chat_badge.setVisible(False)
        self._btn_chat_main.setStyleSheet("""
            QPushButton#btnChatMain {
                background-color: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                color: #8899bb;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0.5px;
                margin: 4px 0px 2px 0px;
            }
            QPushButton#btnChatMain:hover {
                background-color: rgba(255, 255, 255, 0.08);
                border-color: rgba(255, 255, 255, 0.15);
                color: #c8d0e0;
            }
            QPushButton#btnChatMain:checked {
                background-color: rgba(91, 142, 245, 0.12);
                border-color: rgba(91, 142, 245, 0.35);
                color: #5b8ef5;
            }
        """)

    def _on_chat_badge_blink(self) -> None:
        self._chat_blink_state = not self._chat_blink_state
        self._chat_badge.setVisible(self._chat_blink_state)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, '_chat_badge') and self._chat_has_unread:
            self._position_chat_badge()

    def _toggle_chat_panel(self, checked: bool = None) -> None:

        if checked is None:
            checked = not self._chat_panel.isVisible()

        panel_w = ChatPanel.PANEL_WIDTH
        if checked:
            if self._chat_panel.isVisible():
                return
            self._chat_panel.update_room_label(self.current_room)
            self._chat_panel.setVisible(True)
            if not self.isMaximized():
                self.resize(self.width() + panel_w, self.height())
            self._chat_panel.focus_input()
        else:
            if not self._chat_panel.isVisible():
                return
            self._chat_panel.setVisible(False)
            if not self.isMaximized():
                self.resize(max(400, self.width() - panel_w), self.height())

        self._btn_chat_main.blockSignals(True)
        self._btn_chat_main.setChecked(checked)
        self._btn_chat_main.blockSignals(False)

        if checked and self._chat_has_unread:
            self._hide_chat_badge()

    def _on_typing_received(self, uid: int, nick: str) -> None:
        if uid != self.audio.my_uid:
            self._chat_panel.show_typing(uid, nick)

    def _on_chat_panel_send(self, text: str) -> None:
        self.net.send_chat_msg(text)
        self.play_notification("chat_msg_out")

    def _on_chat_msg_received(self, entry: dict) -> None:
        self._chat_panel.add_message(entry)
        is_own = (entry.get('uid', 0) == self.audio.my_uid)
        if is_own:
            self.play_notification("chat_msg_out")
        else:
            self.play_notification("chat_msg_in")
            if not self._chat_panel.isVisible():
                self._show_chat_badge()

    def _on_chat_history_received(self, messages: list) -> None:
        if messages:
            self._chat_panel.load_history(messages)

    def _on_chat_media_requested(self) -> None:
        media = self._chat_panel.take_pending_media()
        if not media:
            return
        file_name, file_type, file_data_b64 = media
        from config import CHAT_MEDIA_MAX_B64
        if len(file_data_b64) > CHAT_MEDIA_MAX_B64:
            print(f"[UI] chat media: слишком большой (>{CHAT_MEDIA_MAX_B64} символов b64)")
            return
        self.net.send_chat_media(file_name, file_type, file_data_b64)
        self.play_notification("chat_msg_out")

    def _on_chat_media_received(self, entry: dict) -> None:
        self._chat_panel.add_message(entry)
        if entry.get('uid', 0) != self.audio.my_uid:
            self.play_notification("chat_msg_in")
            if not self._chat_panel.isVisible():
                self._show_chat_badge()

    def _on_quick_msg_received(self, sender_uid: int, from_nick: str, text: str):

        self.play_notification("quick_msg")

        global_tl = None
        item_h    = 44
        data = self.known_uids.get(sender_uid)
        if data:
            try:
                rect = self.tree.visualItemRect(data['item'])
                item_h = max(rect.height(), 1)
                global_tl = self.tree.viewport().mapToGlobal(rect.topLeft())
            except RuntimeError:
                pass

        existing = self._quick_bubbles.get(sender_uid)
        if existing:
            bubble, timer = existing
            try:
                bubble.update(text)
                if global_tl:
                    bubble.place_left_of(global_tl, item_h)
                bubble.raise_()
                bubble.show()
                timer.stop()
                timer.start(5000)
                return
            except RuntimeError:
                self._quick_bubbles.pop(sender_uid, None)

        bubble = QuickMsgBubble()
        bubble.update(text)

        if global_tl:
            bubble.place_left_of(global_tl, item_h)
        else:
            gp = self.mapToGlobal(
                QPoint(
                    self.width() // 2 - bubble.width() // 2,
                    self._bottom_bar.y() - bubble.height() - 12,
                )
            )
            bubble.move(gp)

        bubble.show()

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(5000)
        timer.timeout.connect(lambda uid=sender_uid: self._hide_quick_bubble(uid))
        timer.start()

        self._quick_bubbles[sender_uid] = (bubble, timer)

    def _hide_quick_bubble(self, uid: int):
        entry = self._quick_bubbles.pop(uid, None)
        if entry:
            bubble, timer = entry
            try:
                timer.stop()
                bubble.hide()
                bubble.deleteLater()
            except RuntimeError:
                pass

    def _reposition_quick_bubbles(self):

        for uid, (bubble, _timer) in list(self._quick_bubbles.items()):
            d = self.known_uids.get(uid)
            if not d:
                continue
            try:
                rect   = self.tree.visualItemRect(d['item'])
                item_h = max(rect.height(), 1)
                global_tl = self.tree.viewport().mapToGlobal(rect.topLeft())
                bubble.place_left_of(global_tl, item_h)
            except RuntimeError:
                pass

    def _on_user_volume_zero(self, uid: int, is_zero: bool):

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
        self.audio.is_muted = self.btn_mute.isChecked()
        ico = "assets/icon/mic_off.svg" if self.audio.is_muted else "assets/icon/mic_on.svg"
        self.btn_mute.setIcon(QIcon(resource_path(ico)))
        self.play_notification("mute" if self.audio.is_muted else "unmute")

    def toggle_deafen(self):
        is_d = self.btn_deafen.isChecked()
        self.audio.is_deafened = is_d
        ico = "assets/icon/volume_off.svg" if is_d else "assets/icon/volume_on.svg"
        self.btn_deafen.setIcon(QIcon(resource_path(ico)))

        if is_d and not self.audio.is_muted:
            self.btn_mute.setChecked(True)
            self.toggle_mute()
        else:
            self.play_notification("mute" if is_d else "unmute")

    def _on_draw_stroke_received(self, sender_uid: int, nick: str,
                                  color: str, points: list, width: int):

        for streamer_uid, win in list(self.stream_windows.items()):
            try:
                if win is not None and not win._closing:
                    win.add_remote_stroke(color, points, width)
            except (RuntimeError, AttributeError):
                pass

        if self._streamer_draw_overlay is not None:
            try:
                self._streamer_draw_overlay.add_stroke(nick, color, points, width)
            except (RuntimeError, AttributeError):
                pass

    def _on_force_muted(self):

        if not self.audio.is_muted:
            self.btn_mute.setChecked(True)
            self.toggle_mute()
        self._sb_toast.setText("🎤  Хост выключил ваш микрофон")
        self._sb_toast.adjustSize()
        _main_w = self.width()
        if self._chat_panel.isVisible():
            _main_w -= ChatPanel.PANEL_WIDTH
        tw = self._sb_toast.width()
        tx = (_main_w - tw) // 2
        ty = self._bottom_bar.y() - self._sb_toast.height() - 28
        self._sb_toast.move(tx, max(4, ty))
        self._sb_toast.raise_()
        self._sb_toast.setVisible(True)
        self._sb_toast_timer.start()

    def _on_kicked_by_host(self, reason: str):
        reason_txt = (reason or '').strip()
        print(f"[UI] kicked by host. reason={reason_txt!r}")
        try:
            self._sb_toast.setText(
                f"👢  Хост отключил вас от сервера"
                + (f":\n{reason_txt}" if reason_txt else "")
            )
            self._sb_toast.adjustSize()
            self._sb_toast.raise_()
            self._sb_toast.setVisible(True)
            self._sb_toast_timer.start()
        except Exception:
            pass
        self._disconnect_and_show_lobby()

    def _on_banned_by_host(self, reason: str):

        reason_txt = (reason or '').strip()
        banned_ip = getattr(self, 'ip', '') or ''
        print(f"[UI] banned by host. ip={banned_ip!r} reason={reason_txt!r}")
        self._disconnect_and_show_banned(banned_ip, reason_txt)

    def on_connected(self, msg):
        try:
            try:
                with self.net._recovery_lock:
                    self.net._recovery_state = 'idle'
            except Exception:
                pass

            self.audio.my_uid = msg['uid']
            self.audio.start(
                self.app_settings.value("device_in_name"),
                self.app_settings.value("device_out_name")
            )

            self._chat_panel.set_my_uid(self.audio.my_uid)
            self._chat_panel.update_room_label(self.current_room)

            self.play_notification("self_move")

            self.prev_all_uids = set()
            self._server_users_initialized = False

            self._stack.setCurrentIndex(0)

            self._btn_reconnect.setEnabled(True)

            if self._my_status_icon:
                self.net.send_presence_update(self._my_status_icon, self._my_status_text)

        except Exception as e:
            import traceback
            print(f"[on_connected] EXCEPTION:\n{traceback.format_exc()}", flush=True)

    def on_video_frame(self, uid, q_image):
        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_frame(q_image)
        # ── камера: тот же QImage уходит в круглое PiP-окно ──
        self.on_camera_frame(uid, q_image)

    def on_stream_stats_updated(self, uid: int, fps: int, loss_pct: int):

        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_stream_stats(fps, loss_pct)

    def update_user_tree(self, users_map):
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
            self._chat_panel.update_room_label(self.current_room)

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
        current_camera_uids = {
            u['uid']
            for u_list in users_map.values()
            for u in u_list
            if u.get('is_camera', False)
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

        # Камера выключилась у кого-то → закрываем его круглое окно (если открыто).
        stopped_cameras = getattr(self, '_prev_camera_uids', set()) - current_camera_uids
        for uid in stopped_cameras:
            if uid in self.cam_windows:
                self._destroy_cam_window(uid)
        self._prev_camera_uids = current_camera_uids

        self.audio.cleanup_users(all_active_uids)
        self.video.cleanup_users(all_active_uids)

        self.prev_room_uids = current_room_uids
        self.prev_streaming_uids = current_streaming_uids

        all_server_uids = all_active_uids - {self.audio.my_uid}
        if not self._server_users_initialized:
            self.prev_all_uids = all_server_uids
            self._server_users_initialized = True
        else:
            new_arrivals = all_server_uids - self.prev_all_uids
            if new_arrivals:
                self.play_notification("friend_connect")
            self.prev_all_uids = all_server_uids

        _new_sig = (
            tuple(self.default_rooms),   # ← изменение списка каналов = rebuild
            tuple(
                (r, u['uid'], u['nick'], u.get('mute'), u.get('deaf'),
                 u.get('is_streaming'), u.get('is_camera'),
                 u.get('avatar'), u.get('status_icon'),
                 u.get('status_text'),

                 tuple(w.get('nick', '') for w in u.get('watchers', [])))
                for r, u_list in sorted(users_map.items())
                for u in sorted(u_list, key=lambda x: x['uid'])
            ),
        )
        if hasattr(self, '_users_map_sig') and self._users_map_sig == _new_sig:
            return
        self._users_map_sig = _new_sig

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

                item_u = QTreeWidgetItem(item_r, [u['nick'], "", "", "", ""])
                avatar_name = u.get('avatar', '1.svg')
                host_uid    = getattr(self.net, '_server_host_uid', 0)
                is_host     = (uid == host_uid and host_uid != 0)
                item_u.setIcon(0, QIcon(resource_path(f"assets/avatars/{avatar_name}")))
                if is_host:
                    _font_host = QFont(font_u)
                    _font_host.setBold(True)
                    _font_host.setUnderline(True)
                    item_u.setFont(0, _font_host)
                    item_u.setForeground(0, self._br_gold)
                else:
                    item_u.setFont(0, font_u)
                item_u.setData(0, Qt.ItemDataRole.UserRole, uid)

                status_icon = u.get('status_icon', '')
                status_text = u.get('status_text', '')
                if status_icon:
                    if status_icon not in self._status_px_cache:

                        if len(self._status_px_cache) >= 200:
                            self._status_px_cache.clear()
                        icon_path = resource_path(f"assets/status/{status_icon}")
                        px = QIcon(icon_path).pixmap(20, 20)
                        self._status_px_cache[status_icon] = px
                    item_u.setData(1, Qt.ItemDataRole.DecorationRole, self._status_px_cache[status_icon])
                    if status_text:
                        item_u.setToolTip(1, status_text)
                else:
                    item_u.setData(1, Qt.ItemDataRole.DecorationRole, None)

                item_u.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)  # live
                item_u.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)  # deaf
                item_u.setTextAlignment(4, Qt.AlignmentFlag.AlignCenter)  # mute

                self.known_uids[uid] = {
                    'item':        item_u,
                    'is_m':        u.get('mute', False),
                    'is_d':        u.get('deaf', False),
                    'is_s':        u.get('is_streaming', False),
                    'is_cam':      u.get('is_camera', False),
                    'avatar_name': avatar_name,
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

        self._reposition_quick_bubbles()

    def refresh_ui(self):
        try:
            self._refresh_ui_impl()
        except Exception as _rui_err:
            import traceback
            print(f"[UI] refresh_ui UNHANDLED error: {_rui_err}\n{traceback.format_exc()}")

    def _refresh_ui_impl(self):
        try:
            ping = self.net.current_ping

            ping_tier = 0 if ping < 60 else (1 if ping < 150 else 2)
            if not hasattr(self, '_ping_tier_cache'):
                self._ping_tier_cache = -1   # форсируем первый рендер
            tier_changed = (ping_tier != self._ping_tier_cache)
            self._ping_tier_cache = ping_tier

            if tier_changed:
                if ping_tier == 0:
                    col = "#2ecc71"
                elif ping_tier == 1:
                    col = "#f1c40f"
                else:
                    col = "#e74c3c"
                self._latency_btn.setStyleSheet(
                    f"QPushButton#barBtn {{"
                    f"  font-size: 11px; font-weight: bold;"
                    f"  color: {col}; padding: 4px;"
                    f"}}"
                )
            self._latency_btn.setText(f"{ping} мс")

            now = time.perf_counter()

            # ── Фаза пульсации обводки камеры (одна на тик) ──────────────
            _cam_phase = pulse_phase(now - self._cam_pulse_t0)
            _cam_phase_step = quantize_phase(_cam_phase)

            if self._theme_dirty:
                self._theme_dirty = False
                self._c_def  = QColor("#ecf0f1")
                self._br_def = QBrush(self._c_def)

            c_talk   = self._c_talk
            c_mute   = self._c_mute
            c_stream = self._c_stream
            c_def    = self._c_def
            icon_size = self._icon_size

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
                my_uid       = self.audio.my_uid
                is_muted     = self.audio.is_muted
                is_deafened  = self.audio.is_deafened

            host_uid = getattr(self.net, '_server_host_uid', 0)

            for uid, data in self.known_uids.items():
                item = data['item']
                is_m = data['is_m']
                is_d = data['is_d']
                is_s = data['is_s']

                curr_s = self.is_streaming if uid == my_uid else is_s
                curr_d = is_deafened       if uid == my_uid else is_d

                if curr_s:
                    item.setData(2, Qt.ItemDataRole.DecorationRole, self._px_live)
                else:
                    item.setData(2, Qt.ItemDataRole.DecorationRole, None)

                if curr_d:
                    item.setData(3, Qt.ItemDataRole.DecorationRole, self._px_vol_off)
                else:
                    item.setData(3, Qt.ItemDataRole.DecorationRole, None)

                if uid == my_uid:
                    talk = me_talk
                    if is_muted:
                        item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_mic_off)
                    else:
                        item.setData(4, Qt.ItemDataRole.DecorationRole, None)
                else:
                    u_vals = remote_snapshot.get(uid)
                    talk             = (now - u_vals[0] < 0.3) if u_vals else False
                    is_locally_muted = u_vals[1]               if u_vals else False
                    is_vol_zero      = u_vals[2]               if u_vals else False

                    if is_locally_muted or is_vol_zero:
                        item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_ban)
                    elif is_m:
                        item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_mic_off)
                    else:
                        item.setData(4, Qt.ItemDataRole.DecorationRole, None)

                # ── Индикатор веб-камеры ──────────────────────────────────
                curr_cam = self.is_camera_on if uid == my_uid else data.get('is_cam', False)
                avatar_name = data.get('avatar_name')

                if curr_cam:
                    # 1) иконка камеры в колонке статуса (1), если там пусто
                    if not data.get('status_icon'):
                        item.setData(1, Qt.ItemDataRole.DecorationRole, self._px_cam)
                    # 2) плавно пульсирующая обводка ПОВЕРХ кромки аватарки
                    #    (колонка 0). Размер аватара НЕ меняется — кольцо
                    #    лежит на краю, аватар занимает весь круг как обычно.
                    if avatar_name:
                        # Кольцо рисуем поверх аватарки ТОЧНО того же размера,
                        # что и обычная иконка дерева — чтобы при включении
                        # камеры аватар не уменьшался и не съезжал.
                        _isz = self.tree.iconSize()
                        _icon_px = _isz.width() if (_isz.isValid() and _isz.width() > 0) else 32
                        key = (avatar_name, _cam_phase_step, _icon_px)
                        ringed = self._avatar_ring_cache.get(key)
                        if ringed is None:
                            if len(self._avatar_ring_cache) > 600:
                                self._avatar_ring_cache.clear()
                            # База — тот же рендер, что использует дерево для
                            # обычной аватарки: QIcon(path).pixmap(icon_size).
                            base = QIcon(
                                resource_path(f"assets/avatars/{avatar_name}")
                            ).pixmap(_icon_px, _icon_px)
                            ringed = make_avatar_with_pulse_ring(
                                base, _icon_px, _cam_phase, ring_width=3,
                            )
                            self._avatar_ring_cache[key] = ringed
                        item.setIcon(0, QIcon(ringed))
                        data['_ring_on'] = True
                else:
                    # камера выключилась — вернуть обычную аватарку один раз
                    if data.get('_ring_on') and avatar_name:
                        item.setIcon(0, QIcon(resource_path(f"assets/avatars/{avatar_name}")))
                        data['_ring_on'] = False
                    # снять иконку камеры, если её ставили и статуса нет
                    if not data.get('status_icon'):
                        # не затираем чужой статус-значок; только наш cam-значок
                        cur = item.data(1, Qt.ItemDataRole.DecorationRole)
                        if cur is self._px_cam:
                            item.setData(1, Qt.ItemDataRole.DecorationRole, None)

                if talk:
                    item.setForeground(0, self._br_talk)
                elif curr_s:
                    item.setForeground(0, self._br_stream)
                elif curr_d or is_m or (uid != my_uid and u_vals and (is_locally_muted or is_vol_zero)):
                    item.setForeground(0, self._br_mute)
                else:
                    if host_uid and uid == host_uid:
                        item.setForeground(0, self._br_gold)
                    else:
                        item.setForeground(0, self._br_def)
        except Exception as _e:
            import traceback
            print(f"[DEBUG] refresh_ui: EXCEPTION:\n{traceback.format_exc()}", flush=True)

    def on_tree_double_click(self, item, col):
        if item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
            room_name = item.data(1, Qt.ItemDataRole.UserRole)
            ch_info = next(
                (ch for ch in self._channel_list if ch['name'] == room_name),
                None
            )
            if ch_info and ch_info.get('has_password', False):
                self._on_join_room_denied(room_name, 'channel_auth_required')
            else:
                self.net.send_json({"action": CMD_JOIN_ROOM, "room": room_name})

    def _on_tree_clicked_cam(self, item, col):
        """
        Одиночный клик по пользователю с активной камерой → круглое PiP-окно.
        Реагируем на клик по аватарке (колонка 0) и по иконке камеры (колонка 1).
        """
        if item is None:
            return
        if item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
            return
        if col not in (0, 1):
            return
        uid = item.data(0, Qt.ItemDataRole.UserRole)
        if not uid:
            return
        data = self.known_uids.get(uid)
        if not data:
            return
        cam_on = self.is_camera_on if uid == self.audio.my_uid else data.get('is_cam', False)
        if cam_on:
            self.open_camera_window(uid, item.text(0).strip())

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)

        if not item or item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
            if self._is_server_host():
                menu = QMenu(self)
                menu.setStyleSheet(
                    "QMenu { background: rgba(20,22,35,245); border: 1px solid rgba(255,255,255,0.14);"
                    " border-radius: 8px; padding: 4px; color: #c8d0e0; font-size: 13px; }"
                    "QMenu::item { padding: 6px 18px; border-radius: 5px; }"
                    "QMenu::item:selected { background: rgba(91,142,245,0.30); color: #fff; }"
                )
                act_create = menu.addAction("🔊  Создать временный канал")

                act_rename = None
                clicked_room = None
                if item and item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
                    clicked_room = item.data(1, Qt.ItemDataRole.UserRole)
                    for ch in self._channel_list:
                        if ch['name'] == clicked_room and ch.get('permanent', False):
                            menu.addSeparator()
                            act_rename = menu.addAction("✏️  Переименовать канал")
                            break

                chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
                if chosen == act_create:
                    self._on_create_channel_requested()
                elif act_rename is not None and chosen == act_rename:
                    self._on_rename_permanent_channel(clicked_room)
            return

        uid = item.data(0, Qt.ItemDataRole.UserRole)
        if not uid:
            return

        if uid == self.audio.my_uid:
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
            on_transfer_server=(
                (lambda _uid=uid: self._on_request_server_transfer(_uid))
                if self._is_server_host() and uid != self.audio.my_uid
                else None
            ),
            on_host_mute=(
                (lambda _uid=uid: self.net.send_host_mute(_uid))
                if self._is_server_host() and uid != self.audio.my_uid
                else None
            ),
            on_host_kick=(
                (lambda _uid=uid, _nk=nick: self._on_request_host_kick(_uid, _nk))
                if self._is_server_host() and uid != self.audio.my_uid
                else None
            ),
            on_host_ban=(
                (lambda _uid=uid, _nk=nick: self._on_request_host_ban(_uid, _nk))
                if self._is_server_host() and uid != self.audio.my_uid
                else None
            ),
        ).show()

    def _is_server_host(self) -> bool:

        my_uid = getattr(self.audio, 'my_uid', 0)
        server_uid = getattr(self.net, '_server_host_uid', 0)
        return bool(my_uid and my_uid == server_uid)

    def _on_request_server_transfer(self, target_uid: int):

        target_info = self.known_uids.get(target_uid, {})
        target_nick = target_info.get('nick', f'uid={target_uid}')

        reply = QMessageBox.question(
            self,
            "Передача сервера",
            f"Передать роль хоста пользователю {target_nick!r}?\n\n"
            "Все участники переподключатся к его ПК автоматически.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.net.send_server_transfer(target_uid)
            print(f"[UI] Запрошена передача сервера → {target_nick} (uid={target_uid})")

    def _on_request_host_kick(self, target_uid: int, target_nick: str):

        nick = (target_nick or '').strip() or f'uid={target_uid}'
        reply = QMessageBox.question(
            self,
            "Кикнуть участника",
            f"Отключить участника {nick!r} от сервера?\n\n"
            "Он сможет снова подключиться вручную.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.net.send_host_kick(target_uid)
            print(f"[UI] Kick → {nick} (uid={target_uid})")

    def _on_request_host_ban(self, target_uid: int, target_nick: str):

        nick = (target_nick or '').strip() or f'uid={target_uid}'
        reply = QMessageBox.question(
            self,
            "Забанить участника",
            f"Забанить участника {nick!r}?\n\n"
            "Он будет отключён и не сможет подключиться к вашему серверу "
            "до ручного разбана.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.net.send_host_ban(target_uid, '')
            print(f"[UI] Ban → {nick} (uid={target_uid})")

    def open_video_window(self, uid, nick):
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

            w.overlay.set_stream_volume_value(self.audio._stream_vol)

            w.draw_stroke_ready.connect(
                lambda color, pts, width, _uid=uid:
                    self.net.send_draw_stroke(_uid, self.nick, color, pts, width)
            )

            w.control_requested.connect(
                lambda _uid=uid, _w=w: self._on_viewer_rc_requested(_uid, _w)
            )
            w.control_released.connect(
                lambda _uid=uid, _w=w: self._on_viewer_rc_released(_uid, _w)
            )
            w.remote_control_event_ready.connect(
                lambda ev, _uid=uid: self.net.send_remote_control_event(_uid, ev)
            )
            self.net.remote_control_response.connect(
                lambda granted, reason, _w=w: self._on_rc_response(_w, granted, reason)
            )
            self.net.remote_control_stopped.connect(
                lambda _w=w: self._on_rc_stopped(_w)
            )

            w.show()
            self.stream_windows[uid] = w
            self.net.start_watching(uid)
        else:
            self.stream_windows[uid].raise_()
            self.stream_windows[uid].activateWindow()

    def _on_stream_window_closed(self, uid):
        w = self.stream_windows.get(uid)
        if w is not None:
            try:
                self.audio.status_changed.disconnect(w.sync_audio_state)
            except (RuntimeError, TypeError):
                pass
            # RC-сигналы — глобальные (self.net.*), не умирают с окном.
            # Без disconnect лямбда с захваченным _w=w дёргается после
            # deleteLater() → RuntimeError "wrapped C++ object has been deleted".
            for _sig in (
                self.net.remote_control_requested,
                self.net.remote_control_response,
                self.net.remote_control_event,
                self.net.remote_control_stopped,
            ):
                try:
                    _sig.disconnect()
                except (RuntimeError, TypeError):
                    pass
            # Сброс RC-состояния при закрытии окна
            if self._rc_streamer_uid_get() == uid:
                self._rc_streamer_uid = None
            if self._rc_viewer_uid_get() is not None:
                self._rc_viewer_uid = None
                self._rc_stop_esc_watch()
        self.stream_windows.pop(uid, None)

        # stop_watching() закрывает _viewer_pc (WebRTC) в asyncio-потоке,
        # сбрасывает _watching_streamer_uid, отправляет stream_watch_stop серверу
        # и вызывает video.stop_viewer_for_uid() — всё в одном методе.
        # Нельзя использовать send_json напрямую: _viewer_pc остаётся открытым
        # на клиенте → повторное открытие стрима не получает новый on("track")
        # и показывает «Ожидание видео...» вместо картинки.
        self.net.stop_watching()

        if w is not None:
            try:
                w.deleteLater()
            except RuntimeError:
                pass

        gc.collect()

        # Откладываем ещё один GC на 2.5 сек: к этому моменту decode_worker
        # гарантированно завершился и его decoder/буферы готовы к сборке.
        # Windows heap trim выполним здесь же — после декодера.
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

    # ==================================================================
    # Remote Control — методы MainWindow
    # ==================================================================

    # ------------------------------------------------------------------
    # Состояние (хранится в MainWindow, не в VideoWindow)
    # ------------------------------------------------------------------
    # _rc_viewer_uid  — uid зрителя, которому стример выдал управление (сторона стримера)
    # _rc_streamer_uid — uid стримера, у которого этот клиент берёт управление (сторона зрителя)
    # Инициализируются лениво при первом использовании.

    def _rc_viewer_uid_get(self):
        return getattr(self, '_rc_viewer_uid', None)

    def _rc_streamer_uid_get(self):
        return getattr(self, '_rc_streamer_uid', None)

    # ------------------------------------------------------------------
    # Сторона ЗРИТЕЛЯ
    # ------------------------------------------------------------------
    def _on_viewer_rc_requested(self, streamer_uid: int, window):
        """Зритель нажал кнопку Control → отправить запрос стримеру."""
        self._rc_streamer_uid = streamer_uid
        self.net.send_remote_control_request(streamer_uid, self.nick)

        # Если ответа нет N секунд (cooldown → сервер молча дропнул запрос,
        # либо стример игнорирует диалог) — отжимаем кнопку обратно.
        self._rc_pending_window = window
        if getattr(self, '_rc_pending_timer', None) is None:
            self._rc_pending_timer = QTimer(self)
            self._rc_pending_timer.setSingleShot(True)
            self._rc_pending_timer.timeout.connect(self._on_rc_request_timeout)
        self._rc_pending_timer.start(12000)

    def _on_rc_request_timeout(self):
        """Ответа на запрос управления не было — сбрасываем кнопку."""
        w = getattr(self, '_rc_pending_window', None)
        self._rc_pending_window = None
        # Управление так и не активировалось → отжать кнопку
        if w is not None and not (
            getattr(w, '_control_mode', False)
        ):
            try:
                w.on_control_denied()
            except RuntimeError:
                pass
        self._rc_streamer_uid = None

    def _rc_cancel_pending_timer(self):
        t = getattr(self, '_rc_pending_timer', None)
        if t is not None:
            t.stop()
        self._rc_pending_window = None

    def _on_viewer_rc_released(self, streamer_uid: int, window):
        """
        Зритель отжал кнопку Control / нажал кнопку отмены в баннере стримера.
        Отправляем stop, сбрасываем состояние.
        """
        self._rc_cancel_pending_timer()
        uid = streamer_uid or self._rc_streamer_uid_get()
        if uid:
            self.net.send_remote_control_stop(uid)
        self._rc_streamer_uid = None
        window.on_control_stopped()

    def _on_rc_response(self, window, granted: bool, reason: str = ''):
        """Сервер relay-нул ответ стримера на запрос управления."""
        # При нескольких открытых окнах сигнал прилетает во все лямбды.
        # Реагирует только то окно, что реально ждёт ответа или уже управляет.
        pending = getattr(self, '_rc_pending_window', None)
        if window is not pending and not getattr(window, '_control_mode', False):
            return

        self._rc_cancel_pending_timer()
        if granted:
            window.on_control_granted()
            return

        # Отказ. Сбрасываем состояние — кнопка отжимается, можно запросить снова.
        window.on_control_denied()
        self._rc_streamer_uid = None

        if reason == 'cooldown':
            # 3 отказа подряд → cooldown. Зрителю показываем мягкое уведомление
            # один раз; дальнейшие нажатия кнопки сервер молча игнорирует.
            self._show_rc_cooldown_dialog(window)
        elif reason == 'busy':
            # Управление уже у кого-то — тихо, без диалога.
            pass
        else:
            self._show_rc_denied_dialog(window)

    def _on_rc_stopped(self, window):
        """
        Сервер сообщил об остановке управления.
        Может прийти как стримеру (зритель нажал «Отменить управление»),
        так и зрителю (стример нажал «Отменить» / 3×ESC).
        Здесь НЕ шлём stop обратно — иначе будет эхо.
        """
        # Сторона зрителя: управление у нас отобрали
        self._rc_streamer_uid = None
        if window is not None:
            try:
                window.on_control_stopped()
            except RuntimeError:
                pass
        # Сторона стримера: гасим ESC-вотч и баннер без повторного send_stop
        if self._rc_viewer_uid_get() is not None:
            self._rc_viewer_uid = None
            self._rc_stop_esc_watch()
            self._rc_release_all_inputs()
            if hasattr(self, '_rc_access_banner'):
                self._rc_access_banner.setVisible(False)

    # ------------------------------------------------------------------
    # Сторона СТРИМЕРА
    # ------------------------------------------------------------------
    def _on_rc_request_global(self, viewer_uid: int, viewer_nick: str):
        """
        Глобальный обработчик запроса управления (подключён в __init__).
        Когда мы сами стримим — VideoWindow нашего стрима не существует,
        поэтому показываем диалог поверх главного окна (self).
        """
        # window=None → _on_rc_request_received покажет диалог поверх MainWindow
        self._on_rc_request_received(None, viewer_uid, viewer_nick)

    def _on_rc_request_received(self, window, viewer_uid: int, viewer_nick: str):
        """
        Сервер relay-нул запрос зрителя.
        window — VideoWindow если мы смотрим чужой стрим, None если мы сами стримим.
        Показываем диалог поверх window или поверх MainWindow (self).
        """
        # Защита от "wrapped C++ object has been deleted"
        if window is not None:
            try:
                is_visible = window.isVisible()
            except RuntimeError:
                self.net.send_remote_control_response(viewer_uid, False)
                return
            if not is_visible:
                self.net.send_remote_control_response(viewer_uid, False)
                return

        # Если уже есть активный контролёр — отклоняем без диалога
        if self._rc_viewer_uid_get() is not None:
            self.net.send_remote_control_response(viewer_uid, False)
            return

        # Показываем диалог: поверх VideoWindow (если есть) или поверх MainWindow
        parent_widget = window if window is not None else self
        granted = self._show_rc_dialog(parent_widget, viewer_nick)
        self.net.send_remote_control_response(viewer_uid, granted)

        if granted:
            self._rc_viewer_uid = viewer_uid
            self._rc_start_esc_watch()
            if window is not None:
                window.show_control_active_banner()
            else:
                self._show_rc_streamer_banner()

    def _show_rc_dialog(self, parent, viewer_nick: str) -> bool:
        """
        Диалог запроса управления в стиле приложения (поверх всех окон).
        Возвращает True если стример нажал «Да».
        """
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QLabel, QPushButton)
        from PyQt6.QtCore import Qt

        dlg = QDialog(parent)
        dlg.setWindowTitle("Запрос управления")
        dlg.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        dlg.setModal(True)

        outer = QFrame(dlg)
        outer.setObjectName("rcReqFrame")
        outer.setStyleSheet("""
            QFrame#rcReqFrame {
                background-color: #1a1c2c;
                border-radius: 12px;
                border: 1px solid rgba(255,255,255,0.10);
            }
        """)

        v = QVBoxLayout(outer)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(14)

        ico_lbl = QLabel("🖱️")
        ico_lbl.setStyleSheet("background:transparent; border:none; font-size:30px;")
        ico_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ico_lbl)

        title = QLabel("Запрос управления")
        title.setStyleSheet(
            "background:transparent; border:none; color:#cdd6f4;"
            "font-size:15px; font-weight:700;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        # Имя экранируем, чтобы ник не сломал разметку
        safe_nick = (
            str(viewer_nick).replace('&', '&amp;')
                            .replace('<', '&lt;')
                            .replace('>', '&gt;')
        )
        msg = QLabel(
            f'Пользователь <b style="color:#cdd6f4;">{safe_nick}</b> хочет взять '
            f'управление вашей мышью и клавиатурой.'
        )
        msg.setStyleSheet(
            "background:transparent; border:none; color:#8890a0; font-size:12px;"
        )
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        v.addWidget(msg)

        hint = QLabel("Остановить можно тройным нажатием ESC.")
        hint.setStyleSheet(
            "background:transparent; border:none; color:#5b6172; font-size:11px;"
        )
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        v.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        btn_no = QPushButton("Отклонить")
        btn_no.setFixedHeight(36)
        btn_no.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_no.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,18);
                border: 1px solid rgba(255,255,255,40);
                border-radius: 8px;
                color: #cdd6f4;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: rgba(255,255,255,35); }
        """)

        btn_yes = QPushButton("Разрешить")
        btn_yes.setFixedHeight(36)
        btn_yes.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_yes.setStyleSheet("""
            QPushButton {
                background-color: rgba(88, 101, 242, 220);
                border: 1px solid rgba(88, 101, 242, 255);
                border-radius: 8px;
                color: #fff;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: rgba(108, 121, 255, 240); }
        """)

        btn_no.clicked.connect(dlg.reject)
        btn_yes.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_no, 1)
        btn_row.addWidget(btn_yes, 1)
        v.addLayout(btn_row)

        outer.adjustSize()

        root_lay = QVBoxLayout(dlg)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.addWidget(outer)
        dlg.setMinimumWidth(340)
        dlg.adjustSize()

        # По центру родителя/экрана и поверх всех окон
        try:
            dlg.raise_()
            dlg.activateWindow()
        except Exception:
            pass

        return dlg.exec() == QDialog.DialogCode.Accepted

    def _show_rc_denied_dialog(self, parent):
        """Кастомный диалог «управление отклонено» в стиле приложения."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        from PyQt6.QtCore import Qt

        dlg = QDialog(parent)
        dlg.setWindowTitle("Управление")
        dlg.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        dlg.setModal(True)

        outer = QFrame(dlg)
        outer.setObjectName("rcDeniedFrame")
        outer.setStyleSheet("""
            QFrame#rcDeniedFrame {
                background-color: #1a1c2c;
                border-radius: 12px;
                border: 1px solid rgba(255,255,255,0.10);
            }
        """)

        v = QVBoxLayout(outer)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)

        ico_lbl = QLabel("🚫")
        ico_lbl.setStyleSheet("background:transparent; border:none; font-size:28px;")
        ico_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ico_lbl)

        title = QLabel("Управление отклонено")
        title.setStyleSheet(
            "background:transparent; border:none; color:#cdd6f4;"
            "font-size:14px; font-weight:700;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        msg = QLabel("Стример отклонил ваш запрос.<br>Вы можете попробовать снова.")
        msg.setStyleSheet(
            "background:transparent; border:none; color:#8890a0; font-size:12px;"
        )
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        v.addWidget(msg)

        btn_ok = QPushButton("Понятно")
        btn_ok.setFixedHeight(34)
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: rgba(88, 101, 242, 200);
                border: 1px solid rgba(88, 101, 242, 255);
                border-radius: 8px;
                color: #fff;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: rgba(108, 121, 255, 230); }
        """)
        btn_ok.clicked.connect(dlg.accept)
        v.addWidget(btn_ok)

        outer.adjustSize()

        root_lay = QVBoxLayout(dlg)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.addWidget(outer)
        dlg.adjustSize()
        dlg.exec()

    def _show_rc_cooldown_dialog(self, parent):
        """Диалог «запросы временно заблокированы» (cooldown после 3 отказов)."""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton
        from PyQt6.QtCore import Qt

        dlg = QDialog(parent)
        dlg.setWindowTitle("Управление")
        dlg.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        dlg.setModal(True)

        outer = QFrame(dlg)
        outer.setObjectName("rcCooldownFrame")
        outer.setStyleSheet("""
            QFrame#rcCooldownFrame {
                background-color: #1a1c2c;
                border-radius: 12px;
                border: 1px solid rgba(255,255,255,0.10);
            }
        """)

        v = QVBoxLayout(outer)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)

        ico_lbl = QLabel("⏳")
        ico_lbl.setStyleSheet("background:transparent; border:none; font-size:28px;")
        ico_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ico_lbl)

        title = QLabel("Запросы заблокированы")
        title.setStyleSheet(
            "background:transparent; border:none; color:#cdd6f4;"
            "font-size:14px; font-weight:700;"
        )
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)

        msg = QLabel(
            "Стример отклонил запрос несколько раз.<br>"
            "Повторная попытка будет доступна через 10 минут."
        )
        msg.setStyleSheet(
            "background:transparent; border:none; color:#8890a0; font-size:12px;"
        )
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        v.addWidget(msg)

        btn_ok = QPushButton("Понятно")
        btn_ok.setFixedHeight(34)
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: rgba(88, 101, 242, 200);
                border: 1px solid rgba(88, 101, 242, 255);
                border-radius: 8px;
                color: #fff;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: rgba(108, 121, 255, 230); }
        """)
        btn_ok.clicked.connect(dlg.accept)
        v.addWidget(btn_ok)

        outer.adjustSize()

        root_lay = QVBoxLayout(dlg)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.addWidget(outer)
        dlg.adjustSize()
        dlg.exec()

    def _show_rc_streamer_banner(self):
        """Показывает встроенный баннер управления над кнопкой «Чат»."""
        if hasattr(self, '_rc_access_banner'):
            self._rc_access_banner.setVisible(True)

    def _hide_rc_streamer_banner(self):
        """Стример нажал «Отменить» в баннере — скрываем, останавливаем управление."""
        if hasattr(self, '_rc_access_banner'):
            self._rc_access_banner.setVisible(False)
        self._rc_viewer_uid = None
        self._rc_stop_esc_watch()
        self._rc_release_all_inputs()
        self.net.send_remote_control_stop(getattr(self.audio, 'my_uid', 0))

    def _rc_release_all_inputs(self):
        """
        Отпускает все зажатые инъекцией клавиши/кнопки. КРИТИЧНО при остановке:
        иначе у стримера «залипают» модификаторы (Shift/Ctrl/Alt/Win) и ПК
        ведёт себя сломанно вплоть до перезагрузки.
        """
        try:
            if _WIN_INPUT_AVAILABLE:
                _win_input.release_all()
                print("[RC-INJECT] release_all: все зажатые входы отпущены")
            elif _PYAUTOGUI_AVAILABLE:
                for k in ('ctrl', 'shift', 'alt', 'win',
                          'ctrlleft', 'ctrlright', 'shiftleft', 'shiftright',
                          'altleft', 'altright', 'winleft', 'winright'):
                    try:
                        pyautogui.keyUp(k, _pause=False)
                    except Exception:
                        pass
                for b in ('left', 'right', 'middle'):
                    try:
                        pyautogui.mouseUp(button=b, _pause=False)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[RC-INJECT] release_all error: {e!r}")

    def _on_rc_event_received(self, ev: dict):
        """
        Стример получил событие мыши/клавиатуры от зрителя.
        Применяем через WinAPI SendInput (надёжно, Unicode), fallback — pyautogui.
        Координаты нормализованы (0.0–1.0).
        """
        ev_type = ev.get('type', '')
        if ev_type != 'mouse_move':
            print(f"[RC-INJECT] got {ev_type} ev={ev} "
                  f"win={_WIN_INPUT_AVAILABLE} pyautogui={_PYAUTOGUI_AVAILABLE} "
                  f"viewer={self._rc_viewer_uid_get()}")

        if self._rc_viewer_uid_get() is None:
            return  # управление не активно

        try:
            nx = float(ev.get('x', 0))
            ny = float(ev.get('y', 0))

            if ev_type == 'mouse_move':
                if _WIN_INPUT_AVAILABLE:
                    _win_input.move_to(nx, ny)
                elif _PYAUTOGUI_AVAILABLE:
                    sw, sh = pyautogui.size()
                    pyautogui.moveTo(int(nx*sw), int(ny*sh), duration=0, _pause=False)

            elif ev_type in ('mouse_press', 'mouse_release'):
                down = (ev_type == 'mouse_press')
                btn = int(ev.get('button', 1))
                if _WIN_INPUT_AVAILABLE:
                    _win_input.mouse_button(btn, down, nx, ny)
                elif _PYAUTOGUI_AVAILABLE:
                    sw, sh = pyautogui.size()
                    pyautogui.moveTo(int(nx*sw), int(ny*sh), duration=0, _pause=False)
                    pa_btn = _qt_button_to_pyautogui(btn)
                    (pyautogui.mouseDown if down else pyautogui.mouseUp)(button=pa_btn, _pause=False)
                print(f"[RC-INJECT] mouse {'down' if down else 'up'} btn={btn}")

            elif ev_type == 'mouse_scroll':
                delta = int(ev.get('delta', 0))
                if _WIN_INPUT_AVAILABLE:
                    if delta:
                        _win_input.mouse_scroll(delta)
                elif _PYAUTOGUI_AVAILABLE:
                    clicks = delta // 120
                    if clicks:
                        pyautogui.scroll(clicks, _pause=False)

            elif ev_type == 'key_press':
                if int(ev.get('key', 0)) == _RC_QT_KEY_ESCAPE:
                    return
                self._rc_inject_key(ev, down=True)

            elif ev_type == 'key_release':
                if int(ev.get('key', 0)) == _RC_QT_KEY_ESCAPE:
                    return
                self._rc_inject_key(ev, down=False)

        except Exception as e:
            import traceback
            print(f"[RC-INJECT] EXCEPTION on {ev_type}: {e!r}")
            traceback.print_exc()

    def _rc_inject_key(self, ev: dict, down: bool):
        """
        Инъекция одной клавиши на стороне стримера.

        Приоритет — WinAPI SendInput:
          • спец-клавиши и модификаторы → по виртуальному коду (нужно для
            удержания и комбинаций Ctrl+C, Alt+Tab, Win и т.д.);
          • если зажат Ctrl/Alt/Win (комбинация) → шлём латинский аналог
            клавиши по VK (комбо срабатывает независимо от раскладки);
          • обычный ввод символа → Unicode (печатает кириллицу/латиницу/!@#
            на нажатии; релиз игнорируем, т.к. type_unicode делает down+up).
        fallback — pyautogui (как раньше).
        """
        qt_key = int(ev.get('key', 0))
        text   = ev.get('text', '') or ''
        mods   = int(ev.get('modifiers', 0))

        special  = _qt_key_to_special(qt_key)          # 'ctrl','enter','f5'...
        combo    = bool(mods & (_RC_MOD_CTRL | _RC_MOD_ALT | _RC_MOD_META))
        key_name = _qt_key_to_basic(qt_key, text)       # 'a'..'z','0'..'9'

        # ── Путь WinAPI ──────────────────────────────────────────────────────
        if _WIN_INPUT_AVAILABLE:
            if special:
                print(f"[RC-INJECT] win special='{special}' down={down}")
                _win_input.key_named(special, down)
                return
            if combo:
                # Комбинация: шлём базовую клавишу по VK (для a-z/0-9), модификатор
                # уже зажат отдельным special-событием.
                vk = _basic_to_vk(key_name)
                print(f"[RC-INJECT] win combo name='{key_name}' vk={vk} down={down}")
                if vk:
                    _win_input.key_vk(vk, down)
                return
            # Обычный символ: печатаем Unicode на нажатии, релиз игнорим.
            if down and text and text.isprintable():
                print(f"[RC-INJECT] win unicode text={text!r}")
                _win_input.type_unicode(text)
            elif down and key_name:
                vk = _basic_to_vk(key_name)
                if vk:
                    print(f"[RC-INJECT] win vk fallback name='{key_name}' vk={vk}")
                    _win_input.key_vk(vk, True)
                    _win_input.key_vk(vk, False)
            return

        # ── Fallback: pyautogui ──────────────────────────────────────────────
        if not _PYAUTOGUI_AVAILABLE:
            return
        if special:
            (pyautogui.keyDown if down else pyautogui.keyUp)(special, _pause=False)
            return
        if combo:
            if key_name:
                (pyautogui.keyDown if down else pyautogui.keyUp)(key_name, _pause=False)
            return
        if down and text and text.isprintable():
            pyautogui.write(text, _pause=False)
        elif key_name and down:
            pyautogui.press(key_name, _pause=False)

    # ------------------------------------------------------------------
    # 3×ESC — экстренная остановка управления (сторона стримера)
    # ------------------------------------------------------------------
    def _rc_start_esc_watch(self):
        """
        Вешает глобальный keyboard-хук на ESC. Колбэк выполняется в потоке
        библиотеки keyboard, поэтому реальную остановку диспатчим в Qt-поток
        через сигнал _rc_esc_triggered.
        """
        if self._rc_esc_hook is not None:
            return
        self._rc_esc_times = []
        try:
            self._rc_esc_hook = keyboard.on_press_key(
                'esc', self._rc_on_esc_press, suppress=False
            )
        except Exception as e:
            print(f"[RemoteControl] не удалось повесить ESC-хук: {e}")
            self._rc_esc_hook = None

    def _rc_stop_esc_watch(self):
        """Снимает ESC-хук (управление завершено)."""
        hook = self._rc_esc_hook
        self._rc_esc_hook = None
        self._rc_esc_times = []
        if hook is not None:
            try:
                keyboard.unhook(hook)
            except Exception:
                pass

    def _rc_on_esc_press(self, event):
        """
        Колбэк keyboard (НЕ Qt-поток!). Считаем нажатия ESC в окне времени.
        Достигли RC_ESC_STOP_COUNT → эмитим сигнал в Qt-поток.
        """
        # управление уже не активно — игнор
        if self._rc_viewer_uid_get() is None:
            return
        now = time.monotonic()
        # отбрасываем устаревшие отметки за пределами окна
        self._rc_esc_times = [
            t for t in self._rc_esc_times if now - t <= RC_ESC_WINDOW_SEC
        ]
        self._rc_esc_times.append(now)
        if len(self._rc_esc_times) >= RC_ESC_STOP_COUNT:
            self._rc_esc_times = []
            # маршалим в Qt-поток
            self._rc_esc_triggered.emit()

    def _on_rc_esc_stop(self):
        """Qt-поток: стример трижды нажал ESC → останавливаем управление."""
        if self._rc_viewer_uid_get() is None:
            return
        # Скрываем баннер, снимаем хук и шлём stop зрителю.
        self._hide_rc_streamer_banner()

    def open_settings(self):
        # Запоминаем текущие устройства ДО открытия диалога.
        _dev_in_before  = self.app_settings.value("device_in_name",  "")
        _dev_out_before = self.app_settings.value("device_out_name", "")
        # Все параметры камеры — чтобы перезапустить захват при ЛЮБОМ изменении.
        _cam_before = (
            str(self.app_settings.value("camera_index", None)),
            str(self.app_settings.value("camera_send_fps", None)),
            str(self.app_settings.value("camera_preview_fps", None)),
            str(self.app_settings.value("camera_send_size", None)),
            str(self.app_settings.value("camera_jpeg_quality", None)),
        )

        if SettingsDialog(self.audio, self).exec():
            self.setup_hotkeys()

            _dev_in_after  = self.app_settings.value("device_in_name",  "")
            _dev_out_after = self.app_settings.value("device_out_name", "")
            _cam_after = (
                str(self.app_settings.value("camera_index", None)),
                str(self.app_settings.value("camera_send_fps", None)),
                str(self.app_settings.value("camera_preview_fps", None)),
                str(self.app_settings.value("camera_send_size", None)),
                str(self.app_settings.value("camera_jpeg_quality", None)),
            )

            # Перезапускаем аудиопоток ТОЛЬКО если устройство реально сменилось.
            # При изменении VAD, громкости, soundboard-кнопок и т.д. —
            # stream не трогаем: слушатели не услышат провала в 100ms.
            if _dev_in_after != _dev_in_before or _dev_out_after != _dev_out_before:
                self.audio.start(_dev_in_after, _dev_out_after)
            else:
                print("[Settings] Устройства не изменились — аудиопоток не перезапускается")

            # Любой параметр камеры изменился И камера включена → рестарт на лету.
            if _cam_after != _cam_before and getattr(self, "is_camera_on", False):
                print("[Settings] Параметры камеры изменены — перезапуск захвата на лету")
                self.restart_camera_capture()

    # ── Статус пользователя ────────────────────────────────────────────────────

    def open_status_dialog(self):
        """
        Программный вызов выбора статуса (резервный метод).
        Основной UX — правый клик по своему нику в дереве (SelfStatusOverlayPanel).
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
        from ui_dialogs.ui_soundboard import SoundboardPanel

        # Проверяем состояние существующей панели с защитой от RuntimeError
        # (возникает если C++ объект уже уничтожен Qt — крайний случай)
        try:
            if self._sb_panel is not None:
                if self._sb_panel.isVisible():
                    # Панель открыта → toggle: закрываем и выходим
                    self._sb_panel.close()
                    self._sb_panel = None
                    return
                else:
                    # Панель существует но скрыта → удаляем старый объект
                    self._sb_panel.deleteLater()
                    self._sb_panel = None
        except RuntimeError:
            # C++ объект уже мёртв — просто сбрасываем ссылку
            self._sb_panel = None

        panel = SoundboardPanel(self.net, self)
        self._sb_panel = panel
        # Позиция: слева от главного окна
        panel.adjustSize()
        win_pos = self.frameGeometry()
        px = win_pos.left() - panel.width() - 4
        py = win_pos.top()
        # Если не помещается слева — показываем справа
        if px < 0:
            px = win_pos.right() + 4
        panel.move(px, py)
        panel.show()

    def _on_soundboard_played(self, from_nick: str):
        """
        Показывает жёлтый тост «🎵 [nick] включил звук» над кнопкой soundboard.
        Также обновляет метку автора в открытой soundboard-панели (главное окно
        и все открытые окна стримов).
        """
        # ── Тост поверх главного окна ─────────────────────────────────────────
        self._sb_toast.setText(f"🎵  {from_nick}  включил звук")
        self._sb_toast.adjustSize()
        # Центрируем по ширине ОСНОВНОЙ области (без ChatPanel).
        _main_w = self.width()
        if self._chat_panel.isVisible():
            _main_w -= ChatPanel.PANEL_WIDTH
        tw = self._sb_toast.width()
        tx = (_main_w - tw) // 2
        ty = self._bottom_bar.y() - self._sb_toast.height() - 28
        self._sb_toast.move(tx, max(4, ty))
        self._sb_toast.raise_()
        self._sb_toast.setVisible(True)
        self._sb_toast_timer.start()

        # ── Метка автора в открытых soundboard-панелях ────────────────────────
        # Главное окно
        try:
            if self._sb_panel is not None and self._sb_panel.isVisible():
                self._sb_panel.flash_from_nick(from_nick)
        except (RuntimeError, AttributeError):
            pass
        # Окна стримов
        for w in list(self.stream_windows.values()):
            try:
                if w.isVisible() and w._sb_panel is not None and w._sb_panel.isVisible():
                    w._sb_panel.flash_from_nick(from_nick)
            except (RuntimeError, AttributeError):
                pass

    # ── Слоты фичи «Пнуть» ───────────────────────────────────────────────────

    def _on_nudge_received(self):
        """
        Нас пнули — показать красный тост.
        Звук уже воспроизводится в потоке NetworkClient._play_nudge_sound().
        """
        self._show_nudge_toast("👟  Тебя пнули!")

    def _on_nudge_triggered(self, target_nick: str, voter_nick: str):
        """
        Broadcast: кого-то пнули в нашей комнате.
        Показываем информационный тост у ВСЕХ участников, включая инициатора.
        """
        self._show_nudge_toast(f"👟  {voter_nick} пнул {target_nick}!")

    def _show_nudge_toast(self, text: str):
        """
        Красный тост в правом нижнем углу главного окна на 4 секунды.
        Создаётся один раз (lazy init) — не аллоцирует при каждом вызове.
        Если тост уже виден — обновляем текст и перезапускаем таймер.
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
        # Правый нижний угол, над панелью управления
        self._nudge_toast_lbl.move(mw - tw - 14, mh - th - 60)
        self._nudge_toast_lbl.raise_()
        self._nudge_toast_lbl.show()
        self._nudge_toast_timer.stop()
        self._nudge_toast_timer.start(4000)

    # ------------------------------------------------------------------
    # Файловая передача P2P — приём входящего предложения
    # ------------------------------------------------------------------
    def _on_file_offer_received(self, msg: dict):
        """
        Входящий file_offer / file_offer_room от другого клиента.

        Показывает компактный toast-диалог «Принять / Отклонить».
        Если пользователь принимает — открывает QFileDialog для выбора
        папки назначения и запускает FileReceiverWorker в отдельном потоке.

        Метод вызывается в GUI-потоке (сигнал из tcp_listen подключён
        через прямое соединение Qt → автоматически queued при cross-thread).
        """
        filename  = msg.get('filename', 'file')
        filesize  = msg.get('filesize', 0)
        sender_ip = msg.get('sender_ip', '')
        port      = msg.get('sender_port', 0)
        token     = msg.get('token', '')

        if not sender_ip or not port or not token:
            print(f"[UI] file_offer: неполные данные — игнорируем ({msg})")
            return

        # Звуковое оповещение о входящем файле — до показа toast,
        # чтобы пользователь услышал сигнал даже если окно не в фокусе.
        self.play_notification("file_received")

        size_str = _format_size(filesize)
        self._show_file_offer_toast(filename, size_str, sender_ip, port, token, filesize)

    def _show_file_offer_toast(self, filename: str, size_str: str,
                               sender_ip: str, port: int,
                               token: str, filesize: int):
        """
        Показывает компактный toast с кнопками «Принять» / «Отклонить».
        Позиционируется в правом нижнем углу главного окна.
        Автоматически скрывается через 30 секунд если нет реакции.
        """
        # ── Создаём виджет предложения ────────────────────────────────────────
        toast = QFrame(self)
        toast.setObjectName("fileOfferToast")
        toast.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        toast.setStyleSheet("""
            QFrame#fileOfferToast {
                background-color: rgba(20, 22, 30, 235);
                border: 1px solid rgba(91,142,245,0.50);
                border-radius: 10px;
            }
            QLabel { color: #c8ccd8; background: transparent; border: none; }
        """)

        lay = QVBoxLayout(toast)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        # Заголовок
        lbl_title = QLabel(f"📁  Входящий файл")
        lbl_title.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #8ab4f8;"
        )
        lay.addWidget(lbl_title)

        # Имя и размер файла
        lbl_file = QLabel(f"{filename}  ({size_str})")
        lbl_file.setStyleSheet("font-size: 11px; color: rgba(200,205,225,0.85);")
        lbl_file.setWordWrap(True)
        lay.addWidget(lbl_file)

        # Кнопки
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_accept = QPushButton("✓  Принять")
        btn_accept.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_accept.setStyleSheet("""
            QPushButton {
                background-color: rgba(39,174,96,0.25);
                color: #82e0aa; border: 1px solid rgba(46,204,113,0.45);
                border-radius: 6px; padding: 4px 12px; font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(39,174,96,0.45);
                border-color: rgba(46,204,113,0.8);
            }
        """)

        btn_decline = QPushButton("✕  Отклонить")
        btn_decline.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_decline.setStyleSheet("""
            QPushButton {
                background-color: rgba(200,60,60,0.18);
                color: #ff9090; border: 1px solid rgba(200,60,60,0.35);
                border-radius: 6px; padding: 4px 12px; font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(200,60,60,0.35);
                border-color: rgba(200,60,60,0.7);
            }
        """)

        btn_row.addWidget(btn_accept)
        btn_row.addWidget(btn_decline)
        lay.addLayout(btn_row)

        toast.adjustSize()
        toast.setFixedSize(toast.sizeHint())

        # Позиционируем в правом нижнем углу главного окна
        mw, mh = self.width(), self.height()
        tx = mw - toast.width() - 14
        ty = mh - toast.height() - 60
        toast.move(tx, max(4, ty))
        toast.raise_()
        toast.show()

        # Авто-скрытие через 30 секунд
        auto_hide = QTimer(self)
        auto_hide.setSingleShot(True)
        auto_hide.setInterval(30_000)
        auto_hide.timeout.connect(toast.hide)
        auto_hide.start()

        # ── Обработчики кнопок ────────────────────────────────────────────────
        def _on_accept():
            auto_hide.stop()
            toast.hide()

            # Выбор пути сохранения
            default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(default_dir, exist_ok=True)
            save_path, _ = QFileDialog.getSaveFileName(
                self,
                "Сохранить файл",
                os.path.join(default_dir, filename),
                "Все файлы (*)",
            )
            if not save_path:
                return   # пользователь отменил диалог сохранения

            # Запускаем приёмник
            worker = FileReceiverWorker(sender_ip, port, token,
                                        save_path, filesize)
            prog = FileTransferProgressWidget(filename, filesize, is_sender=False)
            prog.set_worker(worker)

            # FIX #1: держим жёсткую ссылку на worker чтобы GC не убил поток
            prog._worker_strong_ref = worker

            worker.progress.connect(lambda r, t: prog.update_progress(r, t))

            def _on_transfer_done(p):
                prog.set_done(p)
                print(f"[UI] 📥 Файл сохранён: {p}")
                # FIX #1: автоматически скрываем оверлей через 3 сек после успеха
                QTimer.singleShot(3000, prog.hide)

            def _on_transfer_error(m):
                prog.set_error(m)
                # FIX #1: скрываем через 4 сек после ошибки
                QTimer.singleShot(4000, prog.hide)

            worker.finished.connect(_on_transfer_done)
            worker.error.connect(_on_transfer_error)
            # FIX #1: при отмене сразу скрываем (уже было, убеждаемся что hide, не close)
            worker.cancelled.connect(lambda: prog.hide())

            _show_float_widget(prog)
            worker.start()

        def _on_decline():
            auto_hide.stop()
            toast.hide()

        btn_accept.clicked.connect(_on_accept)
        btn_decline.clicked.connect(_on_decline)

    def _update_known_users_registry(self, users_map):
        """
        Обновляет реестр известных пользователей (known_users.json).

        FIX MEM: раньше этот метод читал JSON-файл с диска при КАЖДОМ вызове
        update_user_tree() — а сервер шлёт sync_users несколько раз в секунду.
        Каждый вызов создавал новый dict, строки, объекты → постоянный мусор.

        Теперь реестр кэшируется в self._known_users_cache и читается с диска
        только один раз при первом вызове. На диск записывается только при реальных
        изменениях данных. Это устраняет постоянную аллокацию/GC-давление.
        """
        REGISTRY_FILE = KNOWN_USERS_PATH

        # Ленивая загрузка кэша (один раз за время жизни приложения)
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

    # ── Автообновление ─────────────────────────────────────────────────────────

    def _start_silent_update_check(self):
        """
        Запускает проверку обновлений в фоне сразу после старта.
        При обнаружении новой версии показывает зелёный баннер внизу окна.
        Никаких всплывающих окон — всё тихо.
        """
        if not GITHUB_REPO:
            return  # репо не настроено — молча пропускаем

        try:
            from core.updater import check_for_updates_async
        except ImportError:
            # updater.py не в sys.path (dev-режим без сборки PyInstaller) — пропускаем.
            print("[Update] updater module не найден — автопроверка отключена")
            return

        def _on_found(version: str, n_files: int, total_bytes: int):
            QTimer.singleShot(0, lambda: self._show_update_banner(version))

        check_for_updates_async(on_update_found=_on_found)

    def _show_update_banner(self, version: str):
        """Показывает зелёный баннер-кнопку с сообщением об обновлении."""
        self._update_banner.setText(
            f"🎉 Доступна новая версия v{version}  —  нажмите чтобы обновить"
        )
        self._update_banner.setVisible(True)

    # ══════════════════════════════════════════════════════════════════════════
    # Методы управления каналами
    # ══════════════════════════════════════════════════════════════════════════

    def _on_create_channel_requested(self):
        """Хост открывает диалог создания временного канала."""
        dlg = _CreateChannelDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name     = dlg.get_channel_name()
        password = dlg.get_password()
        self.net.send_json({
            'action':       'create_channel',
            'channel_name': name,
            'password':     password or '',
        })
        print(f"[UI] Запрос создания канала: '{name}' (пароль: {'да' if password else 'нет'})")

    def _on_rename_permanent_channel(self, old_name: str):
        """
        FIX #4: Хост переименовывает постоянный канал (например General).
        Диалог ввода нового имени. Сохраняем в USER_CONFIG_PATH и применяем на сервере.
        Имя сохраняется между сессиями — при следующем старте сервер прочитает из конфига.
        """
        from PyQt6.QtWidgets import QInputDialog
        new_name, ok = QInputDialog.getText(
            self,
            "Переименовать канал",
            f"Новое название канала (было: {old_name}):",
            QLineEdit.EchoMode.Normal,
            old_name,
        )
        if not ok or not new_name.strip():
            return
        new_name = new_name.strip()[:32]
        if new_name == old_name:
            return

        # Сохраняем в конфиг — будет применено при следующем старте сервера
        try:
            import json as _json
            from config import USER_CONFIG_PATH
            cfg = {}
            if os.path.exists(USER_CONFIG_PATH):
                with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    cfg = _json.load(f)
            # Сохраняем имя главного канала под ключом 'general_channel_name'
            cfg['general_channel_name'] = new_name
            with open(USER_CONFIG_PATH, 'w', encoding='utf-8') as f:
                _json.dump(cfg, f, ensure_ascii=False, indent=2)
            print(f"[UI] Сохранено имя главного канала: '{new_name}'")
        except Exception as e:
            print(f"[UI] Ошибка сохранения имени канала: {e}")

        # Отправляем команду переименования на сервер (горячее применение)
        self.net.send_json({
            'action':    'rename_channel',
            'old_name':  old_name,
            'new_name':  new_name,
        })
        print(f"[UI] Запрос переименования канала: '{old_name}' → '{new_name}'")

    def _on_channel_created(self, channel_name: str):
        """Сервер создал новый канал — обновляем список и сразу перестраиваем дерево."""
        names = [ch['name'] for ch in self._channel_list]
        if channel_name not in names:
            self._channel_list.append({
                'name':         channel_name,
                'has_password': False,
                'permanent':    False,
            })
        # FIX CHANNELS: немедленно обновляем default_rooms чтобы следующий
        # вызов refresh_ui (через ≤100 мс) увидел изменение в подписи дерева
        # и перерисовал его. Без этого новый канал появлялся только после
        # следующего события (mute/connect другого пользователя).
        self._sync_default_rooms_from_channel_list()
        print(f"[UI] Канал создан: '{channel_name}'")

    def _on_channel_deleted(self, channel_name: str):
        """Сервер удалил временный канал (он опустел) — немедленно убираем из дерева."""
        self._channel_list = [
            ch for ch in self._channel_list
            if ch['name'] != channel_name
        ]
        # FIX CHANNELS: то же — немедленный sync default_rooms → rebuild дерева
        self._sync_default_rooms_from_channel_list()
        print(f"[UI] Канал удалён: '{channel_name}'")
        if self.current_room == channel_name:
            self.net.send_json({'action': 'join_room', 'room': 'General'})

    def _sync_default_rooms_from_channel_list(self):
        """Синхронизирует default_rooms из _channel_list без ожидания sync_users."""
        permanent = [ch['name'] for ch in self._channel_list if ch.get('permanent', False)]
        temporary = [ch['name'] for ch in self._channel_list if not ch.get('permanent', False)]
        self.default_rooms = (permanent or ['General']) + sorted(temporary)

    def _on_join_room_denied(self, room: str, reason: str):
        """Вход в канал отклонён — запрашиваем пароль или показываем ошибку."""
        if reason == 'channel_auth_required':
            dlg = _ChannelPasswordDialog(channel_name=room, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            password = dlg.get_password()
            self.net.send_json({
                'action':       'join_channel_auth',
                'channel_name': room,
                'password':     password,
            })
            self._pending_channel_join = room
        elif reason == 'wrong_password':
            print(f"[UI] Неверный пароль для канала '{room}'")
        elif reason == 'not_found':
            print(f"[UI] Канал '{room}' не найден")

    def _on_channel_auth_ok(self, channel_name: str):
        """Авторизация прошла — входим в канал."""
        if self._pending_channel_join == channel_name:
            self._pending_channel_join = None
            self.net.send_json({'action': 'join_room', 'room': channel_name})

    def _on_channel_list_updated(self, channel_list: list):
        """Получен актуальный список каналов от сервера."""
        self._channel_list = channel_list

        # Строим список каналов для отображения в дереве.
        #
        # БЫЛО (баг): только permanent-каналы (General и т.п.).
        # Временный пустой канал не попадал ни в users_map.keys() (никого нет),
        # ни в default_rooms → дерево его не показывало вообще, пользователь
        # не мог никуда кликнуть чтобы зайти в созданный канал.
        #
        # СТАЛО: все каналы идут в default_rooms.
        # Порядок: сначала постоянные (General…), потом временные (по алфавиту).
        # Временный канал виден сразу после создания, даже если он пустой.
        permanent = [ch['name'] for ch in channel_list if ch.get('permanent', False)]
        temporary = [ch['name'] for ch in channel_list if not ch.get('permanent', False)]
        self.default_rooms = (permanent or ['General']) + sorted(temporary)

    def _disconnect_and_show_lobby(self):
        """
        Отключение от сервера и возврат на экран выбора серверов (лобби).

        Логика:
          1. Останавливаем UI-таймеры и keyboard-хуки.
          2. Закрываем вспомогательные окна (стримы, оверлеи).
          3. Останавливаем audio, video, net.
          4. Если мы хост — EmbeddedServerManager.stop() делает graceful
             миграцию (рассылает CMD_SERVER_MIGRATE), точно так же как при
             резком отключении хоста.
          5. Создаём свежий MultiServerScreen (лобби) — стандартный flow:
             выбор сервера → ConnectingScreen → новый MainWindow.
          6. Закрываем текущее главное окно без выхода из приложения.
        """
        print("[UI] _disconnect_and_show_lobby: начинаем отключение...")

        # ── 1. Останавливаем UI-таймеры ───────────────────────────────────────
        try:
            self.ui_timer.stop()
        except Exception:
            pass

        # ── 2. Снимаем keyboard-хуки ──────────────────────────────────────────
        try:
            import keyboard as _kb
            _kb.unhook_all()
        except Exception:
            pass

        # ── 3. Закрываем вспомогательные UI-элементы ──────────────────────────
        if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
            try:
                self._lobby_screen.close()
            except Exception:
                pass
            self._lobby_screen = None

        try:
            self._whisper_overlay.hide_overlay()
        except Exception:
            pass

        try:
            if self._streamer_draw_overlay is not None:
                self._streamer_draw_overlay.close()
                self._streamer_draw_overlay = None
        except Exception:
            pass

        for uid, w in list(self.stream_windows.items()):
            try:
                w.close()
            except Exception:
                pass
        self.stream_windows.clear()

        # Очищаем кэш медиафайлов чата
        try:
            from .ui_widgets import clear_media_cache
            clear_media_cache()
        except Exception:
            pass

        # ── 4. AudioHandler.stop() ────────────────────────────────────────────
        try:
            self.audio.stop()
        except Exception as ex:
            print(f"[UI] disconnect audio.stop() error: {ex}")

        # ── 5. VideoEngine.shutdown() ─────────────────────────────────────────
        try:
            self._stop_camera_capture()
            for _uid in list(getattr(self, 'cam_windows', {}).keys()):
                self._destroy_cam_window(_uid)
        except Exception as ex:
            print(f"[UI] disconnect camera stop error: {ex}")
        try:
            self.video.shutdown()
        except Exception as ex:
            print(f"[UI] disconnect video.shutdown() error: {ex}")

        # ── 6. EmbeddedServer — ПЕРВЫМ (ему нужен живой SFU для миграции) ────
        # FIX: был шаг 7, после net.stop(). Но net.stop() раньше убивал SFU
        # singleton → stop_gracefully() не мог отправить CMD_SERVER_MIGRATE
        # через SFU → клиенты не получали миграцию → висели 12 сек в reconnect.
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                mgr.stop()
        except Exception as ex:
            print(f"[UI] disconnect EmbeddedServer stop error: {ex}")

        # ── 7. NetworkClient.stop() — после сервера ───────────────────────────
        # Блокируем _on_connection_lost: ставим recovery state != IDLE.
        # tcp_listen проверяет _recovery_state перед вызовом _on_connection_lost.
        with self.net._recovery_lock:
            self.net._recovery_state = 'recovering'   # блокируем auto-recovery
        try:
            self.net.stop()
        except Exception as ex:
            print(f"[UI] disconnect net.stop() error: {ex}")
        # Сбрасываем state обратно — мы уходим в лобби, recovery не нужен.
        with self.net._recovery_lock:
            self.net._recovery_state = 'idle'

        # ── 7b. Финальная страховка: убиваем SFU если ещё жив ────────────────
        try:
            from network_engine.sfu_bridge import get_shared as _get_sfu_final
            _sfu_final = _get_sfu_final()
            if _sfu_final.is_running():
                _sfu_final.stop()
        except Exception:
            pass

        # ── 8. Открываем свежий экран выбора серверов ─────────────────────────
        try:
            from client_main.ui_server_select import MultiServerScreen
            from client_main.ui_login import load_server_name, LoginWindow
            server_name = load_server_name()
            screen = MultiServerScreen(self.nick, self.avatar, server_name=server_name)
            screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

            # Привязываем «Ввести вручную» → LoginWindow (стандартный flow)
            def _fallback_to_login(f_ip: str, f_nick: str, f_avatar: str):
                login_win = LoginWindow(
                    ip=f_ip, nick=f_nick, avatar=f_avatar,
                    error_msg=(
                        f"⚠️  Сервер недоступен: {f_ip}\n"
                        "Измените адрес и нажмите «Войти»."
                    ) if f_ip else "",
                )
                login_win.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
                # Кнопка «Назад» из LoginWindow → снова открыть MultiServerScreen
                def _back_to_servers():
                    s2 = MultiServerScreen(self.nick, self.avatar, server_name=server_name)
                    s2.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
                    s2.open_login.connect(_fallback_to_login)
                    s2.show()
                login_win.go_back.connect(_back_to_servers)
                login_win.show()

            screen.open_login.connect(_fallback_to_login)
            screen.show()
        except Exception as e:
            print(f"[UI] _disconnect_and_show_lobby lobby error: {e}")

        # ── 9. Убираем трей и закрываем MainWindow (без QApplication.quit) ───
        self._tray_icon.hide()
        self._returning_to_lobby = True
        self._force_quit = True
        self.close()

        print("[UI] _disconnect_and_show_lobby: завершено")

    def _disconnect_and_show_banned(self, server_ip: str, reason: str):
        """
        Disconnect + открыть BannedScreen вместо MultiServerScreen.

        Логика полностью повторяет _disconnect_and_show_lobby до шага 8.
        На шаге 8 вместо списка серверов открывается экран «Вы забанены».
        Кнопка «Назад» на нём открывает обычный MultiServerScreen —
        забаненный сервер в списке будет помечен иконкой «стоп».

        Мы НЕ делаем рефакторинг _disconnect_and_show_lobby в общую
        teardown-функцию намеренно: метод работающий, в нём много нюансов
        с порядком (EmbeddedServer → net.stop → SFU-страховка), и любое
        изменение порядка ломает миграцию хоста. Копируем шаги 1–7b
        буквально, расходимся только на шаге 8.
        """
        print(f"[UI] _disconnect_and_show_banned: ip={server_ip!r}")

        # ── 1. UI-таймеры ─────────────────────────────────────────────────────
        try:
            self.ui_timer.stop()
        except Exception:
            pass

        # ── 2. keyboard-хуки ──────────────────────────────────────────────────
        try:
            import keyboard as _kb
            _kb.unhook_all()
        except Exception:
            pass

        # ── 3. Вспомогательные окна ───────────────────────────────────────────
        if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
            try:
                self._lobby_screen.close()
            except Exception:
                pass
            self._lobby_screen = None

        try:
            self._whisper_overlay.hide_overlay()
        except Exception:
            pass

        try:
            if self._streamer_draw_overlay is not None:
                self._streamer_draw_overlay.close()
                self._streamer_draw_overlay = None
        except Exception:
            pass

        for uid, w in list(self.stream_windows.items()):
            try:
                w.close()
            except Exception:
                pass
        self.stream_windows.clear()

        try:
            from .ui_widgets import clear_media_cache
            clear_media_cache()
        except Exception:
            pass

        # ── 4. Audio ──────────────────────────────────────────────────────────
        try:
            self.audio.stop()
        except Exception as ex:
            print(f"[UI] banned-teardown audio.stop() error: {ex}")

        # ── 5. Video ──────────────────────────────────────────────────────────
        try:
            self.video.shutdown()
        except Exception as ex:
            print(f"[UI] banned-teardown video.shutdown() error: {ex}")

        # ── 6. EmbeddedServer ────────────────────────────────────────────────
        # Нас забанили как клиента — но мы могли сами держать свой сервер
        # параллельно (такое возможно, если мы хостим ОДИН сервер, а к
        # другому подключались как клиент). stop() на всякий случай.
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                mgr.stop()
        except Exception as ex:
            print(f"[UI] banned-teardown EmbeddedServer stop error: {ex}")

        # ── 7. NetworkClient.stop() ──────────────────────────────────────────
        # Reconnect уже заблокирован (_kicked_flag=True + running=False в
        # features.py). Но для надёжности блокируем recovery через state.
        with self.net._recovery_lock:
            self.net._recovery_state = 'recovering'
        try:
            self.net.stop()
        except Exception as ex:
            print(f"[UI] banned-teardown net.stop() error: {ex}")
        with self.net._recovery_lock:
            self.net._recovery_state = 'idle'

        # ── 7b. SFU-страховка ─────────────────────────────────────────────────
        try:
            from network_engine.sfu_bridge import get_shared as _get_sfu_final
            _sfu_final = _get_sfu_final()
            if _sfu_final.is_running():
                _sfu_final.stop()
        except Exception:
            pass

        # ── 8. Открываем BannedScreen вместо лобби ───────────────────────────
        try:
            from client_main.ui_banned import BannedScreen
            from client_main.ui_server_select import MultiServerScreen
            from client_main.ui_login import load_server_name, LoginWindow

            # Имя сервера для красивого заголовка: берём то, что отдавал
            # announcer при последнем sync_users. Если не знаем — IP.
            srv_name_display = getattr(self, '_connected_server_name', '') or server_ip
            server_name = load_server_name()

            banned = BannedScreen(
                server_ip=server_ip,
                reason=reason or '',
                server_name=srv_name_display,
            )
            banned.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

            # Фабрика перехода «Назад» → MultiServerScreen с fallback на LoginWindow.
            # Ровно повторяем связку что в _disconnect_and_show_lobby — чтобы
            # поведение было предсказуемым и пользователь не видел разницы.
            def _open_server_list(_ip: str = ''):
                screen = MultiServerScreen(self.nick, self.avatar,
                                           server_name=server_name)
                screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

                def _fallback_to_login(f_ip: str, f_nick: str, f_avatar: str):
                    login_win = LoginWindow(
                        ip=f_ip, nick=f_nick, avatar=f_avatar,
                        error_msg=(
                            f"⚠️  Сервер недоступен: {f_ip}\n"
                            "Измените адрес и нажмите «Войти»."
                        ) if f_ip else "",
                    )
                    login_win.setWindowIcon(
                        QIcon(resource_path("assets/icon/logo.ico"))
                    )
                    def _back_to_servers():
                        s2 = MultiServerScreen(self.nick, self.avatar,
                                               server_name=server_name)
                        s2.setWindowIcon(
                            QIcon(resource_path("assets/icon/logo.ico"))
                        )
                        s2.open_login.connect(_fallback_to_login)
                        s2.show()
                    login_win.go_back.connect(_back_to_servers)
                    login_win.show()

                screen.open_login.connect(_fallback_to_login)
                screen.show()
                # Закрываем banned screen ПОСЛЕ того как список появился —
                # иначе event loop может завершиться (если нет других окон).
                try:
                    banned.close()
                except Exception:
                    pass

            banned.back_clicked.connect(_open_server_list)
            banned.show()
        except Exception as e:
            print(f"[UI] _disconnect_and_show_banned screen error: {e}")
            # Fallback: если BannedScreen почему-то упал — показываем лобби
            try:
                from client_main.ui_server_select import MultiServerScreen
                from client_main.ui_login import load_server_name
                screen = MultiServerScreen(
                    self.nick, self.avatar,
                    server_name=load_server_name(),
                )
                screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
                screen.show()
            except Exception as e2:
                print(f"[UI] fallback lobby error: {e2}")

        # ── 9. Закрываем MainWindow ──────────────────────────────────────────
        self._tray_icon.hide()
        self._returning_to_lobby = True
        self._force_quit = True
        self.close()

        print("[UI] _disconnect_and_show_banned: завершено")

    def _on_switch_server(self, info: dict):
        """
        Переключение на другой сервер из лобби.

        ВАЖНО: использует fast_switch_to() вместо _reconnect_loop.
        fast_switch_to() не ждёт RECONNECT_DELAY (3 сек) между попытками —
        сервер уже работает, сеть жива, нужно просто быстро переподключиться.
        """
        new_ip = info.get('ip', '')
        if not new_ip or new_ip == self.ip:
            # Закрываем лобби если пользователь выбрал текущий сервер
            if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
                try:
                    self._lobby_screen.hide()
                except RuntimeError:
                    pass
                self._lobby_screen = None
            return

        # Закрываем лобби при успешном переключении
        if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
            try:
                self._lobby_screen.hide()
            except RuntimeError:
                pass
            self._lobby_screen = None

        # Устанавливаем новый IP в network client ДО любых остановок.
        self.net._ip = new_ip

        # Блокируем recovery: ставим state != IDLE чтобы tcp_listen при
        # закрытии старого сокета не запустил _on_connection_lost.
        # fast_switch_to ниже сам сбросит state=IDLE и стартует новый recovery.
        with self.net._recovery_lock:
            self.net._recovery_state = 'migrating'
        self.net.running = False

        # Останавливаем/передаём встроенный сервер если мы хост.
        # stop_gracefully сам выбирает нового хоста по минимальному пингу
        # и рассылает CMD_SERVER_MIGRATE остальным участникам.
        # send_server_transfer(0) УДАЛЁН — он создавал ДВОЙНУЮ миграцию:
        # server_transfer → broadcast, затем stop_gracefully → снова broadcast.
        if self._is_server_host():
            try:
                from server import EmbeddedServerManager
                mgr = EmbeddedServerManager.get()
                if mgr.is_running():
                    mgr.stop()
            except Exception as e:
                print(f"[UI] _on_switch_server stop error: {e}")

        self.ip = new_ip

        # Показываем экран переключения
        self._lost_title_lbl.setText("Переключение сервера")
        self._lost_status_lbl.setText(
            f"Подключение к {info.get('server_name', 'Сервер')}\n{new_ip}..."
        )
        self._btn_reconnect.setEnabled(False)
        self._stack.setCurrentIndex(1)

        # fast_switch_to: без задержки RECONNECT_DELAY, быстрые попытки 0.35 сек
        self.net.fast_switch_to(new_ip)

    # ------------------------------------------------------------------
    # System Tray
    # ------------------------------------------------------------------
    def _tray_show(self):
        """Показать окно из трея."""
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _tray_quit(self):
        """Полный выход из трея."""
        self._force_quit = True
        self.close()

    def _on_tray_activated(self, reason):
        """Двойной клик по иконке трея — показать окно."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_show()

    def closeEvent(self, e):
        """
        Крестик → сворачиваем в трей. Полный выход — через меню трея.
        Возврат в лобби → закрываем окно без QApplication.quit().
        """

        # ── Возврат в лобби (отключение от сервера) ────────────────────────
        # Cleanup уже выполнен в _disconnect_and_show_lobby. Просто закрываем
        # окно и НЕ завершаем Qt — лобби уже показан.
        if self._returning_to_lobby:
            print("[UI] closeEvent: возврат в лобби, закрываем MainWindow")
            e.accept()
            self.deleteLater()
            return

        # ── Сворачиваем в трей (если не запрошен полный выход) ────────────
        if not self._force_quit:
            e.ignore()
            self.hide()
            self._tray_icon.showMessage(
                APP_NAME,
                "Приложение свёрнуто в трей. ПКМ по иконке → Выйти.",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
            return

        # ── Полное завершение (из трея) ──────────────────────────────────
        print("[UI] closeEvent: начинаем shutdown...")

        # Очищаем кэш медиафайлов чата (конвертированные GIF→MP4, temp-видео)
        try:
            from .ui_widgets import clear_media_cache
            clear_media_cache()
        except Exception:
            pass

        # ── 1. Останавливаем UI-таймеры ПЕРВЫМИ ──────────────────────────────
        # ui_timer (100ms) продолжает дёргать refresh_ui во время teardown —
        # может обращаться к уже закрытым объектам → RuntimeError / краш.
        try:
            self.ui_timer.stop()
        except Exception:
            pass

        # ── 2. Снимаем keyboard-хуки ──────────────────────────────────────────
        # Без unhook_all() глобальные хуки продолжают перехватывать ввод даже
        # после закрытия окна — до полного завершения процесса. На Windows это
        # приводит к тому что другие приложения не получают определённые клавиши.
        try:
            import keyboard as _kb
            _kb.unhook_all()
        except Exception:
            pass

        # ── 3. Закрываем вспомогательные UI-элементы ─────────────────────────
        if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
            try:
                self._lobby_screen.close()
            except Exception:
                pass
            self._lobby_screen = None

        try:
            self._whisper_overlay.hide_overlay()
            self._whisper_overlay.deleteLater()
        except Exception:
            pass

        # Закрываем оверлей аннотаций стримера если активен
        try:
            if self._streamer_draw_overlay is not None:
                self._streamer_draw_overlay.close()
                self._streamer_draw_overlay = None
        except Exception:
            pass

        # Закрываем все открытые VideoWindow (стримы зрителей)
        for uid, w in list(self.stream_windows.items()):
            try:
                w.close()
            except Exception:
                pass
        self.stream_windows.clear()

        # ── 4. AudioHandler.stop() ────────────────────────────────────────────
        # Закрывает PortAudio поток (stream.stop/close) и _pkt_thread.
        # Должен идти до net.stop() — audio использует send_queue сети.
        try:
            self.audio.stop()
        except Exception as ex:
            print(f"[UI] closeEvent audio.stop() error: {ex}")

        # ── 5. VideoEngine.shutdown() ─────────────────────────────────────────
        # Останавливает DXCamTrack и все VideoReceiver.
        # После net.stop() WebRTC треки будут закрыты, поэтому делаем ДО.
        try:
            self._stop_camera_capture()
            for _uid in list(getattr(self, 'cam_windows', {}).keys()):
                self._destroy_cam_window(_uid)
        except Exception as ex:
            print(f"[UI] closeEvent camera stop error: {ex}")
        try:
            self.video.shutdown()
        except Exception as ex:
            print(f"[UI] closeEvent video.shutdown() error: {ex}")

        # ── 6. EmbeddedServer — ПЕРВЫМ (ему нужен живой SFU для миграции) ────
        # FIX: был шаг 7, после net.stop(). Но net.stop() раньше убивал SFU
        # singleton → stop_gracefully() не мог отправить CMD_SERVER_MIGRATE.
        # stop_gracefully(): broadcast CMD_SERVER_MIGRATE → 350мс → close сокеты.
        # Другие клиенты успевают получить команду и переподключиться.
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                mgr.stop()
        except Exception as ex:
            print(f"[UI] closeEvent EmbeddedServer stop error: {ex}")

        # ── 7. NetworkClient.stop() — после сервера ───────────────────────────
        # Закрывает RTCPeerConnection, asyncio loop, TCP/UDP сокеты.
        # После этого все сетевые потоки выйдут из блокирующих recv().
        try:
            self.net.stop()
        except Exception as ex:
            print(f"[UI] closeEvent net.stop() error: {ex}")

        # ── 7b. Финальная страховка: убиваем SFU если ещё жив ────────────────
        try:
            from network_engine.sfu_bridge import get_shared as _get_sfu_final
            _sfu_final = _get_sfu_final()
            if _sfu_final.is_running():
                _sfu_final.stop()
        except Exception:
            pass

        # ── 8. Убираем иконку из трея и завершаем Qt ────────────────────
        print("[UI] closeEvent: shutdown завершён")
        self._tray_icon.hide()
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()
        e.accept()

    def on_audio_status_changed(self, mute, deaf):
        self.net.send_status_update(mute, deaf)

    def update_stream_button_icon(self):
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
            self.btn_stream.setStyleSheet("")  # вернуть к CSS из apply_theme
            # Скрываем индикатор плохого соединения при остановке трансляции
            self._stream_conn_lbl.setVisible(False)

    def _on_bitrate_adjusted(self, bitrate: int):
        """
        STUB — ABR через UDP удалён. WebRTC управляет битрейтом автоматически (TWCC).
        Метод сохранён: _stream_conn_lbl остаётся в UI и будет подключён
        к WebRTC pc.getStats() в следующей итерации (ui_video.py, Шаг 7).
        """
        pass

    def toggle_stream(self):
        from ui_dialogs.ui_stream_settings import StreamSettingsDialog
        if not self.is_streaming:
            dialog = StreamSettingsDialog(self)
            if dialog.exec():
                settings = dialog.get_settings()

                # Порядок операций (важен для WebRTC signaling):
                #   1. CMD_STREAM_START → сервер помечает нас как стримера.
                #      Сервер должен знать о стриме ДО получения webrtc_offer,
                #      иначе он не сможет связать offer с конкретным стримером.
                #   2. net.start_streaming_webrtc(settings):
                #        — создаёт DXCamTrack через VideoEngine или запускает Rust-мост
                #        — создаёт RTCPeerConnection
                #        — добавляет видео/аудио треки
                #        — создаёт SDP offer → ждёт ICE gathering → отправляет серверу
                # Передаём реальный порт SFU серверу — он рассылает его клиентам
                # через global_state → sfu_port, чтобы удалённые зрители знали
                # на какой порт идти (динамический, не всегда 7788).
                _sfu_port = 7788
                try:
                    _sb = getattr(self.net, '_sfu_bridge', None)
                    if _sb is not None:
                        _sfu_port = _sb.port
                except Exception:
                    pass
                self.net.send_json({"action": CMD_STREAM_START, "sfu_port": _sfu_port})
                self.net.start_streaming_webrtc(settings)

                # Синхронная проверка успеха:
                # v3: Rust Media Engine — стрим OK, если процесс жив.
                _rust_ok = (
                        hasattr(self.net, '_media_bridge')
                        and self.net._media_bridge is not None
                        and self.net._media_bridge.is_running()
                )

                if not _rust_ok:
                    print("[UI] Ошибка: Rust Media Engine не запустился.")
                    self.net.send_json({"action": CMD_STREAM_STOP})
                    self.btn_stream.setChecked(False)
                    return

                self.is_streaming = True

                # ── Создаём прозрачный оверлей аннотаций для стримера ────────
                # Показывает мазки зрителей поверх захватываемого экрана.
                # WA_TransparentForMouseEvents → DXCam и мышь работают нормально.
                # Уничтожается при остановке стрима (ниже).
                try:
                    if self._streamer_draw_overlay is not None:
                        self._streamer_draw_overlay.close()
                        self._streamer_draw_overlay = None
                    self._streamer_draw_overlay = StreamerAnnotationOverlay()
                    self._streamer_draw_overlay.show()
                    print("[UI] StreamerAnnotationOverlay создан")
                except Exception as e:
                    print(f"[UI] StreamerAnnotationOverlay ошибка: {e}")
                    self._streamer_draw_overlay = None
            else:
                self.btn_stream.setChecked(False)
                return
        else:
            self.is_streaming = False
            self.net.send_json({"action": CMD_STREAM_STOP})

            # Если в момент остановки трансляции кто-то нами управлял —
            # снимаем управление, гасим баннер и ESC-хук.
            if self._rc_viewer_uid_get() is not None:
                self._hide_rc_streamer_banner()

            # FIX LEAK #1: stop_streaming_webrtc() закрывает RTCPeerConnection
            # стримера и SystemAudioTrack. Без этого вызова _streamer_pc оставался
            # открытым после каждого стрима → H264 encoder + DTLS/SRTP буферы
            # (~50 МБ) никогда не освобождались.
            # Порядок: сначала CMD_STREAM_STOP на сервер (он убирает нас из стримеров),
            # затем закрываем PC (чтобы SFU успел разорвать соединение корректно).
            self.net.stop_streaming_webrtc()

            # v3: VideoEngine.stop_streaming() выполняет GC + heap trim.
            # Вызываем после stop_streaming_webrtc() для корректного порядка очистки.
            self.video.stop_streaming()

            # ── Уничтожаем оверлей аннотаций стримера ───────────────────────
            try:
                if self._streamer_draw_overlay is not None:
                    self._streamer_draw_overlay.close()
                    self._streamer_draw_overlay = None
                    print("[UI] StreamerAnnotationOverlay закрыт")
            except Exception:
                self._streamer_draw_overlay = None

        self.update_stream_button_icon()
        self.refresh_ui()

# ===========================================================================
# Remote Control — вспомогательные функции конвертации Qt → pyautogui
# ===========================================================================

# Qt.Key.Key_Escape == 0x01000000. Держим как модульную константу, чтобы не
# импортировать Qt внутри горячего цикла инъекции событий.
_RC_QT_KEY_ESCAPE = 0x01000000

# Qt.KeyboardModifier значения (битовая маска). Держим как константы, чтобы
# не импортировать Qt в горячем пути инъекции.
_RC_MOD_SHIFT = 0x02000000
_RC_MOD_CTRL  = 0x04000000
_RC_MOD_ALT   = 0x08000000
_RC_MOD_META  = 0x10000000


def _qt_button_to_pyautogui(qt_button: int) -> str:
    """
    Конвертирует Qt.MouseButton int в строку pyautogui.
    Значения Qt.MouseButton стабильны: Left=1, Right=2, Middle=4.
    Используем числовые литералы, т.к. int(Qt.MouseButton.X) в PyQt6
    бросает TypeError.
    """
    mapping = {1: 'left', 2: 'right', 4: 'middle'}
    return mapping.get(int(qt_button), 'left')


def _basic_to_vk(name: str) -> int:
    """
    VK-код для базовой клавиши 'a'..'z' / '0'..'9' (для комбинаций через WinAPI).
    VK букв == ASCII заглавной (A=0x41), VK цифр == ASCII ('0'=0x30).
    """
    if not name or len(name) != 1:
        return 0
    c = name.lower()
    if 'a' <= c <= 'z':
        return ord(c.upper())
    if '0' <= c <= '9':
        return ord(c)
    return 0


def _build_special_key_map() -> dict:
    """
    Карта Qt.Key → имя клавиши pyautogui для НЕпечатных/спец-клавиш.
    Значения Qt.Key стабильны между версиями; задаём их числовыми литералами,
    т.к. int(Qt.Key.X) в PyQt6 бросает TypeError.
    """
    return {
        0x01000004: 'enter',       # Key_Return
        0x01000005: 'enter',       # Key_Enter
        0x01000003: 'backspace',   # Key_Backspace
        0x01000007: 'delete',      # Key_Delete
        0x01000001: 'tab',         # Key_Tab
        0x01000000: 'esc',         # Key_Escape
        0x00000020: 'space',       # Key_Space
        0x01000012: 'left',        # Key_Left
        0x01000014: 'right',       # Key_Right
        0x01000013: 'up',          # Key_Up
        0x01000015: 'down',        # Key_Down
        0x01000010: 'home',        # Key_Home
        0x01000011: 'end',         # Key_End
        0x01000016: 'pageup',      # Key_PageUp
        0x01000017: 'pagedown',    # Key_PageDown
        0x01000006: 'insert',      # Key_Insert
        0x01000009: 'printscreen', # Key_Print
        0x01000024: 'capslock',    # Key_CapsLock
        0x01000025: 'numlock',     # Key_NumLock
        0x01000026: 'scrolllock',  # Key_ScrollLock
        0x01000021: 'ctrl',        # Key_Control
        0x01000020: 'shift',       # Key_Shift
        0x01000023: 'alt',         # Key_Alt
        0x01000022: 'win',         # Key_Meta
        0x01000030: 'f1',  0x01000031: 'f2',
        0x01000032: 'f3',  0x01000033: 'f4',
        0x01000034: 'f5',  0x01000035: 'f6',
        0x01000036: 'f7',  0x01000037: 'f8',
        0x01000038: 'f9',  0x01000039: 'f10',
        0x0100003a: 'f11', 0x0100003b: 'f12',
    }


# Кэш карты спец-клавиш (строится один раз при первом обращении).
_RC_SPECIAL_KEY_MAP: dict | None = None


def _qt_key_to_special(qt_key: int) -> str:
    """Возвращает имя спец-клавиши pyautogui или '' если это печатный символ."""
    global _RC_SPECIAL_KEY_MAP
    if _RC_SPECIAL_KEY_MAP is None:
        _RC_SPECIAL_KEY_MAP = _build_special_key_map()
    return _RC_SPECIAL_KEY_MAP.get(qt_key, '')


def _qt_key_to_basic(qt_key: int, text: str = '') -> str:
    """
    Имя «обычной» клавиши для keyDown/keyUp (нужно для комбинаций Ctrl+X
    и для случаев, когда text пустой).
    Qt.Key_A..Key_Z == 0x41..0x5A, Qt.Key_0..Key_9 == 0x30..0x39 — совпадают
    с ASCII, поэтому маппим напрямую.
    """
    if 0x41 <= qt_key <= 0x5A:            # A-Z
        return chr(qt_key).lower()
    if 0x30 <= qt_key <= 0x39:            # 0-9
        return chr(qt_key)
    if text and len(text) == 1 and text.isprintable():
        return text.lower()
    return ''


# Совместимость со старым именем (на случай внешних вызовов).
def _qt_key_to_pyautogui(qt_key: int, text: str = '') -> str:
    special = _qt_key_to_special(qt_key)
    if special:
        return special
    return _qt_key_to_basic(qt_key, text)
