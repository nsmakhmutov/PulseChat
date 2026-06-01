import os
import base64
import wave
# CORRECT
from PyQt6.QtCore import QSettings  # Add this
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QFrame, QGridLayout, QScrollArea,
                             QFileDialog, QMessageBox)
from PyQt6.QtCore import (Qt, QPoint, QRect, QTimer, QPropertyAnimation, QEasingCurve)
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QFontMetrics

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


class _MarqueeLabel(QWidget):
    """Подпись с бегущей строкой: если текст не влезает в ширину виджета,
    он плавно прокручивается по горизонтали. Если влезает — стоит на месте.
    Фон прозрачный (рисуется только текст), чтобы вписаться в кнопку звука."""

    _GAP = 36  # зазор между концом текста и его повторным началом, px

    def __init__(self, text: str, color: str = "#ffffff", parent=None):
        super().__init__(parent)
        self._text = text
        self._color = QColor(color)
        self._offset = 0
        self._text_w = 0
        self._needs_scroll = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30 fps
        self._timer.timeout.connect(self._tick)

        self._recalc()

    def setText(self, text: str):
        self._text = text
        self._offset = 0
        self._recalc()
        self.update()

    def _recalc(self):
        fm = QFontMetrics(self.font())
        self._text_w = fm.horizontalAdvance(self._text)
        self._needs_scroll = self._text_w > self.width()
        if self._needs_scroll:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._offset = 0
            if self._timer.isActive():
                self._timer.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._recalc()

    def _tick(self):
        self._offset += 1
        if self._offset >= self._text_w + self._GAP:
            self._offset = 0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setPen(self._color)
        p.setFont(self.font())
        fm = QFontMetrics(self.font())
        y = (self.height() + fm.ascent() - fm.descent()) // 2

        if not self._needs_scroll:
            p.drawText(0, y, self._text)
            return

        x = -self._offset
        # рисуем две копии подряд для бесшовной прокрутки
        p.drawText(x, y, self._text)
        p.drawText(x + self._text_w + self._GAP, y, self._text)


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
        # Фиксированная ширина панели. Длинные названия звуков НЕ растягивают
        # окно — вместо этого подпись прокручивается бегущей строкой.
        self._fixed_w = 420

        self.net = net_client
        self._anim: QPropertyAnimation | None = None
        self._settings = QSettings("MyVoiceChat", "GlobalSettings")

        # Режим редактирования "Моих звуков" (показывает кнопки удаления и
        # плейсхолдеры "Добавить"). Сохраняется между rebuild().
        self._edit_mode = False

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

        # Подавляем перерисовку на время пересборки — иначе видны
        # промежуточные кадры (мелькание/дёрганье) при удалении/добавлении.
        was_updates = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            self._build_ui()

            # Геометрию меняем РОВНО один раз, чтобы окно не дёргалось.
            if self.isVisible():
                try:
                    pos = self.pos()
                    new_h = self.sizeHint().height()
                    self.setGeometry(pos.x(), pos.y(), self._fixed_w, new_h)
                except RuntimeError:
                    pass
            else:
                self.adjustSize()
        finally:
            self.setUpdatesEnabled(was_updates)
            self.update()

        if saved_visible and saved_text:
            try:
                self._from_nick_lbl.setText(saved_text)
                self._from_nick_lbl.setVisible(True)
                if saved_ms > 0:
                    self._from_nick_timer.start(saved_ms)
            except (RuntimeError, AttributeError):
                pass

    def _build_ui(self):
        # Полностью убираем прошлый UI: и layout, и старую карточку.
        # Раньше уводился только layout (QWidget().setLayout(...)), а старый
        # self._card оставался живым ребёнком self и накладывался поверх нового
        # при rebuild() — из-за этого в режиме "Изменить" окно выглядело пустым.
        old_card = getattr(self, "_card", None)
        if old_card is not None:
            try:
                old_card.setParent(None)
                old_card.deleteLater()
            except RuntimeError:
                pass
            self._card = None

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

        custom_sounds: list[tuple[int, str, str]] = []   # (slot_idx, name, path)
        free_slots: list[int] = []
        for i in range(CUSTOM_SOUND_SLOTS):
            path = self._settings.value(f"custom_sound_{i}_path", "")
            name = self._settings.value(f"custom_sound_{i}_name", "")
            if path and name and os.path.exists(path):
                custom_sounds.append((i, name, path))
            else:
                free_slots.append(i)

        has_default = bool(default_files)
        has_custom  = bool(custom_sounds)
        # Секция "Мои звуки" показывается ВСЕГДА (даже когда своих звуков нет),
        # чтобы кнопка "Изменить" → "Добавить" была доступна в любой момент.

        if True:
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

            # Секция "Мои звуки" — всегда (с кнопкой "Изменить"/"Добавить").
            if has_default:
                div = QFrame()
                div.setFrameShape(QFrame.Shape.HLine)
                div.setStyleSheet("background: rgba(255,255,255,0.07); border: none; max-height: 1px;")
                content_lay.addWidget(div)

            self._add_custom_section(content_lay, custom_sounds, free_slots)

            scroll.setWidget(content_w)

            # QScrollArea с widgetResizable не раздувается под высоту контента
            # сам — его sizeHint фиксирован, и при rebuild() (режим "Изменить",
            # где контента больше) окно оставалось почти пустым. Поэтому задаём
            # высоту области = высоте контента с разумным потолком: пока всё
            # помещается — окно растёт под контент; если звуков очень много —
            # включается вертикальный скролл.
            content_w.adjustSize()
            content_h = content_w.sizeHint().height()
            try:
                from PyQt6.QtWidgets import QApplication
                screen = QApplication.primaryScreen()
                avail_h = screen.availableGeometry().height() if screen else 900
            except Exception:
                avail_h = 900
            cap_h = max(220, int(avail_h * 0.6))
            scroll.setMinimumHeight(min(content_h, cap_h))
            scroll.setMaximumHeight(cap_h)

            card_lay.addWidget(scroll)

        outer.addWidget(self._card)
        # Фиксируем ширину панели: длинные названия не растягивают окно,
        # а прокручиваются бегущей строкой (_MarqueeLabel). _fixed_w может
        # обновляться методом show_above (под ширину окна стрима).
        self.setMinimumWidth(self._fixed_w)
        self.setMaximumWidth(self._fixed_w)
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

    # ── Секция "Мои звуки" с режимом редактирования ─────────────────────
    def _add_custom_section(self, parent_lay: QVBoxLayout,
                             custom_sounds: list[tuple[int, str, str]],
                             free_slots: list[int]):

        accent_color = "#2ecc71"

        # Заголовок секции + кнопка "Изменить"/"Готово".
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(6)

        sec_hdr = QLabel("  Мои звуки")
        sec_hdr.setStyleSheet(f"""
            font-size: 11px;
            font-weight: bold;
            color: {accent_color};
            background: transparent;
            border: none;
        """)
        hdr_row.addWidget(sec_hdr)
        hdr_row.addStretch()

        # Кнопка редактирования есть ВСЕГДА — даже когда своих звуков нет,
        # иначе после удаления всех звуков нельзя было бы добавить новый.
        if True:
            btn_edit = QPushButton("Готово" if self._edit_mode else "Изменить")
            btn_edit.setFixedHeight(22)
            btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_edit.setStyleSheet("""
                QPushButton {
                    background: rgba(255,255,255,0.06);
                    color: #b9bbbe;
                    border: 1px solid rgba(255,255,255,0.10);
                    border-radius: 6px;
                    padding: 0 10px;
                    font-size: 11px;
                }
                QPushButton:hover {
                    background: rgba(255,255,255,0.14);
                    color: #ffffff;
                }
            """)
            btn_edit.clicked.connect(self._toggle_edit_mode)
            hdr_row.addWidget(btn_edit)

        parent_lay.addLayout(hdr_row)

        grid_w = QWidget()
        grid_w.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)

        COLS = 2
        cells = []
        for slot_idx, name, path in custom_sounds:
            cells.append(self._make_custom_cell(slot_idx, name, path))

        # В режиме редактирования добавляем плейсхолдеры "Добавить" в пустые слоты.
        if self._edit_mode:
            for slot_idx in free_slots:
                cells.append(self._make_add_placeholder(slot_idx))

        for i, cell in enumerate(cells):
            grid.addWidget(cell, i // COLS, i % COLS)

        parent_lay.addWidget(grid_w)

    def _make_custom_cell(self, slot_idx: int, name: str, path: str) -> QWidget:
        emoji = _pick_emoji(name)
        hover_col   = "rgba(39,174,96,0.22)"
        pressed_col = "rgba(39,174,96,0.55)"
        border_hov  = "rgba(39,174,96,0.7)"

        cell = QFrame()
        cell.setFixedHeight(34)
        cell.setObjectName("sbCustomCell")
        cell.setStyleSheet(f"""
            QFrame#sbCustomCell {{
                background-color: {self._BTN_BG};
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 7px;
            }}
            QFrame#sbCustomCell:hover {{
                background-color: {hover_col};
                border: 1px solid {border_hov};
            }}
        """)

        lay = QHBoxLayout(cell)
        lay.setContentsMargins(8, 0, 6, 0)
        lay.setSpacing(6)

        emoji_lbl = QLabel(emoji)
        emoji_lbl.setFixedWidth(20)
        emoji_lbl.setStyleSheet(
            f"color: {self._TEXT_MAIN}; font-size: 12px; background: transparent; border: none;"
        )
        lay.addWidget(emoji_lbl)

        name_lbl = _MarqueeLabel(name, color=self._TEXT_MAIN)
        f = name_lbl.font()
        f.setPointSize(9)
        name_lbl.setFont(f)
        name_lbl.setMinimumWidth(0)
        name_lbl.setFixedHeight(32)
        from PyQt6.QtWidgets import QSizePolicy
        name_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        lay.addWidget(name_lbl, stretch=1)

        if self._edit_mode:
            btn_del = QPushButton("✕")
            btn_del.setFixedSize(22, 22)
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.setToolTip("Удалить звук")
            btn_del.setStyleSheet("""
                QPushButton {
                    background: rgba(220,60,60,0.18);
                    color: #e87070;
                    border: 1px solid rgba(220,60,60,0.40);
                    border-radius: 5px;
                    font-size: 12px;
                    padding: 0;
                }
                QPushButton:hover {
                    background: rgba(220,60,60,0.40);
                    color: #ffffff;
                }
            """)
            btn_del.clicked.connect(lambda _ch, _i=slot_idx: self._on_delete_slot(_i))
            lay.addWidget(btn_del)
        else:
            # Вне режима редактирования по клику на ячейку — проигрываем звук.
            cell.setCursor(Qt.CursorShape.PointingHandCursor)

            def _press(_ev, _p=path, _n=name):
                self._on_custom_sound_clicked(_p, _n)

            cell.mousePressEvent = _press

        return cell

    def _make_add_placeholder(self, slot_idx: int) -> QWidget:
        cell = QFrame()
        cell.setFixedHeight(34)
        cell.setObjectName("sbAddCell")
        cell.setCursor(Qt.CursorShape.PointingHandCursor)
        cell.setStyleSheet("""
            QFrame#sbAddCell {
                background-color: transparent;
                border: 1px dashed rgba(255,255,255,0.22);
                border-radius: 7px;
            }
            QFrame#sbAddCell:hover {
                background-color: rgba(88,101,242,0.18);
                border: 1px dashed rgba(88,101,242,0.7);
            }
        """)

        lay = QHBoxLayout(cell)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)

        lbl = QLabel("＋  Добавить")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            "color: #9aa0b0; font-size: 12px; background: transparent; border: none;"
        )
        lay.addWidget(lbl)

        def _press(_ev, _i=slot_idx):
            self._on_add_slot(_i)

        cell.mousePressEvent = _press
        return cell

    def _toggle_edit_mode(self):
        self._edit_mode = not self._edit_mode
        self.rebuild()

    def _on_delete_slot(self, slot_idx: int):
        self._settings.setValue(f"custom_sound_{slot_idx}_path", "")
        self._settings.setValue(f"custom_sound_{slot_idx}_name", "")
        self.rebuild()

    def _on_add_slot(self, slot_idx: int):
        # Панель — Qt.Popup: при открытии модального диалога она мгновенно
        # закрывается (теряет фокус), и нативное окно выбора файла, открытое
        # синхронно прямо из обработчика мыши, не показывается. Поэтому:
        #  1) запоминаем позицию и явно прячем попап;
        #  2) открываем диалог ОТЛОЖЕННО (singleShot) — Qt успевает выйти из
        #     обработки события и корректно закрыть попап;
        #  3) диалог родителем имеет главное окно, а не попап;
        #  4) после диалога показываем панель заново на том же месте.
        from PyQt6.QtCore import QTimer

        saved_pos = self.pos()
        parent_win = self.parentWidget()
        if parent_win is not None:
            parent_win = parent_win.window()

        self.hide()

        def _do_pick():
            path, _ = QFileDialog.getOpenFileName(
                parent_win, f"Выбрать звук для слота #{slot_idx + 1}",
                "", "Аудио файлы (*.mp3 *.wav)"
            )
            self._finish_add_slot(slot_idx, path, saved_pos, parent_win)

        QTimer.singleShot(0, _do_pick)

    def _finish_add_slot(self, slot_idx: int, path: str, saved_pos, parent_win):
        def _reshow():
            try:
                self.move(saved_pos)
                self.show()
                self.raise_()
                self.activateWindow()
                self.rebuild()
                new_h = self.sizeHint().height()
                self.setGeometry(saved_pos.x(), saved_pos.y(), self._fixed_w, new_h)
            except RuntimeError:
                pass

        if not path:
            _reshow()
            return
        try:
            fsize = os.path.getsize(path)
        except OSError:
            fsize = 0
        if fsize > CUSTOM_SOUND_MAX_BYTES:
            QMessageBox.warning(
                parent_win, "Файл слишком большой",
                f"Максимальный размер — 1 МБ (~7 сек).\n"
                f"Выбранный файл: {fsize // 1024} КБ."
            )
            _reshow()
            return
        if path.lower().endswith(".wav"):
            try:
                with wave.open(path, 'rb') as wf:
                    dur = wf.getnframes() / wf.getframerate()
                if dur > 7.5:
                    QMessageBox.warning(
                        parent_win, "Звук слишком длинный",
                        f"Максимальная длительность — 7 секунд.\n"
                        f"Длительность файла: {dur:.1f} сек."
                    )
                    _reshow()
                    return
            except Exception:
                pass

        name = os.path.splitext(os.path.basename(path))[0]
        self._settings.setValue(f"custom_sound_{slot_idx}_path", path)
        self._settings.setValue(f"custom_sound_{slot_idx}_name", name)
        _reshow()

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
        # Запоминаем выбранную ширину, чтобы rebuild() (после добавления/
        # удаления звука) не сбрасывал её обратно к базовым 420.
        self._fixed_w = target_w
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