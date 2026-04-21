# ui_server_select.py
# ──────────────────────────────────────────────────────────────────────────────
# Экраны выбора сервера:
#   MultiServerScreen  — основной экран (список всех серверов в сети)
#   DiscoveryScreen    — старый экран (оставлен для совместимости)
#
# Helpers:
#   DiscoveryWorker    — UDP поиск одного сервера (QThread)
#   DiscoveryAllWorker — UDP поиск всех серверов (QThread)
#   _PingWorker        — TCP-latency измерение (QThread)
#   _ServerItemWidget  — карточка сервера в списке
#   _CreateServerDialog— диалог ввода имени сервера
# ──────────────────────────────────────────────────────────────────────────────

import socket
import threading
import time as _time

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QScrollArea,
    QLabel, QPushButton, QLineEdit, QDialog,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap

from config import resource_path, DEFAULT_PORT_TCP
from .ui_styles import (
    GLASS_CARD_SS, GLASS_ERROR_SS,
    BTN_PRIMARY_SS, BTN_SECONDARY_SS, BTN_SKIP_SS,
    BTN_CREATE_SS, BTN_CONNECT_SS, BTN_EXIT_SS,
    SERVER_ITEM_SS_IDLE, SERVER_ITEM_SS_SELECTED,
)
from .ui_titlebar import AppTitleBar
from .ui_login import save_server_name


# ══════════════════════════════════════════════════════════════════════════════
# DiscoveryWorker — UDP поиск одного сервера
# ══════════════════════════════════════════════════════════════════════════════

class DiscoveryWorker(QThread):
    """Запускает ServerDiscovery.discover() в отдельном потоке."""
    found     = pyqtSignal(dict)   # {'ip': ..., 'port': ..., 'host_nick': ...}
    not_found = pyqtSignal()

    def __init__(self, timeout: float = 2.5):
        super().__init__()
        self._timeout = timeout

    def run(self):
        try:
            from network_engine.server_discovery import ServerDiscovery
            result = ServerDiscovery().discover(self._timeout)
            if result:
                self.found.emit(result)
            else:
                self.not_found.emit()
        except Exception as e:
            print(f"[Discovery] Worker error: {e}")
            self.not_found.emit()


# ══════════════════════════════════════════════════════════════════════════════
# DiscoveryAllWorker — UDP поиск всех серверов
# ══════════════════════════════════════════════════════════════════════════════

class DiscoveryAllWorker(QThread):
    """Запускает ServerDiscovery.discover_all() в отдельном потоке."""
    done = pyqtSignal(list)

    def __init__(self, timeout: float = 2.5):
        super().__init__()
        self._timeout = timeout

    def run(self):
        try:
            from network_engine.server_discovery import ServerDiscovery
            results = ServerDiscovery().discover_all(self._timeout)
            self.done.emit(results)
        except Exception as e:
            print(f"[DiscoveryAll] Worker error: {e}")
            self.done.emit([])


# ══════════════════════════════════════════════════════════════════════════════
# _PingWorker — TCP-latency в фоне
# ══════════════════════════════════════════════════════════════════════════════

class _PingWorker(QThread):
    """Измеряет RTT TCP-соединения к серверу. Не блокирует UI."""
    result = pyqtSignal(int)   # ping_ms; -1 = недоступен

    def __init__(self, ip: str, port: int = DEFAULT_PORT_TCP, parent=None):
        super().__init__(parent)
        self._ip   = ip
        self._port = port

    def run(self):
        import time as _t
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            t0 = _t.perf_counter()
            s.connect((self._ip, self._port))
            ms = int((_t.perf_counter() - t0) * 1000)
            s.close()
            self.result.emit(ms)
        except Exception:
            self.result.emit(-1)


# ══════════════════════════════════════════════════════════════════════════════
# _ServerItemWidget — карточка сервера в списке
# ══════════════════════════════════════════════════════════════════════════════

class _ServerItemWidget(QFrame):
    """Карточка сервера: имя, кол-во участников, пинг, дерево ников.
    Одиночный клик = подключение.
    """
    clicked        = pyqtSignal()
    double_clicked = pyqtSignal()

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info      = info
        self._selected = False
        self.setObjectName("serverItem")
        self._apply_style()
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(12, 8, 12, 8)
        main_lay.setSpacing(4)

        # ── Верхняя строка: название + кол-во + пинг ──────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        lbl_name = QLabel(info.get('server_name', 'InPulse Server'))
        lbl_name.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #eaeef8;"
            "background: transparent; border: none;"
        )
        top_row.addWidget(lbl_name, stretch=1)

        cnt = info.get('user_count', 0)
        lbl_cnt = QLabel(f"👤 {cnt}")
        lbl_cnt.setStyleSheet(
            "font-size: 12px; color: #82e0aa; font-weight: bold;"
            "background: transparent; border: none;"
        )
        lbl_cnt.setFixedWidth(50)
        lbl_cnt.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top_row.addWidget(lbl_cnt)

        self._lbl_ping = QLabel("…")
        self._lbl_ping.setStyleSheet(
            "font-size: 11px; color: rgba(180,190,210,0.55); font-weight: normal;"
            "background: transparent; border: none;"
        )
        self._lbl_ping.setFixedWidth(52)
        self._lbl_ping.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top_row.addWidget(self._lbl_ping)

        main_lay.addLayout(top_row)

        # ── Список участников (дерево ников) ──────────────────────────────────
        nicks = info.get('user_nicks', [])
        if nicks:
            nicks_text = "  •  ".join(nicks[:20])
            lbl_nicks = QLabel(nicks_text)
            lbl_nicks.setWordWrap(True)
            lbl_nicks.setStyleSheet(
                "font-size: 11px; color: rgba(180,195,220,0.60);"
                "background: transparent; border: none;"
                "padding-left: 2px;"
            )
            main_lay.addWidget(lbl_nicks)

        # ── Пинг ──────────────────────────────────────────────────────────────
        ip = info.get('ip', '')
        if ip:
            self._ping_worker = _PingWorker(ip)
            self._ping_worker.result.connect(self._on_ping)
            self._ping_worker.start()

    def _on_ping(self, ms: int):
        if ms < 0:
            self._lbl_ping.setText("—")
            self._lbl_ping.setStyleSheet(
                "font-size: 11px; color: rgba(180,190,210,0.40); font-weight: normal;"
                "background: transparent; border: none;"
            )
            return
        col = "#2ecc71" if ms < 60 else "#f1c40f" if ms < 150 else "#e74c3c"
        self._lbl_ping.setText(f"{ms} мс")
        self._lbl_ping.setStyleSheet(
            f"font-size: 11px; color: {col}; font-weight: bold;"
            "background: transparent; border: none;"
        )

    def _apply_style(self):
        self.setStyleSheet(
            SERVER_ITEM_SS_SELECTED if self._selected else SERVER_ITEM_SS_IDLE
        )

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


# ══════════════════════════════════════════════════════════════════════════════
# _CreateServerDialog — диалог ввода имени сервера
# ══════════════════════════════════════════════════════════════════════════════

class _CreateServerDialog(QDialog):
    """Диалог: ввод имени сервера перед его созданием."""

    def __init__(self, parent=None, default_nick: str = "User"):
        super().__init__(parent)
        self.setWindowTitle("Создать сервер")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(320, 185)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(GLASS_CARD_SS)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)

        lbl = QLabel("🖥  Создать сервер")
        lbl.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #cdd6f4;"
            "background: transparent; border: none;"
        )
        lay.addWidget(lbl)

        sub = QLabel("Введите имя вашего сервера:")
        sub.setStyleSheet(
            "font-size: 12px; color: #8899bb; background: transparent; border: none;"
        )
        lay.addWidget(sub)

        self._inp = QLineEdit(f"Сервер {default_nick}")
        self._inp.setMaxLength(40)
        self._inp.setStyleSheet(
            "background-color: rgba(255,255,255,0.08);"
            "border: 1px solid rgba(255,255,255,0.18);"
            "border-radius: 7px; padding: 7px 11px;"
            "color: #dde3f0; font-size: 14px;"
        )
        self._inp.selectAll()
        lay.addWidget(self._inp)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_ok = QPushButton("✔  Создать")
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setStyleSheet(BTN_CREATE_SS)
        btn_ok.clicked.connect(self.accept)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet(
            "QPushButton { background: rgba(127,140,141,0.22); color: #8899aa;"
            " border: 1px solid rgba(127,140,141,0.40); border-radius: 8px;"
            " font-size: 13px; padding: 9px 0; }"
            "QPushButton:hover { background: rgba(149,165,166,0.35); color: #c8d0e0; }"
        )
        btn_cancel.clicked.connect(self.reject)

        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        self._inp.returnPressed.connect(self.accept)

    def get_name(self) -> str:
        return self._inp.text().strip() or "InPulse Server"


# ══════════════════════════════════════════════════════════════════════════════
# MultiServerScreen — основной стартовый экран
# ══════════════════════════════════════════════════════════════════════════════

class MultiServerScreen(QWidget):
    """
    Стартовый экран: список всех найденных серверов в сети.
    Заменяет DiscoveryScreen.
    """
    open_login = pyqtSignal(str, str, str)
    _ready     = pyqtSignal(str)   # внутренний: ip готового сервера

    def __init__(self, nick: str, avatar: str, server_name: str = ''):
        super().__init__()
        self.nick        = nick
        self.avatar      = avatar
        self.server_name = server_name or 'InPulse Server'

        self._worker: DiscoveryAllWorker | None     = None
        self._connecting_screen: QWidget | None    = None
        self._connecting_in_progress: bool         = False
        self._selected_info: dict | None           = None
        self._item_widgets: list[_ServerItemWidget] = []

        self._build_ui()
        self._ready.connect(lambda ip: self._open_connecting(ip))
        self._start_discovery()

    def _build_ui(self):
        from version import APP_NAME, APP_VERSION
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setFixedSize(480, 560)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        _tb = AppTitleBar(self, f"{APP_NAME} — Выбор сервера")
        card_lay.addWidget(_tb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(sep)

        root = QVBoxLayout()
        root.setSpacing(10)
        root.setContentsMargins(20, 16, 20, 14)
        card_lay.addLayout(root)

        # ── Заголовок + кнопка обновления (справа, как в браузерах) ─────────
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)

        self.lbl_status = QLabel("Поиск серверов в сети...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_status.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #cdd6f4;"
            "background: transparent; border: none;"
        )
        header_row.addWidget(self.lbl_status, stretch=1)

        self.btn_retry = QPushButton(" Обновить")
        self.btn_retry.setFixedSize(90, 30)
        self.btn_retry.setToolTip("Обновить список серверов")
        self.btn_retry.setStyleSheet(
            "QPushButton { background: rgba(52,152,219,0.25); color: #7ec8e3;"
            " border: 1px solid rgba(52,152,219,0.50); border-radius: 6px;"
            " font-size: 12px; padding: 0; }"
            "QPushButton:hover { background: rgba(52,152,219,0.45);"
            " border-color: rgba(52,152,219,0.80); color: #fff; }"
        )
        self.btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_retry.clicked.connect(self._start_discovery)
        header_row.addWidget(self.btn_retry)

        root.addLayout(header_row)

        # Список серверов
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.07); width: 5px; border-radius: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.22); border-radius: 2px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setSpacing(6)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._lbl_empty = QLabel("Нет серверов в сети")
        self._lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_empty.setStyleSheet(
            "font-size: 14px; color: rgba(200,210,224,0.55);"
            "background: transparent; border: none; padding: 20px 20px 4px 20px;"
        )
        self._lbl_empty.hide()
        self._list_layout.addWidget(self._lbl_empty)

        # Картинка empty.png — заполняет пустоту когда серверов нет
        self._img_empty = QLabel()
        self._img_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_empty.setStyleSheet("background: transparent; border: none;")
        _empty_pix = QPixmap(resource_path("assets/icon/empty.png"))
        if not _empty_pix.isNull():
            self._img_empty.setPixmap(
                _empty_pix.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            )
        self._img_empty.hide()
        self._list_layout.addWidget(self._img_empty)

        scroll.setWidget(self._list_container)
        root.addWidget(scroll, stretch=1)

        # Кнопка «Создать сервер» — полная ширина (на месте бывшей «Подключиться»)
        self.btn_create = QPushButton("➕  Создать сервер")
        self.btn_create.setStyleSheet(BTN_CREATE_SS)
        self.btn_create.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_create.clicked.connect(self._on_create_server)
        root.addWidget(self.btn_create)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setFixedHeight(1)
        sep2.setStyleSheet("background: rgba(255,255,255,0.06); border: none;")
        root.addWidget(sep2)

        bot_row = QHBoxLayout()
        bot_row.setSpacing(8)

        self.btn_manual = QPushButton("✏️  Ввести вручную")
        self.btn_manual.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_manual.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,0.08); color: #8899bb;"
            " border: 1px solid rgba(127,140,141,0.35); border-radius: 8px;"
            " font-size: 13px; font-weight: 600; padding: 10px 0; }"
            "QPushButton:hover { background: rgba(149,165,166,0.28); color: #c8d0e0; }"
        )
        self.btn_manual.clicked.connect(self._on_manual_ip)
        bot_row.addWidget(self.btn_manual, stretch=1)

        # Кнопка «Выйти» — полное завершение приложения (справа)
        self.btn_exit = QPushButton("✖  Выйти")
        self.btn_exit.setStyleSheet(BTN_EXIT_SS)
        self.btn_exit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_exit.clicked.connect(self._on_exit_app)
        bot_row.addWidget(self.btn_exit, stretch=1)

        root.addLayout(bot_row)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _start_discovery(self):
        self._selected_info = None
        self.lbl_status.setText("Поиск серверов в сети...")
        self.lbl_status.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #cdd6f4;"
            "background: transparent; border: none;"
        )
        self._connecting_in_progress = False
        self._clear_list()

        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(500)

        self._worker = DiscoveryAllWorker(timeout=2.5)
        self._worker.done.connect(self._on_discovery_done)
        self._worker.start()

    def _clear_list(self):
        for w in self._item_widgets:
            try:
                w.hide()
                w.deleteLater()
            except RuntimeError:
                pass
        self._item_widgets.clear()
        self._lbl_empty.hide()
        self._img_empty.hide()

    def _on_discovery_done(self, servers: list):
        if not servers:
            self.lbl_status.setText("")
            self._lbl_empty.show()
            self._img_empty.show()
            return

        self.lbl_status.setText(f"Найдено: {len(servers)}")
        self.lbl_status.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #82e0aa;"
            "background: transparent; border: none;"
        )
        for info in servers:
            w = _ServerItemWidget(info)
            w.clicked.connect(lambda i=info, widget=w: self._on_item_selected(i, widget))
            self._list_layout.addWidget(w)
            self._item_widgets.append(w)

    def _on_item_selected(self, info: dict, widget: _ServerItemWidget):
        # FIX #5: одиночный клик = выбор + немедленное подключение.
        # Кнопка «Подключиться» остаётся для доступности с клавиатуры.
        for w in self._item_widgets:
            try:
                w.set_selected(False)
            except RuntimeError:
                pass
        try:
            widget.set_selected(True)
        except RuntimeError:
            pass
        self._selected_info = info
        # Подключаемся сразу при клике
        ip = info.get('ip', '')
        if ip:
            self._open_connecting(ip)

    # ── Действия ──────────────────────────────────────────────────────────────

    def _on_connect_selected(self):
        if self._selected_info:
            ip = self._selected_info.get('ip', '')
            if ip:
                self._open_connecting(ip)

    def _on_create_server(self):
        if self._connecting_in_progress:
            return

        dlg = _CreateServerDialog(self, default_nick=self.nick)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        server_name = dlg.get_name()
        self.btn_create.setEnabled(False)
        self.lbl_status.setText("Запуск сервера...")
        self.lbl_status.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #c39ef5;"
            "background: transparent; border: none;"
        )

        try:
            from network_engine.server_discovery import get_local_radmin_ip
            from server import EmbeddedServerManager
            host_ip = get_local_radmin_ip()
            EmbeddedServerManager.get().start(host_ip, self.nick, server_name=server_name)
            save_server_name(server_name)
        except Exception as e:
            self.lbl_status.setText("Ошибка запуска сервера")
            self.lbl_status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #ff8080;"
                "background: transparent; border: none;"
            )
            print(f"[MultiServerScreen] create server error: {e}")
            self.btn_create.setEnabled(True)
            return

        self.lbl_status.setText("✅  Сервер запущен!  Подключение...")
        self.lbl_status.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #82e0aa;"
            "background: transparent; border: none;"
        )

        def _wait_for_server():
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.3)
                    s.connect((host_ip, DEFAULT_PORT_TCP))
                    s.close()
                    self._ready.emit(host_ip)
                    return
                except Exception:
                    _time.sleep(0.15)
            self._ready.emit(host_ip)

        threading.Thread(target=_wait_for_server, daemon=True, name="srv-ready-probe").start()

    def _on_manual_ip(self):
        self.open_login.emit('', self.nick, self.avatar)
        self.hide()

    def _open_connecting(self, ip: str):
        if self._connecting_in_progress:
            return
        self._connecting_in_progress = True

        # v3: encoder patch removed — Rust handles encoding, aiortc is decode-only

        try:
            from network_engine.server_discovery import get_local_radmin_ip
            local_ip = get_local_radmin_ip()
        except Exception:
            local_ip = '127.0.0.1'


        from .ui_connecting import ConnectingScreen
        self._connecting_screen = ConnectingScreen(
            ip, self.nick, self.avatar,

        )
        self._connecting_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self._connecting_screen.show_login.connect(self._on_return_to_login)
        self._connecting_screen.show()
        self.hide()

    def _on_return_to_login(self, ip: str, nick: str, avatar: str):
        self._connecting_in_progress = False

        try:
            from network_engine.server_discovery import get_local_radmin_ip
            local_ip = get_local_radmin_ip()
        except Exception:
            local_ip = '127.0.0.1'

        from server import EmbeddedServerManager
        own_server_running = (
            ip in ('127.0.0.1', local_ip)
            and EmbeddedServerManager.get().is_running()
        )

        if own_server_running:
            self.lbl_status.setText("Ошибка подключения к своему серверу")
            self.lbl_status.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #ff8080;"
                "background: transparent; border: none;"
            )
            self.btn_create.setEnabled(True)
            self.show()
        else:
            self.open_login.emit(ip, nick, avatar)
            self.hide()

    def _on_exit_app(self):
        """
        Полное завершение приложения — убиваем все процессы.
        Вызывается по кнопке «Выйти» на экране выбора сервера.
        """
        import sys, os, signal
        # Останавливаем встроенный сервер если запущен
        try:
            from server import EmbeddedServerManager
            mgr = EmbeddedServerManager.get()
            if mgr.is_running():
                mgr.stop()
        except Exception:
            pass
        # Завершаем Qt
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()
        # Гарантированно убиваем процесс
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            sys.exit(0)


# ══════════════════════════════════════════════════════════════════════════════
# DiscoveryScreen — старый экран (оставлен для совместимости)
# ══════════════════════════════════════════════════════════════════════════════

class DiscoveryScreen(QWidget):
    """
    Первый экран после запуска приложения (устаревший — используй MultiServerScreen).

    Логика:
      1. Поиск серверов → DiscoveryWorker (UDP, 2.5 сек).
      2. Найден  → автоподключение через ConnectingScreen.
      3. Не найден → «Создать сервер» или «Ввести IP вручную».
    """
    open_login = pyqtSignal(str, str, str)
    _ready     = pyqtSignal(str)

    def __init__(self, nick: str, avatar: str):
        super().__init__()
        self.nick   = nick
        self.avatar = avatar

        self._worker: DiscoveryWorker | None    = None
        self._connecting_screen: QWidget | None = None
        self._connecting_in_progress            = False

        self._build_ui()
        self._ready.connect(lambda ip: self._open_connecting(ip))
        self._start_discovery()

    def _build_ui(self):
        from version import APP_NAME, APP_VERSION
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setFixedSize(420, 480)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        _tb = AppTitleBar(self, f"{APP_NAME} v{APP_VERSION}")
        card_lay.addWidget(_tb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(sep)

        root = QVBoxLayout()
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setSpacing(16)
        root.setContentsMargins(36, 28, 36, 28)
        card_lay.addLayout(root)

        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setFixedHeight(100)
        self.lbl_img.setStyleSheet("background: transparent; border: none;")
        logo = resource_path("assets/icon/logo.ico")
        import os
        if os.path.exists(logo):
            px = QPixmap(logo).scaled(
                84, 84,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.lbl_img.setPixmap(px)
        else:
            self.lbl_img.setText("🔍")
            self.lbl_img.setStyleSheet("font-size: 64px;")
        root.addWidget(self.lbl_img)

        self.lbl_status = QLabel("Поиск серверов в сети...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_status)

        self.lbl_sub = QLabel("")
        self.lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_sub.setStyleSheet(
            "color: rgba(200,210,224,0.60); font-size: 13px; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_sub)

        self.frm_error = QFrame()
        self.frm_error.setStyleSheet(GLASS_ERROR_SS)
        err_lay = QVBoxLayout(self.frm_error)
        err_lay.setContentsMargins(14, 10, 14, 10)
        self.lbl_error = QLabel()
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_error.setStyleSheet(
            "color: #ff8080; font-size: 13px; font-weight: 500; "
            "background: transparent; border: none;"
        )
        err_lay.addWidget(self.lbl_error)
        self.frm_error.hide()
        root.addWidget(self.frm_error)

        self.btn_create = QPushButton("🖥  Создать сервер")
        self.btn_create.setStyleSheet(BTN_PRIMARY_SS)
        self.btn_create.hide()
        self.btn_create.clicked.connect(self._on_create_server)
        root.addWidget(self.btn_create)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(10)

        self.btn_retry = QPushButton("🔁  Искать снова")
        self.btn_retry.setStyleSheet(BTN_SECONDARY_SS)
        self.btn_retry.hide()
        self.btn_retry.clicked.connect(self._start_discovery)
        btn_row2.addWidget(self.btn_retry)

        self.btn_manual = QPushButton("✏️  Ввести IP")
        self.btn_manual.setStyleSheet(BTN_SKIP_SS)
        self.btn_manual.hide()
        self.btn_manual.clicked.connect(self._on_manual_ip)
        btn_row2.addWidget(self.btn_manual)

        root.addLayout(btn_row2)

    def _start_discovery(self):
        self.frm_error.hide()
        self.btn_create.hide()
        self.btn_retry.hide()
        self.btn_manual.hide()
        self.lbl_sub.clear()
        self._connecting_in_progress = False
        self.lbl_status.setText("Поиск серверов в сети...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )

        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(500)

        self._worker = DiscoveryWorker(timeout=2.5)
        self._worker.found.connect(self._on_server_found)
        self._worker.not_found.connect(self._on_server_not_found)
        self._worker.start()

    def _on_server_found(self, info: dict):
        ip   = info.get('ip', '')
        nick = info.get('host_nick', '?')
        self.lbl_status.setText("✅  Сервер найден!")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #82e0aa; "
            "background: transparent; border: none;"
        )
        self.lbl_sub.setText(f"Хост: {nick}  •  {ip}")
        QTimer.singleShot(400, lambda: self._open_connecting(ip))

    def _on_server_not_found(self):
        self.lbl_status.setText("Нет серверов в сети")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #e0b060; "
            "background: transparent; border: none;"
        )
        self.lbl_sub.setText("Никто ещё не создал комнату")
        self.btn_create.show()
        self.btn_retry.show()
        self.btn_manual.show()

    def _on_create_server(self):
        self.btn_create.hide()
        self.btn_retry.hide()
        self.btn_manual.hide()
        self.frm_error.hide()
        self.lbl_sub.clear()
        self.lbl_status.setText("Запуск сервера...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #c39ef5; "
            "background: transparent; border: none;"
        )

        try:
            from network_engine.server_discovery import get_local_radmin_ip
            from server import EmbeddedServerManager
            host_ip = get_local_radmin_ip()
            EmbeddedServerManager.get().start(host_ip, self.nick)
        except Exception as e:
            self.lbl_error.setText(f"⚠️  Ошибка запуска сервера:\n{e}")
            self.frm_error.show()
            self.lbl_sub.setText("Попробуйте ещё раз или введите IP вручную")
            self.btn_create.show()
            self.btn_create.setEnabled(True)
            self.btn_retry.show()
            self.btn_manual.show()
            self.lbl_status.setText("Нет серверов в сети")
            self.lbl_status.setStyleSheet(
                "font-size: 17px; font-weight: bold; color: #e0b060; "
                "background: transparent; border: none;"
            )
            return

        self.lbl_status.setText("✅  Сервер запущен!  Ожидание готовности...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #82e0aa; "
            "background: transparent; border: none;"
        )

        def _wait_for_server():
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.3)
                    s.connect((host_ip, DEFAULT_PORT_TCP))
                    s.close()
                    self._ready.emit(host_ip)
                    return
                except Exception:
                    _time.sleep(0.15)
            self._ready.emit(host_ip)

        threading.Thread(target=_wait_for_server, daemon=True, name="srv-ready-probe").start()

    def _on_manual_ip(self):
        self.open_login.emit('', self.nick, self.avatar)
        self.hide()

    def _open_connecting(self, ip: str):
        if self._connecting_in_progress:
            return
        self._connecting_in_progress = True

        # v3: encoder patch removed — Rust handles encoding, aiortc is decode-only

        try:
            from network_engine.server_discovery import get_local_radmin_ip
            local_ip = get_local_radmin_ip()
        except Exception:
            local_ip = '127.0.0.1'


        from .ui_connecting import ConnectingScreen
        self._connecting_screen = ConnectingScreen(
            ip, self.nick, self.avatar,

        )
        self._connecting_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self._connecting_screen.show_login.connect(self._on_return_to_login)
        self._connecting_screen.show()
        self.hide()

    def _on_return_to_login(self, ip: str, nick: str, avatar: str):
        self._connecting_in_progress = False

        try:
            from network_engine.server_discovery import get_local_radmin_ip
            local_ip = get_local_radmin_ip()
        except Exception:
            local_ip = '127.0.0.1'

        from server import EmbeddedServerManager
        own_server_running = (
            ip in ('127.0.0.1', local_ip)
            and EmbeddedServerManager.get().is_running()
        )

        if own_server_running:
            self.lbl_status.setText("Ошибка подключения")
            self.lbl_status.setStyleSheet(
                "font-size: 17px; font-weight: bold; color: #ff8080; "
                "background: transparent; border: none;"
            )
            self.lbl_sub.setText("Сервер запущен, но подключиться не удалось")
            self.frm_error.hide()
            self.btn_create.hide()
            self.btn_retry.show()
            self.btn_manual.show()
            self.show()
        else:
            self.open_login.emit(ip, nick, avatar)
            self.hide()