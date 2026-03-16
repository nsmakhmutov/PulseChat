from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QComboBox, QCheckBox,
                             QFrame, QWidget, QPushButton, QMessageBox)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication

from .ui_dialogs import _DialogTitleBar


# ──────────────────────────────────────────────────────────────────────────────
# Диалог настроек трансляции
# ──────────────────────────────────────────────────────────────────────────────
class StreamSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        # ── Безрамочный стеклянный дизайн ────────────────────────────────────
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Настройки трансляции")
        self.setMinimumWidth(360)

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self._card = QFrame(self)
        self._card.setObjectName("streamCard")
        self._card.setStyleSheet("""
            QFrame#streamCard {
                background-color: rgba(26, 28, 38, 252);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }
            QLabel { color: #c8d0e0; background: transparent; border: none; }
            QComboBox {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.13);
                border-radius: 6px;
                padding: 5px 10px;
                color: #c8d0e0;
            }
            QComboBox QAbstractItemView {
                background-color: rgba(30,33,48,255);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.13);
                selection-background-color: #3d5c9e;
                selection-color: #ffffff;
                outline: none;
            }
            QComboBox::drop-down { border: none; }
            QCheckBox { color: #c8d0e0; background: transparent; }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border: 1px solid rgba(255,255,255,0.20);
                border-radius: 4px;
                background: rgba(255,255,255,0.06);
            }
            QCheckBox::indicator:checked { background: #5b8ef5; border-color: #5b8ef5; }
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.13);
                border-color: rgba(255,255,255,0.22);
            }
        """)
        root_lay.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # Title bar
        self._title_bar = _DialogTitleBar(self, "📺  Настройки трансляции")
        card_lay.addWidget(self._title_bar)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setFixedHeight(1)
        _sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(_sep)

        # Контент
        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content_w)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        card_lay.addWidget(content_w)

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
            "720p (HD) — 4.5 Mbps": (1280, 720),
            "480p (SD) — 2.2 Mbps": (854,  480),
            "360p      — 1.0 Mbps": (640,  360),
        }
        for text in self.res_options.keys():
            self.res_combo.addItem(text)
        layout.addWidget(self.res_combo)

        layout.addWidget(QLabel("Частота кадров (FPS):"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["15", "30", "60"])
        self.fps_combo.setCurrentText("30")
        layout.addWidget(self.fps_combo)

        # ── Фикс прозрачного popup у всех трёх комбобоксов ───────────────────
        # QComboBox popup — отдельный top-level виджет: при WA_TranslucentBackground
        # родителя он рендерится прозрачным. Задаём solid-фон напрямую на view().
        def _fix_stream_combo(combo):
            try:
                v = combo.view()
                v.setStyleSheet(
                    "QAbstractItemView {"
                    "  background-color: #1e2130;"
                    "  color: #c8d0e0;"
                    "  selection-background-color: #3d5c9e;"
                    "  selection-color: #ffffff;"
                    "  border: 1px solid #333648;"
                    "  outline: none;"
                    "}"
                )
                win = v.window()
                win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                win.setStyleSheet("background-color: #1e2130;")
            except Exception:
                pass
        QTimer.singleShot(0, lambda: _fix_stream_combo(self.monitor_combo))
        QTimer.singleShot(0, lambda: _fix_stream_combo(self.res_combo))
        QTimer.singleShot(0, lambda: _fix_stream_combo(self.fps_combo))

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

        btn_start = QPushButton("▶  Запустить трансляцию")
        btn_start.setStyleSheet("""
            QPushButton {
                background-color: rgba(46,204,113,0.25);
                color: #82e0aa;
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 7px;
                font-weight: bold;
                height: 40px;
            }
            QPushButton:hover {
                background-color: rgba(46,204,113,0.40);
                border-color: rgba(46,204,113,0.80);
                color: #ffffff;
            }
        """)
        btn_start.clicked.connect(self.accept)
        layout.addWidget(btn_start)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.06);
                color: #8899bb;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 7px;
                height: 34px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.11);
                color: #c8d0e0;
            }
        """)
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
            QMessageBox.critical(self, "VB-CABLE",
                "Модуль vbcable_installer.py не найден рядом с программой.")
            return

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