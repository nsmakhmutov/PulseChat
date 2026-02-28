import os
import io
import json
import math
import base64
import wave
import sounddevice as sd
import dxcam
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
                             QWidget, QGridLayout, QLabel, QSlider, QTabWidget,
                             QComboBox, QProgressBar, QLineEdit, QCheckBox, QFrame,
                             QGroupBox, QSizePolicy, QFileDialog, QMessageBox)
from PyQt6.QtCore import Qt, QSize, QSettings, QEvent, QPropertyAnimation, QEasingCurve, QRect, QPoint, QTimer
from PyQt6.QtGui import QIcon, QGuiApplication, QPainter, QColor, QPen, QFont, QPainterPath, QBrush
from config import resource_path, CMD_SOUNDBOARD
from audio_engine import PYRNNOISE_AVAILABLE

# ── Максимальный размер кастомного звука (1 MB) ──────────────────────────────
# 7 секунд MP3 @ 128kbps ≈ 112 KB, @ 320kbps ≈ 280 KB.
# 1 MB с большим запасом перекрывает любой типичный 7-секундный звук.
CUSTOM_SOUND_MAX_BYTES = 1 * 1024 * 1024   # 1 MB
CUSTOM_SOUND_SLOTS     = 3                  # количество кастомных слотов


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции для нелинейной кривой громкости пользователя
# ──────────────────────────────────────────────────────────────────────────────
# Почему экспонента, а не линейный множитель:
#   Речь через Opus кодируется при очень низком уровне (~-20 дБ относительно FS).
#   Линейный диапазон 0–2.0x (слайдер 0–200) даёт буст максимум +6 дБ — почти
#   не слышно. Экспоненциальная кривая 10^((slider-100)/100):
#     slider 0   →  0.01x  (-40 дБ)   — тихо
#     slider 100 →  1.00x  (  0 дБ)   — нейтрально (дефолт, поведение НЕ меняется)
#     slider 150 →  3.16x  (+10 дБ)   — заметный буст
#     slider 200 → 10.00x  (+20 дБ)   — максимальный буст для тихих микрофонов
# При слайдере 100 пользователь слышит ровно то же что раньше — совместимость.
def _slider_to_vol(slider_int: int) -> float:
    """Слайдер 0-200 → коэффициент громкости по экспоненциальной кривой."""
    return 10.0 ** ((slider_int - 100) / 100.0)


def _vol_to_slider(vol: float) -> int:
    """Коэффициент громкости → позиция слайдера (обратная функция)."""
    if vol <= 0.0:
        return 0
    return max(0, min(200, int(math.log10(vol) * 100 + 100)))
from version import APP_VERSION, APP_NAME, APP_AUTHOR,QA_TESTERS, APP_YEAR, ABOUT_TEXT, GITHUB_REPO


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
        self.sl_vol.setValue(_vol_to_slider(current_vol))
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
        # Экспоненциальная кривая: slider 100 = 1.0x (нейтрально),
        # slider 200 = 10.0x (+20 дБ) — позволяет поднять тихие микрофоны.
        self.audio.set_user_volume(self.uid, _slider_to_vol(v))

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
        self.slider.setValue(_vol_to_slider(current_vol))
        self.label = QLabel(f"{self.slider.value()}%")
        self.slider.valueChanged.connect(
            lambda v: (self.label.setText(f"{v}%"), self.audio.set_user_volume(self.uid, _slider_to_vol(v))))

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
# Системный оверлей шёпота — поверх всех окон Windows
# ──────────────────────────────────────────────────────────────────────────────
class WhisperSystemOverlay(QWidget):
    """
    Полупрозрачный оверлей в правом верхнем углу экрана.
    Появляется поверх любых окон (игры, браузер, IDE) когда тебе шепчут.

    Флаги окна:
      WindowStaysOnTopHint  — поверх всего
      FramelessWindowHint   — без заголовка/рамки
      Tool                  — не мигает в панели задач, не крадёт Alt+Tab
    WA_ShowWithoutActivating — не уводит фокус из игры при появлении.
    """

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Высота фиксирована; ширина выставляется динамически в _reposition()
        self.setFixedHeight(46)

        # ── Содержимое ────────────────────────────────────────────────────────
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.setSpacing(12)

        # Иконка whispers.ico вместо эмодзи
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(26, 26)
        icon_path = resource_path("assets/icon/whispers.ico")
        if os.path.exists(icon_path):
            self._icon_lbl.setPixmap(QIcon(icon_path).pixmap(26, 26))
        else:
            # Резерв: рендерим текстовый символ если .ico не найден
            self._icon_lbl.setText("🤫")
            self._icon_lbl.setStyleSheet(
                "font-size: 20px; background: transparent; border: none;"
            )
        self._icon_lbl.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._icon_lbl)

        # Одна строка: "Тебе шепчет  NickName"
        self._text_lbl = QLabel("Тебе шепчет  ...")
        self._text_lbl.setStyleSheet(
            "color: #ecf0f1; font-size: 13px; font-weight: bold; "
            "background: transparent; border: none; letter-spacing: 0.3px;"
        )
        layout.addWidget(self._text_lbl, stretch=1)
        # Анимация намеренно убрана: оверлей горит ровно, без мигания,
        # пока идут пакеты шёпота, и гасится сразу по их окончании.

    def _reposition(self):
        """Растягиваем на всю ширину экрана, прибиваем к верхнему краю."""
        try:
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen:
                g = screen.availableGeometry()
                self.setFixedWidth(g.width())
                self.move(g.left(), g.top())
        except Exception:
            pass

    def show_for(self, nick: str):
        """Показать оверлей с именем шептуна."""
        self._text_lbl.setText(f"Тебе шепчет  {nick}")
        self._reposition()
        self.show()

    def hide_overlay(self):
        """Скрыть оверлей."""
        self.hide()

    def paintEvent(self, event):
        """Полноширинная полупрозрачная плашка — рисуем вручную (WA_TranslucentBackground)."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Фон — тёмная полоса на всю ширину
        p.setBrush(QBrush(QColor(15, 17, 32, 220)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(self.rect())
        # Тонкая акцентная линия снизу
        p.setPen(QPen(QColor(93, 173, 226, 180), 2))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()


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
        # Увеличенное окно — 6 вкладок помещаются без скролла при обычном размере.
        # При уменьшении окна QTabWidget автоматически покажет стрелки прокрутки.
        self.resize(780, 660)
        self.setMinimumSize(480, 520)

        main_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        # Прокрутка вкладок при нехватке места (стрелки ◄ ►)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setStyleSheet("""
            QTabBar::scroller {
                width: 20px;
            }
            QTabBar QToolButton {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 4px;
                color: #cccccc;
            }
            QTabBar QToolButton:hover {
                background: rgba(255,255,255,0.14);
            }
        """)

        # 1. Профиль
        self.setup_profile_tab()

        # 2. Аудио
        self.setup_audio_tab()

        # 3. Персонализация (Тема + Хоткеи в одной вкладке)
        self.setup_personalization_tab()

        # 4. Шёпот — PTT горячие клавиши
        self.setup_whisper_tab()

        # 5. SoundBoard — кастомные звуки + громкость
        self.setup_soundboard_tab()

        # 6. Версия
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

        # Системные звуки (уведомления):  слайдер 0-100, но применяется КВАДРАТ
        # (slider/100)^2.  Это выравнивает перцептивную громкость:
        #   0% →  0.00x  (тихо)
        #  20% →  0.04x  (‑28 dB, комфортно для фоновых уведомлений)
        #  50% →  0.25x  (‑12 dB, средне)
        # 100% →  1.00x  (0 dB, максимум pygame)
        # При линейной шкале default 70 → pygame vol 0.70 — слишком громко.
        # При квадратичной default 30 → 0.09x (≈ −21 dB) — ненавязчиво.
        sys_vol = int(self.app_settings.value("system_sound_volume", 30))
        self.lbl_sys = QLabel(f"Системные звуки: {sys_vol}%")
        self.sl_sys = QSlider(Qt.Orientation.Horizontal)
        self.sl_sys.setRange(0, 100)
        self.sl_sys.setValue(sys_vol)
        self.sl_sys.valueChanged.connect(lambda v: self.lbl_sys.setText(f"Системные звуки: {v}%"))
        aud_lay.addWidget(self.lbl_sys)
        aud_lay.addWidget(self.sl_sys)

        # Примечание: ползунок громкости Soundboard перенесён на вкладку «SoundBoard»
        hint_sb = QLabel("🎵  Громкость Soundboard — на вкладке «SoundBoard»")
        hint_sb.setStyleSheet("font-size: 11px; color: #888;")
        aud_lay.addWidget(hint_sb)

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

    # ── Вкладка «Шёпот» ──────────────────────────────────────────────────────
    def setup_whisper_tab(self):
        """
        5 PTT-слотов: каждый — выбор собеседника (из known_users.json) +
        сочетание клавиш. Тема применяется автоматически через QDialog stylesheet
        родителя (MainWindow.apply_theme): QComboBox, QLineEdit, QPushButton,
        QLabel, QGroupBox наследуют bg/text/border от него.
        """
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)

        # ── Описание ──────────────────────────────────────────────────────────
        desc = QLabel(
            "Удерживай клавишу → голос идёт только этому собеседнику (PTT-шёпот).\n"
            "Работает поверх любых окон (игр, браузера и т.д.).\n"
            "Формат клавиш: <b>alt+1</b>, <b>ctrl+shift+w</b>, <b>f8</b> и т.д."
        )
        desc.setTextFormat(Qt.TextFormat.RichText)
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12px; line-height: 1.5;")
        lay.addWidget(desc)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        lay.addWidget(sep)

        # ── Читаем известных пользователей ────────────────────────────────────
        # known_users.json: {ip: {nick, first_seen, last_seen}}
        # Строим список (nick, ip) — показываем актуальный ник, ключ — IP.
        # Это позволяет горячим клавишам работать даже после смены ника у пользователя:
        # при следующем открытии настроек комбобокс покажет уже новый ник по тому же IP.
        known_users_by_ip: dict = {}   # ip → nick
        try:
            if os.path.exists("known_users.json"):
                with open("known_users.json", "r", encoding="utf-8") as f:
                    registry: dict = json.load(f)
                known_users_by_ip = {
                    ip: v.get("nick", "")
                    for ip, v in registry.items()
                    if v.get("nick", "")
                }
        except Exception:
            pass

        # Список (display_nick, ip), отсортированный по нику (без учёта регистра)
        known_users_list: list[tuple[str, str]] = sorted(
            known_users_by_ip.items(),   # (ip, nick) → swap to (nick, ip) below
            key=lambda kv: kv[1].lower()  # sort by nick (value)
        )
        # known_users_by_ip.items() → (ip, nick); после sort переворачиваем для удобства
        known_users_list = [(nick, ip) for ip, nick in known_users_list]

        EMPTY = "— не выбрано —"

        # ── Заголовки колонок ─────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setContentsMargins(4, 0, 4, 0)
        n_lbl = QLabel("#")
        n_lbl.setFixedWidth(20)
        n_lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        p_lbl = QLabel("Собеседник")
        p_lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        k_lbl = QLabel("Горячая клавиша (удерживать)")
        k_lbl.setStyleSheet("font-weight: bold; font-size: 12px;")
        hdr.addWidget(n_lbl)
        hdr.addWidget(p_lbl, stretch=3)
        hdr.addSpacing(8)
        hdr.addWidget(k_lbl, stretch=4)
        lay.addLayout(hdr)

        # ── 5 слотов ──────────────────────────────────────────────────────────
        self._w_slots: list[tuple[QComboBox, QLineEdit]] = []

        for i in range(5):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.setContentsMargins(0, 0, 0, 0)

            num = QLabel(str(i + 1))
            num.setFixedWidth(20)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet("color: #888; font-size: 12px;")

            # Комбобокс — собеседник
            # userData каждого item = IP пользователя (пустая строка для «не выбрано»)
            cb = QComboBox()
            cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            cb.setMinimumWidth(150)
            cb.addItem(EMPTY, "")  # index 0: не выбрано, userData=""
            for nick_text, ip_addr in known_users_list:
                cb.addItem(nick_text, ip_addr)  # userData=ip

            # Восстанавливаем сохранённый выбор:
            # Приоритет — по IP: ник мог смениться, но IP остаётся тем же.
            saved_ip   = self.app_settings.value(f"whisper_slot_{i}_ip",   "")
            saved_nick = self.app_settings.value(f"whisper_slot_{i}_nick", "")

            selected = False
            if saved_ip:
                # Ищем item с совпадающим IP в userData
                for j in range(cb.count()):
                    if cb.itemData(j) == saved_ip:
                        cb.setCurrentIndex(j)
                        selected = True
                        break
            if not selected and saved_nick:
                # Фолбэк: старые сохранения без IP — ищем по нику
                for j in range(1, cb.count()):
                    if cb.itemText(j) == saved_nick:
                        cb.setCurrentIndex(j)
                        break

            # Поле горячей клавиши
            le = QLineEdit()
            le.setPlaceholderText("напр. alt+1, ctrl+shift+w, f8")
            le.setMinimumWidth(180)
            saved_hk = self.app_settings.value(f"whisper_slot_{i}_hk", "")
            le.setText(saved_hk)

            row.addWidget(num)
            row.addWidget(cb, stretch=3)
            row.addWidget(le, stretch=4)
            lay.addLayout(row)

            self._w_slots.append((cb, le))

        # ── Кнопка очистки ────────────────────────────────────────────────────
        lay.addSpacing(4)
        btn_clear = QPushButton("Очистить все слоты")
        btn_clear.setStyleSheet(
            "QPushButton { color: #e74c3c; border: 1px solid #e74c3c; "
            "border-radius: 6px; padding: 4px 14px; }"
            "QPushButton:hover { background-color: rgba(231,76,60,0.12); }"
        )
        btn_clear.setFixedWidth(200)
        btn_clear.clicked.connect(self._clear_whisper_slots)
        lay.addWidget(btn_clear, alignment=Qt.AlignmentFlag.AlignLeft)

        # ── Примечание о формате ──────────────────────────────────────────────
        note = QLabel(
            "ℹ  Если нужный собеседник не появляется — он ещё не был в сессии.\n"
            "    Зайди в канал вместе с ним, список обновится автоматически."
        )
        note.setStyleSheet("font-size: 11px; color: #888;")
        note.setWordWrap(True)
        lay.addSpacing(6)
        lay.addWidget(note)

        lay.addStretch()
        self.tabs.addTab(tab, "Шёпот")

    def _clear_whisper_slots(self):
        for cb, le in self._w_slots:
            cb.setCurrentIndex(0)
            le.clear()

    # ── Вкладка «SoundBoard» ──────────────────────────────────────────────────
    def setup_soundboard_tab(self):
        """
        Вкладка управления Soundboard:
        - Ползунок громкости (перенесён с вкладки Аудио)
        - 3 слота кастомных звуков: выбор файла mp3/wav с ПК (макс. 1 МБ),
          отображение имени, кнопка удаления.

        Хранение: QSettings, ключи custom_sound_{i}_path и custom_sound_{i}_name.
        Воспроизведение: файл читается в байты → base64 → поле data_b64 в
        JSON-пакете CMD_SOUNDBOARD. Сервер ретранслирует его без изменений.
        Клиенты декодируют base64 и воспроизводят из памяти (BytesIO).
        """
        tab = QWidget()
        lay = QVBoxLayout(tab)
        lay.setSpacing(14)
        lay.setContentsMargins(16, 16, 16, 16)

        # ── Блок: Громкость Soundboard ────────────────────────────────────────
        vol_group = QGroupBox("🔊  Громкость Soundboard")
        vol_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        vol_lay = QVBoxLayout(vol_group)

        vol_hint = QLabel(
            "Квадратичная кривая: 40% → –16 dB, 70% → –6 dB, 100% → 0 dB (полная)."
        )
        vol_hint.setStyleSheet("font-size: 11px; color: #888; font-weight: normal;")
        vol_hint.setWordWrap(True)
        vol_lay.addWidget(vol_hint)

        sb_vol = int(self.app_settings.value("soundboard_volume", 40))
        self.lbl_sb = QLabel(f"Soundboard: {sb_vol}%")
        self.sl_sb = QSlider(Qt.Orientation.Horizontal)
        self.sl_sb.setRange(0, 100)
        self.sl_sb.setValue(sb_vol)
        self.sl_sb.valueChanged.connect(lambda v: self.lbl_sb.setText(f"Soundboard: {v}%"))
        vol_lay.addWidget(self.lbl_sb)
        vol_lay.addWidget(self.sl_sb)
        lay.addWidget(vol_group)

        # ── Блок: Кастомные звуки ─────────────────────────────────────────────
        cust_group = QGroupBox("🎵  Мои звуки (до 3 слотов)")
        cust_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        cust_lay = QVBoxLayout(cust_group)

        desc = QLabel(
            "Добавьте собственные звуки (.mp3 / .wav), максимум 1 МБ (~7 сек).\n"
            "Звук будет воспроизводиться у всех участников канала при нажатии кнопки."
        )
        desc.setStyleSheet("font-size: 11px; color: #aaa; font-weight: normal;")
        desc.setWordWrap(True)
        cust_lay.addWidget(desc)

        self._custom_sound_rows: list[dict] = []   # список виджетов каждого слота

        for i in range(CUSTOM_SOUND_SLOTS):
            saved_path = self.app_settings.value(f"custom_sound_{i}_path", "")
            saved_name = self.app_settings.value(f"custom_sound_{i}_name", "")
            self._add_custom_sound_row(cust_lay, i, saved_path, saved_name)

        lay.addWidget(cust_group)
        lay.addStretch()
        self.tabs.addTab(tab, "SoundBoard")

    def _add_custom_sound_row(self, parent_lay: QVBoxLayout, idx: int,
                               saved_path: str = "", saved_name: str = ""):
        """Создаёт строку кастомного звука с кнопками Browse и Delete."""
        row_frame = QFrame()
        row_frame.setStyleSheet("""
            QFrame {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.09);
                border-radius: 8px;
            }
        """)
        row_lay = QHBoxLayout(row_frame)
        row_lay.setContentsMargins(10, 7, 10, 7)
        row_lay.setSpacing(8)

        # Номер слота
        num_lbl = QLabel(f"#{idx + 1}")
        num_lbl.setFixedWidth(24)
        num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num_lbl.setStyleSheet(
            "font-size: 12px; font-weight: bold; color: #888; "
            "background: transparent; border: none;"
        )
        row_lay.addWidget(num_lbl)

        # Имя файла (или заглушка)
        name_lbl = QLabel(saved_name if saved_name else "— не выбрано —")
        name_lbl.setStyleSheet(
            "font-size: 12px; color: #ccc; background: transparent; border: none;"
        )
        name_lbl.setMinimumWidth(160)
        name_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        name_lbl.setToolTip(saved_path)
        row_lay.addWidget(name_lbl, stretch=1)

        # Кнопка «Выбрать»
        btn_browse = QPushButton("📂  Выбрать")
        btn_browse.setFixedHeight(28)
        btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_browse.setStyleSheet("""
            QPushButton {
                background: rgba(88,101,242,0.25);
                color: #a0b0ff;
                border: 1px solid rgba(88,101,242,0.55);
                border-radius: 6px;
                padding: 0 10px;
                font-size: 12px;
            }
            QPushButton:hover {
                background: rgba(88,101,242,0.45);
                color: #ffffff;
            }
        """)
        row_lay.addWidget(btn_browse)

        # Кнопка «Удалить»
        btn_del = QPushButton("🗑")
        btn_del.setFixedSize(28, 28)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setEnabled(bool(saved_path))
        btn_del.setStyleSheet("""
            QPushButton {
                background: rgba(220,60,60,0.18);
                color: #e87070;
                border: 1px solid rgba(220,60,60,0.40);
                border-radius: 6px;
                font-size: 14px;
            }
            QPushButton:hover {
                background: rgba(220,60,60,0.38);
                color: #ffffff;
            }
            QPushButton:disabled {
                background: transparent;
                color: #555;
                border-color: rgba(255,255,255,0.08);
            }
        """)
        row_lay.addWidget(btn_del)

        slot = {"path": saved_path, "name": saved_name,
                "name_lbl": name_lbl, "btn_del": btn_del}
        self._custom_sound_rows.append(slot)

        # ── Слоты ──────────────────────────────────────────────────────────────
        def _on_browse(checked=False, _idx=idx, _slot=slot):
            path, _ = QFileDialog.getOpenFileName(
                self, f"Выбрать звук для слота #{_idx + 1}",
                "", "Аудио файлы (*.mp3 *.wav)"
            )
            if not path:
                return
            # Проверка размера
            try:
                fsize = os.path.getsize(path)
            except OSError:
                fsize = 0
            if fsize > CUSTOM_SOUND_MAX_BYTES:
                QMessageBox.warning(
                    self, "Файл слишком большой",
                    f"Максимальный размер — 1 МБ (~7 сек).\n"
                    f"Выбранный файл: {fsize // 1024} КБ."
                )
                return
            # Проверка длительности для WAV
            if path.lower().endswith(".wav"):
                try:
                    with wave.open(path, 'rb') as wf:
                        dur = wf.getnframes() / wf.getframerate()
                    if dur > 7.5:
                        QMessageBox.warning(
                            self, "Звук слишком длинный",
                            f"Максимальная длительность — 7 секунд.\n"
                            f"Длительность файла: {dur:.1f} сек."
                        )
                        return
                except Exception:
                    pass  # не WAV-совместимый заголовок — пропускаем проверку

            name = os.path.splitext(os.path.basename(path))[0]
            _slot["path"] = path
            _slot["name"] = name
            _slot["name_lbl"].setText(name)
            _slot["name_lbl"].setToolTip(path)
            _slot["name_lbl"].setStyleSheet(
                "font-size: 12px; color: #7ecf8e; background: transparent; border: none;"
            )
            _slot["btn_del"].setEnabled(True)
            # Сохраняем немедленно — чтобы SoundboardPanel мог перестроиться
            self.app_settings.setValue(f"custom_sound_{_idx}_path", path)
            self.app_settings.setValue(f"custom_sound_{_idx}_name", name)
            # Перестраиваем панель если открыта
            self._rebuild_sb_panel_if_open()

        def _on_delete(checked=False, _idx=idx, _slot=slot):
            _slot["path"] = ""
            _slot["name"] = ""
            _slot["name_lbl"].setText("— не выбрано —")
            _slot["name_lbl"].setToolTip("")
            _slot["name_lbl"].setStyleSheet(
                "font-size: 12px; color: #ccc; background: transparent; border: none;"
            )
            _slot["btn_del"].setEnabled(False)
            self.app_settings.setValue(f"custom_sound_{_idx}_path", "")
            self.app_settings.setValue(f"custom_sound_{_idx}_name", "")
            self._rebuild_sb_panel_if_open()

        btn_browse.clicked.connect(_on_browse)
        btn_del.clicked.connect(_on_delete)

        parent_lay.addWidget(row_frame)

    def _rebuild_sb_panel_if_open(self):
        """Перестраивает SoundboardPanel если он сейчас открыт."""
        try:
            mw = self.mw
            if hasattr(mw, '_sb_panel') and mw._sb_panel is not None:
                try:
                    if mw._sb_panel.isVisible():
                        mw._sb_panel.rebuild()
                except RuntimeError:
                    pass
        except Exception:
            pass

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

        # Сохраняем слоты PTT-шёпота (до 5)
        for i, (cb, le) in enumerate(self._w_slots):
            # Если выбрано «не выбрано» (index 0) — сохраняем пустые строки
            if cb.currentIndex() == 0:
                nick = ""
                ip   = ""
            else:
                nick = cb.currentText()
                ip   = cb.currentData() or ""  # userData = IP
            hk   = le.text().strip()
            s.setValue(f"whisper_slot_{i}_nick", nick)
            s.setValue(f"whisper_slot_{i}_ip",   ip)
            s.setValue(f"whisper_slot_{i}_hk",   hk)

        self.mw.nick = self.ed_nick.text()
        self.mw.avatar = self.cur_av
        self.mw.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.mw.nick}")
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
        # Скрыта по умолчанию, показывается 4 с через flash_from_nick().
        self._from_nick_lbl = QLabel("")
        self._from_nick_lbl.setStyleSheet("""
            color: #f5c518;
            font-size: 12px;
            font-weight: bold;
            background: transparent;
            border: none;
            padding: 0 6px;
        """)
        self._from_nick_lbl.setVisible(False)

        # Таймер скрытия метки (single-shot, 4 с)
        self._from_nick_timer = QTimer(self)
        self._from_nick_timer.setSingleShot(True)
        self._from_nick_timer.timeout.connect(self._hide_from_nick_lbl)

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
                    accent_color="#27ae60",
                    is_custom=True
                )

            # Вычисляем высоту с учётом обоих секций
            total_rows = 0
            if has_default:
                total_rows += (len(default_files) + 1) // 2
            if has_custom:
                total_rows += (len(custom_sounds) + 1) // 2
                if has_default:
                    total_rows += 1  # заголовок второй секции

            ROW_H = 34 + 6
            visible_rows = min(7, total_rows + (1 if has_default else 0) + (1 if has_custom else 0))
            scroll.setFixedHeight(max(50, visible_rows * ROW_H + 10))
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