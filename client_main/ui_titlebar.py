# ui_titlebar.py
# ──────────────────────────────────────────────────────────────────────────────
# Кастомный title bar для безрамочных QWidget.
# Поддерживает перетаскивание мышью и кнопку закрытия.
# Используется в: LoginWindow, ConnectingScreen, MultiServerScreen,
#                 DiscoveryScreen, _CreateServerDialog
# ──────────────────────────────────────────────────────────────────────────────

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

from config import resource_path


class AppTitleBar(QWidget):
    """
    Кастомный title bar для безрамочных QWidget.
    Поддерживает перетаскивание и кнопку закрытия (×).
    """

    def __init__(self, parent_widget: QWidget, title: str = ""):
        super().__init__(parent_widget)
        self._win      = parent_widget
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

        # Иконка приложения
        ico = QLabel()
        ico.setFixedSize(18, 18)
        try:
            ico.setPixmap(QIcon(resource_path("assets/icon/logo.ico")).pixmap(18, 18))
        except Exception:
            pass
        ico.setStyleSheet("background:transparent; border:none;")
        lay.addWidget(ico)

        # Заголовок
        self._lbl = QLabel(title)
        lay.addWidget(self._lbl, stretch=1)

        # Кнопка закрытия
        btn_close = QPushButton("✕")
        btn_close.setObjectName("appBtnClose")
        btn_close.clicked.connect(parent_widget.close)
        lay.addWidget(btn_close)

    # ── Перетаскивание окна ───────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            )
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)