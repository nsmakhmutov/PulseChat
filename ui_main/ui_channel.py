from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                             QWidget, QLabel, QLineEdit, QCheckBox)
from PyQt6.QtCore import Qt

_DIALOG_CHANNEL_SS = """
    QWidget#dlgCard {
        background-color: rgba(22, 24, 35, 252);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 14px;
    }
    QLabel { color: #c8d0e0; background: transparent; border: none; }
    QLineEdit {
        background-color: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 7px; padding: 7px 11px;
        color: #dde3f0; font-size: 14px;
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
"""


class _CreateChannelDialog(QDialog):

    def __init__(self, parent=None, parent_name=None):
        super().__init__(parent)
        self._parent_name = parent_name
        self.setWindowTitle("Создать канал")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(320, 250 if parent_name else 230)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("dlgCard")
        card.setStyleSheet(_DIALOG_CHANNEL_SS)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)

        if parent_name:
            title = f"⤷  Дочерний канал для «{parent_name}»"
        else:
            title = "🔊  Создать временный канал"
        lbl_title = QLabel(title)
        lbl_title.setWordWrap(True)
        lbl_title.setStyleSheet(
            "font-size: 15px; font-weight: bold; color: #cdd6f4;"
            "background: transparent; border: none;"
        )
        lay.addWidget(lbl_title)

        if parent_name:
            lbl_hint = QLabel("Слышит родительский канал. Родитель не слышит дочерний.")
            lbl_hint.setWordWrap(True)
            lbl_hint.setStyleSheet("font-size: 11px; color: #6f7da0;")
            lay.addWidget(lbl_hint)

        lbl_name = QLabel("Название канала:")
        lbl_name.setStyleSheet("font-size: 12px; color: #8899bb;")
        lay.addWidget(lbl_name)

        self._inp_name = QLineEdit()
        self._inp_name.setPlaceholderText("Например: Игровой чат")
        self._inp_name.setMaxLength(32)
        lay.addWidget(self._inp_name)

        self._cb_pass = QCheckBox("Защитить паролем")
        self._cb_pass.setChecked(False)
        self._cb_pass.toggled.connect(self._on_pass_toggle)
        lay.addWidget(self._cb_pass)

        self._inp_pass = QLineEdit()
        self._inp_pass.setPlaceholderText("Пароль для входа")
        self._inp_pass.setMaxLength(64)
        self._inp_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self._inp_pass.setVisible(False)
        lay.addWidget(self._inp_pass)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_ok = QPushButton("✔  Создать")
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setStyleSheet(
            "QPushButton { background: rgba(39,174,96,0.28); color: #82e0aa;"
            " border: 1px solid rgba(46,204,113,0.55); border-radius: 8px;"
            " font-size: 14px; font-weight: bold; padding: 9px 0; }"
            "QPushButton:hover { background: rgba(39,174,96,0.48); border-color: rgba(46,204,113,0.85); color: #fff; }"
        )
        btn_ok.clicked.connect(self._on_ok)

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

        self._inp_name.returnPressed.connect(self._on_ok)

    def _on_pass_toggle(self, checked: bool):
        self._inp_pass.setVisible(checked)
        if checked:
            self._inp_pass.setFocus()
        base = 250 if self._parent_name else 230
        self.setFixedHeight(base + 30 if checked else base)

    def _on_ok(self):
        name = self._inp_name.text().strip()
        if not name:
            self._inp_name.setPlaceholderText("⚠ Введите название!")
            self._inp_name.setFocus()
            return
        self.accept()

    def get_channel_name(self) -> str:
        return self._inp_name.text().strip()

    def get_parent_name(self):
        return self._parent_name

    def get_password(self):
        if self._cb_pass.isChecked():
            p = self._inp_pass.text().strip()
            return p if p else None
        return None


class _ChannelPasswordDialog(QDialog):

    def __init__(self, channel_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Пароль канала")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(300, 165)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget()
        card.setObjectName("dlgCard")
        card.setStyleSheet(_DIALOG_CHANNEL_SS)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)

        lbl = QLabel(f"🔒  Канал «{channel_name}» защищён")
        lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #cdd6f4;"
            "background: transparent; border: none;"
        )
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self._inp = QLineEdit()
        self._inp.setPlaceholderText("Введите пароль...")
        self._inp.setEchoMode(QLineEdit.EchoMode.Password)
        lay.addWidget(self._inp)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_ok = QPushButton("Войти")
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setStyleSheet(
            "QPushButton { background: rgba(39,174,96,0.28); color: #82e0aa;"
            " border: 1px solid rgba(46,204,113,0.55); border-radius: 8px;"
            " font-size: 14px; font-weight: bold; padding: 8px 0; }"
            "QPushButton:hover { background: rgba(39,174,96,0.48); color: #fff; }"
        )
        btn_ok.clicked.connect(self.accept)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet(
            "QPushButton { background: rgba(127,140,141,0.22); color: #8899aa;"
            " border: 1px solid rgba(127,140,141,0.40); border-radius: 8px;"
            " font-size: 13px; padding: 8px 0; }"
            "QPushButton:hover { background: rgba(149,165,166,0.35); color: #c8d0e0; }"
        )
        btn_cancel.clicked.connect(self.reject)

        btn_row.addWidget(btn_ok)
        btn_row.addWidget(btn_cancel)
        lay.addLayout(btn_row)

        self._inp.returnPressed.connect(self.accept)

    def get_password(self) -> str:
        return self._inp.text().strip()
