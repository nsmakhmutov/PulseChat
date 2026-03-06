import os
import json
import ctypes
import sys
import socket
import traceback
import faulthandler

# ── CRASH DIAGNOSTICS ────────────────────────────────────────────────────────
# faulthandler пишет нативный C-стектрейс при SIGSEGV / STATUS_STACK_BUFFER_OVERRUN
# прямо в файл — даже если Python уже не работает.
_crash_log = open("crash_native.log", "w", buffering=1)
faulthandler.enable(file=_crash_log)

# Глобальный перехват необработанных Python-исключений → в файл + консоль
def _global_excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[CRASH] Необработанное исключение:\n{msg}", flush=True)
    with open("crash_python.log", "a", encoding="utf-8") as f:
        f.write(msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _global_excepthook
print("[DEBUG] faulthandler активирован → crash_native.log", flush=True)

def resource_path(relative_path):
    """ Получает абсолютный путь к ресурсам, работает для dev и для PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ── Добавляем папку проекта в поиск DLL (opus.dll, rnnoise.dll) ──────────────
# Делаем это ДО любых импортов, которые грузят нативные библиотеки.
_project_dir = os.path.dirname(os.path.abspath(__file__))
os.add_dll_directory(_project_dir)
try:
    os.add_dll_directory(sys._MEIPASS)
except Exception:
    pass

# Сообщаем системе, что мы поддерживаем DPI (High DPI Aware)
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

import shutil
from config import resource_path, DEFAULT_PORT_TCP, USER_CONFIG_PATH, KNOWN_USERS_PATH
from ui_main import MainWindow
from ui_dialogs import AvatarSelector
from updater import check_for_updates_async, download_and_install


# ══════════════════════════════════════════════════════════════════════════════
# Константы
# ══════════════════════════════════════════════════════════════════════════════
PROBE_TIMEOUT_SEC = 3.0


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════════
def migrate_old_configs() -> None:
    """
    Единожды переносит старые JSON-файлы из корня приложения в AppData.

    Миграция срабатывает только если:
      - старый файл существует в папке запуска (install_dir / CWD)
      - новый файл в AppData ещё НЕ существует (не перезаписываем!)
    После переноса пользователь ничего не замечает.
    """
    # Определяем папку, откуда запущено приложение (frozen или dev)
    if getattr(__import__('sys'), 'frozen', False):
        import sys as _sys
        old_dir = os.path.dirname(_sys.executable)
    else:
        old_dir = os.path.abspath(".")

    for old_name, new_path in (
        ("user_config.json", USER_CONFIG_PATH),
        ("known_users.json", KNOWN_USERS_PATH),
    ):
        old_path = os.path.join(old_dir, old_name)
        if os.path.exists(old_path) and not os.path.exists(new_path):
            try:
                shutil.copy2(old_path, new_path)
                print(f"[Migration] Перенесён {old_name} → AppData")
            except Exception as e:
                print(f"[Migration] Ошибка переноса {old_name}: {e}")


def load_config() -> dict | None:
    migrate_old_configs()           # однократная миграция при первом запуске
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_config(ip: str, nick: str, avatar: str) -> None:
    try:
        with open(USER_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump({"ip": ip, "nick": nick, "avatar": avatar}, f)
    except Exception as e:
        print(f"[Config] Не удалось сохранить конфиг: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Фоновый поток: TCP probe
# ══════════════════════════════════════════════════════════════════════════════
class ConnectWorker(QThread):
    result = pyqtSignal(bool)

    def __init__(self, ip: str):
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


# ══════════════════════════════════════════════════════════════════════════════
# Сигналы апдейтера (thread-safe: фоновый поток → Qt UI-поток)
# ══════════════════════════════════════════════════════════════════════════════
class _UpdaterSignals(QObject):
    """
    Мост между callback'ами updater.py (вызываются из фонового потока)
    и слотами ConnectingScreen (должны работать в UI-потоке).

    PyQt6 гарантирует, что сигналы, испущенные из любого потока,
    доставляются в UI-поток через event loop — никаких мьютексов не нужно.
    """
    update_found = pyqtSignal(str, str)   # (new_version, download_url)
    no_update    = pyqtSignal()
    check_error  = pyqtSignal(str)        # message
    dl_progress  = pyqtSignal(int)        # 0..100
    dl_done      = pyqtSignal()
    dl_error     = pyqtSignal(str)        # message


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные UI-классы (единый тёмный стеклянный дизайн)
# ══════════════════════════════════════════════════════════════════════════════

# Общий стеклянный stylesheet для карточки
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
    """
    Кастомный title bar для безрамочных QWidget (LoginWindow / ConnectingScreen).
    Поддерживает перетаскивание и кнопку закрытия.
    """

    def __init__(self, parent_widget, title: str = ""):
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


# ══════════════════════════════════════════════════════════════════════════════
# Экран подключения
# ══════════════════════════════════════════════════════════════════════════════
class ConnectingScreen(QWidget):
    """
    Показывается пока идёт probe к серверу.

    КЛЮЧЕВЫЕ ПРАВИЛА (чтобы приложение не закрывалось):
      - Никогда не вызываем close() первым.
        Всегда только hide() — окно остаётся в памяти Qt,
        event loop не завершается.
      - show_login испускается ДО hide(), чтобы новое окно
        успело появиться раньше чем это исчезнет.

    НОВЫЙ ПОТОК (auto-update):
      _start_probe()
        └─► _check_for_update_then_connect()
              ├─ on_update_found → _on_update_found() → _start_download()
              │     ├─ on_progress → progressbar
              │     ├─ on_done    → updater вызывает sys.exit(0)
              │     └─ on_error   → показываем ошибку + кнопку «Пропустить»
              ├─ on_no_update  → _do_tcp_probe()   (прежняя логика)
              └─ on_error      → _do_tcp_probe()   (fail-safe: не блокируем)
    """
    show_login = pyqtSignal(str, str, str)   # ip, nick, avatar

    def __init__(self, ip: str, nick: str, avatar: str):
        super().__init__()
        self.ip     = ip
        self.nick   = nick
        self.avatar = avatar
        self._worker: ConnectWorker | None = None
        self._main_window = None  # держим ссылку — GC не убьёт MainWindow

        # Флаг: проверка обновлений уже выполнялась в этой сессии.
        # При повторном нажатии «Повторить» (retry) мы НЕ проверяем ещё раз —
        # пользователь просто ждёт сервер, не нужно снова тратить ~1-2 сек.
        self._update_checked: bool = False

        # Сигналы для безопасного взаимодействия updater-потока с UI
        self._upd_sigs = _UpdaterSignals()
        self._upd_sigs.update_found.connect(self._on_update_found)
        self._upd_sigs.no_update.connect(self._on_no_update)
        self._upd_sigs.check_error.connect(self._on_update_check_error)
        self._upd_sigs.dl_progress.connect(self._on_dl_progress)
        self._upd_sigs.dl_done.connect(self._on_dl_done)
        self._upd_sigs.dl_error.connect(self._on_dl_error)

        self._build_ui()
        self._start_probe()

    # ──────────────────────────────────────────────────────────────────────────
    # UI
    # ──────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        from version import APP_NAME, APP_VERSION
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.setFixedSize(420, 500)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ── Корневой layout (прозрачный фон) ──────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Карточка ──────────────────────────────────────────────────────────
        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(_GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # Title bar (перетаскивание + кнопка ✕)
        _tb = _AppTitleBar(self, f"{APP_NAME} v{APP_VERSION}")
        card_lay.addWidget(_tb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(sep)

        # ── Контент ───────────────────────────────────────────────────────────
        root = QVBoxLayout()
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setSpacing(14)
        root.setContentsMargins(36, 24, 36, 24)
        card_lay.addLayout(root)

        # ── Картинка (меняется в зависимости от состояния) ────────────────────
        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setFixedHeight(120)
        self.lbl_img.setStyleSheet("background: transparent; border: none;")
        root.addWidget(self.lbl_img)

        # ── Статус ────────────────────────────────────────────────────────────
        self.lbl_status = QLabel("Проверка обновлений...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_status)

        # ── IP (серым, мелко) ──────────────────────────────────────────────────
        self.lbl_ip = QLabel(f"Адрес:  {self.ip}")
        self.lbl_ip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_ip.setStyleSheet(
            "color: rgba(200,210,224,0.55); font-size: 13px; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_ip)

        # ── Прогресс-бар (скачивание обновления) ──────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedHeight(22)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        # ── Блок ошибки ────────────────────────────────────────────────────────
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

        # ── Кнопки ────────────────────────────────────────────────────────────
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

        # ── Кнопка «Пропустить обновление» ────────────────────────────────────
        self.btn_skip_update = QPushButton("⏭️  Пропустить обновление и войти")
        self.btn_skip_update.setStyleSheet(_BTN_SKIP_SS)
        self.btn_skip_update.hide()
        self.btn_skip_update.clicked.connect(self._skip_update)
        root.addWidget(self.btn_skip_update)

        # Начальная картинка — логотип
        self._set_image("connecting")

    # ──────────────────────────────────────────────────────────────────────────
    # Картинка
    # ──────────────────────────────────────────────────────────────────────────
    def _set_image(self, state: str):
        """
        state = "connecting" | "fail"
        Для fail ищет assets/fail_connect.svg (или .png) в нескольких
        стандартных местах. Если файла нет — показывает эмодзи-заглушку.
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
            # Файла нет — эмодзи fallback
            self.lbl_img.setPixmap(QPixmap())
            self.lbl_img.setText("❌")
            self.lbl_img.setStyleSheet("font-size: 72px;")

        else:  # connecting
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

    # ──────────────────────────────────────────────────────────────────────────
    # Главная точка входа (вызывается при старте и при нажатии «Повторить»)
    # ──────────────────────────────────────────────────────────────────────────
    def _start_probe(self):
        """
        Точка входа для каждой попытки подключения.

        Если обновления ещё не проверялись в этой сессии — сначала проверяем.
        При повторных попытках (retry после падения сервера) проверку пропускаем
        и сразу идём к TCP-probe, чтобы не раздражать пользователя лишней паузой.
        """
        # Сбрасываем UI в исходное состояние
        self.frm_error.hide()
        self.btn_retry.hide()
        self.btn_change_ip.hide()
        self.btn_skip_update.hide()
        self.progress_bar.hide()
        self.progress_bar.setValue(0)
        self.lbl_ip.setText(f"Адрес:  {self.ip}")
        self._set_image("connecting")

        if not self._update_checked:
            # Первый запуск — проверяем обновления перед подключением
            self._check_for_update_then_connect()
        else:
            # Повторная попытка — сразу к TCP-probe
            self._do_tcp_probe()

    # ──────────────────────────────────────────────────────────────────────────
    # ШАГ 1: Проверка обновлений
    # ──────────────────────────────────────────────────────────────────────────
    def _check_for_update_then_connect(self):
        """Запускает проверку обновлений в фоне. Результат придёт через сигналы."""
        self.lbl_status.setText("Проверка обновлений...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; background: transparent; border: none;"
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
        """
        Ошибка при проверке обновлений (нет сети до GitHub, таймаут и т.д.).
        Не блокируем пользователя — тихо логируем и идём дальше.
        """
        self._update_checked = True
        print(f"[Updater] Ошибка проверки (проигнорирована): {msg}")
        self._do_tcp_probe()

    # ──────────────────────────────────────────────────────────────────────────
    # ШАГ 2а: Найдено обновление → скачиваем
    # ──────────────────────────────────────────────────────────────────────────
    def _on_update_found(self, new_version: str, download_url: str):
        """Новая версия найдена — показываем статус и запускаем скачивание."""
        self._update_checked = True
        print(f"[Updater] Найдена новая версия {new_version}, скачиваем...")

        self.lbl_status.setText(f"⬇️  Обновление {new_version}")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #c39ef5; background: transparent; border: none;"
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
        """Обновляем прогресс-бар скачивания."""
        self.progress_bar.setValue(pct)
        # Показываем мегабайты только если нет — оставим числовой %
        self.lbl_status.setText(f"⬇️  Скачивание обновления...  {pct}%")

    def _on_dl_done(self):
        """
        Скачивание завершено — updater сейчас запустит bat-лончер и вызовет
        sys.exit(0). Показываем финальный статус на случай небольшой задержки.
        """
        self.progress_bar.setValue(100)
        self.lbl_status.setText("✅  Обновление установлено, перезапуск...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #82e0aa; background: transparent; border: none;"
        )

    def _on_dl_error(self, msg: str):
        """
        Ошибка скачивания/установки — показываем ошибку и даём пользователю
        войти без обновления через кнопку «Пропустить».
        """
        print(f"[Updater] Ошибка скачивания: {msg}")
        self.progress_bar.hide()
        self.lbl_status.setText("Ошибка обновления")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #ff8080; background: transparent; border: none;"
        )
        self.lbl_error.setText(f"⚠️  {msg}")
        self.frm_error.show()
        self.btn_skip_update.show()

    def _skip_update(self):
        """
        Пользователь нажал «Пропустить обновление» — сбрасываем UI и
        переходим сразу к TCP-probe (update_checked уже True, retry не будет
        снова лезть в updater).
        """
        self.frm_error.hide()
        self.btn_skip_update.hide()
        self.progress_bar.hide()
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; background: transparent; border: none;"
        )
        self._do_tcp_probe()

    # ──────────────────────────────────────────────────────────────────────────
    # ШАГ 2б: TCP probe (прежняя логика, без изменений)
    # ──────────────────────────────────────────────────────────────────────────
    def _do_tcp_probe(self):
        """Запускает или перезапускает TCP probe (прежняя логика подключения)."""
        self.lbl_status.setText("Подключение к серверу...")
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; background: transparent; border: none;"
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
        if ok:
            self.lbl_status.setText("✅  Подключено!")
            self.lbl_status.setStyleSheet(
                "font-size: 17px; font-weight: bold; color: #82e0aa; background: transparent; border: none;"
            )
            QTimer.singleShot(300, self._open_main_window)
        else:
            self._set_image("fail")
            self.lbl_status.setText("Сервер недоступен")
            self.lbl_status.setStyleSheet(
                "font-size: 17px; font-weight: bold; color: #ff8080; background: transparent; border: none;"
            )
            self.lbl_error.setText(
                f"Не удалось подключиться к {self.ip}\n"
                "Проверьте адрес и убедитесь, что сервер запущен."
            )
            self.frm_error.show()
            self.btn_retry.show()
            self.btn_change_ip.show()

    def _open_main_window(self):
        # ── NVENC monkey-patch ────────────────────────────────────────────────
        # Должен вызываться ОДИН РАЗ до создания первого RTCPeerConnection.
        # Вызов именно здесь гарантирует:
        #   1. GPU/драйвер уже инициализированы (мы прошли загрузку ОС).
        #   2. aiortc ещё не создавал ни одного H264Encoder.
        #   3. DLL (opus.dll, rnnoise.dll, ffmpeg) уже добавлены через add_dll_directory().
        # При отсутствии h264_nvenc функция тихо падает на libx264 (без исключения).
        try:
            from video_engine import patch_aiortc_nvenc
            patch_aiortc_nvenc()
        except Exception as e:
            print(f"[Main] patch_aiortc_nvenc() error (non-fatal): {e}")

        self._main_window = MainWindow(self.ip, self.nick, self.avatar)
        self._main_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self._main_window.show()
        # ✅ hide() — Qt не считает это закрытием последнего окна
        self.hide()

    def _on_change_ip(self):
        """
        ✅ ПОРЯДОК КРИТИЧЕН:
          1. Сначала emit — получатель (LoginWindow) откроется и станет видимым.
          2. Потом hide() — только после появления нового окна.
          hide() а не close() — Qt не завершает event loop.
        """
        self.show_login.emit(self.ip, self.nick, self.avatar)
        self.hide()


# ══════════════════════════════════════════════════════════════════════════════
# Окно входа
# ══════════════════════════════════════════════════════════════════════════════
class LoginWindow(QWidget):
    """
    Показывается:
      1. При первом запуске (нет user_config.json).
      2. Когда ConnectingScreen провалился и пользователь нажал «Изменить IP».
    """

    def __init__(self, ip: str = "127.0.0.1", nick: str = "User",
                 avatar: str = "1.svg", error_msg: str = ""):
        super().__init__()
        self.current_avatar = avatar
        # ✅ Обязательная ссылка на ConnectingScreen — GC не уберёт объект
        self._connecting_screen: ConnectingScreen | None = None

        self._build_ui(ip, nick)

        if error_msg:
            self._show_error(error_msg)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self, ip: str, nick: str):
        from version import APP_NAME, APP_VERSION
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — Вход")
        self.setFixedSize(380, 580)
        self.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # ── Корневой layout (прозрачный) ──────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Карточка ──────────────────────────────────────────────────────────
        card = QWidget()
        card.setObjectName("glassCard")
        card.setStyleSheet(_GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # Title bar
        _tb = _AppTitleBar(self, f"🔗  {APP_NAME} — Вход")
        card_lay.addWidget(_tb)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(sep)

        # ── Контент ───────────────────────────────────────────────────────────
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(32, 20, 32, 24)
        layout.setSpacing(10)
        card_lay.addLayout(layout)

        # ── Аватарка ──────────────────────────────────────────────────────────
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

        # ── IP ────────────────────────────────────────────────────────────────
        lbl_ip = QLabel("IP-адрес сервера")
        lbl_ip.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #8899bb; "
            "background: transparent; border: none;"
        )
        layout.addWidget(lbl_ip)
        self.ip_in = QLineEdit(ip)
        self.ip_in.setPlaceholderText("например: 192.168.1.100")
        layout.addWidget(self.ip_in)

        # ── Ник ───────────────────────────────────────────────────────────────
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

        # ── Блок ошибки ────────────────────────────────────────────────────────
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

        # ── Кнопка входа ──────────────────────────────────────────────────────
        self.btn_go = QPushButton("Войти")
        self.btn_go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_go.setStyleSheet(_BTN_PRIMARY_SS)
        self.btn_go.clicked.connect(self._on_login)
        layout.addWidget(self.btn_go)

        self._refresh_avatar()

    # ------------------------------------------------------------------
    # Аватарка
    # ------------------------------------------------------------------
    def _open_avatar_picker(self):
        d = AvatarSelector(self)
        if d.exec():
            self.current_avatar = d.selected_avatar
            self._refresh_avatar()

    def _refresh_avatar(self):
        p = resource_path(f"assets/avatars/{self.current_avatar}")
        px = QIcon(p).pixmap(100, 100) if os.path.exists(p) else QIcon().pixmap(0, 0)
        self.avatar_lbl.setPixmap(px)

    # ------------------------------------------------------------------
    # Ошибки
    # ------------------------------------------------------------------
    def _show_error(self, msg: str):
        self.lbl_error.setText(msg)
        self.frm_error.show()

    def _hide_error(self):
        self.frm_error.hide()
        self.lbl_error.clear()

    # ------------------------------------------------------------------
    # Логин
    # ------------------------------------------------------------------
    def _on_login(self):
        ip   = self.ip_in.text().strip()
        nick = self.nick_in.text().strip() or "User"

        if not ip:
            self._show_error("⚠️  Введите IP-адрес сервера")
            return

        self._hide_error()

        if self.cb_save.isChecked():
            save_config(ip, nick, self.current_avatar)

        # ✅ hide() — не close(). LoginWindow живёт в памяти,
        # вернётся если ConnectingScreen снова испустит show_login.
        self.hide()
        self._open_connecting(ip, nick, self.current_avatar)

    def _open_connecting(self, ip: str, nick: str, avatar: str):
        # ✅ self._connecting_screen — не локальная переменная!
        # Сохраняем в атрибут, иначе GC убьёт объект сразу после return.
        self._connecting_screen = ConnectingScreen(ip, nick, avatar)
        self._connecting_screen.setWindowIcon(
            QIcon(resource_path("assets/icon/logo.ico"))
        )
        self._connecting_screen.show_login.connect(self._on_return_from_connecting)
        self._connecting_screen.show()

    def _on_return_from_connecting(self, ip: str, nick: str, avatar: str):
        """ConnectingScreen вернул управление — обновляем поля и показываем себя."""
        self.ip_in.setText(ip)
        self.nick_in.setText(nick)
        self.current_avatar = avatar
        self._refresh_avatar()
        self._show_error(
            f"⚠️  Сервер недоступен: {ip}\n"
            "Проверьте адрес и нажмите «Войти»."
        )
        # ✅ show() — окно уже живое, просто было скрыто через hide()
        self.show()


# ══════════════════════════════════════════════════════════════════════════════
# Точка входа
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # ── Дамп аудио-устройств до создания QApplication ───────────────────────
    # Если PortAudio крашится уже при query_devices() — увидим это в логе.
    try:
        import sounddevice as _sd
        print("[DEBUG] Аудио-устройства системы:", flush=True)
        for _i, _d in enumerate(_sd.query_devices()):
            _api = _sd.query_hostapis(_d['hostapi'])['name']
            print(f"  [{_i:2d}] IN={_d['max_input_channels']} OUT={_d['max_output_channels']} "
                  f"| {_d['name']} ({_api})", flush=True)
        print(f"[DEBUG] Дефолтное устройство: IN={_sd.default.device[0]}, OUT={_sd.default.device[1]}", flush=True)
    except Exception as _ex:
        print(f"[DEBUG] query_devices() упал: {_ex}", flush=True)

    # Перехват исключений в дочерних (не-Qt) потоках
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
    print("[DEBUG] threading.excepthook установлен", flush=True)

    # ✅ КРИТИЧНО: QSurfaceFormat ДО создания QApplication
    _gl_fmt = QSurfaceFormat()
    _gl_fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    _gl_fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(_gl_fmt)

    app = QApplication(sys.argv)

    # ✅ Глобальные переменные — держим ссылки на оба возможных окна.
    # Без этого Python GC уничтожит объект после выхода из блока if/else,
    # Qt получит висячий указатель и окно мгновенно закроется.
    _login_window:   LoginWindow    | None = None
    _connect_screen: ConnectingScreen | None = None

    config = load_config()

    if config:
        # ── Конфиг найден → авто-коннект ────────────────────────────────
        ip     = config.get("ip",     "127.0.0.1")
        nick   = config.get("nick",   "User")
        avatar = config.get("avatar", "1.svg")

        _connect_screen = ConnectingScreen(ip, nick, avatar)
        _connect_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

        def _fallback_to_login(f_ip: str, f_nick: str, f_avatar: str):
            """
            ✅ ИСПРАВЛЕНО: LoginWindow сохраняется в глобальную переменную,
            а не в локальную — иначе GC убьёт объект после выхода из функции.
            """
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
        # ── Первый запуск → форма логина ────────────────────────────────
        _login_window = LoginWindow()
        _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        _login_window.show()

    sys.exit(app.exec())