from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QComboBox, QCheckBox,
                             QFrame, QWidget, QPushButton)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication

from config import get_bitrate_for_resolution
from .ui_dialogs import _DialogTitleBar


class StreamSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Настройки трансляции")
        self.setMinimumWidth(400)

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
            QLabel#hintLabel {
                color: #8899bb; font-size: 11px;
            }
        """)
        root_lay.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        self._title_bar = _DialogTitleBar(self, "📺  Настройки трансляции")
        card_lay.addWidget(self._title_bar)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setFixedHeight(1)
        _sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(_sep)

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
                geometry    = screen.geometry()
                screen_name = screen.name()
                display_text = (
                    f"Монитор {i} [{screen_name}] "
                    f"({geometry.width()}×{geometry.height()})"
                )
                self.monitor_combo.addItem(display_text, i)
            if not screens:
                self.monitor_combo.addItem("Основной монитор", 0)
        except Exception as e:
            print(f"[UI] Error listing screens: {e}")
            self.monitor_combo.addItem("Основной монитор", 0)

        layout.addWidget(self.monitor_combo)

        layout.addWidget(QLabel("Разрешение:"))
        self.res_combo = QComboBox()
        # Храним только (width, height), описание формируется динамически
        # после выбора fps — чтобы показать актуальный bitrate.
        self._res_choices: list[tuple[int, int]] = [
            (1920, 1080),   # 1080p (Full HD)
            (1280,  720),   # 720p (HD)
            ( 854,  480),   # 480p (SD)
            ( 640,  360),   # 360p
        ]
        for (w, h) in self._res_choices:
            label = {
                (1920, 1080): "1080p  (Full HD)",
                (1280,  720): "720p   (HD)",
                ( 854,  480): "480p   (SD)",
                ( 640,  360): "360p",
            }.get((w, h), f"{w}×{h}")
            self.res_combo.addItem(label, (w, h))

        self.res_combo.setCurrentIndex(1)  # 720p по умолчанию
        layout.addWidget(self.res_combo)

        layout.addWidget(QLabel("Частота кадров (FPS):"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["30", "60"])
        self.fps_combo.setCurrentText("30")
        layout.addWidget(self.fps_combo)

        # Строка с предполагаемым битрейтом — обновляется при смене
        # разрешения или fps.
        self.lbl_bitrate = QLabel()
        self.lbl_bitrate.setObjectName("hintLabel")
        layout.addWidget(self.lbl_bitrate)

        def _update_bitrate_label():
            w, h = self.res_combo.currentData() or (1280, 720)
            fps  = int(self.fps_combo.currentText())
            br   = get_bitrate_for_resolution(w, h, fps=fps)
            self.lbl_bitrate.setText(
                f"Битрейт: ~{br // 1000} kbps ({w}×{h} @ {fps} fps). "
                f"Требуемая скорость канала: ≥ {int(br * 1.2) // 1000} kbps."
            )

        self.res_combo.currentIndexChanged.connect(lambda _: _update_bitrate_label())
        self.fps_combo.currentIndexChanged.connect(lambda _: _update_bitrate_label())
        _update_bitrate_label()

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

        self.cb_stream_audio = QCheckBox("🔊 Транслировать звук")
        self.cb_stream_audio.setChecked(True)
        layout.addWidget(self.cb_stream_audio)

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

    def get_settings(self) -> dict:
        monitor_idx  = self.monitor_combo.currentData()
        fps          = int(self.fps_combo.currentText())
        audio_on     = self.cb_stream_audio.isChecked()

        res_data = self.res_combo.currentData()
        width, height = res_data

        return {
            "monitor_idx":         monitor_idx,
            "width":               width,
            "height":              height,
            "fps":                 fps,
            "stream_audio":        audio_on,
            "system_audio":        audio_on,
            "system_audio_device": None,
        }