import os
import json
import sounddevice as sd
# Мы убрали mss, dxcam импортируем только для логов, если нужно,
# но для списка используем возможности GUI
import dxcam
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QPushButton, QScrollArea,
                             QWidget, QGridLayout, QLabel, QSlider, QTabWidget,
                             QComboBox, QProgressBar, QLineEdit, QCheckBox)
# Добавил QGuiApplication в импорты для корректного получения списка экранов
from PyQt6.QtCore import Qt, QSize, QSettings
from PyQt6.QtGui import QIcon, QGuiApplication
from config import resource_path, CMD_SOUNDBOARD
from audio_engine import PYRNNOISE_AVAILABLE
from version import APP_VERSION, APP_NAME, APP_AUTHOR, APP_YEAR, ABOUT_TEXT, GITHUB_REPO


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


class SettingsDialog(QDialog):
    def __init__(self, audio_engine, parent):
        super().__init__(parent)
        self.audio = audio_engine
        self.mw = parent  # MainWindow
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.setWindowTitle("Настройки")
        self.resize(600, 550)


        main_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # 1. Profile
        self.setup_profile_tab()

        # 2. Appearance
        app_tab = QWidget()
        app_lay = QVBoxLayout(app_tab)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Светлая", "Темная"])
        self.theme_combo.setCurrentText(self.app_settings.value("theme", "Светлая"))
        self.theme_combo.currentTextChanged.connect(self.mw.apply_theme)
        app_lay.addWidget(QLabel("Тема оформления:"))
        app_lay.addWidget(self.theme_combo)
        app_lay.addStretch()
        self.tabs.addTab(app_tab, "Тема")

        # 3. Audio
        aud_tab = QWidget()
        aud_lay = QVBoxLayout(aud_tab)
        self.cb_in = QComboBox()
        self.cb_out = QComboBox()
        self.refresh_devices_list()

        stat = "ВКЛ" if self.audio.use_noise_reduction else "ВЫКЛ"
        if not PYRNNOISE_AVAILABLE: stat = "НЕТ МОДУЛЯ"
        self.btn_nr = QPushButton(f"Шумодав: {stat}")
        self.btn_nr.setObjectName("btn_nr")
        self.btn_nr.setCheckable(True)
        self.btn_nr.setEnabled(PYRNNOISE_AVAILABLE)
        self.btn_nr.setChecked(self.audio.use_noise_reduction)
        self.btn_nr.clicked.connect(self.toggle_nr)

        aud_lay.addWidget(QLabel("Качество звука (Битрейт):"))
        self.cb_bitrate = QComboBox()
        # Оптимальные пресеты для Opus
        bitrate_options = {"8 kbps (Рация)": 8, "24 kbps (Стандарт)": 24,
                           "64 kbps (Хорошее)": 64}

        for text, val in bitrate_options.items():
            self.cb_bitrate.addItem(text, val)

        # Устанавливаем текущее значение из настроек
        current_bitrate = int(self.app_settings.value("audio_bitrate", 64000)) // 1000
        index = self.cb_bitrate.findData(current_bitrate)
        if index != -1:
            self.cb_bitrate.setCurrentIndex(index)

        # Коннектим сигнал изменения
        self.cb_bitrate.currentIndexChanged.connect(
            lambda: self.audio.set_bitrate(self.cb_bitrate.currentData())
        )
        aud_lay.addWidget(self.cb_bitrate)

        self.progress = QProgressBar()
        self.audio.volume_level_signal.connect(self.progress.setValue)

        aud_lay.addWidget(QLabel("Ввод:"));
        aud_lay.addWidget(self.cb_in)
        aud_lay.addWidget(QLabel("Вывод:"));
        aud_lay.addWidget(self.cb_out)
        aud_lay.addWidget(self.btn_nr);
        aud_lay.addWidget(QLabel("Микрофон:"))
        aud_lay.addWidget(self.progress)

        # Sliders
        aud_lay.addSpacing(15)
        sys_vol = int(self.app_settings.value("system_sound_volume", 70))
        self.lbl_sys = QLabel(f"Системные звуки: {sys_vol}%")
        self.sl_sys = QSlider(Qt.Orientation.Horizontal)
        self.sl_sys.setRange(0, 100);
        self.sl_sys.setValue(sys_vol)
        self.sl_sys.valueChanged.connect(lambda v: self.lbl_sys.setText(f"Системные звуки: {v}%"))
        aud_lay.addWidget(self.lbl_sys);
        aud_lay.addWidget(self.sl_sys)

        sb_vol = int(self.app_settings.value("soundboard_volume", 50))
        self.lbl_sb = QLabel(f"Soundboard: {sb_vol}%")
        self.sl_sb = QSlider(Qt.Orientation.Horizontal)
        self.sl_sb.setRange(0, 100);
        self.sl_sb.setValue(sb_vol)
        self.sl_sb.valueChanged.connect(lambda v: self.lbl_sb.setText(f"Soundboard: {v}%"))
        aud_lay.addWidget(self.lbl_sb);
        aud_lay.addWidget(self.sl_sb)

        # Ползунок порога голосовой активности (VAD)
        aud_lay.addSpacing(10)
        vad_slider_val = int(self.app_settings.value("vad_threshold_slider", 5))
        self.lbl_vad = QLabel()
        self._update_vad_label(vad_slider_val)
        self.sl_vad = QSlider(Qt.Orientation.Horizontal)
        self.sl_vad.setRange(1, 50)
        self.sl_vad.setValue(vad_slider_val)
        self.sl_vad.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sl_vad.setTickInterval(5)
        self.sl_vad.valueChanged.connect(self._on_vad_slider_changed)
        aud_lay.addWidget(self.lbl_vad)
        aud_lay.addWidget(self.sl_vad)

        aud_lay.addStretch()
        self.tabs.addTab(aud_tab, "Аудио")

        # 4. Hotkeys
        hk_tab = QWidget()
        hk_lay = QVBoxLayout(hk_tab)
        self.hk_mute = QLineEdit(self.app_settings.value("hk_mute", "alt+["))
        self.hk_deafen = QLineEdit(self.app_settings.value("hk_deafen", "alt+]"))
        hk_lay.addWidget(QLabel("Mute микрофона:"));
        hk_lay.addWidget(self.hk_mute)
        hk_lay.addWidget(QLabel("Deafen (динамики):"));
        hk_lay.addWidget(self.hk_deafen)
        btn_res = QPushButton("Сбросить")
        btn_res.clicked.connect(lambda: (self.hk_mute.setText("alt+["), self.hk_deafen.setText("alt+]")))
        hk_lay.addWidget(btn_res);
        hk_lay.addStretch()
        self.tabs.addTab(hk_tab, "Хоткеи")

        # ── 5. Версия ─────────────────────────────────────────────────────────
        self.setup_version_tab()

        main_layout.addWidget(self.tabs)
        btn_save = QPushButton("Сохранить")
        btn_save.clicked.connect(self.save_all)
        main_layout.addWidget(btn_save)

    # ── Вкладка «Версия» ──────────────────────────────────────────────────────

    def setup_version_tab(self):
        """Вкладка с информацией о версии и кнопкой проверки обновлений."""
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QFrame

        # ── Сигнальный мост фоновый-поток → UI-поток ─────────────────────────
        # QTimer.singleShot из не-Qt потока ненадёжен в PyQt6.
        # Единственный корректный способ: emit signal — Qt сам доставит его
        # в главный поток через очередь событий.
        class _Bridge(QObject):
            sig_found   = pyqtSignal(str, str)   # (version, url)
            sig_no_upd  = pyqtSignal()
            sig_error   = pyqtSignal(str)         # (message,)
            sig_progress = pyqtSignal(int)        # (percent,)
            sig_done    = pyqtSignal()

        self._upd_bridge = _Bridge()
        self._upd_bridge.sig_found.connect(self._slot_update_found)
        self._upd_bridge.sig_no_upd.connect(self._slot_no_update)
        self._upd_bridge.sig_error.connect(self._slot_update_error)
        self._upd_bridge.sig_progress.connect(self._slot_progress)
        self._upd_bridge.sig_done.connect(self._slot_download_done)

        # ── UI ────────────────────────────────────────────────────────────────
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

    # ── Слоты (вызываются ТОЛЬКО в UI-потоке через сигналы) ──────────────────

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

    # ── Проверка обновлений ───────────────────────────────────────────────────

    def _on_check_update_clicked(self):
        from updater import check_for_updates_async
        self._btn_check_update.setEnabled(False)
        self._btn_install_update.setVisible(False)
        self._ver_progress.setVisible(False)
        self._ver_status_lbl.setText("⏳ Проверяю...")

        bridge = self._upd_bridge   # локальная ссылка для захвата в лямбдах

        check_for_updates_async(
            on_update_found = lambda v, u: bridge.sig_found.emit(v, u),
            on_no_update    = lambda:       bridge.sig_no_upd.emit(),
            on_error        = lambda msg:   bridge.sig_error.emit(msg),
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
            on_progress = lambda pct: bridge.sig_progress.emit(pct),
            on_done     = lambda:     bridge.sig_done.emit(),
            on_error    = lambda msg: bridge.sig_error.emit(msg),
        )

    # ── Профиль (без изменений) ───────────────────────────────────────────────

    def setup_profile_tab(self):
        tab = QWidget();
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
        lay.addWidget(self.ed_nick);
        lay.addStretch()
        self.tabs.addTab(tab, "О себе")

    def open_av_sel(self):
        d = AvatarSelector(self)
        if d.exec():
            self.cur_av = d.selected_avatar;
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
        self.cb_in.clear();
        self.cb_out.clear()
        u_in, u_out = set(), set()
        s_in = self.app_settings.value("device_in_name", "")
        s_out = self.app_settings.value("device_out_name", "")

        for d in devs:
            api = sd.query_hostapis(d['hostapi'])['name']
            if api != def_api: continue
            dn = f"{d['name']} ({api})"
            if d['max_input_channels'] > 0 and dn not in u_in:
                self.cb_in.addItem(dn);
                u_in.add(dn)
            if d['max_output_channels'] > 0 and dn not in u_out:
                self.cb_out.addItem(dn);
                u_out.add(dn)
        self.cb_in.setCurrentText(s_in);
        self.cb_out.setCurrentText(s_out)

    def _update_vad_label(self, val: int):
        """Обновить подпись ползунка VAD с понятным описанием чувствительности."""
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
            f"Порог активации микрофона (VAD): {threshold:.3f}  —  чувствительность: {desc}"
        )

    def _on_vad_slider_changed(self, val: int):
        """Мгновенно применяет новый порог VAD и обновляет подпись."""
        self._update_vad_label(val)
        self.audio.set_vad_threshold(val)

    def toggle_nr(self):
        self.audio.use_noise_reduction = self.btn_nr.isChecked()
        self.btn_nr.setText(f"Шумодав: {'ВКЛ' if self.audio.use_noise_reduction else 'ВЫКЛ'}")
        if self.parent(): self.parent().app_settings.setValue("noise_reduction", self.audio.use_noise_reduction)

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
        if hasattr(self.mw, 'net'): self.mw.net.update_user_info(self.mw.nick, self.mw.avatar)

        # Update JSON config if exists
        if os.path.exists("user_config.json"):
            try:
                with open("user_config.json", 'r') as f:
                    d = json.load(f)
                d['nick'] = self.mw.nick;
                d['avatar'] = self.mw.avatar
                with open("user_config.json", 'w') as f:
                    json.dump(d, f)
            except:
                pass
        self.accept()


class StreamSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки трансляции")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)

        # 1. Выбор монитора
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

        # 2. Выбор разрешения
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

        # 3. Частота кадров (FPS)
        layout.addWidget(QLabel("Частота кадров (FPS):"))
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["15", "30", "60"])
        self.fps_combo.setCurrentText("30")
        layout.addWidget(self.fps_combo)

        layout.addSpacing(10)

        # ── Раздел «Звук» ───────────────────────────────────────────────────────
        sep = QLabel("── Аудио трансляции ──────────────────")
        sep.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(sep)

        # Галочка «Транслировать звук»
        self.cb_stream_audio = QCheckBox("🔊 Транслировать звук")
        self.cb_stream_audio.setChecked(False)
        layout.addWidget(self.cb_stream_audio)

        # ── Статус VB-CABLE ─────────────────────────────────────────────────────
        self._vbc_banner = QLabel()
        self._vbc_banner.setWordWrap(True)
        self._vbc_banner.setStyleSheet(
            "border-radius: 6px; padding: 8px; font-size: 12px;"
        )
        layout.addWidget(self._vbc_banner)

        self._btn_vbc_install = QPushButton("⬇  Установить VB-CABLE")
        self._btn_vbc_install.setStyleSheet(
            "background-color: #e67e22; color: white; font-weight: bold; height: 34px;"
        )
        self._btn_vbc_install.clicked.connect(self._on_install_vbcable)
        layout.addWidget(self._btn_vbc_install)

        # Подсказка по настройке (показывается только когда VB-CABLE установлен)
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

        # Обновляем видимость элементов
        self._refresh_vbc_ui()

        # Когда пользователь включает/выключает трансляцию звука — обновляем UI
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

    # ── VB-CABLE helpers ──────────────────────────────────────────────────────

    def _refresh_vbc_ui(self):
        """Обновляет статус-баннер и кнопку установки по текущему состоянию VB-CABLE."""
        try:
            from vbcable_installer import is_vbcable_installed, find_zip
            installed = is_vbcable_installed()
        except ImportError:
            installed = False
            find_zip = lambda: None

        audio_on = self.cb_stream_audio.isChecked()

        if installed:
            self._vbc_banner.setText(
                "✅  VB-CABLE установлен — захват без эха активен"
            )
            self._vbc_banner.setStyleSheet(
                "background-color: #1e8449; color: #a9dfbf; "
                "border-radius: 6px; padding: 8px; font-size: 12px;"
            )
            self._btn_vbc_install.setVisible(False)
            self._hint_lbl.setVisible(audio_on)
        else:
            # Проверяем наличие архива
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

        # Весь блок VB-CABLE виден только когда включена трансляция звука
        self._vbc_banner.setVisible(audio_on)
        self._btn_vbc_install.setVisible(
            audio_on and not installed and self._btn_vbc_install.isVisible()
        )
        self._hint_lbl.setVisible(audio_on and installed)
        self.adjustSize()

    def _on_audio_toggled(self, checked):
        self._refresh_vbc_ui()

    def _on_install_vbcable(self):
        """Запускает установку VB-CABLE."""
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

    # ── get_settings ──────────────────────────────────────────────────────────

    def get_settings(self):
        res_text = self.res_combo.currentText()
        width, height = self.res_options[res_text]
        audio_enabled = self.cb_stream_audio.isChecked()
        return {
            "monitor_idx":   self.monitor_combo.currentData(),
            "width":         width,
            "height":        height,
            "fps":           int(self.fps_combo.currentText()),
            "stream_audio":  audio_enabled,
            # system_audio_device: None означает «VB-CABLE (автоопределение)»
            # или WASAPI Loopback по умолчанию если VB-CABLE не найден
            "system_audio":         audio_enabled,
            "system_audio_device":  None,
        }

class SoundboardDialog(QDialog):
    def __init__(self, net_client, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Soundboard")
        self.setFixedSize(350, 400)
        self.net = net_client
        layout = QVBoxLayout(self)
        scroll = QScrollArea();
        scroll.setWidgetResizable(True)
        container = QWidget();
        grid = QGridLayout(container)
        sd_dir = resource_path("assets/panel")

        if os.path.exists(sd_dir):
            files = sorted([f for f in os.listdir(sd_dir) if f.lower().endswith(('.wav', '.mp3'))])
            row, col = 0, 0
            for f in files:
                btn = QPushButton(f.split('.')[0])
                btn.setMinimumHeight(40)
                btn.clicked.connect(lambda ch, fname=f: self.net.send_json({"action": CMD_SOUNDBOARD, "file": fname}))
                grid.addWidget(btn, row, col)
                col += 1
                if col > 1: col = 0; row += 1

        container.setLayout(grid);
        scroll.setWidget(container)
        layout.addWidget(scroll)