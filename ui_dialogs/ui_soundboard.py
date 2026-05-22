import os
import base64
# CORRECT
from PyQt6.QtCore import QSettings  # Add this
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QFrame, QGridLayout, QScrollArea)
from PyQt6.QtCore import (Qt, QPoint, QRect, QTimer, QPropertyAnimation, QEasingCurve)
from PyQt6.QtGui import QPainter, QColor, QPainterPath

from config import resource_path, CMD_SOUNDBOARD
from .ui_dialogs import CUSTOM_SOUND_MAX_BYTES, CUSTOM_SOUND_SLOTS

_SB_EMOJI_MAP = {
    "drum": "🥁", "bass": "🎸", "guitar": "🎸", "piano": "🎹",
    "gun": "🔫", "shot": "💥", "boom": "💥", "explode": "💣",
    "yes": "✅", "no": "❌", "win": "🏆", "fail": "😬", "lose": "💀",
    "applause": "👏", "clap": "👏", "laugh": "😂", "lol": "😂",
    "sad": "😢", "cry": "😭", "wow": "😮", "omg": "😱",
    "airhorn": "📣", "horn": "📣", "bell": "🔔", "alarm": "🚨",
    "fart": "💨", "bruh": "😑", "damn": "😤", "nice": "😎",
    "sus": "🫵", "among": "🫵", "amogus": "🫵",
    "troll": "😈", "rip": "⚰️", "death": "💀",
    "music": "🎵", "song": "🎵", "sound": "🔊",
    "alert": "⚠️", "error": "❗",
}

def _pick_emoji(name: str) -> str:
    lo = name.lower()
    for kw, em in _SB_EMOJI_MAP.items():
        if kw in lo:
            return em
    return "🎵"

class SoundboardPanel(QWidget):

    _PANEL_BG   = QColor(32, 34, 42, 235)
    _ACCENT     = QColor(88, 101, 242)
    _BTN_BG     = "#2f3136"
    _BTN_HOVER  = "#40444b"
    _BTN_PRESS  = "#5865f2"
    _TEXT_MAIN  = "#ffffff"
    _TEXT_DIM   = "#b9bbbe"

    def __init__(self, net_client, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(420)

        self.net = net_client
        self._anim: QPropertyAnimation | None = None
        self._settings = QSettings("MyVoiceChat", "GlobalSettings")

        self._build_ui()

    def rebuild(self):

        saved_text    = ""
        saved_visible = False
        saved_ms      = 0
        try:
            saved_text    = self._from_nick_lbl.text()
            saved_visible = self._from_nick_lbl.isVisible()
            if self._from_nick_timer.isActive():
                saved_ms = self._from_nick_timer.remainingTime()
            self._from_nick_timer.stop()
        except (RuntimeError, AttributeError):
            pass

        self._build_ui()
        self.adjustSize()

        if saved_visible and saved_text:
            try:
                self._from_nick_lbl.setText(saved_text)
                self._from_nick_lbl.setVisible(True)
                if saved_ms > 0:
                    self._from_nick_timer.start(saved_ms)
            except (RuntimeError, AttributeError):
                pass

    def _build_ui(self):
        existing = self.layout()
        if existing is not None:
            QWidget().setLayout(existing)   # «уводим» старый layout

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        self._card = QWidget(self)
        self._card.setObjectName("sbCard")
        self._card.setStyleSheet("""
            QWidget#sbCard {
                background-color: rgba(32, 34, 42, 235);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 14px;
            }
        """)
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(12, 10, 12, 12)
        card_lay.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)

        lbl_title = QLabel("  🎵  Soundboard")
        lbl_title.setStyleSheet(f"""
            color: {self._TEXT_MAIN};
            font-size: 14px;
            font-weight: bold;
            background: transparent;
            border: none;
        """)

        self._from_nick_lbl = QLabel("")
        self._from_nick_lbl.setVisible(False)   # всегда скрыта в заголовке панели

        self._from_nick_timer = QTimer(self)
        self._from_nick_timer.setSingleShot(True)
        self._from_nick_timer.timeout.connect(self._hide_from_nick_lbl)

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(30, 30)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {self._TEXT_DIM};
                border: none;
                font-size: 15px;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,0.12);
                color: {self._TEXT_MAIN};
            }}
        """)
        btn_close.clicked.connect(self.close)

        hdr.addWidget(lbl_title)
        hdr.addStretch()
        hdr.addWidget(self._from_nick_lbl)
        hdr.addWidget(btn_close)
        card_lay.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.07); border: none; max-height: 1px;")
        card_lay.addWidget(sep)

        sd_dir = resource_path("assets/panel")
        default_files = []
        if os.path.exists(sd_dir):
            default_files = sorted([f for f in os.listdir(sd_dir)
                                    if f.lower().endswith(('.wav', '.mp3', '.ogg'))])

        custom_sounds: list[tuple[str, str]] = []
        for i in range(CUSTOM_SOUND_SLOTS):
            path = self._settings.value(f"custom_sound_{i}_path", "")
            name = self._settings.value(f"custom_sound_{i}_name", "")
            if path and name and os.path.exists(path):
                custom_sounds.append((name, path))

        has_default = bool(default_files)
        has_custom  = bool(custom_sounds)

        if not has_default and not has_custom:
            empty_lbl = QLabel("Нет звуков.\nДобавьте свои в Настройки → SoundBoard,\nили положите файлы в assets/panel/")
            empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty_lbl.setStyleSheet(
                f"color: {self._TEXT_DIM}; font-size: 12px; background: transparent; border: none;"
            )
            empty_lbl.setContentsMargins(0, 10, 0, 10)
            card_lay.addWidget(empty_lbl)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setStyleSheet("""
                QScrollArea { background: transparent; border: none; }
                QScrollBar:vertical {
                    background: rgba(255,255,255,0.04);
                    width: 5px; border-radius: 2px; margin: 0;
                }
                QScrollBar::handle:vertical {
                    background: rgba(255,255,255,0.2);
                    border-radius: 2px;
                }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            """)

            content_w = QWidget()
            content_w.setStyleSheet("background: transparent;")
            content_lay = QVBoxLayout(content_w)
            content_lay.setContentsMargins(0, 0, 0, 0)
            content_lay.setSpacing(10)

            if has_default:
                self._add_sounds_section(
                    content_lay,
                    title="Стандартные",
                    buttons_data=[(os.path.splitext(f)[0], f, None) for f in default_files],
                    accent_color="#5865f2",
                    is_custom=False
                )

            if has_custom:
                if has_default:
                    div = QFrame()
                    div.setFrameShape(QFrame.Shape.HLine)
                    div.setStyleSheet("background: rgba(255,255,255,0.07); border: none; max-height: 1px;")
                    content_lay.addWidget(div)

                self._add_sounds_section(
                    content_lay,
                    title="Мои звуки",
                    buttons_data=[(name, None, path) for name, path in custom_sounds],
                    accent_color="#2ecc71",
                    is_custom=True
                )

            scroll.setWidget(content_w)
            card_lay.addWidget(scroll)

        outer.addWidget(self._card)
        self.adjustSize()

    def _add_sounds_section(self, parent_lay: QVBoxLayout,
                             title: str,
                             buttons_data: list[tuple[str, str | None, str | None]],
                             accent_color: str,
                             is_custom: bool):

        sec_hdr = QLabel(f"  {title}")
        sec_hdr.setStyleSheet(f"""
            font-size: 11px;
            font-weight: bold;
            color: {accent_color};
            background: transparent;
            border: none;
        """)
        parent_lay.addWidget(sec_hdr)

        grid_w = QWidget()
        grid_w.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        COLS = 2
        hover_col   = "#40444b" if not is_custom else "rgba(39,174,96,0.22)"
        pressed_col = "#5865f2" if not is_custom else "rgba(39,174,96,0.55)"
        border_hov  = "rgba(88,101,242,0.6)" if not is_custom else "rgba(39,174,96,0.7)"

        for idx, (name, fname, fpath) in enumerate(buttons_data):
            emoji    = _pick_emoji(name)
            display  = f"{emoji}  {name}"

            btn = QPushButton(display)
            btn.setFixedHeight(34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {self._BTN_BG};
                    color: {self._TEXT_MAIN};
                    border: 1px solid rgba(255,255,255,0.06);
                    border-radius: 7px;
                    padding: 2px 8px;
                    font-size: 12px;
                    text-align: left;
                }}
                QPushButton:hover {{
                    background-color: {hover_col};
                    border: 1px solid {border_hov};
                }}
                QPushButton:pressed {{
                    background-color: {pressed_col};
                    color: #ffffff;
                }}
            """)

            if is_custom and fpath:
                btn.clicked.connect(
                    lambda _ch, _p=fpath, _n=name: self._on_custom_sound_clicked(_p, _n)
                )
            else:
                btn.clicked.connect(
                    lambda _ch, f=fname: self._on_sound_clicked(f)
                )
            grid.addWidget(btn, idx // COLS, idx % COLS)

        parent_lay.addWidget(grid_w)

    def _on_custom_sound_clicked(self, fpath: str, name: str):

        try:
            fsize = os.path.getsize(fpath)
            if fsize > CUSTOM_SOUND_MAX_BYTES:
                return
            with open(fpath, 'rb') as f:
                raw_bytes = f.read()
            b64 = base64.b64encode(raw_bytes).decode('ascii')
            self.net.send_json({
                "action":  CMD_SOUNDBOARD,
                "file":    f"__custom__:{name}",
                "data_b64": b64,
            })
        except Exception as e:
            print(f"[SoundboardPanel] Custom sound error: {e}")

    def _on_sound_clicked(self, fname: str):
        self.net.send_json({"action": CMD_SOUNDBOARD, "file": fname})

    def flash_from_nick(self, nick: str):
        try:
            self._from_nick_lbl.setText(f"▶  {nick}")
            self._from_nick_lbl.setVisible(True)
            self._from_nick_timer.start(4000)
        except (RuntimeError, AttributeError):
            pass

    def _hide_from_nick_lbl(self):
        try:
            self._from_nick_lbl.setVisible(False)
        except (RuntimeError, AttributeError):
            pass

    def show_above(self, ref_widget: QWidget):

        top_win = ref_widget.window()
        target_w = max(self.minimumWidth(), top_win.width() - 32)
        self.setMinimumWidth(target_w)
        self.setMaximumWidth(target_w)

        self.adjustSize()
        panel_w = self.width()
        panel_h = self.height()

        g_win = top_win.mapToGlobal(QPoint(0, 0))
        x = g_win.x() + (top_win.width() - panel_w) // 2

        g_btn = ref_widget.mapToGlobal(QPoint(0, 0))
        y_final = g_btn.y() - panel_h - 6
        y_start = y_final + 18

        self.setGeometry(x, y_start, panel_w, panel_h)
        self.show()

        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(QRect(x, y_start, panel_w, panel_h))
        self._anim.setEndValue(QRect(x, y_final, panel_w, panel_h))
        self._anim.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        for i in range(4, 0, -1):
            shadow_rect = self._card.geometry().adjusted(-i, -i, i, i)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 18 * i))
            path = QPainterPath()
            path.addRoundedRect(
                float(shadow_rect.x()), float(shadow_rect.y()),
                float(shadow_rect.width()), float(shadow_rect.height()),
                16.0, 16.0
            )
            p.drawPath(path)

SoundboardDialog = SoundboardPanel