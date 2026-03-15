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


# ── Emoji-коллекция для подбора иконки по имени файла ────────────────────────
_SB_EMOJI_MAP = {
    # Keywords → emoji
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
    """Подбирает подходящий эмодзи для названия звука по ключевым словам."""
    lo = name.lower()
    for kw, em in _SB_EMOJI_MAP.items():
        if kw in lo:
            return em
    return "🎵"  # дефолт


# ══════════════════════════════════════════════════════════════════════════════
# SoundboardPanel
# ══════════════════════════════════════════════════════════════════════════════
class SoundboardPanel(QWidget):
    """
    Discord-style прозрачная панель Soundboard.
    Выезжает снизу вверх над кнопкой вызова с анимацией.
    Закрывается при клике вне панели (Popup).

    ИСПРАВЛЕНО:
    - WA_DeleteOnClose УБРАН — он уничтожал C++ объект при close(), но Python-ссылка
      _sb_panel в MainWindow оставалась живой → RuntimeError при следующем обращении.
      Теперь close() просто скрывает виджет; MainWindow сам управляет временем жизни.
    - _flash_timer хранится как атрибут экземпляра — больше не используем __import__
      и не создаём новый QTimer на каждый клик.
    - Кнопки: setFixedHeight(34) вместо setMinimumHeight(46).
    """

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
        # WA_DeleteOnClose намеренно НЕ установлен — см. docstring выше
        self.setMinimumWidth(420)

        self.net = net_client
        self._anim: QPropertyAnimation | None = None
        self._settings = QSettings("MyVoiceChat", "GlobalSettings")

        self._build_ui()

    # ── Public: пересборка при изменении кастомных звуков ─────────────────────

    def rebuild(self):
        """
        Пересобирает UI панели при добавлении / удалении кастомных звуков.
        Вызывается через _rebuild_sb_panel_if_open().
        Сохраняет состояние жёлтой метки автора между пересборками.
        """
        # Сохраняем состояние метки автора — _build_ui создаст новые виджеты
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

        # Восстанавливаем метку если была активна
        if saved_visible and saved_text:
            try:
                self._from_nick_lbl.setText(saved_text)
                self._from_nick_lbl.setVisible(True)
                if saved_ms > 0:
                    self._from_nick_timer.start(saved_ms)
            except (RuntimeError, AttributeError):
                pass

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Если уже есть layout — очищаем его
        existing = self.layout()
        if existing is not None:
            QWidget().setLayout(existing)   # «уводим» старый layout

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        # ── Карточка ──────────────────────────────────────────────────────────
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

        # Заголовок
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

        # Жёлтая метка «▶ [ник]» — кто последний включил звук.
        # Живёт в заголовке панели, но скрыта: уведомление теперь
        # показывается тостом в MainWindow (над нижней панелью, по центру).
        # Оставляем объект для flash_from_nick() — чтобы не ломать вызовы из MainWindow.
        self._from_nick_lbl = QLabel("")
        self._from_nick_lbl.setVisible(False)   # всегда скрыта в заголовке панели

        # Таймер скрытия метки (single-shot, 4 с)
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

        # Разделитель
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.07); border: none; max-height: 1px;")
        card_lay.addWidget(sep)

        # ── Собираем все звуки ────────────────────────────────────────────────
        sd_dir = resource_path("assets/panel")
        default_files = []
        if os.path.exists(sd_dir):
            default_files = sorted([f for f in os.listdir(sd_dir)
                                    if f.lower().endswith(('.wav', '.mp3', '.ogg'))])

        # Кастомные звуки из QSettings
        custom_sounds: list[tuple[str, str]] = []   # (name, path)
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
            # Общий scroll-контейнер для обоих разделов
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

            # ── Секция: Стандартные звуки ─────────────────────────────────────
            if has_default:
                self._add_sounds_section(
                    content_lay,
                    title="Стандартные",
                    buttons_data=[(os.path.splitext(f)[0], f, None) for f in default_files],
                    accent_color="#5865f2",
                    is_custom=False
                )

            # ── Секция: Мои звуки ─────────────────────────────────────────────
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
        """
        Добавляет секцию кнопок звуков в parent_lay.

        buttons_data: list of (display_name, fname_or_None, path_or_None)
          - fname: имя файла в assets/panel/ (стандартные звуки)
          - path:  абсолютный путь (кастомные звуки)
        """
        # Подзаголовок секции
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
        """
        Кастомный звук: читает файл → base64 → отправляет JSON с data_b64.

        Сервер ретранслирует этот JSON всем клиентам без изменений.
        Клиенты в play_soundboard_file() декодируют data_b64 и воспроизводят
        из BytesIO (soundfile.read поддерживает файловоподобные объекты).

        Имя файла в поле 'file' помечается префиксом '__custom__:',
        чтобы получатель не искал этот «файл» в assets/panel/.
        """
        try:
            fsize = os.path.getsize(fpath)
            if fsize > CUSTOM_SOUND_MAX_BYTES:
                return  # защита (теоретически уже проверено при добавлении)
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
        """Отправляет soundboard-команду серверу. Flash-эффект убран."""
        self.net.send_json({"action": CMD_SOUNDBOARD, "file": fname})

    # ── Публичный API: желтая метка автора ───────────────────────────────────

    def flash_from_nick(self, nick: str):
        """
        Показывает «▶ [nick]» жёлтым в заголовке панели на 4 секунды.
        Вызывается из MainWindow/_on_soundboard_played каждый раз при звуке.
        Безопасен к вызову даже если панель скрыта (обновит метку к следующему открытию).
        """
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

    # ── Анимация ──────────────────────────────────────────────────────────────

    def show_above(self, ref_widget: QWidget):
        """
        Центрирует панель горизонтально по родительскому окну.
        Ширина = ширина окна − 32 px (16 px отступ с каждого края).
        Панель выезжает снизу вверх над ref_widget с анимацией.
        """
        # Верхнеуровневое окно — по его ширине растягиваем панель
        top_win = ref_widget.window()
        target_w = max(self.minimumWidth(), top_win.width() - 32)
        self.setMinimumWidth(target_w)
        self.setMaximumWidth(target_w)

        self.adjustSize()
        panel_w = self.width()
        panel_h = self.height()

        # X: центр окна
        g_win = top_win.mapToGlobal(QPoint(0, 0))
        x = g_win.x() + (top_win.width() - panel_w) // 2

        # Y: над кнопкой ref_widget
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

    # ── Отрисовка ─────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        """Рисуем лёгкую тень вокруг карточки."""
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


# Backward-compatible alias
SoundboardDialog = SoundboardPanel