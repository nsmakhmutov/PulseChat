

import os
import json
import ctypes
import sys

def resource_path(relative_path):
    """ Получает абсолютный путь к ресурсам, работает для dev и для PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)
# Сообщаем системе, что мы поддерживаем DPI (High DPI Aware)
try:
    # Для Windows 8.1 и 10+
    ctypes.windll.shcore.SetProcessDpiAwareness(1) # 1 = PROCESS_SYSTEM_DPI_AWARE
except Exception:
    try:
        # Для более старых систем
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QLineEdit,
                             QPushButton, QLabel, QCheckBox)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QSurfaceFormat

from config import resource_path
from ui_main import MainWindow
from ui_dialogs import AvatarSelector

class LoginWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.config_file = "user_config.json"
        self.current_avatar = "1.svg"
        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        self.setWindowTitle("Вход")
        self.setFixedSize(350, 480)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.avatar_lbl = QLabel()
        self.avatar_lbl.setFixedSize(120, 120)
        self.avatar_lbl.setStyleSheet("border: 2px solid #3498db; border-radius: 60px;")
        self.avatar_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.avatar_lbl, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_ch = QPushButton("Выбрать аватарку")
        btn_ch.clicked.connect(self.open_av)
        layout.addWidget(btn_ch)

        layout.addSpacing(20)
        layout.addWidget(QLabel("IP сервера:"))
        self.ip_in = QLineEdit("127.0.0.1")
        layout.addWidget(self.ip_in)

        layout.addWidget(QLabel("Никнейм:"))
        self.nick_in = QLineEdit("User")
        layout.addWidget(self.nick_in)

        self.cb_save = QCheckBox("Сохранить данные")
        self.cb_save.setChecked(True)
        layout.addWidget(self.cb_save)

        btn_go = QPushButton("Войти")
        btn_go.setStyleSheet("background-color: #2ecc71; color: white; height: 40px; font-weight: bold;")
        btn_go.clicked.connect(self.go)
        layout.addWidget(btn_go)

    def open_av(self):
        d = AvatarSelector(self)
        if d.exec():
            self.current_avatar = d.selected_avatar; self.update_av()

    def update_av(self):
        p = resource_path(f"assets/avatars/{self.current_avatar}")
        self.avatar_lbl.setPixmap(QIcon(p).pixmap(100, 100) if os.path.exists(p) else QIcon().pixmap(0,0))

    def load_settings(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    d = json.load(f)
                    self.ip_in.setText(d.get("ip", "127.0.0.1"))
                    self.nick_in.setText(d.get("nick", "User"))
                    self.current_avatar = d.get("avatar", "1.svg")
                    self.update_av()
            except: pass

    def go(self):
        if self.cb_save.isChecked():
            with open(self.config_file, 'w') as f:
                json.dump({"ip": self.ip_in.text(), "nick": self.nick_in.text(), "avatar": self.current_avatar}, f)
        self.mw = MainWindow(self.ip_in.text(), self.nick_in.text(), self.current_avatar)
        self.mw.show()
        self.close()

if __name__ == "__main__":
    # ✅ КРИТИЧНО: QSurfaceFormat ДОЛЖЕН быть настроен ДО создания QApplication.
    # Если сделать это после (или в модуле ui_video.py при импорте) —
    # Windows падает с 0xC0000409 (Stack Buffer Overflow) при открытии OpenGL окна.
    _gl_fmt = QSurfaceFormat()
    _gl_fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    _gl_fmt.setSwapInterval(1)   # VSync
    QSurfaceFormat.setDefaultFormat(_gl_fmt)

    app = QApplication(sys.argv)
    w = LoginWindow()
    w.show()
    sys.exit(app.exec())