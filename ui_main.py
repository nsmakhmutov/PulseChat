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



# ──────────────────────────────────────────────────────────────────────────────
# Кастомная строка заголовка окна (вместо системного title bar Windows)
# ──────────────────────────────────────────────────────────────────────────────
class CustomTitleBar(QWidget):
    """
    Кастомный title bar для безрамочного окна.
    Поддерживает: перетаскивание окна, сворачивание, разворачивание/восстановление,
    закрытие, двойной клик для maximize/restore.
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

        # Иконка приложения (logo.ico — единый источник иконки по всему проекту)
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(22, 22)
        self._icon_lbl.setPixmap(
            QIcon(resource_path("assets/icon/logo.ico")).pixmap(22, 22)
        )
        # Inline-стиль намеренно НЕ устанавливается: у QLabel без своего
        # setStyleSheet() родительский stylesheet (#customTitleBar *) применяется
        # корректно и задаёт прозрачный фон через CSS.
        layout.addWidget(self._icon_lbl)

        # Текст заголовка
        # ВАЖНО: не вызываем self._title_lbl.setStyleSheet() здесь.
        # Если у виджета есть собственный stylesheet (даже без color:), Qt полностью
        # блокирует наследование цвета из родительского stylesheet — именно поэтому
        # #titleBarText { color: ... } в apply_theme не работал в светлой теме.
        # Всё оформление делается через apply_theme CSS-правила.
        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("titleBarText")
        layout.addWidget(self._title_lbl, stretch=1)

        # ── Кнопки управления окном ──────────────────────────────────────────
        # Размеры задаём через setFixedSize, а не через inline stylesheet —
        # по той же причине: inline stylesheet блокирует цвет из apply_theme.
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
        self._title_lbl.setText(title)

    def _toggle_maximize(self):
        if self._win.isMaximized():
            self._win.showNormal()
            self._btn_max.setText("□")
        else:
            self._win.showMaximized()
            self._btn_max.setText("❐")

    # ── Drag to move ─────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            if self._win.isMaximized():
                self._win.showNormal()
                self._btn_max.setText("□")
                # Пересчитываем drag_pos после восстановления нормального размера
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

        # ── Состояние для ресайза безрамочного окна ──────────────────────────
        self._resize_margin = 6          # px — зона у края для начала ресайза
        self._resize_direction: str | None = None
        self._resize_start_pos: QPoint | None = None
        self._resize_start_geom: QRect | None = None
        self.setMouseTracking(True)

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
        # Сигнал из audio_engine: ползунок громкости пользователя достиг/покинул 0.
        # Обновляем ban-иконку немедленно, не дожидаясь следующего refresh_ui() (100 мс).
        self.audio.user_volume_zero.connect(self._on_user_volume_zero)
        self.video.frame_received.connect(self.on_video_frame)

        # Тост «кто включил soundboard» — желтый лейбл поверх окна
        self.net.soundboard_played.connect(self._on_soundboard_played)

        # Сигналы фичи «Пнуть»
        self.net.nudge_received.connect(self._on_nudge_received)
        self.net.nudge_triggered.connect(self._on_nudge_triggered)

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(100)

        self.setup_hotkeys()
        self.net.connect_to_server(self.ip, self.nick, self.avatar)
        self.is_streaming = False
        self._sb_panel = None   # ссылка на SoundboardPanel (для toggle и lifecycle)

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
        self._cache_theme = self.app_settings.value("theme", "Светлая")
        self._c_talk   = QColor("#2ecc71")
        self._c_mute   = QColor("#e74c3c")
        self._c_stream = QColor("#3498db")
        self._c_def    = QColor("#ecf0f1") if self._cache_theme == "Темная" else QColor("#444444")
        self._icon_size = QSize(20, 20)

        # ── Кэш QBrush для refresh_ui() ────────────────────────────────────────
        # QBrush(QColor) создавался на КАЖДЫЙ вызов refresh_ui() (10 раз/сек)
        # для КАЖДОГО пользователя → постоянное давление на GC.
        # Кэшируем один раз, пересоздаём только при смене темы.
        self._br_talk   = QBrush(self._c_talk)
        self._br_mute   = QBrush(self._c_mute)
        self._br_stream = QBrush(self._c_stream)
        self._br_def    = QBrush(self._c_def)
        self._br_gray   = QBrush(QColor("#888888"))   # для заголовков комнат и watchers

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
        self.setMinimumSize(450, 600)
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

    def setWindowTitle(self, title: str):
        """Переопределяем — синхронно обновляем кастомный title bar."""
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
        # Сбрасываем курсор ресайза обратно в стандартный.
        # Без этого курсор «застревал» в форме SizeXxx после отпускания кнопки мыши,
        # потому что mouseMoveEvent с зажатой кнопкой обновлял курсор только во время
        # перетаскивания, а setCursor() остаётся в силе пока явно не вызван unsetCursor().
        self.unsetCursor()
        super().mouseReleaseEvent(e)

    def apply_theme(self, theme_name):
        font_f = self.custom_font_family
        is_dark = (theme_name == "Темная")

        # ────────────────────────────────────────────────────────────────────────
        # Палитра — единый «стеклянный» язык дизайна.
        # Тёмная тема: глубокий navy-dark, rgba-слои, как в SoundboardPanel.
        # Светлая тема: молочно-синяя, сохраняет читаемость, чуть прозрачнее.
        # ────────────────────────────────────────────────────────────────────────
        if is_dark:
            win_bg       = "rgba(28, 32, 50, 255)"      # основной фон окна (было 18,20,30 — слишком тёмно)
            surface      = "rgba(255,255,255,0.10)"      # фон дерева (было 0.04 — иконки не видны)
            surface_solid= "#252840"                     # для QComboBox dropdown (нет rgba)
            text         = "#eaeef8"                     # ярче (было #d4d8e8)
            text_dim     = "#8898bb"                     # ярче (было #7888a8)
            border       = "rgba(255,255,255,0.13)"
            border_solid = "#3d4260"
            hover        = "rgba(255,255,255,0.12)"
            hover_solid  = "#363a58"                     # был #2a2d40 — почти не отличался от фона
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

            /* ── Корневой контейнер окна ─────────────────────────────────────── */
            #windowRoot {{
                background-color: {win_bg};
                border: 1px solid {win_border};
                border-radius: 10px;
            }}

            /* ── Кастомный title bar ─────────────────────────────────────────── */
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

            /* ── Главная область контента ───────────────────────────────────── */
            QMainWindow, #centralWidget {{
                background-color: transparent;
            }}

            /* ── Дерево пользователей ───────────────────────────────────────── */
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
                /* Нет border-radius — иначе Qt рисует закруглённый клип поверх
                   прозрачного фона и при hover видны «просветы» по углам. */
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
            /* Hover — сплошная полоса на всю ширину, без border-radius.
               Убираем rgba-прозрачность: при быстром движении мыши Qt не успевает
               перерисовать соседние итемы → между ними мерцает тонкая серая линия.
               Используем чуть более непрозрачный цвет чтобы перекрывать фон дерева. */
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
            /* Tooltip иконок статусов — «парящий» текст */
            QTreeWidget QToolTip {{
                background-color: transparent;
                border: none;
                color: {text};
                font-size: 12px;
                padding: 0px;
            }}
            /* Скроллбар в дереве */
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

            /* ── Нижняя панель кнопок ───────────────────────────────────────── */
            #bottomBar {{
                background-color: {bottom_bg};
                border-top: 1px solid {bottom_sep};
                border-bottom-left-radius: 9px;
                border-bottom-right-radius: 9px;
            }}

            /* Все кнопки в bottomBar */
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

            /* Кнопка трансляции — отдельный objectName (управляется из toggle_stream) */
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

            /* ── Баннер обновления ──────────────────────────────────────────── */
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

            /* ── Fallback: обычные QPushButton вне bottomBar (reconnect и т.п.) */
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

            /* Кнопка переподключения на экране ошибки */
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

            /* QDialog / QScrollArea / etc. — не трогаем стиль диалогов отсюда */
            QDialog {{ background: transparent; }}
        """)

        # ── Кэш цветов для refresh_ui: пересоздаём при смене темы ────────────
        self._cache_theme = theme_name
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
                sd.play(data * vol, sr)
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

    def on_connected(self, msg):
        print(f"[DEBUG] on_connected: START — uid={msg.get('uid')}", flush=True)
        try:
            self.audio.my_uid = msg['uid']
            print(f"[DEBUG] on_connected: my_uid установлен = {self.audio.my_uid}", flush=True)

            print(f"[DEBUG] on_connected: вызов audio.start() ...", flush=True)
            self.audio.start(
                self.app_settings.value("device_in_name"),
                self.app_settings.value("device_out_name")
            )
            print(f"[DEBUG] on_connected: audio.start() завершён", flush=True)

            self.play_notification("self_move")
            print(f"[DEBUG] on_connected: play_notification выполнен", flush=True)

            self._stack.setCurrentIndex(0)
            print(f"[DEBUG] on_connected: setCurrentIndex(0) выполнен", flush=True)

            self._btn_reconnect.setEnabled(True)
            print(f"[DEBUG] on_connected: DONE", flush=True)

            # Восстанавливаем сохранённый статус после переподключения.
            # Сервер не хранит статусы постоянно — только в рамках сессии,
            # поэтому при каждом (пере)подключении отправляем сохранённый статус.
            if self._my_status_icon:
                self.net.send_presence_update(self._my_status_icon, self._my_status_text)

        except Exception as e:
            import traceback
            print(f"[DEBUG] on_connected: EXCEPTION:\n{traceback.format_exc()}", flush=True)

    def on_video_frame(self, uid, q_image):
        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_frame(q_image)

    def update_user_tree(self, users_map):
        print(f"[DEBUG] update_user_tree: START — rooms={list(users_map.keys())}", flush=True)
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
                    # FIX MEM: раньше здесь был deleteLater() + del stream_windows[uid].
                    # Это НЕ вызывало _on_stream_window_closed() → decode_worker
                    # для этого uid продолжал работать, держа H264-декодер в памяти
                    # (~10–40 МБ) до своего 2-секундного тайм-аута. Сигнал
                    # audio.status_changed тоже не отключался.
                    #
                    # Теперь: _on_stream_window_closed() обрабатывает ВСЁ:
                    #   — disconnect audio.status_changed
                    #   — stop_viewer_for_uid (немедленно сигнализирует воркеру)
                    #   — deleteLater() окна
                    #   — deferred GC + Windows heap trim через 2.5 сек
                    if uid in self.stream_windows:
                        self._on_stream_window_closed(uid)

        # ИСПРАВЛЕНИЕ 2.1: Очищаем движки от мусора и отключившихся
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
                item_u.setIcon(0, QIcon(resource_path(f"assets/avatars/{u.get('avatar', '1.svg')}")))
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
        print(f"[DEBUG] update_user_tree: DONE", flush=True)

    def refresh_ui(self):
        try:
            ping = self.net.current_ping
            self.ping_lbl.setText(f"Ping: {ping} ms")
            col = "#2ecc71" if ping < 60 else "#f1c40f" if ping < 150 else "#e74c3c"
            self.ping_lbl.setStyleSheet(f"color: {col}; font-weight: bold; font-size: 13px; margin-right: 10px;")

            now = time.time()

            # Обновляем кэш цветов при смене темы (меняется редко)
            theme_now = self.app_settings.value("theme", "Светлая")
            if theme_now != self._cache_theme:
                self._cache_theme = theme_now
                self._c_def  = QColor("#ecf0f1") if theme_now == "Темная" else QColor("#444444")
                self._br_def = QBrush(self._c_def)   # пересоздаём кисть при смене темы

            c_talk   = self._c_talk
            c_mute   = self._c_mute
            c_stream = self._c_stream
            c_def    = self._c_def
            icon_size = self._icon_size

            with self.audio.users_lock:
                me_talk = (now - self.audio.last_voice_time < 0.3) and not self.audio.is_muted

                for uid, data in self.known_uids.items():
                    item = data['item']
                    is_m = data['is_m']
                    is_d = data['is_d']
                    is_s = data['is_s']

                    curr_s = self.is_streaming if uid == self.audio.my_uid else is_s
                    curr_d = self.audio.is_deafened if uid == self.audio.my_uid else is_d

                    if curr_s:
                        item.setData(2, Qt.ItemDataRole.DecorationRole, self._px_live)
                    else:
                        item.setData(2, Qt.ItemDataRole.DecorationRole, None)

                    if curr_d:
                        item.setData(3, Qt.ItemDataRole.DecorationRole, self._px_vol_off)
                    else:
                        item.setData(3, Qt.ItemDataRole.DecorationRole, None)

                    if uid == self.audio.my_uid:
                        talk = me_talk
                        if self.audio.is_muted:
                            item.setData(4, Qt.ItemDataRole.DecorationRole, self._px_mic_off)
                        else:
                            item.setData(4, Qt.ItemDataRole.DecorationRole, None)
                    else:
                        u_audio = self.audio.remote_users.get(uid)
                        talk = (now - u_audio.last_packet_time < 0.3) if u_audio else False
                        is_locally_muted = u_audio.is_locally_muted if u_audio else False
                        # volume_zero=True когда ползунок выставлен в 0 — визуально
                        # неотличимо от кнопки «заглушить»: та же ban-иконка.
                        is_vol_zero = (u_audio.volume_zero if u_audio else False)

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
                    elif curr_d or is_m or (uid != self.audio.my_uid and u_audio and (is_locally_muted or is_vol_zero)):
                        item.setForeground(0, self._br_mute)
                    else:
                        item.setForeground(0, self._br_def)
        except Exception as _e:
            import traceback
            print(f"[DEBUG] refresh_ui: EXCEPTION:\n{traceback.format_exc()}", flush=True)

    def on_tree_double_click(self, item, col):
        if item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
            self.net.send_json({"action": CMD_JOIN_ROOM, "room": item.data(1, Qt.ItemDataRole.UserRole)})

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return

        uid = item.data(0, Qt.ItemDataRole.UserRole)
        if not uid or uid == "ROOM_HEADER":
            return

        # ── Правый клик по СЕБЕ → оверлей выбора статуса ─────────────────────
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
        ).show()

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

            # Громкость стрима — зритель регулирует из оверлея
            w.overlay_stream_volume_changed.connect(self.audio.set_stream_volume)
            # Синхронизировать попап с текущим значением громкости стрима
            w.overlay._vol_popup.set_value(self.audio.stream_volume)

            # --- Качество видео (per-viewer server-side throttling) ---
            # Когда зритель нажимает кнопку качества → сообщаем серверу skip_factor.
            # Сервер начнёт пропускать N-1 из N кадров для этого зрителя.
            w.quality_changed.connect(
                lambda sf, _uid=uid: self.net.send_quality_request(sf)
            )
            # Периодический IDR-запрос в режиме MEDIUM/LOW качества:
            # каждые 2 с зритель просит I-frame, чтобы P-frame артефакты
            # от пропущенных кадров очищались быстро.
            w.viewer_keyframe_needed.connect(
                lambda _uid=uid: self.net.request_viewer_keyframe(_uid)
            )

            w.show()
            self.stream_windows[uid] = w
            self.net.send_json({"action": "stream_watch_start", "streamer_uid": uid})
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
        self.net.send_json({"action": "stream_watch_stop", "streamer_uid": uid})

        # FIX MEM: Останавливаем decode_worker для этого uid.
        #
        # Без этого вызова после закрытия окна decode_worker продолжал работать:
        #   — При сценарии 3 (зритель закрыл вручную): воркер работает ВЕЧНО пока
        #     стример не остановит трансляцию, всё это время держа FFmpeg декодер
        #     (FRAME×2 потока = ~10 МБ, ранее AUTO = ~40 МБ).
        #   — При сценарии 4 (стример остановил): воркер ждёт 2 сек тайм-аут,
        #     всё это время память не освобождается.
        # stop_viewer_for_uid() — non-blocking: кладёт None в очередь и уходит.
        # Воркер завершится сам и вызовет decoder.close() + gc.collect() в finally.
        self.video.stop_viewer_for_uid(uid)

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
        if SettingsDialog(self.audio, self).exec():
            self.setup_hotkeys()
            self.audio.start(self.app_settings.value("device_in_name"), self.app_settings.value("device_out_name"))

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
        from ui_dialogs import SoundboardPanel

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
        panel.show_above(self.btn_sb)

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
        ty = self._bottom_bar.y() - self._sb_toast.height() - 8
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
        REGISTRY_FILE = "known_users.json"

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

        from updater import check_for_updates_async

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

    def closeEvent(self, e):
        """При нажатии ✕ — корректно завершаем приложение."""
        # Скрываем системный оверлей (поверх всех окон — должен исчезнуть первым)
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

    def toggle_stream(self):
        from ui_dialogs import StreamSettingsDialog
        if not self.is_streaming:
            dialog = StreamSettingsDialog(self)
            if dialog.exec():
                settings = dialog.get_settings()

                # Звук трансляции:
                #   set_stream_audio_enabled(True) запускает StreamAudioCapture:
                #     1. Если VB-CABLE установлен → захват из «CABLE Output» (чисто, без эха)
                #     2. Если нет → WASAPI Loopback с AEC (старый путь, запасной)
                #   device_idx=None: VB-CABLE определяется по имени автоматически,
                #   WASAPI выбирает дефолтное устройство.
                self.audio.set_stream_audio_enabled(settings.get("stream_audio", False))

                success = self.video.start_streaming(settings)
                if success:
                    self.is_streaming = True
                else:
                    self.audio.set_stream_audio_enabled(False)
                    self.btn_stream.setChecked(False)
                    return
            else:
                self.btn_stream.setChecked(False)
                return
        else:
            self.video.stop_streaming()
            self.audio.set_stream_audio_enabled(False)
            self.is_streaming = False
            # stop_streaming() уже делает gc.collect() + Windows heap trim внутри.
            # Доп. trim здесь — для Qt-объектов (QImage в on_video_frame буфере).

        self.update_stream_button_icon()

        action = CMD_STREAM_START if self.is_streaming else CMD_STREAM_STOP
        self.net.send_json({"action": action})
        self.refresh_ui()