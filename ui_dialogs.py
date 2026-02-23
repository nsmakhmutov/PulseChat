import os
import json
import sounddevice as sd
import dxcam
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
                             QWidget, QGridLayout, QLabel, QSlider, QTabWidget,
                             QComboBox, QProgressBar, QLineEdit, QCheckBox, QFrame,
                             QGroupBox, QSizePolicy, QGraphicsOpacityEffect)
from PyQt6.QtCore import Qt, QSize, QSettings, QEvent, QPropertyAnimation, QEasingCurve, QRect, QPoint, QTimer
from PyQt6.QtGui import QIcon, QGuiApplication, QPainter, QColor, QPen, QFont, QPainterPath, QBrush
from config import resource_path, CMD_SOUNDBOARD
from audio_engine import PYRNNOISE_AVAILABLE
from version import APP_VERSION, APP_NAME, APP_AUTHOR, APP_YEAR, ABOUT_TEXT, GITHUB_REPO


# ──────────────────────────────────────────────────────────────────────────────
# Виджет: полоса уровня микрофона + маркер порога VAD в одной плоскости
# ──────────────────────────────────────────────────────────────────────────────
class MicVadWidget(QWidget):
    """
    Комбинированный виджет: отображает уровень микрофона (зелёная полоса)
    и порог VAD (красная вертикальная линия) в одном пространстве.
    Так пользователь сразу видит, насколько нужно поднять/опустить громкость
    относительно порога активации.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0          # 0–100 (из volume_level_signal)
        self._threshold_pos = 10 # 0–100 (позиция на полосе)
        self.setMinimumHeight(30)
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_level(self, val: int):
        self._level = max(0, min(100, val))
        self.update()

    def set_threshold(self, slider_val: int):
        # slider_val: 1–50 → позиция 2–100 на полосе (slider_val * 2)
        self._threshold_pos = max(0, min(100, slider_val * 2))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()

        # Фон
        p.fillRect(0, 0, w, h, QColor("#2a2a2a"))

        # Полоса уровня микрофона
        bar_w = int(self._level / 100.0 * w)
        if self._level < self._threshold_pos:
            bar_color = QColor("#27ae60")   # ниже порога — зелёный
        else:
            bar_color = QColor("#2ecc71")   # выше порога — яркий зелёный (голос принят)
        p.fillRect(0, 0, bar_w, h, bar_color)

        # Маркер порога VAD (красная вертикальная черта)
        tx = int(self._threshold_pos / 100.0 * w)
        pen = QPen(QColor("#e74c3c"), 3)
        p.setPen(pen)
        p.drawLine(tx, 0, tx, h)

        # Подпись маркера
        p.setPen(QPen(QColor("#ffffff"), 1))
        p.setFont(QFont("Segoe UI", 8))
        label_x = min(tx + 5, w - 40)
        p.drawText(label_x, h - 5, "VAD")

        p.end()


# ──────────────────────────────────────────────────────────────────────────────
# Всплывающий оверлей управления пользователем (вместо отдельного окна)
# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# Всплывающий оверлей управления пользователем (вместо отдельного окна)
# ──────────────────────────────────────────────────────────────────────────────
class UserOverlayPanel(QFrame):
    """
    Выпадающий полупрозрачный оверлей прямо под ником пользователя.
    Qt.WindowType.Popup — автоматически закрывается при клике вне панели,
    корректно работает при двух мониторах.

    Особенности дизайна:
    • Полупрозрачный тёмный фон, скруглённые углы без артефактов
    • Никнейм убран из шапки (уже виден в дереве)
    • Кнопка «Шепнуть» — удерживай, чтобы говорить только этому пользователю
    • Кнопка «Смотреть стрим» — отображается только если пользователь стримит
    """

    def __init__(self, nick: str, current_vol: float, uid: int, audio_handler, global_pos,
                 parent=None, is_streaming: bool = False, on_watch_stream=None):
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.audio = audio_handler
        self.uid = uid
        self._nick = nick.strip()
        self._whisper_active = False
        self._on_watch_stream = on_watch_stream

        # ── Прозрачность окна + рисуем фон сами в paintEvent ─────────────────
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("userOverlay")

        # Внешний padding — чтобы тень/скругление не обрезалось
        self.setContentsMargins(0, 0, 0, 0)

        # ── Внутренний контейнер с фоном ─────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QFrame(self)
        self._card.setObjectName("card")
        self._card.setStyleSheet("""
            QFrame#card {
                background-color: rgba(22, 22, 28, 215);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }
            QLabel {
                color: #d0d0d8;
                font-size: 12px;
                background: transparent;
                border: none;
            }
            QSlider::groove:horizontal {
                height: 5px;
                background: rgba(255,255,255,0.12);
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                width: 14px; height: 14px;
                margin: -5px 0;
                background: #5b8ef5;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal {
                background: #5b8ef5;
                border-radius: 2px;
            }
        """)
        outer.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(14, 10, 14, 12)
        card_lay.setSpacing(7)

        # ── Громкость ─────────────────────────────────────────────────────────
        lbl_vol_title = QLabel("🔊  Громкость")
        lbl_vol_title.setStyleSheet("font-size: 11px; color: rgba(200,200,210,0.7); background:transparent; border:none;")
        card_lay.addWidget(lbl_vol_title)

        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        self.sl_vol = QSlider(Qt.Orientation.Horizontal)
        self.sl_vol.setRange(0, 200)
        self.sl_vol.setValue(int(current_vol * 100))
        self.lbl_vol = QLabel(f"{self.sl_vol.value()}%")
        self.lbl_vol.setFixedWidth(38)
        self.lbl_vol.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sl_vol.valueChanged.connect(self._on_vol_changed)
        vol_row.addWidget(self.sl_vol)
        vol_row.addWidget(self.lbl_vol)
        card_lay.addLayout(vol_row)

        # ── Разделитель ───────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none; max-height: 1px;")
        sep.setMaximumHeight(1)
        card_lay.addWidget(sep)

        # ── Кнопка: заглушить ─────────────────────────────────────────────────
        is_m = audio_handler.remote_users[uid].is_locally_muted \
               if uid in audio_handler.remote_users else False
        self.btn_mute = self._make_btn(
            "🔇  Заглушить" if not is_m else "🔊  Разглушить",
            checkable=True, checked=is_m
        )
        self.btn_mute.clicked.connect(self._on_toggle_mute)
        card_lay.addWidget(self.btn_mute)

        # ── Кнопка: шёпот (удерживать) ───────────────────────────────────────
        self.btn_whisper = self._make_btn("🤫  Шепнуть  (удерживай)", checkable=False)
        self.btn_whisper.setStyleSheet(self.btn_whisper.styleSheet() + """
            QPushButton { border-color: rgba(130,100,220,0.5); color: #c8b0ff; }
            QPushButton:pressed {
                background-color: rgba(100,60,200,0.55);
                border-color: #7b52d4;
                color: #ffffff;
            }
        """)
        # press/release — не click, иначе сработает только при отпускании
        self.btn_whisper.pressed.connect(self._on_whisper_press)
        self.btn_whisper.released.connect(self._on_whisper_release)
        card_lay.addWidget(self.btn_whisper)

        # ── Кнопка: смотреть стрим (только если пользователь стримит) ────────
        if is_streaming and on_watch_stream is not None:
            sep2 = QFrame()
            sep2.setFrameShape(QFrame.Shape.HLine)
            sep2.setStyleSheet("background: rgba(255,255,255,0.08); border: none; max-height: 1px;")
            sep2.setMaximumHeight(1)
            card_lay.addWidget(sep2)

            self.btn_watch = self._make_btn("📺  Смотреть стрим", checkable=False)
            self.btn_watch.setStyleSheet(self.btn_watch.styleSheet() + """
                QPushButton { border-color: rgba(46,204,113,0.45); color: #82e0aa; }
                QPushButton:hover {
                    background-color: rgba(39,174,96,0.25);
                    border-color: rgba(46,204,113,0.8);
                }
                QPushButton:pressed {
                    background-color: rgba(39,174,96,0.45);
                    color: #ffffff;
                }
            """)
            self.btn_watch.clicked.connect(self._on_watch_clicked)
            card_lay.addWidget(self.btn_watch)

        # ── Подсказка под кнопкой шёпота ─────────────────────────────────────
        # Всегда занимает место в layout (нет Layout Shift при появлении).
        # Видимость управляется только цветом текста: прозрачный ↔ фиолетовый.
        self._lbl_whisper_hint = QLabel("Остальные тебя не слышат пока держишь")
        self._lbl_whisper_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_whisper_hint.setWordWrap(False)
        self._lbl_whisper_hint_active_style = (
            "font-size: 10px; color: rgba(180,150,255,0.80); "
            "background:transparent; border:none;"
        )
        self._lbl_whisper_hint_idle_style = (
            "font-size: 10px; color: transparent; "
            "background:transparent; border:none;"
        )
        self._lbl_whisper_hint.setStyleSheet(self._lbl_whisper_hint_idle_style)
        card_lay.addWidget(self._lbl_whisper_hint)

        # Фиксируем размер ПОСЛЕ добавления всех виджетов (включая hint).
        # Это гарантирует, что место под hint уже учтено и панель
        # не будет прыгать при появлении текста.
        self.adjustSize()
        self.setFixedSize(self.sizeHint())

        # ── Позиционирование прямо под элементом дерева ───────────────────────
        screen = QGuiApplication.screenAt(global_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()

        x = global_pos.x()
        y = global_pos.y()

        if x + self.width() > avail.right():
            x = avail.right() - self.width() - 4
        if y + self.height() > avail.bottom():
            y = global_pos.y() - self.height()

        x = max(avail.left() + 4, x)
        y = max(avail.top() + 4, y)

        self.move(x, y)

    # ── Фабрика кнопок ────────────────────────────────────────────────────────

    def _make_btn(self, text: str, checkable=False, checked=False) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(checkable)
        btn.setChecked(checked)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.06);
                color: #d0d0d8;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 7px;
                padding: 5px 10px;
                font-size: 12px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.11);
                border-color: rgba(255,255,255,0.18);
            }
            QPushButton:checked {
                background-color: rgba(220,60,60,0.35);
                border-color: rgba(220,60,60,0.6);
                color: #ff9090;
            }
        """)
        return btn

    # ── Слоты ─────────────────────────────────────────────────────────────────

    def _on_vol_changed(self, v: int):
        self.lbl_vol.setText(f"{v}%")
        self.audio.set_user_volume(self.uid, v / 100.0)

    def _on_toggle_mute(self):
        state = self.audio.toggle_user_mute(self.uid)
        self.btn_mute.setText("🔊  Разглушить" if state else "🔇  Заглушить")

    def _on_whisper_press(self):
        """Начинаем шёпот при нажатии."""
        if not self._whisper_active:
            self._whisper_active = True
            self.audio.start_whisper(self.uid)
            self.btn_whisper.setText("🤫  Шепчу...")
            # Показываем подсказку только цветом — размер панели не меняется
            self._lbl_whisper_hint.setStyleSheet(self._lbl_whisper_hint_active_style)

    def _on_whisper_release(self):
        """Останавливаем шёпот при отпускании."""
        if self._whisper_active:
            self._whisper_active = False
            self.audio.stop_whisper()
            self.btn_whisper.setText("🤫  Шепнуть  (удерживай)")
            self._lbl_whisper_hint.setStyleSheet(self._lbl_whisper_hint_idle_style)

    def _on_watch_clicked(self):
        """Открываем окно стрима и закрываем оверлей."""
        self.close()
        if self._on_watch_stream is not None:
            self._on_watch_stream()

    def hideEvent(self, event):
        """Если панель закрылась пока шептали — останавливаем шёпот."""
        if self._whisper_active:
            self._whisper_active = False
            self.audio.stop_whisper()
        super().hideEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# Выбор аватара (без изменений)
# ──────────────────────────────────────────────────────────────────────────────
class AvatarSelector(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Выбор аватара")
        self.setFixedSize(500, 400)
        self.selected_avatar = None
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        grid = QGridLayout(container)
        av_dir = resource_path("assets/avatars")

        if os.path.exists(av_dir):
            files = sorted([f for f in os.listdir(av_dir) if f.endswith('.svg')])
            for i, f in enumerate(files):
                btn = QPushButton()
                btn.setFixedSize(80, 80)
                btn.setIcon(QIcon(os.path.join(av_dir, f)))
                btn.setIconSize(QSize(60, 60))
                btn.clicked.connect(lambda ch, fname=f: self.select_and_close(fname))
                grid.addWidget(btn, i // 5, i % 5)
        container.setLayout(grid)
        scroll.setWidget(container)
        layout.addWidget(scroll)
        btn_cancel = QPushButton("Отмена")
        btn_cancel.clicked.connect(self.reject)
        layout.addWidget(btn_cancel)

    def select_and_close(self, filename):
        self.selected_avatar = filename
        self.accept()


# ──────────────────────────────────────────────────────────────────────────────
# Панель громкости (оставляем для совместимости, но в UI используем Overlay)
# ──────────────────────────────────────────────────────────────────────────────
class VolumePanel(QDialog):
    def __init__(self, nick, current_vol, uid, audio_handler, parent=None):
        super().__init__(parent)
        self.audio, self.uid = audio_handler, uid
        self.setWindowTitle(f"Громкость: {nick}")
        layout = QVBoxLayout(self)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 200)
        self.slider.setValue(int(current_vol * 100))
        self.label = QLabel(f"{self.slider.value()}%")
        self.slider.valueChanged.connect(
            lambda v: (self.label.setText(f"{v}%"), self.audio.set_user_volume(self.uid, v / 100.0)))

        layout.addWidget(QLabel("Уровень громкости:"))
        layout.addWidget(self.slider)
        layout.addWidget(self.label)

        is_m = self.audio.remote_users[uid].is_locally_muted if uid in self.audio.remote_users else False
        self.btn_mute = QPushButton("Разглушить" if is_m else "Заглушить")
        self.btn_mute.clicked.connect(self.toggle_mute)
        layout.addWidget(self.btn_mute)

    def toggle_mute(self):
        s = self.audio.toggle_user_mute(self.uid)
        self.btn_mute.setText("Разглушить" if s else "Заглушить")


# ──────────────────────────────────────────────────────────────────────────────
# Диалог настроек
# ──────────────────────────────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, audio_engine, parent):
        super().__init__(parent)
        self.audio = audio_engine
        self.mw = parent  # MainWindow
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.setWindowTitle("Настройки")
        self.resize(620, 580)

        main_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # 1. Профиль
        self.setup_profile_tab()

        # 2. Аудио
        self.setup_audio_tab()

        # 3. Персонализация (Тема + Хоткеи в одной вкладке)
        self.setup_personalization_tab()

        # 4. Версия
        self.setup_version_tab()

        main_layout.addWidget(self.tabs)
        btn_save = QPushButton("Сохранить")
        btn_save.clicked.connect(self.save_all)
        main_layout.addWidget(btn_save)

    # ── Вкладка «О себе» ──────────────────────────────────────────────────────
    def setup_profile_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        self.av_lbl = QLabel()
        self.av_lbl.setFixedSize(100, 100)
        self.av_lbl.setStyleSheet("border: 2px solid gray; border-radius: 10px;")
        self.av_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cur_av = self.mw.avatar
        self.upd_av_preview()

        btn_ch = QPushButton("Выбрать аватарку")
        btn_ch.clicked.connect(self.open_av_sel)
        lay.addWidget(self.av_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(btn_ch)
        lay.addWidget(QLabel("Никнейм:"))
        self.ed_nick = QLineEdit(self.mw.nick)
        lay.addWidget(self.ed_nick)
        lay.addStretch()
        self.tabs.addTab(tab, "О себе")

    # ── Вкладка «Аудио» ───────────────────────────────────────────────────────
    def setup_audio_tab(self):
        aud_tab = QWidget()
        aud_lay = QVBoxLayout(aud_tab)

        self.cb_in = QComboBox()
        self.cb_out = QComboBox()
        self.refresh_devices_list()

        stat = "ВКЛ" if self.audio.use_noise_reduction else "ВЫКЛ"
        if not PYRNNOISE_AVAILABLE:
            stat = "НЕТ МОДУЛЯ"
        self.btn_nr = QPushButton(f"Шумодав: {stat}")
        self.btn_nr.setObjectName("btn_nr")
        self.btn_nr.setCheckable(True)
        self.btn_nr.setEnabled(PYRNNOISE_AVAILABLE)
        self.btn_nr.setChecked(self.audio.use_noise_reduction)
        self.btn_nr.clicked.connect(self.toggle_nr)

        aud_lay.addWidget(QLabel("Качество звука (Битрейт):"))
        self.cb_bitrate = QComboBox()
        bitrate_options = {
            "8 kbps (Рация)": 8,
            "24 kbps (Стандарт)": 24,
            "64 kbps (Хорошее)": 64
        }
        for text, val in bitrate_options.items():
            self.cb_bitrate.addItem(text, val)
        current_bitrate = int(self.app_settings.value("audio_bitrate", 64000)) // 1000
        index = self.cb_bitrate.findData(current_bitrate)
        if index != -1:
            self.cb_bitrate.setCurrentIndex(index)
        self.cb_bitrate.currentIndexChanged.connect(
            lambda: self.audio.set_bitrate(self.cb_bitrate.currentData())
        )
        aud_lay.addWidget(self.cb_bitrate)

        aud_lay.addWidget(QLabel("Ввод:"))
        aud_lay.addWidget(self.cb_in)
        aud_lay.addWidget(QLabel("Вывод:"))
        aud_lay.addWidget(self.cb_out)
        aud_lay.addWidget(self.btn_nr)

        # ── Блок: Микрофон + Порог VAD (объединённый) ────────────────────────
        aud_lay.addSpacing(10)
        mic_group = QGroupBox("🎙  Микрофон и порог активации (VAD)")
        mic_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        mic_lay = QVBoxLayout(mic_group)

        # Подпись над комбо-виджетом
        hint_lbl = QLabel(
            "Говорите в микрофон — зелёная полоса показывает уровень.\n"
            "Красная черта — порог VAD. Поднимите полосу выше черты чтобы передать голос."
        )
        hint_lbl.setStyleSheet("font-size: 11px; color: #aaa; font-weight: normal;")
        hint_lbl.setWordWrap(True)
        mic_lay.addWidget(hint_lbl)

        # Комбо-виджет: полоса уровня + маркер VAD
        self.mic_vad = MicVadWidget()
        self.audio.volume_level_signal.connect(self.mic_vad.set_level)
        mic_lay.addWidget(self.mic_vad)

        # Ползунок VAD — прямо под полосой, одинаковой ширины
        vad_slider_val = int(self.app_settings.value("vad_threshold_slider", 5))
        self.lbl_vad = QLabel()
        self._update_vad_label(vad_slider_val)
        self.lbl_vad.setStyleSheet("font-size: 12px; font-weight: normal;")
        mic_lay.addWidget(self.lbl_vad)

        self.sl_vad = QSlider(Qt.Orientation.Horizontal)
        self.sl_vad.setRange(1, 50)
        self.sl_vad.setValue(vad_slider_val)
        self.sl_vad.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sl_vad.setTickInterval(5)
        self.sl_vad.valueChanged.connect(self._on_vad_slider_changed)
        mic_lay.addWidget(self.sl_vad)

        # Инициализируем начальное положение маркера
        self.mic_vad.set_threshold(vad_slider_val)

        aud_lay.addWidget(mic_group)

        # ── Прочие ползунки ───────────────────────────────────────────────────
        aud_lay.addSpacing(8)
        sys_vol = int(self.app_settings.value("system_sound_volume", 70))
        self.lbl_sys = QLabel(f"Системные звуки: {sys_vol}%")
        self.sl_sys = QSlider(Qt.Orientation.Horizontal)
        self.sl_sys.setRange(0, 100)
        self.sl_sys.setValue(sys_vol)
        self.sl_sys.valueChanged.connect(lambda v: self.lbl_sys.setText(f"Системные звуки: {v}%"))
        aud_lay.addWidget(self.lbl_sys)
        aud_lay.addWidget(self.sl_sys)

        sb_vol = int(self.app_settings.value("soundboard_volume", 50))
        self.lbl_sb = QLabel(f"Soundboard: {sb_vol}%")
        self.sl_sb = QSlider(Qt.Orientation.Horizontal)
        self.sl_sb.setRange(0, 100)
        self.sl_sb.setValue(sb_vol)
        self.sl_sb.valueChanged.connect(lambda v: self.lbl_sb.setText(f"Soundboard: {v}%"))
        aud_lay.addWidget(self.lbl_sb)
        aud_lay.addWidget(self.sl_sb)

        aud_lay.addStretch()
        self.tabs.addTab(aud_tab, "Аудио")

    # ── Вкладка «Персонализация» (Тема + Хоткеи) ──────────────────────────────
    def setup_personalization_tab(self):
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setSpacing(12)

        # ─── Секция: Тема оформления ──────────────────────────────────────────
        theme_group = QGroupBox("🎨  Тема оформления")
        theme_lay = QVBoxLayout(theme_group)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Светлая", "Темная"])
        self.theme_combo.setCurrentText(self.app_settings.value("theme", "Светлая"))
        self.theme_combo.currentTextChanged.connect(self.mw.apply_theme)
        theme_lay.addWidget(QLabel("Цветовая схема приложения:"))
        theme_lay.addWidget(self.theme_combo)
        lay.addWidget(theme_group)

        # ─── Секция: Горячие клавиши ──────────────────────────────────────────
        hk_group = QGroupBox("⌨  Горячие клавиши")
        hk_lay = QVBoxLayout(hk_group)

        hk_lay.addWidget(QLabel("Mute микрофона:"))
        self.hk_mute = QLineEdit(self.app_settings.value("hk_mute", "alt+["))
        hk_lay.addWidget(self.hk_mute)

        hk_lay.addWidget(QLabel("Deafen (динамики):"))
        self.hk_deafen = QLineEdit(self.app_settings.value("hk_deafen", "alt+]"))
        hk_lay.addWidget(self.hk_deafen)

        hint = QLabel("Формат: alt+[, ctrl+m, f9 и т.д.")
        hint.setStyleSheet("font-size: 11px; color: #888;")
        hk_lay.addWidget(hint)

        btn_res = QPushButton("Сбросить к значениям по умолчанию")
        btn_res.clicked.connect(lambda: (
            self.hk_mute.setText("alt+["),
            self.hk_deafen.setText("alt+]")
        ))
        hk_lay.addWidget(btn_res)
        lay.addWidget(hk_group)

        lay.addStretch()
        self.tabs.addTab(tab, "Персонализация")

    # ── Вкладка «Версия» ──────────────────────────────────────────────────────
    def setup_version_tab(self):
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Bridge(QObject):
            sig_found    = pyqtSignal(str, str)
            sig_no_upd   = pyqtSignal()
            sig_error    = pyqtSignal(str)
            sig_progress = pyqtSignal(int)
            sig_done     = pyqtSignal()

        self._upd_bridge = _Bridge()
        self._upd_bridge.sig_found.connect(self._slot_update_found)
        self._upd_bridge.sig_no_upd.connect(self._slot_no_update)
        self._upd_bridge.sig_error.connect(self._slot_update_error)
        self._upd_bridge.sig_progress.connect(self._slot_progress)
        self._upd_bridge.sig_done.connect(self._slot_download_done)

        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 20, 20, 20)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_path = resource_path("assets/icon/app_icon.ico")
        if os.path.exists(icon_path):
            icon_lbl.setPixmap(QIcon(icon_path).pixmap(64, 64))
        lay.addWidget(icon_lbl)

        about_lbl = QLabel(ABOUT_TEXT)
        about_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        about_lbl.setWordWrap(True)
        about_lbl.setStyleSheet("font-size: 13px; line-height: 1.6;")
        lay.addWidget(about_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep)

        self._ver_status_lbl = QLabel("Обновления не проверялись")
        self._ver_status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ver_status_lbl.setWordWrap(True)
        lay.addWidget(self._ver_status_lbl)

        self._btn_check_update = QPushButton("🔍  Проверить обновления")
        self._btn_check_update.setFixedHeight(36)
        self._btn_check_update.clicked.connect(self._on_check_update_clicked)
        lay.addWidget(self._btn_check_update)

        self._btn_install_update = QPushButton("⬇  Скачать и установить")
        self._btn_install_update.setFixedHeight(36)
        self._btn_install_update.setVisible(False)
        self._btn_install_update.setStyleSheet(
            "background-color: #2ecc71; color: white; font-weight: bold;"
        )
        self._btn_install_update.clicked.connect(self._on_install_update_clicked)
        lay.addWidget(self._btn_install_update)
        self._pending_download_url = None

        self._ver_progress = QProgressBar()
        self._ver_progress.setVisible(False)
        self._ver_progress.setTextVisible(True)
        lay.addWidget(self._ver_progress)

        if not GITHUB_REPO:
            self._btn_check_update.setEnabled(False)
            self._ver_status_lbl.setText("⚠ GITHUB_REPO не задан в version.py")

        lay.addStretch()
        self.tabs.addTab(tab, "Версия")

    # ── Слоты обновления ──────────────────────────────────────────────────────

    def _slot_update_found(self, version: str, url: str):
        self._pending_download_url = url
        self._ver_status_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._ver_status_lbl.setText(
            f"🎉 Доступна новая версия: <b>v{version}</b>"
            f"<br><small>Текущая: v{APP_VERSION}</small>"
        )
        self._btn_install_update.setVisible(True)
        self._btn_check_update.setEnabled(True)

    def _slot_no_update(self):
        self._ver_status_lbl.setText(f"✅ Версия актуальна  (v{APP_VERSION})")
        self._btn_check_update.setEnabled(True)

    def _slot_update_error(self, message: str):
        self._ver_status_lbl.setText(f"❌ {message}")
        self._btn_check_update.setEnabled(True)

    def _slot_progress(self, pct: int):
        self._ver_progress.setValue(pct)

    def _slot_download_done(self):
        self._ver_status_lbl.setText(
            "✅ Загрузка завершена. Приложение сейчас перезапустится..."
        )

    def _on_check_update_clicked(self):
        from updater import check_for_updates_async
        self._btn_check_update.setEnabled(False)
        self._btn_install_update.setVisible(False)
        self._ver_progress.setVisible(False)
        self._ver_status_lbl.setText("⏳ Проверяю...")
        bridge = self._upd_bridge
        check_for_updates_async(
            on_update_found=lambda v, u: bridge.sig_found.emit(v, u),
            on_no_update=lambda: bridge.sig_no_upd.emit(),
            on_error=lambda msg: bridge.sig_error.emit(msg),
        )

    def _on_install_update_clicked(self):
        if not self._pending_download_url:
            return
        from updater import download_and_install
        self._btn_install_update.setEnabled(False)
        self._btn_check_update.setEnabled(False)
        self._ver_progress.setVisible(True)
        self._ver_progress.setValue(0)
        self._ver_status_lbl.setText("⬇ Загружаю обновление...")
        bridge = self._upd_bridge
        download_and_install(
            self._pending_download_url,
            on_progress=lambda pct: bridge.sig_progress.emit(pct),
            on_done=lambda: bridge.sig_done.emit(),
            on_error=lambda msg: bridge.sig_error.emit(msg),
        )

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def open_av_sel(self):
        d = AvatarSelector(self)
        if d.exec():
            self.cur_av = d.selected_avatar
            self.upd_av_preview()

    def upd_av_preview(self):
        p = resource_path(f"assets/avatars/{self.cur_av}")
        self.av_lbl.setPixmap(QIcon(p).pixmap(80, 80) if os.path.exists(p) else QIcon().pixmap(0, 0))

    def refresh_devices_list(self):
        devs = sd.query_devices()
        try:
            def_api = sd.query_hostapis(sd.default.hostapi)['name']
        except:
            def_api = ""
        self.cb_in.clear()
        self.cb_out.clear()
        u_in, u_out = set(), set()
        s_in = self.app_settings.value("device_in_name", "")
        s_out = self.app_settings.value("device_out_name", "")

        for d in devs:
            api = sd.query_hostapis(d['hostapi'])['name']
            if api != def_api:
                continue
            dn = f"{d['name']} ({api})"
            if d['max_input_channels'] > 0 and dn not in u_in:
                self.cb_in.addItem(dn)
                u_in.add(dn)
            if d['max_output_channels'] > 0 and dn not in u_out:
                self.cb_out.addItem(dn)
                u_out.add(dn)
        self.cb_in.setCurrentText(s_in)
        self.cb_out.setCurrentText(s_out)

    def _update_vad_label(self, val: int):
        threshold = val / 1000.0
        if val <= 5:
            desc = "Очень высокая"
        elif val <= 12:
            desc = "Высокая"
        elif val <= 20:
            desc = "Средняя"
        elif val <= 35:
            desc = "Низкая"
        else:
            desc = "Минимальная"
        self.lbl_vad.setText(
            f"Порог VAD: {threshold:.3f}  —  чувствительность: {desc}"
        )

    def _on_vad_slider_changed(self, val: int):
        self._update_vad_label(val)
        self.audio.set_vad_threshold(val)
        self.mic_vad.set_threshold(val)

    def toggle_nr(self):
        self.audio.use_noise_reduction = self.btn_nr.isChecked()
        self.btn_nr.setText(f"Шумодав: {'ВКЛ' if self.audio.use_noise_reduction else 'ВЫКЛ'}")
        if self.parent():
            self.parent().app_settings.setValue("noise_reduction", self.audio.use_noise_reduction)

    def get_devices(self):
        return self.cb_in.currentText(), self.cb_out.currentText()

    def save_all(self):
        s = self.app_settings
        s.setValue("device_in_name", self.cb_in.currentText())
        s.setValue("device_out_name", self.cb_out.currentText())
        s.setValue("hk_mute", self.hk_mute.text())
        s.setValue("hk_deafen", self.hk_deafen.text())
        s.setValue("system_sound_volume", self.sl_sys.value())
        s.setValue("soundboard_volume", self.sl_sb.value())
        s.setValue("vad_threshold_slider", self.sl_vad.value())
        s.setValue("theme", self.theme_combo.currentText())

        self.mw.nick = self.ed_nick.text()
        self.mw.avatar = self.cur_av
        self.mw.setWindowTitle(f"VoiceChat - {self.mw.nick}")
        if hasattr(self.mw, 'net'):
            self.mw.net.update_user_info(self.mw.nick, self.mw.avatar)

        if os.path.exists("user_config.json"):
            try:
                with open("user_config.json", 'r') as f:
                    d = json.load(f)
                d['nick'] = self.mw.nick
                d['avatar'] = self.mw.avatar
                with open("user_config.json", 'w') as f:
                    json.dump(d, f)
            except:
                pass
        self.accept()


# ──────────────────────────────────────────────────────────────────────────────
# Диалог настроек трансляции (без изменений)
# ──────────────────────────────────────────────────────────────────────────────
class StreamSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки трансляции")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Выберите монитор:"))
        self.monitor_combo = QComboBox()

        try:
            screens = QGuiApplication.screens()
            for i, screen in enumerate(screens):
                geometry = screen.geometry()
                screen_name = screen.name()
                display_text = f"Монитор {i} [{screen_name}] ({geometry.width()}x{geometry.height()})"
                self.monitor_combo.addItem(display_text, i)
            if not screens:
                self.monitor_combo.addItem("Основной монитор", 0)
        except Exception as e:
            print(f"[UI] Error listing screens: {e}")
            self.monitor_combo.addItem("Основной монитор", 0)

        layout.addWidget(self.monitor_combo)

        layout.addWidget(QLabel("Разрешение:"))
        self.res_combo = QComboBox()
        self.res_options = {
            "720p (HD)": (1280, 720),
            "480p (SD)": (854, 480),
            "360p": (640, 360)
        }
        for text in self.res_options.keys():
            self.res_combo.addItem(text)
        layout.addWidget(self.res_combo)

        layout.addWidget(QLabel("Частота кадров (FPS):"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["15", "30", "60"])
        self.fps_combo.setCurrentText("30")
        layout.addWidget(self.fps_combo)

        layout.addSpacing(10)

        sep = QLabel("── Аудио трансляции ──────────────────")
        sep.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(sep)

        self.cb_stream_audio = QCheckBox("🔊 Транслировать звук")
        self.cb_stream_audio.setChecked(False)
        layout.addWidget(self.cb_stream_audio)

        self._vbc_banner = QLabel()
        self._vbc_banner.setWordWrap(True)
        self._vbc_banner.setStyleSheet("border-radius: 6px; padding: 8px; font-size: 12px;")
        layout.addWidget(self._vbc_banner)

        self._btn_vbc_install = QPushButton("⬇  Установить VB-CABLE")
        self._btn_vbc_install.setStyleSheet(
            "background-color: #e67e22; color: white; font-weight: bold; height: 34px;"
        )
        self._btn_vbc_install.clicked.connect(self._on_install_vbcable)
        layout.addWidget(self._btn_vbc_install)

        self._hint_lbl = QLabel(
            "💡 Направьте вывод игры/плеера на «CABLE Input»\n"
            "    (Настройки Windows → Звук → Приложения)\n"
            "    Ваши наушники оставьте основным устройством."
        )
        self._hint_lbl.setStyleSheet(
            "background-color: #1a5276; color: #aed6f1; "
            "border-radius: 6px; padding: 8px; font-size: 11px;"
        )
        self._hint_lbl.setWordWrap(True)
        layout.addWidget(self._hint_lbl)

        self._refresh_vbc_ui()
        self.cb_stream_audio.toggled.connect(self._on_audio_toggled)

        layout.addSpacing(8)

        btn_start = QPushButton("Запустить трансляцию")
        btn_start.setStyleSheet(
            "background-color: #2ecc71; color: white; font-weight: bold; height: 40px;"
        )
        btn_start.clicked.connect(self.accept)
        layout.addWidget(btn_start)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.clicked.connect(self.reject)
        layout.addWidget(btn_cancel)

        self.adjustSize()

    def _refresh_vbc_ui(self):
        try:
            from vbcable_installer import is_vbcable_installed, find_zip
            installed = is_vbcable_installed()
        except ImportError:
            installed = False
            find_zip = lambda: None

        audio_on = self.cb_stream_audio.isChecked()

        if installed:
            self._vbc_banner.setText("✅  VB-CABLE установлен — захват без эха активен")
            self._vbc_banner.setStyleSheet(
                "background-color: #1e8449; color: #a9dfbf; "
                "border-radius: 6px; padding: 8px; font-size: 12px;"
            )
            self._btn_vbc_install.setVisible(False)
            self._hint_lbl.setVisible(audio_on)
        else:
            try:
                from vbcable_installer import find_zip
                zip_found = find_zip() is not None
            except ImportError:
                zip_found = False

            if zip_found:
                self._vbc_banner.setText(
                    "⚠  VB-CABLE не установлен.\n"
                    "Архив найден в папке проекта — нажмите кнопку ниже."
                )
                self._btn_vbc_install.setEnabled(True)
            else:
                self._vbc_banner.setText(
                    "⚠  VB-CABLE не установлен.\n"
                    "Без него звук стрима будет захватываться через WASAPI Loopback\n"
                    "и зрители могут слышать эхо своего голоса.\n\n"
                    "Скачайте VBCABLE_Driver_Pack45.zip с vb-audio.com\n"
                    "и положите его в папку с программой."
                )
                self._btn_vbc_install.setEnabled(False)

            self._vbc_banner.setStyleSheet(
                "background-color: #7d6608; color: #fef9e7; "
                "border-radius: 6px; padding: 8px; font-size: 12px;"
            )
            self._btn_vbc_install.setVisible(True)
            self._hint_lbl.setVisible(False)

        self._vbc_banner.setVisible(audio_on)
        self._btn_vbc_install.setVisible(
            audio_on and not installed and self._btn_vbc_install.isVisible()
        )
        self._hint_lbl.setVisible(audio_on and installed)
        self.adjustSize()

    def _on_audio_toggled(self, checked):
        self._refresh_vbc_ui()

    def _on_install_vbcable(self):
        try:
            from vbcable_installer import install_vbcable, find_zip
        except ImportError:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "VB-CABLE",
                "Модуль vbcable_installer.py не найден рядом с программой.")
            return

        from PyQt6.QtWidgets import QMessageBox
        self._btn_vbc_install.setEnabled(False)
        self._btn_vbc_install.setText("Устанавливаю…")
        success, msg = install_vbcable()
        if success:
            QMessageBox.information(self, "VB-CABLE", msg)
        else:
            QMessageBox.warning(self, "VB-CABLE — ошибка", msg)
        self._btn_vbc_install.setEnabled(True)
        self._btn_vbc_install.setText("⬇  Установить VB-CABLE")
        self._refresh_vbc_ui()

    def get_settings(self):
        res_text = self.res_combo.currentText()
        width, height = self.res_options[res_text]
        audio_enabled = self.cb_stream_audio.isChecked()
        return {
            "monitor_idx":         self.monitor_combo.currentData(),
            "width":               width,
            "height":              height,
            "fps":                 int(self.fps_combo.currentText()),
            "stream_audio":        audio_enabled,
            "system_audio":        audio_enabled,
            "system_audio_device": None,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Soundboard (без изменений)
# ──────────────────────────────────────────────────────────────────────────────

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

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
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

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(22, 22)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {self._TEXT_DIM};
                border: none;
                font-size: 12px;
                border-radius: 11px;
            }}
            QPushButton:hover {{
                background: rgba(255,255,255,0.1);
                color: {self._TEXT_MAIN};
            }}
        """)
        btn_close.clicked.connect(self.close)

        hdr.addWidget(lbl_title)
        hdr.addStretch()
        hdr.addWidget(btn_close)
        card_lay.addLayout(hdr)

        # Разделитель
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.07); border: none; max-height: 1px;")
        card_lay.addWidget(sep)

        # Список звуков
        sd_dir = resource_path("assets/panel")
        files = []
        if os.path.exists(sd_dir):
            files = sorted([f for f in os.listdir(sd_dir)
                            if f.lower().endswith(('.wav', '.mp3', '.ogg'))])

        if not files:
            empty_lbl = QLabel("Нет звуковых файлов.\nПоложите .wav/.mp3 в assets/panel/")
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

            grid_widget = QWidget()
            grid_widget.setStyleSheet("background: transparent;")
            grid = QGridLayout(grid_widget)
            grid.setContentsMargins(0, 2, 0, 0)
            grid.setSpacing(6)

            COLS = 2
            for idx, fname in enumerate(files):
                name = os.path.splitext(fname)[0]
                emoji = _pick_emoji(name)
                display = f"{emoji}  {name}"

                btn = QPushButton(display)
                btn.setFixedHeight(34)       # ИСПРАВЛЕНО: было setMinimumHeight(46)
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
                        background-color: {self._BTN_HOVER};
                        border: 1px solid rgba(88,101,242,0.6);
                    }}
                    QPushButton:pressed {{
                        background-color: {self._BTN_PRESS};
                        color: #ffffff;
                    }}
                """)
                btn.clicked.connect(
                    lambda _ch, f=fname: self._on_sound_clicked(f)
                )
                grid.addWidget(btn, idx // COLS, idx % COLS)

            # Не более 5 строк без скролла; высота ряда = 34px кнопка + 6px spacing
            ROW_H = 34 + 6
            visible_rows = min(5, (len(files) + COLS - 1) // COLS)
            scroll.setFixedHeight(visible_rows * ROW_H + 6)
            scroll.setWidget(grid_widget)
            card_lay.addWidget(scroll)

        outer.addWidget(self._card)
        self.adjustSize()

    def _on_sound_clicked(self, fname: str):
        """Отправляет soundboard-команду серверу. Flash-эффект убран."""
        self.net.send_json({"action": CMD_SOUNDBOARD, "file": fname})

    # ── Анимация ──────────────────────────────────────────────────────────────

    def show_above(self, ref_widget: QWidget):
        """
        Позиционирует панель над ref_widget (кнопкой soundboard)
        и запускает анимацию выезда снизу вверх.
        """
        self.adjustSize()

        g_pos = ref_widget.mapToGlobal(QPoint(0, 0))
        panel_w = self.width()
        panel_h = self.height()

        x = g_pos.x() + ref_widget.width() // 2 - panel_w // 2
        y_final = g_pos.y() - panel_h - 6

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