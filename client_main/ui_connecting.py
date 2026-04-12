# ui_connecting.py
# ──────────────────────────────────────────────────────────────────────────────
# Экран подключения к серверу с обязательной проверкой обновлений.
#
# Поток работы:
#   _start_probe()
#     └─► _check_for_update_then_connect()   (всегда первым делом)
#           ├─ on_update_found → скачиваем → PS1 перезапустит приложение
#           ├─ on_no_update  → _do_tcp_probe()
#           └─ on_error      → _do_tcp_probe()   (fail-safe)
#     └─► _do_tcp_probe()                    (после проверки обновлений)
# ──────────────────────────────────────────────────────────────────────────────

import os
import socket

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame,
    QLabel, QPushButton, QProgressBar,
)
from PyQt6.QtCore import Qt, QTimer, QThread, QObject, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap

from config import resource_path, DEFAULT_PORT_TCP
from .ui_styles import GLASS_CARD_SS, GLASS_ERROR_SS, BTN_PRIMARY_SS, BTN_SECONDARY_SS
from .ui_titlebar import AppTitleBar


# ══════════════════════════════════════════════════════════════════════════════
# Константы
# ══════════════════════════════════════════════════════════════════════════════

PROBE_TIMEOUT_SEC = 3.0


# ══════════════════════════════════════════════════════════════════════════════
# ConnectWorker — TCP probe в фоне
# ══════════════════════════════════════════════════════════════════════════════

class ConnectWorker(QThread):
    """Проверяет TCP-доступность сервера. Не блокирует UI."""
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
# _UpdaterSignals — мост updater-поток → Qt UI-поток
# ══════════════════════════════════════════════════════════════════════════════

class _UpdaterSignals(QObject):
    """
    PyQt6 гарантирует, что сигналы, испущенные из любого потока,
    доставляются в UI-поток через event loop — никаких мьютексов не нужно.
    """
    update_found = pyqtSignal(str, int, int)   # (new_version, n_files, total_bytes)
    no_update    = pyqtSignal()
    check_error  = pyqtSignal(str)   # message
    dl_progress  = pyqtSignal(int, str)   # (0..100, status_text)
    dl_done      = pyqtSignal()
    dl_error     = pyqtSignal(str)   # message


# ══════════════════════════════════════════════════════════════════════════════
# ConnectingScreen
# ══════════════════════════════════════════════════════════════════════════════

class ConnectingScreen(QWidget):
    """
    Показывается пока идёт probe к серверу.

    КЛЮЧЕВЫЕ ПРАВИЛА:
      - Никогда не вызываем close() первым — только hide().
        Qt не считает hide() закрытием последнего окна → event loop не завершается.
      - show_login испускается ДО hide(), чтобы новое окно появилось раньше.
    """
    show_login = pyqtSignal(str, str, str)   # ip, nick, avatar

    def __init__(
        self,
        ip: str,
        nick: str,
        avatar: str,
    ):
        super().__init__()
        self.ip     = ip
        self.nick   = nick
        self.avatar = avatar

        self._worker: ConnectWorker | None = None
        self._main_window = None   # держим ссылку — GC не убьёт MainWindow

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
        root.setSpacing(14)
        root.setContentsMargins(36, 24, 36, 24)
        card_lay.addLayout(root)

        # Изображение состояния
        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setFixedHeight(120)
        self.lbl_img.setStyleSheet("background: transparent; border: none;")
        root.addWidget(self.lbl_img)

        # Статус
        self.lbl_status = QLabel("Проверка обновлений...")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet(
            "font-size: 17px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_status)

        # IP (мелко)
        self.lbl_ip = QLabel(f"Адрес:  {self.ip}")
        self.lbl_ip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_ip.setStyleSheet(
            "color: rgba(200,210,224,0.55); font-size: 13px; "
            "background: transparent; border: none;"
        )
        root.addWidget(self.lbl_ip)

        # Прогресс-бар скачивания
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedHeight(22)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        # Блок ошибки
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

        # Кнопки (Повторить / Изменить IP)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_retry = QPushButton("🔁  Повторить")
        self.btn_retry.setStyleSheet(BTN_PRIMARY_SS)
        self.btn_retry.hide()
        self.btn_retry.clicked.connect(self._start_probe)
        btn_row.addWidget(self.btn_retry)

        self.btn_change_ip = QPushButton("✏️  Изменить IP")
        self.btn_change_ip.setStyleSheet(BTN_SECONDARY_SS)
        self.btn_change_ip.hide()
        self.btn_change_ip.clicked.connect(self._on_change_ip)
        btn_row.addWidget(self.btn_change_ip)

        root.addLayout(btn_row)

        self._set_image("connecting")

    # ── Изображение состояния ─────────────────────────────────────────────────

    def _set_image(self, state: str):
        """state = 'connecting' | 'fail'"""
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

    # ── Главная точка входа ───────────────────────────────────────────────────

    def _start_probe(self):
        """
        Вызывается при старте и при «Повторить».
        Первый запуск — проверяем обновления, потом TCP probe.
        Повторная попытка — сразу TCP probe (не раздражаем пользователя).
        """
        self.frm_error.hide()
        self.btn_retry.hide()
        self.btn_change_ip.hide()
        self.progress_bar.hide()
        self.progress_bar.setValue(0)
        self.lbl_ip.setText(f"Адрес:  {self.ip}")
        self._set_image("connecting")

        # Обновление ОБЯЗАТЕЛЬНО — всегда проверяем первым делом
        self._check_for_update_then_connect()

    # ── Шаг 1: Проверка обновлений ───────────────────────────────────────────

    def _check_for_update_then_connect(self):
        self.lbl_status.setText("Проверка обновлений...")
        self._set_status_style("#cdd6f4")

        sigs = self._upd_sigs
        from updater import check_for_updates_async
        check_for_updates_async(
            on_update_found=lambda v, n, b: sigs.update_found.emit(v, n, b),
            on_no_update=lambda: sigs.no_update.emit(),
            on_error=lambda msg: sigs.check_error.emit(msg),
        )

    def _on_no_update(self):
        print("[Updater] Версия актуальна.")
        self._do_tcp_probe()

    def _on_update_check_error(self, msg: str):
        """Ошибка проверки — логируем, не блокируем (fail-safe)."""
        print(f"[Updater] Ошибка проверки: {msg}")
        self._do_tcp_probe()

    # ── Шаг 2а: Найдено обновление → скачиваем ───────────────────────────────

    def _on_update_found(self, new_version: str, n_files: int, total_bytes: int):
        mb = total_bytes / (1 << 20)
        print(f"[Updater] Найдена v{new_version}: {mb:.1f} MB")

        size_str = f"{mb:.1f} MB" if mb >= 0.1 else f"{total_bytes // 1024} KB"
        self.lbl_status.setText(f"⬇️  Обновление v{new_version} ({size_str})")
        self._set_status_style("#c39ef5")
        self.progress_bar.setValue(0)
        self.progress_bar.show()

        sigs = self._upd_sigs
        from updater import download_and_apply
        download_and_apply(
            on_progress=lambda pct, status: sigs.dl_progress.emit(pct, status),
            on_done=lambda: sigs.dl_done.emit(),
            on_error=lambda msg: sigs.dl_error.emit(msg),
        )

    def _on_dl_progress(self, pct: int, status: str = ""):
        self.progress_bar.setValue(pct)
        if status:
            self.lbl_status.setText(f"⬇️  {status}")
        else:
            self.lbl_status.setText(f"⬇️  Обновление...  {pct}%")

    def _on_dl_done(self):
        """PS1 скрипт применит обновление и перезапустит приложение."""
        self.progress_bar.setValue(100)
        self.lbl_status.setText("✅  Обновление установлено, перезапуск...")
        self._set_status_style("#82e0aa")
        from PyQt6.QtWidgets import QApplication
        QTimer.singleShot(1500, QApplication.instance().quit)

    def _on_dl_error(self, msg: str):
        print(f"[Updater] Ошибка скачивания: {msg}")
        self.progress_bar.hide()
        self.lbl_status.setText("Ошибка обновления")
        self._set_status_style("#ff8080")
        self.lbl_error.setText(f"⚠️  {msg}")
        self.frm_error.show()
        self.btn_retry.show()

    # ── Шаг 2б: TCP probe ────────────────────────────────────────────────────

    def _do_tcp_probe(self):
        self.lbl_status.setText("Подключение к серверу...")
        self._set_status_style("#cdd6f4")
        self.lbl_ip.setText(f"Адрес:  {self.ip}")
        self.frm_error.hide()
        self.btn_retry.hide()
        self.btn_change_ip.hide()
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
            self._set_status_style("#82e0aa")
            QTimer.singleShot(300, self._open_main_window)
        else:
            self._set_image("fail")
            self.lbl_status.setText("Сервер недоступен")
            self._set_status_style("#ff8080")
            self.lbl_error.setText(
                f"Не удалось подключиться к {self.ip}\n"
                "Проверьте адрес и убедитесь, что сервер запущен."
            )
            self.frm_error.show()
            self.btn_retry.show()
            self.btn_change_ip.show()

    def _open_main_window(self):
        # v3: encoder patch removed — encoding is done by Rust (media-engine.exe)
        # (media-engine.exe). aiortc у зрителя только декодирует, патч не нужен.

        from ui_main.ui_main import MainWindow
        self._main_window = MainWindow(self.ip, self.nick, self.avatar)
        self._main_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        self._main_window.show()
        # ✅ hide() — Qt не считает это закрытием последнего окна
        self.hide()

    def _on_change_ip(self):
        """
        ✅ ПОРЯДОК КРИТИЧЕН:
          1. Сначала emit — получатель откроется и станет видимым.
          2. Потом hide() — только после появления нового окна.
        """
        self.show_login.emit(self.ip, self.nick, self.avatar)
        self.hide()

    # ── Вспомогательный метод ─────────────────────────────────────────────────

    def _set_status_style(self, color: str):
        self.lbl_status.setStyleSheet(
            f"font-size: 17px; font-weight: bold; color: {color}; "
            "background: transparent; border: none;"
        )