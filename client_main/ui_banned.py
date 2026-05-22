
import os

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame,
    QLabel, QPushButton,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QFont

from config import resource_path
from .ui_styles import GLASS_CARD_SS, BTN_SECONDARY_SS
from .ui_titlebar import AppTitleBar


def _make_banned_pixmap(size: int = 128) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(231, 76, 60))
    pen.setWidth(max(6, size // 12))
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    margin = max(4, size // 16)
    p.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)

    import math
    cx, cy = size / 2, size / 2
    r = (size - 2 * margin) / 2
    k = math.sqrt(2) / 2
    p.drawLine(
        int(cx - r * k), int(cy - r * k),
        int(cx + r * k), int(cy + r * k),
    )
    p.end()
    return pm


class BannedScreen(QWidget):

    back_clicked = pyqtSignal(str)

    def __init__(
        self,
        server_ip: str,
        reason: str = '',
        server_name: str = '',
    ):
        super().__init__()
        self._server_ip   = server_ip or ''
        self._reason      = (reason or '').strip()
        self._server_name = (server_name or '').strip() or self._server_ip

        self._build_ui()

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

        self.lbl_img = QLabel()
        self.lbl_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_img.setFixedHeight(140)
        self.lbl_img.setStyleSheet("background: transparent; border: none;")
        pm = QPixmap()
        banned_path = resource_path(os.path.join("assets", "icon", "banned.png"))
        if os.path.exists(banned_path):
            pm.load(banned_path)
        if pm.isNull():
            pm = _make_banned_pixmap(128)
        else:
            pm = pm.scaled(
                128, 128,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.lbl_img.setPixmap(pm)
        root.addWidget(self.lbl_img)

        # ── Заголовок ─────────────────────────────────────────────────────────
        lbl_title = QLabel("Вы забанены на сервере")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #ff8080;"
            "background: transparent; border: none;"
        )
        root.addWidget(lbl_title)

        lbl_srv = QLabel(self._server_name)
        lbl_srv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_srv.setStyleSheet(
            "color: rgba(200,210,224,0.75); font-size: 13px;"
            "background: transparent; border: none;"
        )
        root.addWidget(lbl_srv)

        if self._reason:
            frm_reason = QFrame()
            frm_reason.setStyleSheet(
                "background: rgba(231,76,60,0.10);"
                "border: 1px solid rgba(231,76,60,0.30);"
                "border-radius: 8px;"
            )
            rlay = QVBoxLayout(frm_reason)
            rlay.setContentsMargins(14, 10, 14, 10)
            lbl_r = QLabel(f"Причина: {self._reason}")
            lbl_r.setWordWrap(True)
            lbl_r.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_r.setStyleSheet(
                "color: #e8ecf5; font-size: 13px;"
                "background: transparent; border: none;"
            )
            rlay.addWidget(lbl_r)
            root.addWidget(frm_reason)

        root.addStretch()

        self.btn_back = QPushButton("← Назад")
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.setFixedHeight(38)
        self.btn_back.setStyleSheet(BTN_SECONDARY_SS)
        self.btn_back.clicked.connect(self._on_back)
        root.addWidget(self.btn_back)

    def _on_back(self):
        self.back_clicked.emit(self._server_ip)
