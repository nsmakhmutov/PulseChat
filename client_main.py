import os
import json
import ctypes
import sys
import socket
import traceback
import faulthandler

# Нативный C-стектрейс при SIGSEGV даже если Python уже не работает
_crash_log = open("crash_native.log", "w", buffering=1)
faulthandler.enable(file=_crash_log)


def _global_excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[CRASH] Необработанное исключение:\n{msg}", flush=True)
    with open("crash_python.log", "a", encoding="utf-8") as f:
        f.write(msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _global_excepthook


def resource_path(relative_path: str) -> str:
    """Возвращает абсолютный путь к ресурсу (dev и PyInstaller).

        :param relative_path: относительный путь к ресурсу
        :return: абсолютный путь
    """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# Добавляем папку проекта в поиск DLL (opus.dll, rnnoise.dll) ДО любых импортов
_project_dir = os.path.dirname(os.path.abspath(__file__))
os.add_dll_directory(_project_dir)
try:
    os.add_dll_directory(sys._MEIPASS)
except Exception:
    pass

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QLineEdit, QPushButton, QLabel, QCheckBox, QFrame,
                             QSizePolicy, QProgressBar)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QSurfaceFormat, QPixmap

from config import resource_path, DEFAULT_PORT_TCP
from ui_main import MainWindow
from ui_dialogs import AvatarSelector
from updater import check_for_updates_async, download_and_install


CONFIG_FILE = "user_config.json"
PROBE_TIMEOUT_SEC = 3.0


def load_config() -> dict | None:
    """Загружает конфиг из JSON-файла.

        :return: словарь конфига или None если файл отсутствует/повреждён
    """
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_config(ip: str, nick: str, avatar: str) -> None:
    """Сохраняет данные подключения в JSON-файл.

        :param ip: IP-адрес сервера
        :param nick: никнейм пользователя
        :param avatar: имя файла аватарки
    """
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({"ip": ip, "nick": nick, "avatar": avatar}, f)
    except Exception as e:
        print(f"[Config] Не удалось сохранить конфиг: {e}")


class ConnectWorker(QThread):
    """Фоновый поток TCP-probe: проверяет доступность сервера."""

    result = pyqtSignal(bool)

    def __init__(self, ip: str):
        """
            :param ip: IP-адрес сервера
        """
        super().__init__()
        self.ip = ip

    def run(self):
        ok = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(PROBE_TIMEOUT_SEC)
            s.connect((self.ip, DEFAULT_PORT_TCP))
            s.close()
            ok = True
        except Exception:
            pass
        self.result.emit(ok)


class _UpdaterSignals(QObject):
    """Мост между callback'ами updater.py (фоновый поток) и слотами UI-потока."""

    update_found = pyqtSignal(str, str)   # (new_version, download_url)
    no_update    = pyqtSignal()
    check_error  = pyqtSignal(str)
    dl_progress  = pyqtSignal(int)        # 0..100
    dl_done      = pyqtSignal()
    dl_error     = pyqtSignal(str)


# ── Stylesheets ───────────────────────────────────────────────────────────────

_GLASS_CARD_SS = """
    QWidget#glassCard {
        background-color: rgba(22, 24, 35, 252);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 14px;
    }
    QLabel {
        color: #c8d0e0;
        background: transparent;
        border: none;
    }
    QLineEdit {
        background-color: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 7px;
        padding: 7px 11px;
        color: #dde3f0;
        font-size: 14px;
    }
    QLineEdit:focus { border-color: rgba(91,142,245,0.70); }
    QCheckBox { color: #9aa5bb; font-size: 13px; background: transparent; }
    QCheckBox::indicator {
        width: 16px; height: 16px;
        border: 1px solid rgba(255,255,255,0.20);
        border-radius: 4px;
        background: rgba(255,255,255,0.06);
    }
    QCheckBox::indicator:checked { background: #5b8ef5; border-color: #5b8ef5; }
    QProgressBar {
        background: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 5px;
        color: #c8d0e0;
        text-align: center;
        font-size: 12px;
    }
    QProgressBar::chunk {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #2ecc71, stop:1 #27ae60);
        border-radius: 4px;
    }
"""

_GLASS_ERROR_SS = """
    QFrame {
        background: rgba(192,57,43,0.18);
        border: 1px solid rgba(231,76,60,0.55);
        border-radius: 8px;
    }
"""

_BTN_PRIMARY_SS = (
    "QPushButton {"
    "  background-color: rgba(39,174,96,0.30);"
    "  color: #82e0aa;"
    "  border: 1px solid rgba(46,204,113,0.55);"
    "  border-radius: 8px;"
    "  font-size: 15px;"
    "  font-weight: bold;"
    "  padding: 10px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(39,174,96,0.50);"
    "  border-color: rgba(46,204,113,0.85);"
    "  color: #ffffff;"
    "}"
    "QPushButton:pressed { background-color: rgba(39,174,96,0.70); }"
)

_BTN_SECONDARY_SS = (
    "QPushButton {"
    "  background-color: rgba(41,128,185,0.28);"
    "  color: #7ec8e3;"
    "  border: 1px solid rgba(52,152,219,0.55);"
    "  border-radius: 8px;"
    "  font-size: 14px;"
    "  font-weight: bold;"
    "  padding: 9px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(52,152,219,0.45);"
    "  border-color: rgba(52,152,219,0.85);"
    "  color: #ffffff;"
    "}"
)

_BTN_SKIP_SS = (
    "QPushButton {"
    "  background-color: rgba(127,140,141,0.22);"
    "  color: #8899aa;"
    "  border: 1px solid rgba(127,140,141,0.40);"
    "  border-radius: 7px;"
    "  font-size: 13px;"
    "  font-weight: bold;"
    "  padding: 8px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(149,165,166,0.35);"
    "  color: #c8d0e0;"
    "}"
)


class _AppTitleBar(QWidget):
    """Кастомный title bar для безрамочных окон. Поддерживает перетаскивание и кнопку ✕."""

    def __init__(self, parent_widget: QWidget, title: str = ""):
        """
            :param parent_widget: родительское окно (QWidget)
            :param title: текст заголовка
        """
        super().__init__(parent_widget)
        self._win = parent_widget
        self._drag_pos = None
        self.setFixedHeight(38)
        self.setObjectName("appTitleBar")
        self.setStyleSheet("""
            QWidget#appTitleBar {
                background-color: rgba(14, 16, 26, 245);
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }
            QLabel {
                color: #cdd6f4; font-size: 13px; font-weight: bold;
                background: transparent; border: none; padding-left: 4px;
            }
            QPushButton {
                background: transparent; border: none; border-radius: 5px;
                color: #8890a0; font-size: 14px;
                min-width: 28px; max-width: 28px;
                min-height: 26px; max-height: 26px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.10); color: #cdd6f4; }
            QPushButton#appBtnClose:hover { background: #e74c3c; color: white; }
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(4)

        ico = QLabel()
        ico.setFixedSize(18, 18)
        try:
            ico.setPixmap(QIcon(resource_path("assets/icon/logo.ico")).pixmap(18, 18))
        except Exception:
            pass
        ico.setStyleSheet("background:transparent; border:none;")
        lay.addWidget(ico)

        self._lbl = QLabel(title)
        lay.addWidget(self._lbl, stretch=1)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("appBtnClose")
        btn_close.clicked.connect(parent_widget.close)
        lay.addWidget(btn_close)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)


class ConnectingScreen(QWidget):
    """Экран подключения: проверка обновлений → TCP-probe → открытие MainWindow.

    Никогда не вызывает close() — только hide(), чтобы event loop не завершился.
    show_login испускается ДО hide() — новое окно появляется до исчезновения этого.
    """

    show_login = pyqtSignal(str, str, str)   # ip, nick, avatar

    def __init__(self, ip: str, nick: str, avatar: str):
        """
            :param ip: IP-адрес сервера
            :param nick: никнейм пользователя
            :param avatar: имя файла аватарки
        """
        super().__init__()
        self.ip = ip
        self.nick = nick
        self.avatar = avatar
        self._worker: ConnectWorker | None = None
        self._main_window = None

        # При повторном нажатии «Повторить» не проверяем обновления снова
        self._update_checked: bool = False

        self._upd_sigs = _UpdaterSignals()
        self._upd_sigs.update_found.connect(self._on_update_found)
        self._upd_sigs.no_update.connect(self._on_no_update)
        self._upd_sigs.check_error.connect(self._on_update_check_error)
        self._upd_sigs.dl_progress.connect(self._on_dl_progress)
        self._upd_sigs.dl_done.connect(self._on_dl_done)
        self._upd_sigs.dl_error.connect(self._on_dl_error)

        self._build_ui()
        self._start_probe()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        from version import APP_NAME, APP_VERSION
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setFixedSize(420, 500)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(_GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        _tb = _AppTitleBar(self, f"{APP_NAME} v{APP_VERSION}")
        card_lay.addWidget(_tb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(sep)

        root = QVBoxLayout()
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setSpacing(14)
        root.setContentsMargins(36, 24, 36, 24)
        card_lay.addLayout(root)

        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setFixedHeight(120)
        self.lbl_img.setStyleSheet("background: transparent; border: none;")
        root.addWidget(self.lbl_img)

        self.lbl_status = QLabel("Проверка обновлений...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_status)

        self.lbl_ip = QLabel(f"Адрес:  {self.ip}")
        self.lbl_ip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_ip.setStyleSheet(
            "color: rgba(200,210,224,0.55); font-size: 13px; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_ip)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedHeight(22)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        self.frm_error = QFrame()
        self.frm_error.setStyleSheet(_GLASS_ERROR_SS)
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

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_retry = QPushButton("🔁  Повторить")
        self.btn_retry.setStyleSheet(_BTN_PRIMARY_SS)
        self.btn_retry.hide()
        self.btn_retry.clicked.connect(self._start_probe)
        btn_row.addWidget(self.btn_retry)

        self.btn_change_ip = QPushButton("✏️  Изменить IP")
        self.btn_change_ip.setStyleSheet(_BTN_SECONDARY_SS)
        self.btn_change_ip.hide()
        self.btn_change_ip.clicked.connect(self._on_change_ip)
        btn_row.addWidget(self.btn_change_ip)

        root.addLayout(btn_row)

        self.btn_skip_update = QPushButton("⏭️  Пропустить обновление и войти")
        self.btn_skip_update.setStyleSheet(_BTN_SKIP_SS)
        self.btn_skip_update.hide()
        self.btn_skip_update.clicked.connect(self._skip_update)
        root.addWidget(self.btn_skip_update)

        self._set_image("connecting")

    # ── Картинка ──────────────────────────────────────────────────────────────

    def _set_image(self, state: str):
        """Меняет картинку в зависимости от состояния подключения.

            :param state: 'connecting' — логотип, 'fail' — иконка ошибки
        """
        if state == "fail":
            candidates = [
                resource_path("assets/fail_connect.svg"),
                resource_path("assets/fail_connect.png"),
                resource_path("assets/icon/fail_connect.svg"),
                resource_path("assets/icon/fail_connect.png"),
                resource_path("assets/images/fail_connect.svg"),
                resource_path("assets/images/fail_connect.png"),
            ]
            for path in candidates:
                if os.path.exists(path):
                    px = QPixmap(path).scaled(
                        120, 120,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self.lbl_img.setPixmap(px)
                    self.lbl_img.setStyleSheet("")
                    self.lbl_img.setText("")
                    return
            self.lbl_img.setPixmap(QPixmap())
            self.lbl_img.setText("❌")
            self.lbl_img.setStyleSheet("font-size: 72px;")
        else:
            logo = resource_path("assets/icon/logo.ico")
            if os.path.exists(logo):
                px = QPixmap(logo).scaled(
                    90, 90,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.lbl_img.setPixmap(px)
                self.lbl_img.setStyleSheet("")
                self.lbl_img.setText("")
            else:
                self.lbl_img.setPixmap(QPixmap())
                self.lbl_img.setText("🔄")
                self.lbl_img.setStyleSheet("font-size: 72px;")

    # ── Точка входа каждой попытки подключения ────────────────────────────────

    def _start_probe(self):
        """Начинает попытку подключения.

        При первом вызове — сначала проверяет обновления.
        При повторных (retry) — сразу TCP-probe без проверки обновлений.
        """
        self.frm_error.hide()
        self.btn_retry.hide()
        self.btn_change_ip.hide()
        self.btn_skip_update.hide()
        self.progress_bar.hide()
        self.progress_bar.setValue(0)
        self.lbl_ip.setText(f"Адрес:  {self.ip}")
        self._set_image("connecting")

        if not self._update_checked:
            self._check_for_update_then_connect()
        else:
            self._do_tcp_probe()

    # ── Шаг 1: проверка обновлений ────────────────────────────────────────────

    def _check_for_update_then_connect(self):
        """Запускает проверку обновлений в фоне; результат приходит через сигналы."""
        self.lbl_status.setText("Проверка обновлений...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        sigs = self._upd_sigs
        check_for_updates_async(
            on_update_found=lambda v, u: sigs.update_found.emit(v, u),
            on_no_update=lambda: sigs.no_update.emit(),
            on_error=lambda msg: sigs.check_error.emit(msg),
        )

    def _on_no_update(self):
        """Обновлений нет — переходим к TCP-probe."""
        self._update_checked = True
        print("[Updater] Версия актуальна, продолжаем подключение.")
        self._do_tcp_probe()

    def _on_update_check_error(self, msg: str):
        """Ошибка проверки обновлений — тихо логируем и продолжаем подключение.

            :param msg: сообщение об ошибке
        """
        self._update_checked = True
        print(f"[Updater] Ошибка проверки (проигнорирована): {msg}")
        self._do_tcp_probe()

    # ── Шаг 2а: найдено обновление → скачиваем ───────────────────────────────

    def _on_update_found(self, new_version: str, download_url: str):
        """Показывает прогресс-бар и запускает скачивание обновления.

            :param new_version: номер новой версии
            :param download_url: URL файла обновления
        """
        self._update_checked = True
        print(f"[Updater] Найдена новая версия {new_version}, скачиваем...")

        self.lbl_status.setText(f"⬇️  Обновление {new_version}")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #c39ef5; "
            "background: transparent; border: none;"
        )
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        sigs = self._upd_sigs
        download_and_install(
            download_url=download_url,
            on_progress=lambda pct: sigs.dl_progress.emit(pct),
            on_done=lambda: sigs.dl_done.emit(),
            on_error=lambda msg: sigs.dl_error.emit(msg),
        )

    def _on_dl_progress(self, pct: int):
        """Обновляет прогресс-бар скачивания.

            :param pct: прогресс 0..100
        """
        self.progress_bar.setValue(pct)
        self.lbl_status.setText(f"⬇️  Скачивание обновления...  {pct}%")

    def _on_dl_done(self):
        """Скачивание завершено — показываем финальный статус перед рестартом."""
        self.progress_bar.setValue(100)
        self.lbl_status.setText("✅  Обновление установлено, перезапуск...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #82e0aa; "
            "background: transparent; border: none;"
        )

    def _on_dl_error(self, msg: str):
        """Ошибка скачивания — показываем ошибку и кнопку «Пропустить».

            :param msg: сообщение об ошибке
        """
        print(f"[Updater] Ошибка скачивания: {msg}")
        self.progress_bar.hide()
        self.lbl_status.setText("Ошибка обновления")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #ff8080; "
            "background: transparent; border: none;"
        )
        self.lbl_error.setText(f"⚠️  {msg}")
        self.frm_error.show()
        self.btn_skip_update.show()

    def _skip_update(self):
        """Пользователь пропускает обновление — сбрасываем UI и идём к TCP-probe."""
        self.frm_error.hide()
        self.btn_skip_update.hide()
        self.progress_bar.hide()
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        self._do_tcp_probe()

    # ── Шаг 2б: TCP-probe ────────────────────────────────────────────────────

    def _do_tcp_probe(self):
        """Запускает или перезапускает TCP probe к серверу."""
        self.lbl_status.setText("Подключение к серверу...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        self.lbl_ip.setText(f"Адрес:  {self.ip}")
        self.frm_error.hide()
        self.btn_retry.hide()
        self.btn_change_ip.hide()
        self.btn_skip_update.hide()
        self.progress_bar.hide()
        self._set_image("connecting")

        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(500)

        self._worker = ConnectWorker(self.ip)
        self._worker.result.connect(self._on_probe_result)
        self._worker.start()

    def _on_probe_result(self, ok: bool):
        """Обрабатывает результат TCP-probe.

            :param ok: True — сервер доступен, False — недоступен
        """
        if ok:
            self.lbl_status.setText("✅  Подключено!")
            self.lbl_status.setStyleSheet(
                "font-size: 17px; font-weight: bold; color: #82e0aa; "
                "background: transparent; border: none;"
            )
            QTimer.singleShot(300, self._open_main_window)
        else:
            self._set_image("fail")
            self.lbl_status.setText("Сервер недоступен")
            self.lbl_status.setStyleSheet(
                "font-size: 17px; font-weight: bold; color: #ff8080; "
                "background: transparent; border: none;"
            )
            self.lbl_error.setText(
                f"Не удалось подключиться к {self.ip}\n"
                "Проверьте адрес и убедитесь, что сервер запущен."
            )
            self.frm_error.show()
            self.btn_retry.show()
            self.btn_change_ip.show()

    def _open_main_window(self):
        """Открывает MainWindow и скрывает экран подключения."""
        self._main_window = MainWindow(self.ip, self.nick, self.avatar)
        self._main_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self._main_window.show()
        self.hide()

    def _on_change_ip(self):
        """Возвращает пользователя на экран логина для смены IP."""
        self.show_login.emit(self.ip, self.nick, self.avatar)
        self.hide()


class LoginWindow(QWidget):
    """Экран входа — первый запуск или возврат после неудачного подключения."""

    def __init__(self, ip: str = "127.0.0.1", nick: str = "User",
                 avatar: str = "1.svg", error_msg: str = ""):
        """
            :param ip: начальное значение поля IP
            :param nick: начальное значение никнейма
            :param avatar: имя файла аватарки
            :param error_msg: сообщение об ошибке для показа при открытии
        """
        super().__init__()
        self.current_avatar = avatar
        self._connecting_screen: ConnectingScreen | None = None

        self._build_ui(ip, nick)

        if error_msg:
            self._show_error(error_msg)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self, ip: str, nick: str):
        from version import APP_NAME, APP_VERSION
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — Вход")
        self.setFixedSize(380, 580)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(_GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        _tb = _AppTitleBar(self, f"🔗  {APP_NAME} — Вход")
        card_lay.addWidget(_tb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(sep)

        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(32, 20, 32, 24)
        layout.setSpacing(10)
        card_lay.addLayout(layout)

        self.avatar_lbl = QLabel()
        self.avatar_lbl.setFixedSize(110, 110)
        self.avatar_lbl.setStyleSheet(
            "border: 2px solid rgba(91,142,245,0.70);"
            "border-radius: 55px;"
            "background: rgba(255,255,255,0.05);"
        )
        self.avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.avatar_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_av = QPushButton("🖼  Выбрать аватарку")
        btn_av.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_av.setStyleSheet("""
            QPushButton {
                background-color: rgba(91,142,245,0.16);
                color: #8ab0f5;
                border: 1px solid rgba(91,142,245,0.40);
                border-radius: 6px;
                font-size: 13px;
                padding: 5px 14px;
            }
            QPushButton:hover {
                background-color: rgba(91,142,245,0.28);
                border-color: rgba(91,142,245,0.70);
                color: #ffffff;
            }
        """)
        btn_av.clicked.connect(self._open_avatar_picker)
        layout.addWidget(btn_av, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addSpacing(6)

        lbl_ip = QLabel("IP-адрес сервера")
        lbl_ip.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #8899bb; "
            "background: transparent; border: none;"
        )
        layout.addWidget(lbl_ip)
        self.ip_in = QLineEdit(ip)
        self.ip_in.setPlaceholderText("например: 192.168.1.100")
        layout.addWidget(self.ip_in)

        lbl_nick = QLabel("Никнейм")
        lbl_nick.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #8899bb; "
            "background: transparent; border: none;"
        )
        layout.addWidget(lbl_nick)
        self.nick_in = QLineEdit(nick)
        self.nick_in.setPlaceholderText("User")
        layout.addWidget(self.nick_in)

        self.cb_save = QCheckBox("Сохранить данные для следующего запуска")
        self.cb_save.setChecked(True)
        layout.addWidget(self.cb_save)

        layout.addSpacing(4)

        self.frm_error = QFrame()
        self.frm_error.setStyleSheet(_GLASS_ERROR_SS)
        err_lay = QVBoxLayout(self.frm_error)
        err_lay.setContentsMargins(12, 8, 12, 8)
        self.lbl_error = QLabel()
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_error.setStyleSheet(
            "color: #ff8080; font-size: 13px; font-weight: 500; "
            "background: transparent; border: none;"
        )
        err_lay.addWidget(self.lbl_error)
        self.frm_error.hide()
        layout.addWidget(self.frm_error)

        self.btn_go = QPushButton("Войти")
        self.btn_go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_go.setStyleSheet(_BTN_PRIMARY_SS)
        self.btn_go.clicked.connect(self._on_login)
        layout.addWidget(self.btn_go)

        self._refresh_avatar()

    # ── Аватарка ──────────────────────────────────────────────────────────────

    def _open_avatar_picker(self):
        """Открывает диалог выбора аватарки."""
        d = AvatarSelector(self)
        if d.exec():
            self.current_avatar = d.selected_avatar
            self._refresh_avatar()

    def _refresh_avatar(self):
        """Обновляет отображение аватарки по текущему current_avatar."""
        p = resource_path(f"assets/avatars/{self.current_avatar}")
        px = QIcon(p).pixmap(100, 100) if os.path.exists(p) else QIcon().pixmap(0, 0)
        self.avatar_lbl.setPixmap(px)

    # ── Ошибки ────────────────────────────────────────────────────────────────

    def _show_error(self, msg: str):
        """Показывает блок ошибки с текстом.

            :param msg: текст ошибки
        """
        self.lbl_error.setText(msg)
        self.frm_error.show()

    def _hide_error(self):
        """Скрывает блок ошибки."""
        self.frm_error.hide()
        self.lbl_error.clear()

    # ── Логин ─────────────────────────────────────────────────────────────────

    def _on_login(self):
        """Обрабатывает нажатие кнопки «Войти»."""
        ip = self.ip_in.text().strip()
        nick = self.nick_in.text().strip() or "User"

        if not ip:
            self._show_error("⚠️  Введите IP-адрес сервера")
            return

        self._hide_error()

        if self.cb_save.isChecked():
            save_config(ip, nick, self.current_avatar)

        # hide() — окно остаётся в памяти, вернётся если ConnectingScreen испустит show_login
        self.hide()
        self._open_connecting(ip, nick, self.current_avatar)

    def _open_connecting(self, ip: str, nick: str, avatar: str):
        """Создаёт и показывает экран подключения.

            :param ip: IP-адрес сервера
            :param nick: никнейм пользователя
            :param avatar: имя файла аватарки
        """
        # Сохраняем в атрибут — GC не уберёт объект после return
        self._connecting_screen = ConnectingScreen(ip, nick, avatar)
        self._connecting_screen.setWindowIcon(
            QIcon(resource_path("assets/icon/logo.ico"))
        )
        self._connecting_screen.show_login.connect(self._on_return_from_connecting)
        self._connecting_screen.show()

    def _on_return_from_connecting(self, ip: str, nick: str, avatar: str):
        """Обновляет поля и показывает себя при возврате с экрана подключения.

            :param ip: IP-адрес из ConnectingScreen
            :param nick: никнейм из ConnectingScreen
            :param avatar: аватарка из ConnectingScreen
        """
        self.ip_in.setText(ip)
        self.nick_in.setText(nick)
        self.current_avatar = avatar
        self._refresh_avatar()
        self._show_error(
            f"⚠️  Сервер недоступен: {ip}\n"
            "Проверьте адрес и нажмите «Войти»."
        )
        self.show()


# ── Точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import threading as _threading

    _orig_thread_excepthook = getattr(_threading, 'excepthook', None)

    def _thread_excepthook(args):
        import traceback as _tb
        msg = "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        print(f"[CRASH] Исключение в потоке '{args.thread.name}':\n{msg}", flush=True)
        with open("crash_python.log", "a", encoding="utf-8") as _f:
            _f.write(f"Thread '{args.thread.name}':\n{msg}")
        if _orig_thread_excepthook:
            _orig_thread_excepthook(args)

    _threading.excepthook = _thread_excepthook

    # QSurfaceFormat должен быть установлен ДО создания QApplication
    _gl_fmt = QSurfaceFormat()
    _gl_fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    _gl_fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(_gl_fmt)

    app = QApplication(sys.argv)

    # Держим ссылки на оба возможных окна — GC не уберёт объекты после if/else
    _login_window:   LoginWindow     | None = None
    _connect_screen: ConnectingScreen | None = None

    config = load_config()

    if config:
        ip     = config.get("ip",     "127.0.0.1")
        nick   = config.get("nick",   "User")
        avatar = config.get("avatar", "1.svg")

        _connect_screen = ConnectingScreen(ip, nick, avatar)
        _connect_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

        def _fallback_to_login(f_ip: str, f_nick: str, f_avatar: str):
            global _login_window
            _login_window = LoginWindow(
                ip=f_ip, nick=f_nick, avatar=f_avatar,
                error_msg=(
                    f"⚠️  Сервер недоступен: {f_ip}\n"
                    "Измените адрес и нажмите «Войти»."
                )
            )
            _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
            _login_window.show()

        _connect_screen.show_login.connect(_fallback_to_login)
        _connect_screen.show()

    else:
        _login_window = LoginWindow()
        _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        _login_window.show()

    sys.exit(app.exec())