import os
import base64
import gc
import json
import sounddevice as sd
import soundfile as sf
import winsound
import keyboard
import time
from video_engine import VideoEngine
from ui_video import VideoWindow, StreamerAnnotationOverlay
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
from version import APP_VERSION, APP_NAME, GITHUB_REPO




# ──────────────────────────────────────────────────────────────────────────────
# QuickMsgBubble — стеклянный пузырь быстрого сообщения
# ──────────────────────────────────────────────────────────────────────────────



class MainWindow(QMainWindow):
    def __init__(self, ip, nick, avatar):
        super().__init__()
        font_path = resource_path("assets/font/MyFont.ttf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        self.custom_font_family = QFontDatabase.applicationFontFamilies(font_id)[0] if font_id != -1 else "Segoe UI"

        self.ip, self.nick, self.avatar = ip, nick, avatar
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.known_uids = {}
        self._icon_size = 24  # Мы уже выяснили, что она нужна
        self._my_status_icon = None  # Для фикса ошибки в on_connected
        from PyQt6.QtGui import QFont
        self._font_room = QFont()  # Базовый шрифт для комнат
        self._font_user = QFont()
        self.current_room = "General"
        self.default_rooms = ["General"]
        # Актуальный список каналов (обновляется из sync_users → channel_list)
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
            # ── Новые звуки ───────────────────────────────────────────────────
            # friend_connect.wav — воспроизводится когда ЛЮБОЙ пользователь
            # появляется на сервере (подключается впервые в текущей сессии).
            # file_received.wav  — воспроизводится при входящем предложении файла.
            "friend_connect": resource_path("assets/music/friend_connect.wav"),
            "file_received":  resource_path("assets/music/file.wav"),
            # Чат: входящее сообщение / исходящее
            "chat_msg_in":    resource_path("assets/music/message.wav"),
            "chat_msg_out":   resource_path("assets/music/message_send.wav"),
        }
        self.prev_room_uids: set = set()
        self.prev_streaming_uids: set = set()
        # ── Состояние для звука подключения друга ─────────────────────────────
        # prev_all_uids — UIDs всех пользователей на сервере (без себя) с прошлого
        # обновления. Используется в update_user_tree() для детектирования новых
        # подключений к серверу (не только к текущей комнате).
        #
        # _server_users_initialized — False сразу после (пере)подключения.
        # При первом sync_users просто засеваем prev_all_uids без звука,
        # чтобы не воспроизводить friend_connect для ВСЕХ уже подключённых
        # пользователей в момент входа в сервер.
        self.prev_all_uids: set = set()
        self._server_users_initialized: bool = False

        self.audio = AudioHandler()
        self.net = NetworkClient(self.audio)

        # ── Состояние для ресайза безрамочного окна ──────────────────────────
        self._resize_margin = 6          # px — зона у края для начала ресайза
        self._resize_direction: str | None = None
        self._resize_start_pos: QPoint | None = None
        self._resize_start_geom: QRect | None = None
        self.setMouseTracking(True)

        # Приложение-уровневый фильтр для корректного сброса курсора ресайза
        # когда мышь уходит с края рамки на дочерние виджеты (tree, кнопки и т.п.)
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

        # Предзагрузка звуков уведомлений: каждый звук загружается ОДИН РАЗ.
        # Хранится как (data, sr) кортеж — sounddevice воспроизводит напрямую без
        # повторного чтения с диска при каждом событии.
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

        # ── System Tray Icon ─────────────────────────────────────────────────
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
        self._force_quit = False   # True = полный выход из трея

        # ── ChatPanel: встроена в _main_row (окно расширяется при открытии) ───
        # ChatPanel добавлена в QHBoxLayout рядом с main_page в setup_ui().
        # При открытии: setVisible(True) + resize(w + PANEL_WIDTH, h).
        # При закрытии: setVisible(False) + resize(w - PANEL_WIDTH, h).
        self._chat_panel.set_my_uid(0)     # обновится в on_connected
        self._chat_panel.message_sent.connect(self._on_chat_panel_send)
        self._chat_panel.media_send_requested.connect(self._on_chat_media_requested)
        # Chat button moved from title bar to full-width button above bottom_bar

        self.net.connected.connect(self.on_connected)
        self.net.global_state_update.connect(self.update_user_tree)
        self.net.error_occurred.connect(self.on_connection_error)
        self.net.connection_lost.connect(self.on_connection_lost)
        self.net.connection_restored.connect(self.on_connection_restored)
        self.net.reconnect_failed.connect(self.on_reconnect_failed)

        self.audio.status_changed.connect(self.on_audio_status_changed)
        self.audio.status_changed.connect(self.net.send_status_update)
        self.audio.whisper_received.connect(self._on_whisper_received)
        # Сигнал из audio_engine: ползунок громкости пользователя достиг/покинул 0.
        # Обновляем ban-иконку немедленно, не дожидаясь следующего refresh_ui() (100 мс).
        self.audio.user_volume_zero.connect(self._on_user_volume_zero)
        self.video.frame_received.connect(self.on_video_frame)
        # Статистика качества (FPS + Loss%) → обновляет HUD VideoWindow каждые 2 сек.
        self.video.stream_stats_updated.connect(self.on_stream_stats_updated)

        # Тост «кто включил soundboard» — желтый лейбл поверх окна
        self.net.soundboard_played.connect(self._on_soundboard_played)

        # Сигналы фичи «Пнуть»
        self.net.nudge_received.connect(self._on_nudge_received)
        self.net.nudge_triggered.connect(self._on_nudge_triggered)

        # Хост выключил наш микрофон
        self.net.force_muted.connect(self._on_force_muted)

        # ABR-сигнал (bitrate_adjusted) удалён: WebRTC управляет битрейтом через TWCC.
        # _stream_conn_lbl оставлен в UI — будет подключён к WebRTC getStats() позже.

        # Входящий запрос файловой передачи от другого пользователя
        self.net.file_offer_received.connect(self._on_file_offer_received)

        # Быстрый чат: всплывающий пузырь у ника отправителя
        self.net.quick_msg_received.connect(self._on_quick_msg_received)
        # Словарь активных пузырей: uid → (QLabel, QTimer)
        # Хранение предотвращает создание нескольких пузырей для одного юзера.
        self._quick_bubbles: dict[int, tuple] = {}

        # Постоянный чат
        self.net.chat_msg_received.connect(self._on_chat_msg_received)
        self.net.chat_history_received.connect(self._on_chat_history_received)
        self.net.chat_media_received.connect(self._on_chat_media_received)
        self.net.typing_received.connect(self._on_typing_received)
        self._chat_panel.typing_started.connect(self.net.send_typing)

        # ── Встроенный сервер: миграция хоста ────────────────────────────────
        # become_host      — нам нужно стать новым хостом сервера.
        # server_migrating — сервер переезжает к другому хосту.
        self.net.become_host.connect(self._on_become_host)
        self.net.server_migrating.connect(self._on_server_migrating)

        # ── Каналы и мульти-серверная панель ─────────────────────────────────
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
        self._sb_panel = None   # ссылка на SoundboardPanel (для toggle и lifecycle)

        # ── Оверлей аннотаций на экране стримера ────────────────────────────
        # Создаётся при старте стрима, уничтожается при остановке.
        # Показывает мазки зрителей поверх захватываемого контента.
        self._streamer_draw_overlay: StreamerAnnotationOverlay | None = None

        # Входящие мазки (от сервера) → распределяем по назначению:
        #   — если мы стример → _streamer_draw_overlay.add_stroke()
        #   — если мы зритель → соответствующий VideoWindow.add_remote_stroke()
        self.net.draw_stroke_received.connect(self._on_draw_stroke_received)

        # ── Тост soundboard ─────────────────────────────────────────────────────
        # QLabel поверх главного окна с абсолютным позиционированием.
        # Показывается на 3.5 с когда кто-то нажимает кнопку в soundboard-панели.
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

        # Таймер завершения шёпота: если >1.5 с не было пакетов — скрываем баннер/оверлей
        self._whisper_end_timer = QTimer()
        self._whisper_end_timer.setSingleShot(True)
        self._whisper_end_timer.setInterval(1500)
        self._whisper_end_timer.timeout.connect(self._on_whisper_ended)

        # Системный оверлей шёпота — поверх всех окон Windows
        # Создаём один раз, показываем/скрываем при событиях шёпота.
        self._whisper_overlay = WhisperSystemOverlay()

        # Тихая проверка обновлений в фоне (без всплывающих окон)
        self._start_silent_update_check()

        # ── Кэш объектов для refresh_ui() ──────────────────────────────────────
        # refresh_ui() вызывается каждые 100 мс. Создание QColor/QSize/QSettings
        # внутри метода = 10 аллокаций/сек × N_users без необходимости.
        # Кэшируем один раз здесь, обновляем только при смене темы.
        # FIX #7: только тёмная тема — _cache_theme всегда "Темная".
        self._cache_theme = "Темная"
        self._theme_dirty = False  # ВАЖН-6: флаг вместо QSettings.value() каждые 100 мс
        self._c_talk   = QColor("#2ecc71")
        self._c_mute   = QColor("#e74c3c")
        self._c_stream = QColor("#3498db")
        self._c_def    = QColor("#ecf0f1")
        self._icon_size = QSize(26, 26)

        # ── Кэш QBrush для refresh_ui() ────────────────────────────────────────
        # QBrush(QColor) создавался на КАЖДЫЙ вызов refresh_ui() (10 раз/сек)
        # для КАЖДОГО пользователя → постоянное давление на GC.
        # Кэшируем один раз, пересоздаём только при смене темы.
        self._br_talk   = QBrush(self._c_talk)
        self._br_mute   = QBrush(self._c_mute)
        self._br_stream = QBrush(self._c_stream)
        self._br_def    = QBrush(self._c_def)
        self._br_gray   = QBrush(QColor("#888888"))   # для заголовков комнат и watchers
        self._br_gold   = QBrush(QColor("#f5c518"))   # золотой — для ника хоста

        # ── Кэш иконок для refresh_ui() ────────────────────────────────────────
        # refresh_ui() вызывается каждые 100 мс и раньше создавал QIcon().pixmap()
        # внутри цикла → 4 иконки × N пользователей × 10 вызовов/сек = лишние
        # аллокации и давление на GC. Создаём pixmap один раз здесь.
        self._px_live      = QIcon(resource_path("assets/icon/live.svg")).pixmap(25, 25)
        self._px_vol_off   = QIcon(resource_path("assets/icon/volume_off.svg")).pixmap(self._icon_size)
        self._px_mic_off   = QIcon(resource_path("assets/icon/mic_off.svg")).pixmap(self._icon_size)
        self._px_ban       = QIcon(resource_path("assets/icon/ban.svg")).pixmap(self._icon_size)

        # Кэш пиксмапов иконок статусов пользователей (assets/status/*.svg).
        # Ключ: имя файла (например 'afk.svg'). Значение: QPixmap 20×20.
        # Заполняется лениво в update_user_tree() при первом появлении иконки.
        # Пересоздавать при смене темы не нужно — SVG не зависят от темы.
        self._status_px_cache: dict = {}

        # Текущий статус пользователя. Загружается из QSettings при старте,
        # отправляется на сервер при каждом (пере)подключении.
        # Изменяется через SettingsDialog → вкладка «О себе».
        self._my_status_icon: str = self.app_settings.value("my_status_icon", "")
        self._my_status_text: str = self.app_settings.value("my_status_text", "")
        # Создание QFont внутри метода = лишние аллокации при каждом обновлении.
        # Шрифты зависят от custom_font_family, который не меняется в runtime.
        self._font_room    = QFont(self.custom_font_family, 12)
        self._font_room.setBold(True)
        self._font_user    = QFont(self.custom_font_family, 14)
        self._font_watcher = QFont(self.custom_font_family, 11)

    def setup_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.nick}")
        self.setMinimumSize(400, 600)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        # Прозрачность по краям окна — углы и 4px внешний отступ становятся
        # полностью прозрачными, создавая эффект «парящего» окна без жёстких
        # прямоугольных краёв. Требует border-radius в #windowRoot stylesheet.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ── Корневой контейнер окна ──────────────────────────────────────────
        _root = QWidget()
        _root.setObjectName("windowRoot")
        _root_layout = QVBoxLayout(_root)
        # 4px внешний отступ: прозрачная «аура» вокруг окна,
        # в которой видна тень и скруглённые углы (см. border-radius в apply_theme).
        _root_layout.setContentsMargins(0, 0, 0, 0)
        _root_layout.setSpacing(0)

        # Кастомный заголовок
        self._title_bar = CustomTitleBar(self, f"{APP_NAME} v{APP_VERSION} — {self.nick}")
        _root_layout.addWidget(self._title_bar)

        # Разделитель под заголовком
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
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)

        layout.addWidget(self.tree, stretch=1)

        # ── Баннер автообновления (скрыт до обнаружения новой версии) ─────────
        self._update_banner = QPushButton()
        self._update_banner.setObjectName("updateBanner")
        self._update_banner.setVisible(False)
        self._update_banner.clicked.connect(self.open_settings)  # откроет вкладку Версия
        layout.addWidget(self._update_banner)

        # ── Баннер входящего шёпота (скрыт, показывается при получении шёпота) ─
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

        # ── Кнопка чата: полноширинная стеклянная кнопка ─────────────────────
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
        layout.addWidget(self._btn_chat_main)

        # ── Нижняя панель кнопок управления ─────────────────────────────────
        # Отдельный QFrame с собственным фоном — визуальная иерархия:
        # область чата (дерево) vs панель управления (кнопки), как в Discord.
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

        # ── Кнопка лобби: открыть экран выбора сервера ────────────────────────
        # Пользователь остаётся подключённым к текущему серверу пока явно
        # не выберет другой. Кнопка просто открывает MultiServerScreen поверх
        # главного окна, не обрывая соединение.
        self.btn_lobby = QPushButton()
        self.btn_lobby.setFixedSize(46, 46)
        self.btn_lobby.setObjectName("barBtn")
        self.btn_lobby.setIcon(QIcon(resource_path("assets/icon/lobby.svg")))
        self.btn_lobby.setIconSize(QSize(26, 26))
        self.btn_lobby.setToolTip("Лобби — выбрать сервер")
        self.btn_lobby.clicked.connect(self._open_lobby)

        # --- Индикатор качества соединения стримера ---
        # Маленький QLabel с иконкой connection_bad.svg, появляется рядом
        # с кнопкой трансляции когда сервер понизил битрейт из-за плохого
        # upload-канала. Аналог индикатора «слабое соединение» в Discord.
        # Состояния:
        #   скрыт           — нет трансляции или битрейт ≥ 4 Mbps (норма)
        #   🟡 tooltip      — битрейт 1.5–4 Mbps (умеренная деградация)
        #   🔴 tooltip      — битрейт < 1.5 Mbps (сильная деградация)
        self._stream_conn_lbl = QLabel()
        self._stream_conn_lbl.setFixedSize(22, 22)
        self._stream_conn_lbl.setScaledContents(True)
        self._stream_conn_lbl.setVisible(False)
        self._stream_conn_lbl.setPixmap(
            QIcon(resource_path("assets/icon/connection_bad.svg")).pixmap(QSize(22, 22))
        )

        # ── Пинг (компактный текст, не кнопка) ────────────────────────────────
        self._latency_lbl = QLabel("--")
        self._latency_lbl.setFixedSize(46, 20)
        self._latency_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._latency_lbl.setStyleSheet(
            "font-size: 10px; font-weight: bold; color: #8899aa;"
            "background: transparent; border: none;"
        )

        btn_set = QPushButton()
        btn_set.setFixedSize(46, 46)
        btn_set.setObjectName("barBtn")
        btn_set.setIcon(QIcon(resource_path("assets/icon/settings.svg")))
        btn_set.setIconSize(QSize(26, 26))
        btn_set.clicked.connect(self.open_settings)

        # Пинг + настройки в вертикальном мини-стеке справа
        _right_col = QVBoxLayout()
        _right_col.setContentsMargins(0, 0, 0, 0)
        _right_col.setSpacing(0)
        _right_col.addWidget(self._latency_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        _right_col.addWidget(btn_set, alignment=Qt.AlignmentFlag.AlignCenter)

        btns.addWidget(self.btn_mute)
        btns.addWidget(self.btn_deafen)
        btns.addWidget(self.btn_sb)
        btns.addWidget(self.btn_stream)
        btns.addWidget(self._stream_conn_lbl)
        btns.addWidget(self.btn_lobby)
        btns.addStretch()
        btns.addLayout(_right_col)

        layout.addWidget(self._bottom_bar)

        # ── ChatPanel + горизонтальный контейнер (Discord-стиль) ─────────────
        # ChatPanel скрыта по умолчанию — при hidden QHBoxLayout не выделяет
        # ей место, окно остаётся компактным.
        # При toggle: setVisible(True/False) + window resize(+/- PANEL_WIDTH).
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

    # ── Встроенный сервер: стать хостом ──────────────────────────────────────

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
            from server_discovery import get_local_radmin_ip
            host_ip     = get_local_radmin_ip()
            server_name = load_server_name()
            EmbeddedServerManager.get().start(host_ip, self.nick, server_name=server_name)
        except Exception as e:
            print(f"[UI] _on_become_host error: {e}")
            self._lost_status_lbl.setText(f"Ошибка запуска сервера:\n{e}\n\nПопробуйте перезапустить.")
            self._btn_reconnect.setEnabled(True)
            return

        def _reconnect_to_self():
            # FIX (Bug F): сбрасываем _migration_pending (установлен в process_message
            # вместе с running=False, чтобы tcp_listen не вызвал _on_connection_lost).
            # Сбрасываем здесь — после того как сервер стартовал и мы готовы подключаться.
            self.net._migration_pending = False
            # Сбрасываем _reconnecting — fast_switch_to имеет guard "if _reconnecting: return"
            self.net._reconnecting = False
            self.net.fast_switch_to(host_ip)

        # Было 700 мс + _reconnect_loop (3с первый sleep).
        # 150 мс: достаточно для bind/listen локальных сокетов.
        # fast_switch_to сам ретраится 8 × 0.35с если порт ещё не готов.
        QTimer.singleShot(150, _reconnect_to_self)

    def _on_server_migrating(self, new_host_ip: str):
        """
        Вызывается когда сервер переезжает к другому хосту (мы — не новый хост).
        Показываем индикатор. Сетевой движок сам переподключится через _migrate_reconnect().

        ИСПРАВЛЕНИЕ — призрак сервера в лобби:
        Если мы сами были хостом и только что передали сервер (CMD_SERVER_TRANSFER),
        то CMD_SERVER_MIGRATE прилетает и нам тоже (broadcast всем).
        Без явной остановки EmbeddedServerManager наш ServerAnnouncer продолжал
        рассылать broadcast → в лобби висел старый сервер с 0 участников.
        Теперь: если мы хост и видим миграцию к кому-то другому — останавливаемся.
        """
        print(f"[UI] _on_server_migrating: новый хост {new_host_ip}")

        # FIX (Bug E): stop_silent вместо stop_announcer_only.
        # stop_announcer_only останавливал только UDP-broadcast, но оставлял
        # TCP/UDP listening-сокеты связанными:
        #   - Сервер продолжал принимать соединения (видно в списке)
        #   - mgr._server != None → become_host → новый SFUServer → bind(5000)
        #     → "адрес уже используется" (Bug #3)
        # stop_silent закрывает ВСЕ сокеты и устанавливает mgr._server=None.
        # Не вызывает _broadcast_server_migrate — миграция уже разослана сервером.
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                print("[UI] _on_server_migrating: stop_silent (порты освобождаем)")
                mgr.stop_silent()
        except Exception as e:
            print(f"[UI] _on_server_migrating stop error: {e}")

        self._lost_title_lbl.setText("Смена хоста")
        self._lost_status_lbl.setText(
            f"Сервер переезжает...\nНовый хост: {new_host_ip}\n"
            "Переподключение через несколько секунд."
        )
        self._btn_reconnect.setEnabled(False)
        self._stack.setCurrentIndex(1)

    def setWindowTitle(self, title: str):
        """Переопределяем — синхронно обновляем кастомный title bar."""
        super().setWindowTitle(title)
        if hasattr(self, '_title_bar'):
            self._title_bar.set_title(title)

    # ── Edge-resize для безрамочного окна ────────────────────────────────────
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

            # Нижняя граница
            if d in ("bottom", "bottom-left", "bottom-right"):
                new_bottom = orig.bottom() + delta.y()
                g.setBottom(max(new_bottom, orig.top() + min_h))

            # Правая граница
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
        """
        Приложение-уровневый фильтр: перехватывает MouseMove у ЛЮБОГО дочернего
        виджета и пересчитывает курсор относительно границ главного окна.
        """
        if (event.type() == QEvent.Type.MouseMove
                and not self._resize_direction
                and not self.isMaximized()):
            # Глобальные координаты → локальные координаты MainWindow
            pos = self.mapFromGlobal(QCursor.pos())
            edge = self._edge_at(pos)
            if edge:
                self.setCursor(self._EDGE_CURSORS[edge])
            else:
                self.unsetCursor()
        return False   # никогда не поглощаем событие

    def mouseReleaseEvent(self, e):
        self._resize_direction = None
        self._resize_start_pos = None
        self._resize_start_geom = None
        # Сбрасываем курсор ресайза обратно в стандартный.
        # Без этого курсор «застревал» в форме SizeXxx после отпускания кнопки мыши,
        # потому что mouseMoveEvent с зажатой кнопкой обновлял курсор только во время
        # перетаскивания, а setCursor() остаётся в силе пока явно не вызван unsetCursor().
        self.unsetCursor()
        super().mouseReleaseEvent(e)

    def moveEvent(self, e):
        """При перемещении окна синхронно двигаем все активные пузыри чата."""
        super().moveEvent(e)
        if hasattr(self, '_quick_bubbles') and self._quick_bubbles:
            self._reposition_quick_bubbles()

    def resizeEvent(self, e):
        """При изменении размера окна пересчитываем позиции пузырей."""
        super().resizeEvent(e)
        if hasattr(self, '_quick_bubbles') and self._quick_bubbles:
            self._reposition_quick_bubbles()

    def apply_theme(self, theme_name):
        font_f = self.custom_font_family
        # FIX #7: светлая тема удалена — единственная тема «Темная» (glassmorphism dark).
        # Параметр theme_name сохранён для обратной совместимости вызовов,
        # но значение игнорируется — используется всегда тёмная палитра.

        # ── Glassmorphism dark palette ────────────────────────────────────────
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

        # ── Кэш цветов для refresh_ui: пересоздаём при смене темы ────────────
        # FIX #7: только тёмная палитра — is_dark всегда True.
        self._cache_theme = "Темная"
        self._theme_dirty = True   # сигнал refresh_ui: обновить _c_def / _br_def
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
        """
        Регистрирует все глобальные горячие клавиши:
          — mute/deafen (toggle)
          — PTT-шёпот для каждого из 5 слотов (press → start, release → stop)

        Почему НЕ используем trigger_on_release=True:
          keyboard.add_hotkey(hk, cb, trigger_on_release=True) — это НЕ "при отпускании клавиши".
          Это "повторить срабатывание хоткея когда комбо отпущено как единица".
          На практике: либо не срабатывает вовсе, либо срабатывает непредсказуемо.
          В итоге whisper_target_uid остаётся != 0 → голос навсегда застрял в шёпоте.

        Правильный PTT:
          1. keyboard.add_hotkey(hk, _press) — срабатывает при физическом нажатии комбо.
          2. keyboard.hook(_raw_key_up)      — глобальный перехват всех key-up событий.
             Как только физически отпущена триггер-клавиша (последняя в комбо) —
             сразу вызываем stop_whisper(). Это работает мгновенно и надёжно.

        keyboard.unhook_all() в начале снимает оба типа хуков (add_hotkey + hook).
        suppress=False — клавиши проходят в игру/браузер без блокировки.
        """
        try:
            keyboard.unhook_all()

            # ── Базовые хоткеи ────────────────────────────────────────────────
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

            # Ctrl+T — открыть/закрыть чат
            try:
                keyboard.add_hotkey("ctrl+t", lambda: self._toggle_chat_panel())
            except Exception as e:
                print(f"[HK] chat hotkey error: {e}")

            # ── PTT-хоткеи шёпота (слоты 0–4) ────────────────────────────────
            for i in range(5):
                ip   = self.app_settings.value(f"whisper_slot_{i}_ip",   "")
                nick = self.app_settings.value(f"whisper_slot_{i}_nick", "")
                hk   = self.app_settings.value(f"whisper_slot_{i}_hk",   "")
                if (not ip and not nick) or not hk:
                    continue

                def _make_ptt(target_ip: str, target_nick: str, hotkey_str: str):
                    active = [False]

                    # Триггер-клавиша = последняя в комбо: "alt+1" → "1", "f8" → "f8"
                    # Именно её key-up означает "пользователь отпустил PTT".
                    trigger_key = hotkey_str.replace(" ", "").split("+")[-1].lower()

                    def _press():
                        if active[0]:
                            return  # автоповтор ОС — игнорируем
                        uid = None
                        # ── Приоритет 1: поиск по IP (работает при любом нике) ──
                        if target_ip:
                            with self.audio.users_lock:
                                for u_uid, u_ip in self.audio.uid_to_ip.items():
                                    if u_ip == target_ip:
                                        uid = u_uid
                                        break
                        # ── Приоритет 2: фолбэк по нику (для старых сохранений) ─
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
                        """
                        Глобальный перехват key-up.
                        Срабатывает при отпускании ЛЮБОЙ клавиши — но мы проверяем
                        только нашу триггер-клавишу и только если PTT активен.
                        Это гарантирует что stop_whisper() всегда вызовется,
                        даже если система не доставила "hotkey release" событие.
                        """
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
                    # Только press через add_hotkey (обрабатывает модификаторы корректно)
                    keyboard.add_hotkey(hk, _press, trigger_on_release=False, suppress=False)
                    # Release через raw hook — надёжный физический key-up
                    keyboard.hook(_raw_key_up, suppress=False)
                    print(f"[HK] Whisper slot {i}: ip='{ip}' nick='{nick}' → '{hk}' (trigger_key='{hk.replace(' ','').split('+')[-1].lower()}')")
                except Exception as e:
                    print(f"[HK] Whisper slot {i} error ({hk!r}): {e}")

            # ── Хоткеи кастомных звуков soundboard ───────────────────────────
            # Читаем hk_table_* и для каждой записи с ftype=="sound" регистрируем
            # hotkey, который ищет путь к файлу по имени и отправляет его через сеть.
            hk_count = int(self.app_settings.value("hk_table_count", 0))
            for i in range(hk_count):
                ftype = self.app_settings.value(f"hk_table_{i}_type", "none")
                if ftype != "sound":
                    continue
                fdata = self.app_settings.value(f"hk_table_{i}_data", "")  # имя звука
                hk    = self.app_settings.value(f"hk_table_{i}_key",  "")
                if not fdata or not hk:
                    continue

                # Ищем путь к файлу по имени среди сохранённых кастомных слотов
                sound_path = ""
                for j in range(10):  # >= CUSTOM_SOUND_SLOTS, с запасом
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

    def play_notification(self, stype="self_move"):
        # Квадратичная кривая: vol_linear = (slider/100)^2
        # При slider=30 (default) → 0.09x  (≈ −21 dB, ненавязчиво)
        # При slider=70           → 0.49x  (вдвое тише прежних 0.70)
        # При slider=100          → 1.00x  (максимум)
        raw = int(self.app_settings.value("system_sound_volume", 30)) / 100.0
        vol = raw ** 2  # перцептивно равномерная шкала вместо линейной
        entry = self._loaded_sounds.get(stype)
        if entry is not None:
            try:
                data, sr = entry
                # FIX: sd.play() открывает новый PortAudio-поток → WASAPI
                # перебалансирует буферы → основной callback теряет CPU →
                # треск/провал у всех слушателей. Используем внутренний микшер.
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
        """
        Вызывается на КАЖДЫЙ входящий пакет шёпота (audio_engine эмитит
        whisper_received на каждый пакет, ~50/сек).

        Логика разделена на два уровня:
          1. ВСЕГДА: перезапускаем _whisper_end_timer (1500 мс).
             Пока идут пакеты — таймер никогда не истечёт → оверлей горит всегда.
          2. ТОЛЬКО ПРИ СМЕНЕ ОТПРАВИТЕЛЯ или когда оверлей ещё не показан:
             обновляем ник и вызываем show_for(). Это исключает 50 вызовов
             show()/setText() в секунду, которые вызывали бы мерцание анимации.
        """
        # ── 1. Всегда: сбрасываем таймер завершения ──────────────────────────
        self._whisper_end_timer.stop()
        self._whisper_end_timer.start()

        # ── 2. При смене отправителя или первом появлении: обновляем UI ──────
        if sender_uid == getattr(self, '_current_whisper_uid', None) \
                and self._whisper_banner.isVisible():
            # Тот же шептун, оверлей уже виден — только таймер сброшен, больше ничего.
            return

        self._current_whisper_uid = sender_uid

        # Ищем ник шептуна среди активных пользователей
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

        # ── Баннер в главном окне ─────────────────────────────────────────────
        self._whisper_banner.setText(f"🤫  {nick} шепчет вам...")
        self._whisper_banner.setVisible(True)

        # ── Системный оверлей поверх всех окон ───────────────────────────────
        self._whisper_overlay.show_for(nick)

    def _on_whisper_ended(self):
        """Шёпот завершился (1500 мс без пакетов) — скрываем баннер и системный оверлей."""
        self._current_whisper_uid = None
        self._whisper_banner.setVisible(False)
        self._whisper_overlay.hide_overlay()

    # ── Быстрый чат ────────────────────────────────────────────────────────────

    def _send_quick_msg(self):
        """Быстрый чат отключён — строка ввода убрана. Метод-заглушка."""
        pass

    # ── Постоянный чат (ChatPanel) ─────────────────────────────────────────────

    def _toggle_chat_panel(self, checked: bool = None) -> None:
        """
        Открыть/закрыть ChatPanel. Окно расширяется/сжимается на PANEL_WIDTH.
        checked: True = открыть, False = закрыть, None = переключить.
        """
        if checked is None:
            checked = not self._chat_panel.isVisible()

        panel_w = ChatPanel.PANEL_WIDTH
        if checked:
            if self._chat_panel.isVisible():
                return  # уже открыт
            self._chat_panel.update_room_label(self.current_room)
            self._chat_panel.setVisible(True)
            if not self.isMaximized():
                self.resize(self.width() + panel_w, self.height())
            self._chat_panel.focus_input()
        else:
            if not self._chat_panel.isVisible():
                return  # уже закрыт
            self._chat_panel.setVisible(False)
            if not self.isMaximized():
                self.resize(max(400, self.width() - panel_w), self.height())

        self._btn_chat_main.blockSignals(True)
        self._btn_chat_main.setChecked(checked)
        self._btn_chat_main.blockSignals(False)

        # Сбрасываем индикатор "новое сообщение" при открытии
        if checked and self._chat_has_unread:
            self._chat_has_unread = False
            self._btn_chat_main.setText("💬  Чат")
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

    def _on_typing_received(self, uid: int, nick: str) -> None:
        """Typing indicator: другой пользователь печатает в чате."""
        if uid != self.audio.my_uid:
            self._chat_panel.show_typing(uid, nick)

    def _on_chat_panel_send(self, text: str) -> None:
        """Пользователь отправил сообщение из ChatPanel."""
        self.net.send_chat_msg(text)
        self.play_notification("chat_msg_out")

    def _on_chat_msg_received(self, entry: dict) -> None:
        """Входящее сообщение чата — добавляем в панель."""
        self._chat_panel.add_message(entry)
        is_own = (entry.get('uid', 0) == self.audio.my_uid)
        if is_own:
            self.play_notification("chat_msg_out")
        else:
            self.play_notification("chat_msg_in")
            # Показываем индикатор если чат закрыт
            if not self._chat_panel.isVisible():
                self._chat_has_unread = True
                self._btn_chat_main.setText("💬  Чат                    новое сообщение")
                self._btn_chat_main.setStyleSheet("""
                    QPushButton#btnChatMain {
                        background-color: rgba(255, 255, 255, 0.04);
                        border: 1px solid rgba(231, 76, 60, 0.35);
                        border-radius: 8px;
                        color: #e88;
                        font-size: 13px;
                        font-weight: 600;
                        margin: 4px 0px 2px 0px;
                    }
                    QPushButton#btnChatMain:hover {
                        background-color: rgba(231, 76, 60, 0.08);
                        border-color: rgba(231, 76, 60, 0.50);
                        color: #ff9999;
                    }
                """)

    def _on_chat_history_received(self, messages: list) -> None:
        """История чата получена (при подключении) — загружаем в панель."""
        if messages:
            self._chat_panel.load_history(messages)

    def _on_chat_media_requested(self) -> None:
        """ChatPanel выбрала файл — берём данные и отправляем через network."""
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
        """Входящее медиа-вложение — добавляем в ChatPanel."""
        self._chat_panel.add_message(entry)
        if entry.get('uid', 0) != self.audio.my_uid:
            self.play_notification("chat_msg_in")
            if not self._chat_panel.isVisible() and not self._chat_has_unread:
                self._chat_has_unread = True
                self._btn_chat_main.setText("💬  Чат                    новое сообщение")
                self._btn_chat_main.setStyleSheet("""
                    QPushButton#btnChatMain {
                        background-color: rgba(255, 255, 255, 0.04);
                        border: 1px solid rgba(231, 76, 60, 0.35);
                        border-radius: 8px; color: #e88;
                        font-size: 13px; font-weight: 600;
                        margin: 4px 0px 2px 0px;
                    }
                    QPushButton#btnChatMain:hover {
                        background-color: rgba(231, 76, 60, 0.08);
                        border-color: rgba(231, 76, 60, 0.50);
                        color: #ff9999;
                    }
                """)

    def _on_quick_msg_received(self, sender_uid: int, from_nick: str, text: str):
        """
        Входящее быстрое сообщение.

        Создаёт (или обновляет) QuickMsgBubble — frameless tool-окно поверх
        всего приложения. Пузырь позиционируется слева от аватарки отправителя
        по глобальным координатам экрана.

        Повторное сообщение от того же uid обновляет текст без пересоздания.
        Автоскрытие — 5 секунд.
        """
        # ── Звук уведомления ─────────────────────────────────────────────────
        self.play_notification("quick_msg")

        # ── Глобальные координаты строки пользователя в дереве ───────────────
        global_tl = None
        item_h    = 44   # высота строки по умолчанию
        data = self.known_uids.get(sender_uid)
        if data:
            try:
                rect = self.tree.visualItemRect(data['item'])
                item_h = max(rect.height(), 1)
                global_tl = self.tree.viewport().mapToGlobal(rect.topLeft())
            except RuntimeError:
                pass

        # ── Переиспользуем существующий пузырь ───────────────────────────────
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

        # ── Создаём новый пузырь ──────────────────────────────────────────────
        bubble = QuickMsgBubble()   # top-level frameless tool window
        bubble.update(text)

        if global_tl:
            bubble.place_left_of(global_tl, item_h)
        else:
            # Отправитель не виден в дереве — по центру над нижней панелью
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
        """Скрыть и удалить пузырь быстрого сообщения для данного uid."""
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
        """
        Репозиционировать все активные пузыри после пересборки дерева.
        Вызывается из update_user_tree() после expandAll().
        Использует глобальные экранные координаты — работает корректно
        даже если пузырь выходит за границы главного окна.
        """
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
        """
        Вызывается немедленно когда ползунок громкости пользователя
        выставляется в 0 или уходит от 0.

        Логика иконки:
          is_zero=True  → ban-иконка (тот же визуал что и у кнопки «Заглушить»)
          is_zero=False → убираем ban-иконку, НО только если кнопка «Заглушить»
                          тоже не нажата — чтобы не конфликтовать с ней.

        Цвет ника:
          is_zero=True  → красный (_br_mute), как у заглушённых.
          is_zero=False → стандартный (_br_def), если нет других причин краснеть.

        Важно: refresh_ui() тоже проверяет volume_zero каждые 100 мс.
        Этот слот нужен для мгновенного отклика (без задержки 0..100 мс),
        который пользователь заметит при быстром движении ползунком.
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

            # ── Иконка (колонка 4) ──────────────────────────────────────────────
            if is_zero or is_muted_btn:
                # Выставлен в 0 ИЛИ нажата кнопка «Заглушить» → ban
                item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_ban)
            elif is_m_remote:
                # Сам заглушил себя на сервере → mic_off
                item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_mic_off)
            else:
                item.setData(4, Qt.ItemDataRole.DecorationRole, None)

            # ── Цвет ника (колонка 0) ───────────────────────────────────────────
            if is_zero or is_muted_btn:
                item.setForeground(0, self._br_mute)
            else:
                item.setForeground(0, self._br_def)

        except RuntimeError:
            # item уже удалён Qt (дерево пересоздалось) — просто игнорируем
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
        """
        Центральный диспетчер входящих мазков (CMD_DRAW_STROKE от сервера).

        Сервер ретранслирует мазок:
          — всем зрителям стрима  (включая рисовавшего — для эха)
          — стримеру              (он видит у себя на экране)

        Здесь мы определяем кому адресован мазок и направляем его:
          • Мы зритель и у нас открыт VideoWindow → add_remote_stroke()
          • Мы стример и overlay создан → add_stroke()

        streamer_uid в пакете не передаётся напрямую сюда (его фильтрует сервер
        и шлёт нам только то что относится к нашему стриму/просмотру),
        но для определения нужного VideoWindow мы ищем по UID стримера
        (который является ключом stream_windows).
        """
        # ── Случай 1: мы зритель — ищем VideoWindow по streamer uid ─────────
        # stream_windows: {streamer_uid: VideoWindow}
        # Мазок может прийти пока нет открытых окон (race при закрытии) — guard.
        for streamer_uid, win in list(self.stream_windows.items()):
            try:
                if win is not None and not win._closing:
                    win.add_remote_stroke(color, points, width)
            except (RuntimeError, AttributeError):
                pass

        # ── Случай 2: мы стример — показываем на прозрачном оверлее ─────────
        # _streamer_draw_overlay существует только пока is_streaming=True.
        if self._streamer_draw_overlay is not None:
            try:
                self._streamer_draw_overlay.add_stroke(nick, color, points, width)
            except (RuntimeError, AttributeError):
                pass

    def _on_force_muted(self):
        """
        Хост выключил наш микрофон (CMD_FORCE_MUTED от сервера).
        Только mic off — уши не трогаем, динамики остаются включены.
        Кнопки НЕ блокируются — участник может нажать mic и включить
        микрофон обратно в любой момент.
        """
        if not self.audio.is_muted:
            self.btn_mute.setChecked(True)
            self.toggle_mute()
        # Тост над нижней панелью
        self._sb_toast.setText("🎤  Хост выключил ваш микрофон")
        self._sb_toast.adjustSize()
        tw = self._sb_toast.width()
        tx = (self.width() - tw) // 2
        ty = self._bottom_bar.y() - self._sb_toast.height() - 28
        self._sb_toast.move(tx, max(4, ty))
        self._sb_toast.raise_()
        self._sb_toast.setVisible(True)
        self._sb_toast_timer.start()

    def on_connected(self, msg):
        try:
            self.audio.my_uid = msg['uid']
            self.audio.start(
                self.app_settings.value("device_in_name"),
                self.app_settings.value("device_out_name")
            )

            # Обновляем UID в ChatPanel (был 0 до первого подключения)
            self._chat_panel.set_my_uid(self.audio.my_uid)
            self._chat_panel.update_room_label(self.current_room)

            self.play_notification("self_move")

            # Сброс состояния «подключение друга» при каждом (пере)подключении.
            # При первом sync_users после входа просто засеваем prev_all_uids —
            # без звука, чтобы не «приветствовать» уже находящихся на сервере.
            self.prev_all_uids = set()
            self._server_users_initialized = False

            self._stack.setCurrentIndex(0)

            self._btn_reconnect.setEnabled(True)

            # Восстанавливаем сохранённый статус после переподключения.
            # Сервер не хранит статусы постоянно — только в рамках сессии,
            # поэтому при каждом (пере)подключении отправляем сохранённый статус.
            if self._my_status_icon:
                self.net.send_presence_update(self._my_status_icon, self._my_status_text)

        except Exception as e:
            import traceback
            print(f"[on_connected] EXCEPTION:\n{traceback.format_exc()}", flush=True)

    def on_video_frame(self, uid, q_image):
        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_frame(q_image)

    def on_stream_stats_updated(self, uid: int, fps: int, loss_pct: int):
        """
        Принимает статистику качества потока от VideoEngine (каждые 2 сек).
        Пробрасывает в соответствующий VideoWindow для обновления HUD.
        """
        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_stream_stats(fps, loss_pct)

    def update_user_tree(self, users_map):
        user_rooms: dict = {}
        all_active_uids = set() # Исправление 2.1: собираем всех активных пользователей

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
            for u in users_map.get(self.current_room, [])  # ИСПРАВЛЕНИЕ: только текущий канал
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

        # ИСПРАВЛЕНИЕ 2.1: Очищаем движки от мусора и отключившихся
        self.audio.cleanup_users(all_active_uids)
        self.video.cleanup_users(all_active_uids)

        self.prev_room_uids = current_room_uids
        self.prev_streaming_uids = current_streaming_uids

        # ── Звук подключения друга к серверу ──────────────────────────────────
        all_server_uids = all_active_uids - {self.audio.my_uid}
        if not self._server_users_initialized:
            self.prev_all_uids = all_server_uids
            self._server_users_initialized = True
        else:
            new_arrivals = all_server_uids - self.prev_all_uids
            if new_arrivals:
                self.play_notification("friend_connect")
            self.prev_all_uids = all_server_uids

        # FIX #18: пропускаем полную перестройку дерева если состав и
        # размещение пользователей не изменились.
        # Раньше tree.clear() + полный rebuild выполнялись при КАЖДОМ sync_users
        # (update_status → отправляется ~1 раз/сек с каждого клиента при неизменном
        # mute/deaf). При 20 юзерах = до 20 full rebuild/сек на UI-потоке.
        # Сигнатура: tuple отсортированных (room, uid, nick, mute, deaf, is_streaming,
        # avatar, status_icon, status_text) + СПИСОК КАНАЛОВ.
        #
        # FIX CHANNELS: default_rooms включён в подпись.
        # Проблема: при создании/удалении пустого временного канала users_map
        # не меняется (в канале нет пользователей) → подпись была идентична →
        # ранний return → дерево не перерисовывалось → канал не появлялся/исчезал
        # пока не происходило любое другое событие (mute, подключение юзера и т.п.).
        # Решение: добавляем tuple(self.default_rooms) в подпись — как только
        # _on_channel_list_updated или _on_channel_created/deleted изменят список
        # каналов, следующий вызов refresh_ui немедленно перестроит дерево.
        _new_sig = (
            tuple(self.default_rooms),   # ← изменение списка каналов = rebuild
            tuple(
                (r, u['uid'], u['nick'], u.get('mute'), u.get('deaf'),
                 u.get('is_streaming'), u.get('avatar'), u.get('status_icon'),
                 u.get('status_text'),
                 # FIX #3: watchers в подписи → дерево перерисовывается когда
                 # зритель подключается/отключается от стрима налету.
                 tuple(w.get('nick', '') for w in u.get('watchers', [])))
                for r, u_list in sorted(users_map.items())
                for u in sorted(u_list, key=lambda x: x['uid'])
            ),
        )
        if hasattr(self, '_users_map_sig') and self._users_map_sig == _new_sig:
            return  # ничего не изменилось — не трогаем дерево
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

                # ── Колонка 1: статус дела (иконка SVG из assets/status/) ──────
                # Показывается только если пользователь выставил статус.
                # Tooltip показывает text-подпись при наведении мыши.
                status_icon = u.get('status_icon', '')
                status_text = u.get('status_text', '')
                if status_icon:
                    # Ленивое создание пиксмапа с кэшированием
                    if status_icon not in self._status_px_cache:
                        # FIX MEM: ограничиваем кэш — 200 иконок достаточно для
                        # любого реального набора статусов. Сброс при переполнении
                        # прост и надёжен (статусы — маленький набор SVG-файлов).
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

        # Репозиционируем активные пузыри быстрого чата
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

            # FIX #16: кэшируем «зону» пинга и пересоздаём stylesheet только при
            # переходе между зонами (зелёная < 60 < жёлтая < 150 < красная).
            # setStyleSheet() и setToolTip() с f-строками каждые 100 мс — лишняя
            # работа Qt-стека (CSS парсинг + repaint) даже когда ничего не изменилось.
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
                self._latency_lbl.setStyleSheet(
                    f"font-size: 10px; font-weight: bold; color: {col};"
                    f"background: transparent; border: none;"
                )
            self._latency_lbl.setText(f"{ping} мс")

            # FIX: perf_counter() — синхронизируем с last_packet_time и last_voice_time
            # в audio_engine (тоже переведены на perf_counter). time.time() и
            # perf_counter() — разные часы, их нельзя вычитать друг из друга.
            # Без этой правки (now - last_packet_time) давало ~50_000_000 сек → никогда < 0.3.
            now = time.perf_counter()

            # ВАЖН-6: тема меняется только через apply_theme() → флаг _theme_dirty.
            # Избегаем QSettings.value() (обращение к реестру) 10 раз/сек.
            # FIX #7: только тёмная тема — цвета фиксированные.
            if self._theme_dirty:
                self._theme_dirty = False
                self._c_def  = QColor("#ecf0f1")
                self._br_def = QBrush(self._c_def)

            c_talk   = self._c_talk
            c_mute   = self._c_mute
            c_stream = self._c_stream
            c_def    = self._c_def
            icon_size = self._icon_size

            # ВАЖН-5: Быстрый снимок под локом — только примитивы, без Qt-вызовов.
            # users_lock не удерживается во время setData/setForeground: Qt может
            # вызвать перерисовку внутри этих методов и заблокировать _packet_processor_loop.
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

            # Снимок host_uid вне лока — getattr безопасен без блокировки.
            # Кэшируем здесь раз на итерацию (не вызываем getattr N раз в цикле).
            host_uid = getattr(self.net, '_server_host_uid', 0)

            # Обновляем Qt-дерево БЕЗ лока
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

                if talk:
                    item.setForeground(0, self._br_talk)
                elif curr_s:
                    item.setForeground(0, self._br_stream)
                elif curr_d or is_m or (uid != my_uid and u_vals and (is_locally_muted or is_vol_zero)):
                    item.setForeground(0, self._br_mute)
                else:
                    # Хост выделяется золотым даже в «дефолтном» состоянии.
                    # Проверка выполняется O(1) — host_uid снят до цикла.
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
            # Проверяем: защищён ли канал паролем?
            ch_info = next(
                (ch for ch in self._channel_list if ch['name'] == room_name),
                None
            )
            if ch_info and ch_info.get('has_password', False):
                self._on_join_room_denied(room_name, 'channel_auth_required')
            else:
                self.net.send_json({"action": CMD_JOIN_ROOM, "room": room_name})

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)

        # ── ПКМ по пустому месту или заголовку канала — создать/переименовать ─
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

                # FIX #4: если клик по конкретному заголовку постоянного канала
                # — показываем пункт переименования
                act_rename = None
                clicked_room = None
                if item and item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
                    clicked_room = item.data(1, Qt.ItemDataRole.UserRole)
                    # Проверяем, является ли канал постоянным
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

        # ── Правый клик по СЕБЕ → оверлей выбора статуса ─────────────────────
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

        # Получаем текущую громкость пользователя
        current_vol = 1.0
        with self.audio.users_lock:
            u = self.audio.remote_users.get(uid)
            if u is not None:
                current_vol = u.volume
            else:
                ip = self.audio.uid_to_ip.get(uid, '')
                if ip:
                    current_vol = float(self.audio.settings.value(f"vol_ip_{ip}", 1.0))

        # Позиция оверлея: прямо под ником в дереве
        item_rect = self.tree.visualItemRect(item)
        global_pos = self.tree.viewport().mapToGlobal(item_rect.bottomLeft())

        # Флаг стрима + колбэк «смотреть» — передаём в панель
        user_data = self.known_uids.get(uid)
        is_streaming = user_data.get('is_s', False) if user_data else False

        watch_cb = None
        if is_streaming:
            # Захватываем uid/item в замыкание без ref на loop-переменные
            _uid = uid
            _nick_txt = item.text(0)
            watch_cb = lambda: self.open_video_window(_uid, _nick_txt)

        UserOverlayPanel(
            nick, current_vol, uid, self.audio, global_pos,
            parent=self,
            is_streaming=is_streaming,
            on_watch_stream=watch_cb,
            net=self.net,
            # Передача сервера: callback виден только хосту (первый в host_order)
            on_transfer_server=(
                (lambda _uid=uid: self._on_request_server_transfer(_uid))
                if self._is_server_host() and uid != self.audio.my_uid
                else None
            ),
            # Выключить микрофон: только хост, участник может включить сам
            on_host_mute=(
                (lambda _uid=uid: self.net.send_host_mute(_uid))
                if self._is_server_host() and uid != self.audio.my_uid
                else None
            ),
        ).show()

    def _is_server_host(self) -> bool:
        """
        Возвращает True если текущий пользователь является хозяином сервера.
        Используется для отображения кнопки «Передать сервер» в контекстном меню.
        """
        my_uid = getattr(self.audio, 'my_uid', 0)
        server_uid = getattr(self.net, '_server_host_uid', 0)
        return bool(my_uid and my_uid == server_uid)

    def _on_request_server_transfer(self, target_uid: int):
        """
        Запрашивает подтверждение и отправляет CMD_SERVER_TRANSFER на сервер.
        Вызывается из UserOverlayPanel при клике «Передать сервер».
        """
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

    def open_video_window(self, uid, nick):
        if uid not in self.stream_windows or not self.stream_windows[uid].isVisible():
            w = VideoWindow(nick)
            w.uid = uid
            # Передаём NetworkClient для SoundboardPanel в оверлее стрима
            w.set_net(self.net)
            w.window_closed.connect(self._on_stream_window_closed)

            # --- Оверлей: подключаем кнопки управления ---
            # Микрофон и динамики переключают состояние аудио-движка
            w.overlay_mute_toggled.connect(lambda: self.btn_mute.click())
            w.overlay_deafen_toggled.connect(lambda: self.btn_deafen.click())
            # «Прекратить просмотр» — окно само закрывается, нам остаётся отправить stop
            w.overlay_stop_watch.connect(lambda _uid=uid: self._on_stream_window_closed(_uid))

            # Синхронизировать иконки оверлея при каждом изменении статуса аудио.
            # ВАЖНО: используем прямое подключение (не lambda), чтобы можно было
            # вызвать disconnect() по имени метода при закрытии окна.
            # Lambda-соединения накапливались в audio.status_changed и никогда
            # не отключались → каждое открытие окна оставляло мёртвую лямбду
            # с живой ссылкой на VideoWindow в памяти.
            self.audio.status_changed.connect(w.sync_audio_state)
            # Установить актуальное состояние прямо сейчас
            w.sync_audio_state(self.audio.is_muted, self.audio.is_deafened)

            # Громкость стрима: подключаем к AudioHandler.set_stream_volume().
            # AudioHandler._stream_vol управляет усилением в audio_callback (0.0–2.0).
            # При deafen auto-ducking (0.4×) применяется поверх этого коэффициента.
            w.overlay_stream_volume_changed.connect(self.audio.set_stream_volume)
            # Синхронизируем ползунок попапа с текущим значением движка,
            # чтобы при повторном открытии окна ползунок не сбрасывался в 1.0.
            w.overlay.set_stream_volume_value(self.audio._stream_vol)

            # ── Рисование: зритель закончил мазок → отправляем серверу ─────
            # draw_stroke_ready(color, norm_points, width) испускается DrawCanvas
            # при отпускании кнопки мыши. nick берём из self.nick (локальный).
            # Сервер ретранслирует мазок всем зрителям + стримеру.
            w.draw_stroke_ready.connect(
                lambda color, pts, width, _uid=uid:
                    self.net.send_draw_stroke(_uid, self.nick, color, pts, width)
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

    def open_settings(self):
        # Запоминаем текущие устройства ДО открытия диалога.
        _dev_in_before  = self.app_settings.value("device_in_name",  "")
        _dev_out_before = self.app_settings.value("device_out_name", "")

        if SettingsDialog(self.audio, self).exec():
            self.setup_hotkeys()

            _dev_in_after  = self.app_settings.value("device_in_name",  "")
            _dev_out_after = self.app_settings.value("device_out_name", "")

            # Перезапускаем аудиопоток ТОЛЬКО если устройство реально сменилось.
            # При изменении VAD, громкости, soundboard-кнопок и т.д. —
            # stream не трогаем: слушатели не услышат провала в 100ms.
            if _dev_in_after != _dev_in_before or _dev_out_after != _dev_out_before:
                self.audio.start(_dev_in_after, _dev_out_after)
            else:
                print("[Settings] Устройства не изменились — аудиопоток не перезапускается")

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
        # Центрируем по ширине окна, над нижней панелью.
        # Используем _bottom_bar.y() — btn_sb.y() даёт позицию внутри bottomBar (~13px),
        # а не относительно окна → тост позиционировался почти у заголовка.
        tw = self._sb_toast.width()
        tx = (self.width() - tw) // 2
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
            from updater import check_for_updates_async
        except ImportError:
            # updater.py не в sys.path (dev-режим без сборки PyInstaller) — пропускаем.
            print("[Update] updater module не найден — автопроверка отключена")
            return

        def _on_found(version: str, url: str):
            # Используем QTimer чтобы обновление UI произошло в главном потоке
            QTimer.singleShot(0, lambda: self._show_update_banner(version, url))

        check_for_updates_async(on_update_found=_on_found)

    def _show_update_banner(self, version: str, url: str):
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

    def _open_lobby(self):
        """
        Открывает экран выбора сервера (MultiServerScreen) поверх главного окна.

        Пользователь остаётся подключённым к текущему серверу до тех пор пока
        явно не выберет другой и нажмёт «Подключиться». Соединение не обрывается.
        Повторное нажатие — если лобби уже открыто, поднимаем его вперёд.

        Ключевой трюк: переопределяем _open_connecting у MultiServerScreen,
        чтобы выбор сервера вёл через _on_switch_server, а не через отдельный
        ConnectingScreen + новый MainWindow.
        """
        # Если лобби уже открыто — просто поднимаем поверх
        if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
            try:
                if self._lobby_screen.isVisible():
                    self._lobby_screen.raise_()
                    self._lobby_screen.activateWindow()
                    return
            except RuntimeError:
                self._lobby_screen = None

        try:
            from client_main.ui_server_select import MultiServerScreen
            from client_main.ui_login import load_server_name
            server_name = load_server_name()
            screen = MultiServerScreen(self.nick, self.avatar, server_name=server_name)
            screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

            # ── Перехватываем выбор сервера ───────────────────────────────────
            # MultiServerScreen._open_connecting() по умолчанию создаёт новый
            # ConnectingScreen → новый MainWindow. Нам это не нужно: MainWindow
            # уже открыт. Подменяем метод чтобы переключение шло через нас.
            _main = self   # ссылка на MainWindow для замыкания

            def _lobby_open_connecting(ip: str):
                """Перехватчик: закрываем лобби, вызываем быстрое переключение."""
                try:
                    screen.hide()
                except RuntimeError:
                    pass
                _main._lobby_screen = None
                # Ищем имя сервера по IP из последнего списка Discovery
                server_info = {'ip': ip, 'server_name': ip}
                try:
                    for srv in (screen._worker.result if hasattr(screen._worker, 'result') else []):
                        if srv.get('ip') == ip:
                            server_info = srv
                            break
                except Exception:
                    pass
                _main._on_switch_server(server_info)

            screen._open_connecting = _lobby_open_connecting

            # При «Ввести IP вручную» — закрываем лобби, открываем LoginWindow
            screen.open_login.connect(self._on_lobby_manual_ip)

            self._lobby_screen = screen
            screen.show()
        except Exception as e:
            print(f"[UI] _open_lobby error: {e}")

    def _on_lobby_manual_ip(self, ip: str, nick: str, avatar: str):
        """
        Из лобби нажали «Ввести IP вручную».
        Закрываем лобби, переключаемся на введённый IP если он не пустой.
        """
        if hasattr(self, '_lobby_screen') and self._lobby_screen is not None:
            try:
                self._lobby_screen.hide()
            except RuntimeError:
                pass
            self._lobby_screen = None
        if ip and ip != self.ip:
            self._on_switch_server({'ip': ip, 'server_name': ip})

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

        # FIX #2: устанавливаем новый IP в network client ДО любых остановок.
        # Если mgr.stop() → stop_gracefully() успеет прислать нам CMD_SERVER_MIGRATE
        # раньше чем мы вызовем fast_switch_to, то _migrate_reconnect прочитает
        # self._ip и должен получить ПРАВИЛЬНЫЙ (новый) адрес, а не старый.
        self.net._ip = new_ip
        # Снимаем migration_pending чтобы любой параллельный _migrate_reconnect
        # от старого CMD_SERVER_MIGRATE не перехватил управление.
        self.net._migration_pending = False

        # FIX: net.running=False ПЕРВЫМ.
        # stop_gracefully рассылает CMD_SERVER_MIGRATE всем клиентам, включая нас.
        # Если running=True в момент получения CMD_SERVER_MIGRATE:
        #   process_message → _migration_pending=True, _migrate_reconnect стартует
        #   _migrate_reconnect: running=False, fast_switch_to(Client2.ip)
        #   fast_switch_to видит _reconnecting=False → работает ✓
        # НО: также server_migrating.emit → _on_server_migrating → stop_silent
        # (наш сервер) — это лишнее, т.к. mgr.stop() ниже уже его остановит.
        # Установив running=False сейчас, tcp_listen выйдет без _on_connection_lost,
        # и _migration_pending-флаг тоже не успеет установиться (мы уходим сами).
        self.net.running       = False
        self.net._reconnecting = False   # сброс на случай зависшего флага

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
        """Крестик → сворачиваем в трей. Полный выход — через меню трея."""

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
            self.video.shutdown()
        except Exception as ex:
            print(f"[UI] closeEvent video.shutdown() error: {ex}")

        # ── 6. NetworkClient.stop() ───────────────────────────────────────────
        # Закрывает RTCPeerConnection, asyncio loop, TCP/UDP сокеты.
        # После этого все сетевые потоки выйдут из блокирующих recv().
        try:
            self.net.stop()
        except Exception as ex:
            print(f"[UI] closeEvent net.stop() error: {ex}")

        # ── 7. EmbeddedServerManager — корректная передача хостинга ──────────
        # stop_gracefully(): broadcast CMD_SERVER_MIGRATE → 350мс → close сокеты.
        # Другие клиенты успевают получить команду и переподключиться.
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                mgr.stop()
        except Exception as ex:
            print(f"[UI] closeEvent EmbeddedServer stop error: {ex}")

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