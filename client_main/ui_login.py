# ui_login.py
# ──────────────────────────────────────────────────────────────────────────────
# Окно входа и вспомогательные функции работы с конфигом пользователя.
#
# Показывается:
#   1. При первом запуске (нет user_config.json).
#   2. Когда ConnectingScreen провалился и пользователь нажал «Изменить IP».
# ──────────────────────────────────────────────────────────────────────────────

import os
import json
import shutil

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QLabel, QLineEdit,
    QPushButton, QCheckBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

from config import resource_path, USER_CONFIG_PATH, KNOWN_USERS_PATH
from .ui_styles import GLASS_CARD_SS, GLASS_ERROR_SS, BTN_PRIMARY_SS
from .ui_titlebar import AppTitleBar


# ══════════════════════════════════════════════════════════════════════════════
# Config helpers
# ══════════════════════════════════════════════════════════════════════════════

def migrate_old_configs() -> None:
    """
    Единожды переносит старые JSON-файлы из корня приложения в AppData.

    Миграция срабатывает только если:
      - старый файл существует в папке запуска
      - новый файл в AppData ещё НЕ существует (не перезаписываем!)
    """
    import sys as _sys
    old_dir = (
        os.path.dirname(_sys.executable)
        if getattr(_sys, 'frozen', False)
        else os.path.abspath("..")
    )

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
    """Загружает конфиг пользователя. Возвращает dict или None при отсутствии."""
    migrate_old_configs()
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_config(ip: str, nick: str, avatar: str) -> None:
    """Сохраняет IP, ник и аватар в user_config.json, сохраняя остальные поля."""
    try:
        existing: dict = {}
        if os.path.exists(USER_CONFIG_PATH):
            try:
                with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing.update({"ip": ip, "nick": nick, "avatar": avatar})
        with open(USER_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(existing, f)
    except Exception as e:
        print(f"[Config] Не удалось сохранить конфиг: {e}")


def save_server_name(name: str) -> None:
    """Сохраняет имя сервера в user_config.json."""
    try:
        cfg: dict = {}
        if os.path.exists(USER_CONFIG_PATH):
            try:
                with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception:
                pass
        cfg['server_name'] = name
        with open(USER_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
    except Exception as e:
        print(f"[Config] save_server_name error: {e}")


def load_server_name() -> str:
    """Загружает сохранённое имя сервера или возвращает дефолт."""
    try:
        if os.path.exists(USER_CONFIG_PATH):
            with open(USER_CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f).get('server_name', 'InPulse Server')
    except Exception:
        pass
    return 'InPulse Server'


# ══════════════════════════════════════════════════════════════════════════════
# LoginWindow
# ══════════════════════════════════════════════════════════════════════════════

class LoginWindow(QWidget):
    """Окно первичного входа: IP, ник, аватар."""

    def __init__(
        self,
        ip: str = "127.0.0.1",
        nick: str = "User",
        avatar: str = "1.svg",
        error_msg: str = "",
    ):
        super().__init__()
        self.current_avatar = avatar

        # ✅ Обязательная ссылка на ConnectingScreen — GC не уберёт объект
        self._connecting_screen = None

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
        card.setStyleSheet(GLASS_CARD_SS)
        outer.addWidget(card)

        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        _tb = AppTitleBar(self, f"🔗  {APP_NAME} — Вход")
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

        # Аватарка
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

        # IP
        lbl_ip = QLabel("IP-адрес сервера")
        lbl_ip.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #8899bb; "
            "background: transparent; border: none;"
        )
        layout.addWidget(lbl_ip)

        self.ip_in = QLineEdit(ip)
        self.ip_in.setPlaceholderText("например: 192.168.1.100")
        layout.addWidget(self.ip_in)

        # Ник
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

        # Блок ошибки
        self.frm_error = QFrame()
        self.frm_error.setStyleSheet(GLASS_ERROR_SS)
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

        # Кнопка входа
        self.btn_go = QPushButton("Войти")
        self.btn_go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_go.setStyleSheet(BTN_PRIMARY_SS)
        self.btn_go.clicked.connect(self._on_login)
        layout.addWidget(self.btn_go)

        self._refresh_avatar()

    # ── Аватар ────────────────────────────────────────────────────────────────

    def _open_avatar_picker(self):
        from ui_dialogs import AvatarSelector
        d = AvatarSelector(self)
        if d.exec():
            self.current_avatar = d.selected_avatar
            self._refresh_avatar()

    def _refresh_avatar(self):
        p = resource_path(f"assets/avatars/{self.current_avatar}")
        px = QIcon(p).pixmap(100, 100) if os.path.exists(p) else QIcon().pixmap(0, 0)
        self.avatar_lbl.setPixmap(px)

    # ── Ошибки ────────────────────────────────────────────────────────────────

    def _show_error(self, msg: str):
        self.lbl_error.setText(msg)
        self.frm_error.show()

    def _hide_error(self):
        self.frm_error.hide()
        self.lbl_error.clear()

    # ── Логин ─────────────────────────────────────────────────────────────────

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
        from .ui_connecting import ConnectingScreen
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