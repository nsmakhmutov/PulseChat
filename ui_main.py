import os
import json
import pygame
import winsound
import keyboard
import time
from video_engine import VideoEngine
from ui_video import VideoWindow
from PyQt6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
                             QHeaderView, QMenu, QMessageBox, QStackedWidget,
                             QFrame)
from PyQt6.QtCore import Qt, QTimer, QSize, QSettings
from PyQt6.QtGui import QIcon, QAction, QFont, QFontDatabase, QBrush, QColor

from config import *
from audio_engine import AudioHandler
from network_engine import NetworkClient
from ui_dialogs import VolumePanel, SettingsDialog, SoundboardDialog
from version import APP_VERSION, APP_NAME, GITHUB_REPO


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

        pygame.mixer.init()
        self.audio = AudioHandler()
        self.net = NetworkClient(self.audio)

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
        self.video.frame_received.connect(self.on_video_frame)

        self.ui_timer = QTimer()
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(100)

        self.setup_hotkeys()
        self.net.connect_to_server(self.ip, self.nick, self.avatar)
        self.is_streaming = False

        # Тихая проверка обновлений в фоне (без всплывающих окон)
        self._start_silent_update_check()

    def setup_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.nick}")
        self.setMinimumSize(450, 600)
        self.setWindowIcon(QIcon(resource_path("assets/icon/app_icon.ico")))

        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        main_page = QWidget()
        main_page.setObjectName("centralWidget")
        layout = QVBoxLayout(main_page)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Ник", "", "", ""])
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
        header.resizeSection(1, 35)
        header.resizeSection(2, 35)
        header.resizeSection(3, 35)
        header.hide()

        self.tree.itemDoubleClicked.connect(self.on_tree_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.tree, stretch=1)

        # ── Баннер автообновления (скрыт до обнаружения новой версии) ─────────
        self._update_banner = QPushButton()
        self._update_banner.setVisible(False)
        self._update_banner.setStyleSheet(
            "background-color: #2ecc71; color: white; font-weight: bold; "
            "border-radius: 6px; padding: 6px; text-align: center;"
        )
        self._update_banner.clicked.connect(self.open_settings)  # откроет вкладку Версия
        layout.addWidget(self._update_banner)

        btns = QHBoxLayout()
        btns.setSpacing(15)

        self.btn_mute = QPushButton()
        self.btn_mute.setCheckable(True)
        self.btn_mute.setFixedSize(50, 50)
        self.btn_mute.setIcon(QIcon(resource_path("assets/icon/mic_on.svg")))
        self.btn_mute.setIconSize(QSize(30, 30))
        self.btn_mute.clicked.connect(self.toggle_mute)

        self.btn_deafen = QPushButton()
        self.btn_deafen.setCheckable(True)
        self.btn_deafen.setFixedSize(50, 50)
        self.btn_deafen.setIcon(QIcon(resource_path("assets/icon/volume_on.svg")))
        self.btn_deafen.setIconSize(QSize(30, 30))
        self.btn_deafen.clicked.connect(self.toggle_deafen)

        self.btn_sb = QPushButton()
        self.btn_sb.setFixedSize(50, 50)
        self.btn_sb.setIcon(QIcon(resource_path("assets/icon/bells.svg")))
        self.btn_sb.setIconSize(QSize(30, 30))
        self.btn_sb.clicked.connect(self.open_soundboard)

        self.btn_stream = QPushButton()
        self.btn_stream.setFixedSize(50, 50)
        self.btn_stream.setIconSize(QSize(30, 30))
        self.btn_stream.setIcon(QIcon(resource_path("assets/icon/stream_off.svg")))
        self.btn_stream.setCheckable(True)
        self.btn_stream.setObjectName("btnStream")
        self.btn_stream.clicked.connect(self.toggle_stream)

        self.ping_lbl = QLabel("0 ms")
        self.ping_lbl.setObjectName("pingLabel")
        self.ping_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_set = QPushButton()
        btn_set.setFixedSize(50, 50)
        btn_set.setIcon(QIcon(resource_path("assets/icon/settings.svg")))
        btn_set.setIconSize(QSize(30, 30))
        btn_set.clicked.connect(self.open_settings)

        btns.addWidget(self.btn_mute)
        btns.addWidget(self.btn_deafen)
        btns.addWidget(self.btn_sb)
        btns.addWidget(self.btn_stream)
        btns.addStretch()
        btns.addWidget(self.ping_lbl)
        btns.addWidget(btn_set)
        layout.addLayout(btns)

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

    def apply_theme(self, theme_name):
        font_f = self.custom_font_family
        is_dark = (theme_name == "Темная")

        bg = "#2b2b2b" if is_dark else "#e6e6e6"
        surface = "#3c3f41" if is_dark else "#f2f2f2"
        text = "#e0e0e0" if is_dark else "#1a1a1a"
        header_bg = "#4e5254" if is_dark else "#d1d1d1"
        border = "#515151" if is_dark else "#b8b8b8"
        accent_red = "#e74c3c" if is_dark else "#d32f2f"
        hover = "#505457" if is_dark else "#bcbcbc"
        tab_inactive = "#323537" if is_dark else "#dcdcdc"
        grad_s = "#45494a" if is_dark else "#fdfdfd"
        grad_e = "#323232" if is_dark else "#d8d8d8"

        self.setStyleSheet(f"""
            * {{ font-family: '{font_f}'; font-size: 18px; color: {text}; }}

            QMainWindow, QDialog, #centralWidget, QTabWidget, QScrollArea {{ 
                background-color: {bg}; 
            }}

            QDialog QWidget {{ background-color: transparent; }}
            QDialog QPushButton {{ background-color: {header_bg}; }}

            QHeaderView::section {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {grad_s}, stop:1 {grad_e});
                color: {text}; 
                padding: 12px; 
                height: 40px; 
                border: none; 
                border-bottom: 1px solid {border};
                font-weight: bold; 
                font-size: 22px;
            }}

            QTabWidget::pane {{ 
                border: 1px solid {border}; 
                background-color: {surface}; 
                border-radius: 5px; 
            }}
            QTabBar::tab {{ 
                background-color: {tab_inactive}; 
                padding: 10px 20px; 
                border-top-left-radius: 5px; 
                border-top-right-radius: 5px; 
                margin-right: 2px; 
                color: {text}; 
            }}
            QTabBar::tab:selected {{ 
                background-color: {surface}; 
                border: 1px solid {border}; 
                border-bottom: none; 
                font-weight: bold; 
            }}

            QComboBox {{ 
                background-color: {surface}; 
                border: 1px solid {border}; 
                border-radius: 5px; 
                padding: 5px 10px; 
                color: {text}; 
            }}
            QComboBox QAbstractItemView {{
                background-color: {surface};
                color: {text};
                border: 1px solid {border};
                selection-background-color: {hover};
                selection-color: {text};
                outline: none;
            }}

            QTreeWidget {{ 
                background-color: {surface}; 
                color: {text}; 
                border: 1px solid {border};
                border-radius: 0px; 
                outline: none; 
                padding: 0px; 
            }}

            QTreeWidget::item {{
                outline: none;
                border: none;
                padding-left: 5px;
            }}

            QTreeWidget::item:!has-children {{ 
                height: 45px; 
            }}

            QTreeWidget::item:selected {{ 
                background-color: transparent; 
                border-radius: 0px; 
                color: {text}; 
            }}

            QTreeWidget::item:selected:hover {{
                background-color: {hover};
                border-radius: 0px;
            }}

            QTreeWidget::item:hover {{
                background-color: {hover};
                border-radius: 0px;
            }}

            QPushButton {{ 
                background-color: {header_bg}; 
                border: 1px solid {border}; 
                border-radius: 8px; 
                padding: 5px; 
            }}
            QPushButton:hover {{ 
                background-color: {hover}; 
            }}
            QPushButton:checked {{ 
                background-color: {accent_red}; 
                color: white; 
            }}

            #btn_nr {{ background-color: #d65d4e; color: white; }}
            #btn_nr:checked {{ background-color: #27ae60; color: white; }}

            #pingLabel {{ font-size: 13px; margin-right: 10px; opacity: 0.8; }}
        """)

    def setup_hotkeys(self):
        try:
            keyboard.unhook_all()
            m = self.app_settings.value("hk_mute", "Tab+m")
            d = self.app_settings.value("hk_deafen", "Tab+d")
            keyboard.add_hotkey(m, lambda: self.btn_mute.click())
            keyboard.add_hotkey(d, lambda: self.btn_deafen.click())
        except Exception:
            pass

    def play_notification(self, stype="self_move"):
        path = self.sound_files.get(stype)
        vol = int(self.app_settings.value("system_sound_volume", 70)) / 100.0
        if path and os.path.exists(path):
            try:
                s = pygame.mixer.Sound(path)
                s.set_volume(vol)
                s.play()
            except Exception:
                pass
        else:
            if vol > 0:
                winsound.Beep(600 if stype == "self_move" else 400, 150)

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
        self.audio.my_uid = msg['uid']
        self.audio.start(self.app_settings.value("device_in_name"), self.app_settings.value("device_out_name"))
        self.play_notification("self_move")
        self._stack.setCurrentIndex(0)
        self._btn_reconnect.setEnabled(True)

    def on_video_frame(self, uid, q_image):
        if uid in self.stream_windows and self.stream_windows[uid].isVisible():
            self.stream_windows[uid].update_frame(q_image)

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

        current_room_uids = {
            u['uid']
            for u in users_map.get(self.current_room, [])
            if u['uid'] != self.audio.my_uid
        }
        current_streaming_uids = {
            u['uid']
            for room_list in users_map.values()
            for u in room_list
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
                        # ИСПРАВЛЕНИЕ: Удаляем окно из памяти
                        self.stream_windows[uid].deleteLater()
                        del self.stream_windows[uid]

        # ИСПРАВЛЕНИЕ 2.1: Очищаем движки от мусора и отключившихся
        self.audio.cleanup_users(all_active_uids)
        self.video.cleanup_users(all_active_uids)

        self.prev_room_uids = current_room_uids
        self.prev_streaming_uids = current_streaming_uids

        self.tree.clear()
        self.known_uids.clear()

        font_r = QFont(self.custom_font_family, 12)
        font_r.setBold(True)
        font_u = QFont(self.custom_font_family, 14)

        all_rooms = sorted(list(set(users_map.keys()).union(set(self.default_rooms))),
                           key=lambda x: (x not in self.default_rooms, x))

        for room in all_rooms:
            cl_room = room.replace("#", "").strip()
            item_r = QTreeWidgetItem(self.tree, [f"# {cl_room.upper()}", "", "", ""])
            item_r.setFirstColumnSpanned(True)
            item_r.setFont(0, font_r)
            item_r.setForeground(0, QBrush(QColor("#888888")))
            item_r.setFlags(item_r.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item_r.setData(0, Qt.ItemDataRole.UserRole, "ROOM_HEADER")
            item_r.setData(1, Qt.ItemDataRole.UserRole, room)

            for u in users_map.get(room, []):
                uid = u['uid']
                ip_addr = u.get('ip', '')
                if hasattr(self.audio, 'register_ip_mapping'):
                    self.audio.register_ip_mapping(uid, ip_addr)

                item_u = QTreeWidgetItem(item_r, [f"  {u['nick']}", "", "", ""])
                item_u.setIcon(0, QIcon(resource_path(f"assets/avatars/{u.get('avatar', '1.svg')}")))
                item_u.setFont(0, font_u)
                item_u.setData(0, Qt.ItemDataRole.UserRole, uid)

                item_u.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
                item_u.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
                item_u.setTextAlignment(3, Qt.AlignmentFlag.AlignCenter)

                self.known_uids[uid] = {
                    'item': item_u,
                    'is_m': u.get('mute', False),
                    'is_d': u.get('deaf', False),
                    'is_s': u.get('is_streaming', False)
                }

                watchers = u.get('watchers', [])
                if u.get('is_streaming', False) and watchers:
                    font_w = QFont(self.custom_font_family, 11)
                    for watcher in watchers:
                        w_nick = watcher.get('nick', '?')
                        watcher_item = QTreeWidgetItem(item_u, [f"    {w_nick}", "", "", ""])
                        watcher_item.setFont(0, font_w)
                        watcher_item.setForeground(0, QBrush(QColor("#888888")))
                        watcher_item.setFlags(watcher_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                        watcher_item.setData(0, Qt.ItemDataRole.UserRole, None)

        self.tree.expandAll()
        self._update_known_users_registry(users_map)

    def refresh_ui(self):
        ping = self.net.current_ping
        self.ping_lbl.setText(f"Ping: {ping} ms")
        col = "#2ecc71" if ping < 60 else "#f1c40f" if ping < 150 else "#e74c3c"
        self.ping_lbl.setStyleSheet(f"color: {col}; font-weight: bold; font-size: 13px; margin-right: 10px;")

        now = time.time()
        c_talk = QColor("#2ecc71")
        c_mute = QColor("#e74c3c")
        c_stream = QColor("#3498db")
        c_def = QColor("#ecf0f1") if self.app_settings.value("theme") == "Темная" else QColor("#444444")
        icon_size = QSize(20, 20)

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
                    item.setData(1, Qt.ItemDataRole.DecorationRole,
                                 QIcon(resource_path("assets/icon/live.svg")).pixmap(25, 25))
                else:
                    item.setData(1, Qt.ItemDataRole.DecorationRole, None)

                if curr_d:
                    item.setData(2, Qt.ItemDataRole.DecorationRole,
                                 QIcon(resource_path("assets/icon/volume_off.svg")).pixmap(icon_size))
                else:
                    item.setData(2, Qt.ItemDataRole.DecorationRole, None)

                if uid == self.audio.my_uid:
                    talk = me_talk
                    if self.audio.is_muted:
                        item.setData(3, Qt.ItemDataRole.DecorationRole,
                                     QIcon(resource_path("assets/icon/mic_off.svg")).pixmap(icon_size))
                    else:
                        item.setData(3, Qt.ItemDataRole.DecorationRole, None)
                else:
                    u_audio = self.audio.remote_users.get(uid)
                    talk = (now - u_audio.last_packet_time < 0.3) if u_audio else False
                    is_locally_muted = u_audio.is_locally_muted if u_audio else False

                    if is_locally_muted:
                        item.setData(3, Qt.ItemDataRole.DecorationRole,
                                     QIcon(resource_path("assets/icon/ban.svg")).pixmap(icon_size))
                    elif is_m:
                        item.setData(3, Qt.ItemDataRole.DecorationRole,
                                     QIcon(resource_path("assets/icon/mic_off.svg")).pixmap(icon_size))
                    else:
                        item.setData(3, Qt.ItemDataRole.DecorationRole, None)

                if talk:
                    item.setForeground(0, QBrush(c_talk))
                elif curr_s:
                    item.setForeground(0, QBrush(c_stream))
                elif curr_d or is_m or (uid != self.audio.my_uid and u_audio and is_locally_muted):
                    item.setForeground(0, QBrush(c_mute))
                else:
                    item.setForeground(0, QBrush(c_def))

    def on_tree_double_click(self, item, col):
        if item.data(0, Qt.ItemDataRole.UserRole) == "ROOM_HEADER":
            self.net.send_json({"action": CMD_JOIN_ROOM, "room": item.data(1, Qt.ItemDataRole.UserRole)})

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return

        uid = item.data(0, Qt.ItemDataRole.UserRole)
        if not uid or uid == "ROOM_HEADER" or uid == self.audio.my_uid:
            return

        menu = QMenu()

        def open_volume_panel(checked=False, _uid=uid, _nick=item.text(0).strip()):
            current_vol = 1.0
            with self.audio.users_lock:
                u = self.audio.remote_users.get(_uid)
                if u is not None:
                    current_vol = u.volume
                else:
                    ip = self.audio.uid_to_ip.get(_uid, '')
                    if ip:
                        current_vol = float(self.audio.settings.value(f"vol_ip_{ip}", 1.0))
            VolumePanel(_nick, current_vol, _uid, self.audio, self).exec()

        menu.addAction("Управление звуком").triggered.connect(open_volume_panel)

        user_data = self.known_uids.get(uid)
        is_streaming = user_data.get('is_s', False) if user_data else False

        if is_streaming:
            menu.addSeparator()
            action_watch = QAction(QIcon(resource_path("assets/icon/stream_on.svg")), "Смотреть стрим", self)
            action_watch.triggered.connect(lambda: self.open_video_window(uid, item.text(0)))
            menu.addAction(action_watch)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def open_video_window(self, uid, nick):
        if uid not in self.stream_windows or not self.stream_windows[uid].isVisible():
            w = VideoWindow(nick)
            w.uid = uid
            w.window_closed.connect(self._on_stream_window_closed)

            # --- Оверлей: подключаем кнопки управления ---
            # Микрофон и динамики переключают состояние аудио-движка
            w.overlay_mute_toggled.connect(lambda: self.btn_mute.click())
            w.overlay_deafen_toggled.connect(lambda: self.btn_deafen.click())
            # «Прекратить просмотр» — окно само закрывается, нам остаётся отправить stop
            w.overlay_stop_watch.connect(lambda _uid=uid: self._on_stream_window_closed(_uid))

            # Синхронизировать иконки оверлея при каждом изменении статуса аудио
            self.audio.status_changed.connect(
                lambda muted, deafened, _w=w: _w.sync_audio_state(muted, deafened)
            )
            # Установить актуальное состояние прямо сейчас
            w.sync_audio_state(self.audio.is_muted, self.audio.is_deafened)

            # Громкость стрима — зритель регулирует из оверлея
            w.overlay_stream_volume_changed.connect(self.audio.set_stream_volume)
            # Синхронизировать попап с текущим значением громкости стрима
            w.overlay._vol_popup.set_value(self.audio.stream_volume)

            w.show()
            self.stream_windows[uid] = w
            self.net.send_json({"action": "stream_watch_start", "streamer_uid": uid})
        else:
            self.stream_windows[uid].raise_()
            self.stream_windows[uid].activateWindow()

    def _on_stream_window_closed(self, uid):
        # Окно само удалится из памяти, просто удаляем ссылку
        self.stream_windows.pop(uid, None)
        self.net.send_json({"action": "stream_watch_stop", "streamer_uid": uid})

    def open_settings(self):
        if SettingsDialog(self.audio, self).exec():
            self.setup_hotkeys()
            self.audio.start(self.app_settings.value("device_in_name"), self.app_settings.value("device_out_name"))

    def open_soundboard(self):
        SoundboardDialog(self.net, self).exec()

    def _update_known_users_registry(self, users_map):
        REGISTRY_FILE = "known_users.json"
        try:
            registry = {}
            if os.path.exists(REGISTRY_FILE):
                with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
                    registry = json.load(f)
        except Exception:
            registry = {}

        changed = False
        now_str = time.strftime("%Y-%m-%d %H:%M")

        for room, u_list in users_map.items():
            for u in u_list:
                ip = u.get('ip', '')
                nick = u.get('nick', '')
                if not ip:
                    continue
                entry = registry.get(ip, {})
                if entry.get('nick') != nick or entry.get('last_seen') != now_str:
                    registry[ip] = {
                        'nick': nick,
                        'first_seen': entry.get('first_seen', now_str),
                        'last_seen': now_str,
                    }
                    changed = True

        if changed:
            try:
                with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(registry, f, ensure_ascii=False, indent=2)
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

    # ── Закрытие ───────────────────────────────────────────────────────────────

    def closeEvent(self, e):
        self.audio.stop()
        self.net.running = False
        super().closeEvent(e)

    def on_audio_status_changed(self, mute, deaf):
        self.net.send_status_update(mute, deaf)

    def update_stream_button_icon(self):
        if self.is_streaming:
            path = resource_path("assets/icon/stream_on.svg")
            self.btn_stream.setIcon(QIcon(path))
            self.btn_stream.setStyleSheet("background-color: #2ecc71; border: 1px solid #27ae60;")
        else:
            path = resource_path("assets/icon/stream_off.svg")
            self.btn_stream.setIcon(QIcon(path))
            self.btn_stream.setStyleSheet("")

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

        self.update_stream_button_icon()

        action = CMD_STREAM_START if self.is_streaming else CMD_STREAM_STOP
        self.net.send_json({"action": action})
        self.refresh_ui()