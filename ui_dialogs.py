import os
import io
import json
import math
import base64
import wave
import socket
import secrets
import sounddevice as sd
import dxcam
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
                             QWidget, QGridLayout, QLabel, QSlider, QTabWidget,
                             QComboBox, QProgressBar, QLineEdit, QCheckBox, QFrame,
                             QGroupBox, QSizePolicy, QFileDialog, QMessageBox)
from PyQt6.QtCore import (Qt, QSize, QSettings, QEvent, QPropertyAnimation, QEasingCurve,
                          QRect, QPoint, QTimer, pyqtSignal, QThread)
from PyQt6.QtGui import QIcon, QGuiApplication, QPainter, QColor, QPen, QFont, QPainterPath, QBrush
from config import (resource_path, CMD_SOUNDBOARD, USER_CONFIG_PATH, KNOWN_USERS_PATH,
                    FILE_CHUNK_SIZE, FILE_TRANSFER_TIMEOUT)
from audio_engine import PYRNNOISE_AVAILABLE

# ── Максимальный размер кастомного звука (1 MB) ──────────────────────────────
# 7 секунд MP3 @ 128kbps ≈ 112 KB, @ 320kbps ≈ 280 KB.
# 1 MB с большим запасом перекрывает любой типичный 7-секундный звук.
CUSTOM_SOUND_MAX_BYTES = 1 * 1024 * 1024   # 1 MB
CUSTOM_SOUND_SLOTS     = 6                  # количество кастомных слотов


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
    """Слайдер 0-200 → коэффициент громкости по экспоненциальной кривой.
    Особый случай: slider=0 → 0.0 (полная тишина).
    Без этой проверки 10^((0-100)/100) = 10^-1 = 0.1, то есть 10% — не ноль!
    """
    if slider_int == 0:
        return 0.0
    return 10.0 ** ((slider_int - 100) / 100.0)


def _vol_to_slider(vol: float) -> int:
    """Коэффициент громкости → позиция слайдера (обратная функция)."""
    if vol <= 0.0:
        return 0
    return max(0, min(200, int(math.log10(vol) * 100 + 100)))

# Громкость буста: 15x ≈ +23.5 дБ — выше потолка слайдера (10x = +20 дБ).
# Soft-limiter в аудио-движке защищает от клиппинга.
_BOOST_VOL = 15.0
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
# Кнопка с удержанием: заполняется за 3 секунды, эмитит hold_complete
# ──────────────────────────────────────────────────────────────────────────────
class NudgeHoldButton(QPushButton):
    """
    QPushButton с механикой удержания 3 секунды.

    Логика:
      • mousePress  → запускает QTimer с шагом _TICK_MS мс.
      • каждый тик  → _progress растёт 0 → 1, вызывает update() для перерисовки.
      • mouseRelease / leaveEvent до завершения → сброс (_progress=0).
      • progress == 1 → emit hold_complete, кнопка блокируется (_fired=True).

    paintEvent:
      • super().paintEvent() рисует стандартную кнопку (фон, текст, рамка).
      • Поверх рисуем скруглённый оранжевый fill с alpha=90 (≈35%),
        шириной progress * rect.width() — текст остаётся читаемым.
    """

    hold_complete = pyqtSignal()

    _HOLD_MS = 3000   # общее время удержания, мс
    _TICK_MS = 20     # интервал таймера, мс  → 150 тиков за 3 с, ~50 FPS

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._progress: float = 0.0   # 0.0–1.0
        self._holding:  bool  = False
        self._fired:    bool  = False  # сработал → больше не принимаем нажатия

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self._TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ── Таймер ────────────────────────────────────────────────────────────────
    def _on_tick(self):
        self._progress += self._TICK_MS / self._HOLD_MS
        if self._progress >= 1.0:
            self._progress = 1.0
            self._tick_timer.stop()
            self._holding = False
            self._fired = True
            self.update()
            self.hold_complete.emit()
        else:
            self.update()

    # ── Мышь ──────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton
                and self.isEnabled()
                and not self._fired):
            self._holding = True
            self._progress = 0.0
            self._tick_timer.start()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        if self._holding:
            self._holding = False
            self._progress = 0.0
            self._tick_timer.stop()
            self.update()
        super().mouseReleaseEvent(e)

    def leaveEvent(self, e):
        """Отпускаем удержание, если курсор ушёл за пределы кнопки."""
        if self._holding:
            self._holding = False
            self._progress = 0.0
            self._tick_timer.stop()
            self.update()
        super().leaveEvent(e)

    # ── Отрисовка ─────────────────────────────────────────────────────────────
    def paintEvent(self, e):
        # 1. Стандартная отрисовка кнопки (фон из stylesheet, текст, рамка)
        super().paintEvent(e)

        # 2. Оранжевый fill-оверлей поверх — только во время удержания
        if self._progress <= 0.0 or self._fired:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = self.rect()
        fill_w = int(r.width() * self._progress)

        # Скруглённый клип совпадает с border-radius кнопки (7 px)
        clip = QPainterPath()
        clip.addRoundedRect(0.0, 0.0, float(r.width()), float(r.height()), 7.0, 7.0)
        p.setClipPath(clip)

        # alpha растёт от 70 до 130 по ходу заливки — плавно проявляется
        alpha = int(70 + 60 * self._progress)
        p.fillRect(0, 0, fill_w, r.height(), QColor(230, 126, 34, alpha))

        # Тонкая светлая граница на краю заливки — визуальный «фронт»
        pen = QPen(QColor(255, 180, 80, 160), 1.5)
        p.setPen(pen)
        p.drawLine(fill_w, 2, fill_w, r.height() - 2)

        p.end()


# ──────────────────────────────────────────────────────────────────────────────
# Всплывающий оверлей управления пользователем (вместо отдельного окна)
# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# Всплывающий оверлей управления пользователем (вместо отдельного окна)
# ──────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# P2P Файловая передача — Workers (QThread)
# ══════════════════════════════════════════════════════════════════════════════
# Архитектура: прямое TCP соединение между клиентами.
# Сервер используется ТОЛЬКО для передачи сигнала-предложения.
# Все байты данных идут напрямую, GIL сервера не нагружается.
#
# FileSenderWorker:
#   1. bind(0.0.0.0, 0) → получаем свободный порт от ОС
#   2. Сигнал ready(port, token) → UI отправляет file_offer через NetworkClient
#   3. accept() с таймаутом FILE_TRANSFER_TIMEOUT секунд
#   4. Читаем токен (8 байт hex) от приёмника — проверяем
#   5. Стримим файл чанками FILE_CHUNK_SIZE, сигнализируем прогресс
#
# FileReceiverWorker:
#   1. connect(sender_ip, sender_port) с таймаутом
#   2. Отправляем токен (8 байт hex)
#   3. Читаем данные до закрытия сокета, пишем во временный файл
#   4. rename temp → target, сигнализируем finished(save_path)
# ══════════════════════════════════════════════════════════════════════════════


def _show_float_widget(widget: QWidget, margin: int = 18) -> None:
    """
    Позиционирует и показывает плавающий виджет в правом нижнем углу
    основного экрана (над панелью задач Windows).
    Виджет должен быть уже добавлен в layout или быть top-level.
    """
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        widget.show()
        return
    avail = screen.availableGeometry()
    widget.adjustSize()
    w, h = widget.width(), widget.height()
    widget.move(avail.right() - w - margin, avail.bottom() - h - margin)
    widget.show()
    widget.raise_()


class FileSenderWorker(QThread):
    """
    Поток-отправитель файла (P2P TCP).

    Сигналы:
        ready(port, token)          — сокет слушает, можно отправлять file_offer
        progress(sent, total)       — обновление прогресс-бара (байты)
        finished()                  — файл передан успешно
        error(message)              — ошибка (таймаут / отказ / ввод-вывод)
        cancelled()                 — пользователь нажал «Отмена»
    """
    ready     = pyqtSignal(int, str)    # (port, token)
    progress  = pyqtSignal(int, int)    # (bytes_sent, total_bytes)
    finished  = pyqtSignal()
    error     = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self._filepath   = filepath
        self._cancel_flag = False

    def cancel(self):
        """Вызывается из UI-потока для отмены передачи."""
        self._cancel_flag = True

    def run(self):
        # Используем context manager — гарантированное закрытие при любом исходе
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            try:
                srv.bind(('0.0.0.0', 0))
                srv.listen(1)
                port = srv.getsockname()[1]

                # Криптографически стойкий 8-символьный hex-токен.
                # Исключает подключение посторонних клиентов из той же VPN-сети.
                token = secrets.token_hex(4)   # 4 байта → 8 символов hex
                self.ready.emit(port, token)
                token_bytes = token.encode('ascii')

                srv.settimeout(FILE_TRANSFER_TIMEOUT)
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    self.error.emit("Получатель не подключился — таймаут")
                    return

            except Exception as e:
                self.error.emit(f"Ошибка создания сокета: {e}")
                return

        # Соединение принято — работаем внутри второго context manager
        with conn:
            try:
                conn.settimeout(FILE_TRANSFER_TIMEOUT)

                # Шаг 1: читаем токен от приёмника
                raw_token = b''
                while len(raw_token) < len(token_bytes):
                    chunk = conn.recv(len(token_bytes) - len(raw_token))
                    if not chunk:
                        self.error.emit("Соединение разорвано до передачи токена")
                        return
                    raw_token += chunk

                if raw_token != token_bytes:
                    self.error.emit("Неверный токен — подозрительное подключение")
                    return

                # Шаг 2: стримим файл чанками
                total = os.path.getsize(self._filepath)
                sent  = 0
                with open(self._filepath, 'rb') as f:
                    while True:
                        if self._cancel_flag:
                            self.cancelled.emit()
                            return
                        chunk = f.read(FILE_CHUNK_SIZE)
                        if not chunk:
                            break
                        # sendall гарантирует отправку всего чанка
                        conn.sendall(chunk)
                        sent += len(chunk)
                        self.progress.emit(sent, total)

                self.finished.emit()

            except socket.timeout:
                self.error.emit("Таймаут — соединение зависло во время передачи")
            except ConnectionResetError:
                if self._cancel_flag:
                    self.cancelled.emit()
                else:
                    self.error.emit("Получатель неожиданно разорвал соединение")
            except Exception as e:
                self.error.emit(f"Ошибка передачи: {e}")


class FileReceiverWorker(QThread):
    """
    Поток-приёмник файла (P2P TCP).

    Сигналы:
        progress(received, total)   — обновление прогресс-бара (байты)
        finished(save_path)         — файл принят и сохранён по пути save_path
        error(message)              — ошибка сети / диска
        cancelled()                 — пользователь нажал «Отмена»
    """
    progress  = pyqtSignal(int, int)   # (bytes_received, total_bytes)
    finished  = pyqtSignal(str)        # save_path
    error     = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, sender_ip: str, sender_port: int,
                 token: str, save_path: str, filesize: int, parent=None):
        super().__init__(parent)
        self._sender_ip   = sender_ip
        self._sender_port = sender_port
        self._token       = token
        self._save_path   = save_path
        self._filesize    = filesize
        self._cancel_flag = False

    def cancel(self):
        """Вызывается из UI-потока для отмены приёма."""
        self._cancel_flag = True

    def run(self):
        # Временный файл: записываем рядом с целевым с суффиксом .inpulse_tmp
        tmp_path = self._save_path + '.inpulse_tmp'

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.settimeout(FILE_TRANSFER_TIMEOUT)
                s.connect((self._sender_ip, self._sender_port))

                # Шаг 1: отправляем токен — подтверждаем личность
                s.sendall(self._token.encode('ascii'))

                # Шаг 2: читаем данные и пишем во временный файл
                received = 0
                with open(tmp_path, 'wb') as f:
                    while True:
                        if self._cancel_flag:
                            self.cancelled.emit()
                            return
                        try:
                            chunk = s.recv(FILE_CHUNK_SIZE)
                        except socket.timeout:
                            self.error.emit("Таймаут — отправитель завис во время передачи")
                            return
                        if not chunk:
                            break   # отправитель закрыл соединение — передача завершена
                        f.write(chunk)
                        received += len(chunk)
                        self.progress.emit(received, self._filesize)

            except socket.timeout:
                self.error.emit("Не удалось подключиться к отправителю — таймаут")
                return
            except ConnectionRefusedError:
                self.error.emit("Отправитель недоступен — соединение отклонено")
                return
            except Exception as e:
                self.error.emit(f"Ошибка приёма: {e}")
                return

        # Успешно получили — атомарно переименовываем temp → target
        try:
            # Если файл уже существует — добавляем суффикс (1), (2), ...
            final_path = self._save_path
            if os.path.exists(final_path):
                base, ext = os.path.splitext(final_path)
                n = 1
                while os.path.exists(f"{base} ({n}){ext}"):
                    n += 1
                final_path = f"{base} ({n}){ext}"
            os.rename(tmp_path, final_path)
            self.finished.emit(final_path)
        except Exception as e:
            self.error.emit(f"Ошибка сохранения файла: {e}")
        finally:
            # Удаляем temp если что-то пошло не так
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


class FileTransferProgressWidget(QFrame):
    """
    Компактный плавающий прогресс-бар передачи файла.
    Стиль единый с остальными overlay-виджетами приложения.

    Используется и отправителем, и получателем — передаём worker_ref
    для кнопки «Отмена».
    """
    def __init__(self, filename: str, filesize: int,
                 is_sender: bool, parent=None):
        super().__init__(parent)
        self._is_sender  = is_sender
        self._worker_ref = None   # устанавливается снаружи после создания

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)

        # ── Карточка ──────────────────────────────────────────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("ftCard")
        card.setStyleSheet("""
            QFrame#ftCard {
                background-color: rgba(20, 22, 30, 230);
                border: 1px solid rgba(91, 142, 245, 0.35);
                border-radius: 10px;
            }
            QLabel { color: #c8ccd8; font-size: 12px;
                     background: transparent; border: none; }
            QProgressBar {
                background: rgba(255,255,255,0.10);
                border: none; border-radius: 4px; height: 7px;
                text-align: center; color: transparent;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4a7fdb, stop:1 #7b52d4);
                border-radius: 4px;
            }
        """)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)

        # Заголовок
        direction = "📤  Отправка" if is_sender else "📥  Получение"
        lbl_title = QLabel(f"{direction}:  {filename}")
        lbl_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        lbl_title.setWordWrap(True)
        lay.addWidget(lbl_title)

        # Прогресс-бар
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFixedHeight(7)
        lay.addWidget(self._bar)

        # Строка статуса + кнопка отмены
        row = QHBoxLayout()
        row.setSpacing(8)
        self._lbl_status = QLabel("Ожидание подключения…" if is_sender else "Подключение…")
        self._lbl_status.setStyleSheet("font-size: 11px; color: rgba(180,190,220,0.75);")
        row.addWidget(self._lbl_status, 1)

        btn_cancel = QPushButton("✕")
        btn_cancel.setFixedSize(22, 22)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background: rgba(200,60,60,0.25); color: #ff9090;
                border: 1px solid rgba(200,60,60,0.4); border-radius: 5px;
                font-size: 10px; font-weight: bold;
            }
            QPushButton:hover { background: rgba(200,60,60,0.45); }
        """)
        btn_cancel.clicked.connect(self._on_cancel)
        row.addWidget(btn_cancel)
        lay.addLayout(row)

        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    def set_worker(self, worker):
        """Устанавливаем ссылку на worker для кнопки Отмена."""
        self._worker_ref = worker

    def update_progress(self, done: int, total: int):
        """Обновляем бар и текстовый статус."""
        if total > 0:
            pct = int(done * 100 / total)
            self._bar.setValue(pct)
        self._lbl_status.setText(
            f"{_format_size(done)} / {_format_size(total)}"
        )

    def set_done(self, save_path: str = ''):
        """Помечаем передачу как завершённую."""
        self._bar.setValue(100)
        if save_path:
            self._lbl_status.setText(f"✓  Сохранено")
        else:
            self._lbl_status.setText("✓  Отправлено")

    def set_error(self, msg: str):
        self._lbl_status.setText(f"✗  {msg}")
        self._bar.setStyleSheet(self._bar.styleSheet().replace(
            "#4a7fdb", "#c0392b").replace("#7b52d4", "#e74c3c"
        ))

    def _on_cancel(self):
        if self._worker_ref is not None:
            self._worker_ref.cancel()
        self._lbl_status.setText("Отменено")
        # Плавно скрываем через 1.5 с
        QTimer.singleShot(1500, self.hide)


def _format_size(n: int) -> str:
    """Форматирует количество байт в читаемую строку (KB / MB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


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
                 parent=None, is_streaming: bool = False, on_watch_stream=None,
                 net=None, on_transfer_server=None, on_host_mute=None):
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.audio = audio_handler
        self.uid = uid
        self._nick = nick.strip()
        self._whisper_active = False
        self._on_watch_stream = on_watch_stream
        self._net = net
        self._on_transfer_server = on_transfer_server
        self._on_host_mute = on_host_mute

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
        # Хороший шаг: стрелки ±5%, клик по треку ±25%
        self.sl_vol.setSingleStep(5)
        self.sl_vol.setPageStep(25)
        self.lbl_vol = QLabel(f"{self.sl_vol.value()}%")
        self.lbl_vol.setFixedWidth(38)
        self.lbl_vol.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sl_vol.valueChanged.connect(self._on_vol_changed)
        vol_row.addWidget(self.sl_vol)
        vol_row.addWidget(self.lbl_vol)
        card_lay.addLayout(vol_row)

        # ── Загружаем состояние буста (персистентное между сессиями) ─────────
        _s_boost = QSettings("MyVoiceChat", "GlobalSettings")
        self._is_boosted: bool = _s_boost.value(f"vol_boost_{uid}", "false") == "true"
        if self._is_boosted:
            self.sl_vol.blockSignals(True)
            self.sl_vol.setValue(200)
            self.sl_vol.blockSignals(False)
            self.sl_vol.setEnabled(False)
            self.lbl_vol.setText("⚡")

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

        # ── Кнопка: Буст звука ────────────────────────────────────────────────
        sep_b = QFrame()
        sep_b.setFrameShape(QFrame.Shape.HLine)
        sep_b.setStyleSheet(
            "background: rgba(255,255,255,0.08); border: none; max-height: 1px;"
        )
        sep_b.setMaximumHeight(1)
        card_lay.addWidget(sep_b)

        self.btn_boost = self._make_btn("⚡  Буст звука", checkable=True, checked=self._is_boosted)
        self.btn_boost.setStyleSheet(self.btn_boost.styleSheet() + """
            QPushButton:checked {
                background-color: rgba(255, 200, 0, 0.25);
                border-color: rgba(255, 190, 0, 0.70);
                color: #ffe066;
            }
            QPushButton:not(:checked):hover {
                background-color: rgba(255, 200, 0, 0.10);
                border-color: rgba(255, 190, 0, 0.40);
            }
        """)
        self.btn_boost.clicked.connect(self._on_toggle_boost)
        card_lay.addWidget(self.btn_boost)

        # ── Кнопка: Пнуть (Nudge) — удержание 3 секунды ─────────────────────
        # NudgeHoldButton: заполняется оранжевым за 3 с, только тогда отправляет.
        # Защита от случайного нажатия — нельзя задеть мимоходом.
        if net is not None:
            sep_n = QFrame()
            sep_n.setFrameShape(QFrame.Shape.HLine)
            sep_n.setStyleSheet(
                "background: rgba(255,255,255,0.08); border: none; max-height: 1px;"
            )
            sep_n.setMaximumHeight(1)
            card_lay.addWidget(sep_n)

            self.btn_nudge = NudgeHoldButton("👟  Пнуть  (держи 3с)")
            self.btn_nudge.setStyleSheet("""
                QPushButton {
                    background-color: rgba(255,255,255,0.06);
                    color: #f0a060;
                    border: 1px solid rgba(230, 126, 34, 0.5);
                    border-radius: 7px;
                    padding: 5px 10px;
                    font-size: 12px;
                    text-align: left;
                }
                QPushButton:hover {
                    background-color: rgba(230, 126, 34, 0.14);
                    border-color: rgba(230, 126, 34, 0.85);
                }
                QPushButton:disabled {
                    color: rgba(150, 100, 50, 0.55);
                    border-color: rgba(150, 100, 50, 0.25);
                    background-color: rgba(255,255,255,0.03);
                }
            """)

            # Проверяем кулдаун из QSettings — показываем «через Xм» если ещё активен
            import time as _nudge_time
            _s = QSettings("MyVoiceChat", "GlobalSettings")
            _last = float(_s.value(f"nudge_ts_{uid}", 0))
            _remaining = int(600 - (_nudge_time.time() - _last))
            if _remaining > 0:
                _mins = (_remaining + 59) // 60
                self.btn_nudge.setEnabled(False)
                self.btn_nudge.setText(f"👟  Пнуть  (через {_mins}м)")

            self.btn_nudge.hold_complete.connect(self._on_nudge_clicked)
            card_lay.addWidget(self.btn_nudge)

            # Подсказка под кнопкой — занимает место всегда, видна только при удержании
            self._lbl_nudge_hint = QLabel("Держи, чтобы отправить голос «Пнуть»")
            self._lbl_nudge_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._lbl_nudge_hint.setWordWrap(False)
            self._lbl_nudge_hint_active_style = (
                "font-size: 10px; color: rgba(240,160,80,0.85); "
                "background:transparent; border:none;"
            )
            self._lbl_nudge_hint_idle_style = (
                "font-size: 10px; color: transparent; "
                "background:transparent; border:none;"
            )
            self._lbl_nudge_hint.setStyleSheet(self._lbl_nudge_hint_idle_style)
            card_lay.addWidget(self._lbl_nudge_hint)

            # Показываем / скрываем hint через сигналы таймера кнопки
            self.btn_nudge._tick_timer.timeout.connect(self._on_nudge_tick_hint)

        # ── Кнопка: Передать файл ─────────────────────────────────────────────
        # Доступна только если net задан (есть активное соединение)
        if net is not None:
            sep_f = QFrame()
            sep_f.setFrameShape(QFrame.Shape.HLine)
            sep_f.setStyleSheet(
                "background: rgba(255,255,255,0.08); border: none; max-height: 1px;"
            )
            sep_f.setMaximumHeight(1)
            card_lay.addWidget(sep_f)

            self.btn_file = self._make_btn("📁  Передать файл")
            self.btn_file.setStyleSheet(self.btn_file.styleSheet() + """
                QPushButton { border-color: rgba(91,142,245,0.4); color: #8ab4f8; }
                QPushButton:hover {
                    background-color: rgba(74,127,219,0.20);
                    border-color: rgba(91,142,245,0.80);
                }
                QPushButton:pressed {
                    background-color: rgba(74,127,219,0.40);
                    color: #ffffff;
                }
            """)
            self.btn_file.clicked.connect(self._on_send_file_clicked)
            card_lay.addWidget(self.btn_file)

        # ── Кнопка: Передать сервер (только если on_transfer_server задан) ────
        # Видна исключительно хосту встроенного сервера в его контекстном меню.
        if self._on_transfer_server is not None:
            sep_ts = QFrame()
            sep_ts.setFrameShape(QFrame.Shape.HLine)
            sep_ts.setStyleSheet(
                "background: rgba(255,255,255,0.08); border: none; max-height: 1px;"
            )
            sep_ts.setMaximumHeight(1)
            card_lay.addWidget(sep_ts)

            self.btn_transfer_server = self._make_btn("🔀  Передать сервер")
            self.btn_transfer_server.setStyleSheet(
                self.btn_transfer_server.styleSheet() + """
                QPushButton { border-color: rgba(200,160,50,0.45); color: #f0c060; }
                QPushButton:hover {
                    background-color: rgba(200,160,50,0.18);
                    border-color: rgba(220,180,60,0.85);
                }
                QPushButton:pressed {
                    background-color: rgba(200,160,50,0.35);
                    color: #ffffff;
                }
            """)
            self.btn_transfer_server.clicked.connect(self._on_transfer_server_clicked)
            card_lay.addWidget(self.btn_transfer_server)

        # ── Кнопка: Выключить микрофон (только хост) ─────────────────────────
        # Только mic off — уши не трогаются.
        # Участник может включить mic обратно сам в любой момент.
        if self._on_host_mute is not None:
            sep_hm = QFrame()
            sep_hm.setFrameShape(QFrame.Shape.HLine)
            sep_hm.setStyleSheet(
                "background: rgba(255,255,255,0.08); border: none; max-height: 1px;"
            )
            sep_hm.setMaximumHeight(1)
            card_lay.addWidget(sep_hm)

            self.btn_host_mute = self._make_btn("🎤  Выключить микрофон")
            self.btn_host_mute.setStyleSheet(self.btn_host_mute.styleSheet() + """
                QPushButton { border-color: rgba(220,60,60,0.45); color: #ff9090; }
                QPushButton:hover {
                    background-color: rgba(220,60,60,0.18);
                    border-color: rgba(220,60,60,0.80);
                }
                QPushButton:pressed {
                    background-color: rgba(220,60,60,0.35);
                    color: #ffffff;
                }
            """)
            self.btn_host_mute.clicked.connect(self._on_host_mute_clicked)
            card_lay.addWidget(self.btn_host_mute)

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
        # При v=0 показываем "Mute" вместо "0%" — понятнее пользователю
        if v == 0:
            self.lbl_vol.setText("🔇")
        else:
            self.lbl_vol.setText(f"{v}%")
        # Экспоненциальная кривая: slider 100 = 1.0x (нейтрально),
        # slider 200 = 10.0x (+20 дБ) — позволяет поднять тихие микрофоны.
        # slider 0 → 0.0 (полная тишина, _slider_to_vol гарантирует это).
        self.audio.set_user_volume(self.uid, _slider_to_vol(v))

    def _on_toggle_mute(self):
        state = self.audio.toggle_user_mute(self.uid)
        self.btn_mute.setText("🔊  Разглушить" if state else "🔇  Заглушить")

    def _on_toggle_boost(self, checked: bool):
        """
        Активирует / деактивирует буст громкости пользователя (15x ≈ +23.5 дБ).

        ВКЛ: сохраняем текущий слайдер в QSettings (vol_pre_boost_{uid}),
             ставим флаг vol_boost_{uid}="true", применяем _BOOST_VOL (15x).
             Слайдер блокируется — случайное движение не сбросит буст.
        ВЫКЛ: восстанавливаем сохранённый слайдер, снимаем флаги,
              разблокируем слайдер, применяем восстановленную громкость.

        Громкость сохраняется через audio.set_user_volume() в vol_ip_{ip} —
        совместимость с register_ip_mapping() при переподключении.
        """
        _s = QSettings("MyVoiceChat", "GlobalSettings")
        self._is_boosted = checked
        if checked:
            _s.setValue(f"vol_pre_boost_{self.uid}", self.sl_vol.value())
            _s.setValue(f"vol_boost_{self.uid}", "true")
            self.sl_vol.blockSignals(True)
            self.sl_vol.setValue(200)
            self.sl_vol.blockSignals(False)
            self.sl_vol.setEnabled(False)
            self.lbl_vol.setText("⚡")
            self.audio.set_user_volume(self.uid, _BOOST_VOL)
        else:
            pre_slider = int(_s.value(f"vol_pre_boost_{self.uid}", 100))
            _s.setValue(f"vol_boost_{self.uid}", "false")
            _s.remove(f"vol_pre_boost_{self.uid}")
            self.sl_vol.setEnabled(True)
            self.sl_vol.blockSignals(True)
            self.sl_vol.setValue(pre_slider)
            self.sl_vol.blockSignals(False)
            self.lbl_vol.setText("🔇" if pre_slider == 0 else f"{pre_slider}%")
            self.audio.set_user_volume(self.uid, _slider_to_vol(pre_slider))

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

    def _on_nudge_tick_hint(self):
        """
        Вызывается каждые 20 мс пока кнопка удерживается.
        Показывает подсказку при старте удержания (первый тик),
        скрывает при сбросе (holding=False).
        """
        if not hasattr(self, '_lbl_nudge_hint'):
            return
        if self.btn_nudge._holding:
            self._lbl_nudge_hint.setStyleSheet(self._lbl_nudge_hint_active_style)
            # Динамический текст с прогрессом
            pct = int(self.btn_nudge._progress * 100)
            self._lbl_nudge_hint.setText(f"Держи… {pct}%")
        else:
            self._lbl_nudge_hint.setStyleSheet(self._lbl_nudge_hint_idle_style)
            self._lbl_nudge_hint.setText("Держи, чтобы отправить голос «Пнуть»")

    def _on_nudge_clicked(self):
        """
        Вызывается после успешного 3-секундного удержания (hold_complete).

        Двойная защита от спама:
          1. QSettings 'nudge_ts_<uid>' — кулдаун хранится между сессиями.
          2. NudgeHoldButton._fired = True — повторный hold невозможен.

        Кулдаун совпадает с серверным (NUDGE_COOLDOWN_SEC = 600 с).
        """
        if self._net is None:
            return
        import time as _t
        _s   = QSettings("MyVoiceChat", "GlobalSettings")
        _key = f"nudge_ts_{self.uid}"
        _now = _t.time()
        # guard: проверяем кулдаун ещё раз
        if _now - float(_s.value(_key, 0)) < 600:
            return
        # Сохраняем оптимистично — до ответа сервера
        _s.setValue(_key, _now)
        self._net.send_nudge_vote(self.uid)
        self.btn_nudge.setEnabled(False)
        self.btn_nudge.setText("👟  Проголосовал ✓")
        # Скрываем hint
        if hasattr(self, '_lbl_nudge_hint'):
            self._lbl_nudge_hint.setStyleSheet(self._lbl_nudge_hint_idle_style)
        print(f"[UI] Nudge vote → uid={self.uid} nick={self._nick!r}")

    def _on_send_file_clicked(self):
        """
        Пользователь нажал «📁 Передать файл».

        Открываем стандартный диалог выбора файла.
        Закрываем оверлей до старта QFileDialog — иначе popup-тип
        перехватывает события и диалог может не отобразиться.
        Запуск FileSenderWorker делегируется родительскому окну
        через специальный сигнал, чтобы не тащить логику workers
        внутрь оверлея.
        """
        if self._net is None:
            return

        self.close()    # закрываем popup ДО открытия QFileDialog

        filepath, _ = QFileDialog.getOpenFileName(
            None,
            "Выберите файл для передачи",
            os.path.expanduser("~"),
            "Все файлы (*)",
        )
        if not filepath:
            return

        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)

        # Создаём и запускаем worker-отправитель
        worker = FileSenderWorker(filepath)

        # Создаём прогресс-виджет.
        # Родитель None — он будет показан MainWindow отдельно.
        prog = FileTransferProgressWidget(filename, filesize, is_sender=True)
        prog.set_worker(worker)

        def on_ready(port: int, token: str):
            # Сигнал из рабочего потока — нужен переход в GUI-поток
            # используем QTimer.singleShot(0, ...) для безопасного вызова
            def _send():
                self._net.send_file_offer(
                    self.uid, filename, filesize, port, token
                )
            QTimer.singleShot(0, _send)

        worker.ready.connect(on_ready)
        worker.progress.connect(lambda s, t: prog.update_progress(s, t))
        worker.finished.connect(lambda: prog.set_done())
        worker.error.connect(lambda msg: prog.set_error(msg))
        worker.cancelled.connect(lambda: prog.hide())

        # Показываем прогресс-виджет в правом нижнем углу экрана
        _show_float_widget(prog)
        worker.start()

    def _on_transfer_server_clicked(self):
        """
        Пользователь нажал «🔀 Передать сервер».
        Закрываем popup и вызываем callback из MainWindow
        (он покажет QMessageBox с подтверждением).
        """
        self.close()
        if self._on_transfer_server is not None:
            self._on_transfer_server()

    def _on_host_mute_clicked(self):
        """
        Хост нажал «🎤 Выключить микрофон».
        Закрываем popup до callback — Popup-тип перехватывает события мыши.
        Участник может включить mic обратно сам в любой момент.
        """
        self.close()
        if self._on_host_mute is not None:
            self._on_host_mute()

    def hideEvent(self, event):
        """Если панель закрылась пока шептали — останавливаем шёпот."""
        if self._whisper_active:
            self._whisper_active = False
            self.audio.stop_whisper()
        super().hideEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# Выбор аватара — стеклянный тёмный дизайн (единый стиль с SettingsDialog)
# ──────────────────────────────────────────────────────────────────────────────
class AvatarSelector(QDialog):
    """
    Диалог выбора аватарки.
    Дизайн: безрамочный, тёмное стекло, кастомный title bar (_DialogTitleBar).
    Кнопки аватарок подсвечиваются синим при hover и зелёной рамкой при выборе.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_avatar = None

        # ── Безрамочное окно с прозрачным фоном ──────────────────────────────
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Выбор аватара")
        self.setFixedSize(520, 430)

        # ── Корневой layout (прозрачный) ──────────────────────────────────────
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # ── Карточка: тёмный полупрозрачный фон со скруглёнными углами ────────
        self._card = QFrame(self)
        self._card.setObjectName("avatarCard")
        self._card.setStyleSheet("""
            QFrame#avatarCard {
                background-color: rgba(26, 28, 38, 252);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }
            QLabel {
                color: #c8d0e0;
                background: transparent;
                border: none;
            }
            QPushButton.avatarBtn {
                background-color: rgba(255,255,255,0.05);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 10px;
            }
            QPushButton.avatarBtn:hover {
                background-color: rgba(91,142,245,0.18);
                border: 1px solid rgba(91,142,245,0.55);
            }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.04);
                width: 6px; border-radius: 3px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 3px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollArea { background: transparent; border: none; }
        """)
        root_lay.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # ── Кастомный title bar ───────────────────────────────────────────────
        self._title_bar = _DialogTitleBar(self, "🖼  Выбор аватара")
        card_lay.addWidget(self._title_bar)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setFixedHeight(1)
        _sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(_sep)

        # ── Контент ───────────────────────────────────────────────────────────
        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        content_lay = QVBoxLayout(content_w)
        content_lay.setContentsMargins(16, 14, 16, 14)
        content_lay.setSpacing(10)
        card_lay.addWidget(content_w, stretch=1)

        hint = QLabel("Нажмите на аватарку чтобы выбрать её")
        hint.setStyleSheet("font-size: 12px; color: rgba(200,208,224,0.55);")
        content_lay.addWidget(hint)

        # ── Скролл-зона с сеткой аватарок ─────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        grid = QGridLayout(container)
        grid.setSpacing(8)
        grid.setContentsMargins(0, 0, 0, 0)

        av_dir = resource_path("assets/avatars")
        if os.path.exists(av_dir):
            files = sorted([f for f in os.listdir(av_dir) if f.endswith('.svg')])
            for i, f in enumerate(files):
                btn = QPushButton()
                btn.setProperty("class", "avatarBtn")
                btn.setFixedSize(82, 82)
                btn.setIcon(QIcon(os.path.join(av_dir, f)))
                btn.setIconSize(QSize(60, 60))
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(f.rsplit('.', 1)[0])
                btn.setStyleSheet("""
                    QPushButton {
                        background-color: rgba(255,255,255,0.05);
                        border: 1px solid rgba(255,255,255,0.08);
                        border-radius: 10px;
                    }
                    QPushButton:hover {
                        background-color: rgba(91,142,245,0.18);
                        border: 1px solid rgba(91,142,245,0.55);
                    }
                    QPushButton:pressed {
                        background-color: rgba(46,204,113,0.22);
                        border: 2px solid rgba(46,204,113,0.70);
                    }
                """)
                btn.clicked.connect(lambda ch, fname=f: self.select_and_close(fname))
                grid.addWidget(btn, i // 5, i % 5)

        scroll.setWidget(container)
        content_lay.addWidget(scroll, stretch=1)

        # ── Кнопка «Отмена» ────────────────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background: rgba(255,255,255,0.08); border: none; max-height: 1px;")
        content_lay.addWidget(sep2)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_cancel = QPushButton("Отмена")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.06);
                color: #8899bb;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 6px;
                padding: 7px 20px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.12);
                color: #c8d0e0;
            }
        """)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        content_lay.addLayout(btn_row)

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
# Кастомный title bar для безрамочных диалогов
# ──────────────────────────────────────────────────────────────────────────────
class _DialogTitleBar(QWidget):
    """
    Компактный кастомный title bar для безрамочных QDialog.
    Поддерживает: перетаскивание, сворачивание (опционально), закрытие.
    Дизайн в едином стиле со SoundboardPanel и UserOverlayPanel.
    """

    def __init__(self, parent_dialog, title: str = "", show_minimize: bool = False):
        super().__init__(parent_dialog)
        self._dlg = parent_dialog
        self._drag_pos = None
        self.setFixedHeight(38)
        self.setObjectName("dlgTitleBar")

        self.setStyleSheet("""
            QWidget#dlgTitleBar {
                background-color: rgba(18, 20, 30, 245);
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border: none;
            }
            QLabel#dlgTitleText {
                color: #cdd6f4;
                font-size: 13px;
                font-weight: bold;
                background: transparent;
                border: none;
                padding-left: 6px;
            }
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 5px;
                color: #8890a0;
                font-size: 14px;
                min-width: 28px;
                max-width: 28px;
                min-height: 26px;
                max-height: 26px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.10); color: #cdd6f4; }
            QPushButton#dlgBtnClose:hover { background: #e74c3c; color: white; }
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 6, 0)
        lay.setSpacing(4)

        ico_lbl = QLabel()
        ico_lbl.setFixedSize(18, 18)
        try:
            from config import resource_path
            ico_lbl.setPixmap(QIcon(resource_path("assets/icon/logo.ico")).pixmap(18, 18))
        except Exception:
            pass
        ico_lbl.setStyleSheet("background:transparent; border:none;")
        lay.addWidget(ico_lbl)

        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("dlgTitleText")
        lay.addWidget(self._title_lbl, stretch=1)

        if show_minimize:
            btn_min = QPushButton("─")
            btn_min.clicked.connect(parent_dialog.showMinimized)
            lay.addWidget(btn_min)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("dlgBtnClose")
        btn_close.clicked.connect(parent_dialog.reject)
        lay.addWidget(btn_close)

    def set_title(self, title: str):
        self._title_lbl.setText(title)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._dlg.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._dlg.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)


# ──────────────────────────────────────────────────────────────────────────────
# Виджет перехвата нажатия горячих клавиш
# ──────────────────────────────────────────────────────────────────────────────
class HotkeyCaptureEdit(QLineEdit):
    """
    Поле для записи горячей клавиши кликом.

    Поведение:
      • Кликни → поле подсвечивается фиолетовым, появляется «Нажми клавишу…»
      • Нажми любую клавишу (одиночную или с модификаторами) → записывается
        строка вида «ctrl+shift+a», «alt+f4», «f8» и т.д.
      • Escape во время захвата → отменяет, восстанавливает прежнее значение
      • Повторный клик по занятому полю → очищает и снова ждёт ввода

    Формат совпадает с форматом keyboard-библиотеки (строчные, '+' как разделитель).
    """

    _WAIT_SS = (
        "QLineEdit {"
        "  background: rgba(100,60,200,0.22);"
        "  border: 1px solid rgba(130,80,230,0.70);"
        "  border-radius: 6px;"
        "  color: #c8b0ff;"
        "  padding: 4px 8px;"
        "}"
    )
    _FILLED_SS = (
        "QLineEdit {"
        "  background: rgba(46,204,113,0.12);"
        "  border: 1px solid rgba(46,204,113,0.45);"
        "  border-radius: 6px;"
        "  color: #82e0aa;"
        "  padding: 4px 8px;"
        "}"
    )
    _EMPTY_SS = (
        "QLineEdit {"
        "  background: rgba(255,255,255,0.06);"
        "  border: 1px solid rgba(255,255,255,0.13);"
        "  border-radius: 6px;"
        "  color: #c8d0e0;"
        "  padding: 4px 8px;"
        "}"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._capturing = False
        self._prev_value = ""
        self.setReadOnly(True)
        self.setPlaceholderText("Кликни для задания клавиши")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(self._EMPTY_SS)
        self.setMinimumWidth(180)
        self.setFixedHeight(30)

    # ── публичный API ─────────────────────────────────────────────────────────

    def set_hotkey(self, text: str):
        """Программно задать значение (без перехода в режим захвата)."""
        self._prev_value = text
        self.setText(text)
        self.setStyleSheet(self._FILLED_SS if text else self._EMPTY_SS)

    def get_hotkey(self) -> str:
        return self.text()

    # ── события ───────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_capture()
        super().mousePressEvent(event)

    def _start_capture(self):
        self._prev_value = self.text()
        self._capturing = True
        self.setText("")
        self.setPlaceholderText("Нажми клавишу…")
        self.setStyleSheet(self._WAIT_SS)
        self.setFocus()

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return

        key = event.key()

        # Escape — отмена
        if key == Qt.Key.Key_Escape:
            self._capturing = False
            self.setText(self._prev_value)
            self.setPlaceholderText("Кликни для задания клавиши")
            self.setStyleSheet(self._FILLED_SS if self._prev_value else self._EMPTY_SS)
            self.clearFocus()
            return

        # Игнорируем нажатие одних модификаторов — ждём основную клавишу
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
                   Qt.Key.Key_Meta, Qt.Key.Key_AltGr):
            return

        # Собираем строку модификаторов
        mods = event.modifiers()
        parts = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            parts.append("ctrl")
        if mods & Qt.KeyboardModifier.AltModifier:
            parts.append("alt")
        if mods & Qt.KeyboardModifier.ShiftModifier:
            parts.append("shift")

        # Название основной клавиши
        key_name = self._key_to_str(key)
        if key_name:
            parts.append(key_name)

        combo = "+".join(parts) if parts else ""
        self._capturing = False
        self.setText(combo)
        self.setPlaceholderText("Кликни для задания клавиши")
        self.setStyleSheet(self._FILLED_SS if combo else self._EMPTY_SS)
        self.clearFocus()

    def focusOutEvent(self, event):
        """Отмена захвата при потере фокуса."""
        if self._capturing:
            self._capturing = False
            self.setText(self._prev_value)
            self.setPlaceholderText("Кликни для задания клавиши")
            self.setStyleSheet(self._FILLED_SS if self._prev_value else self._EMPTY_SS)
        super().focusOutEvent(event)

    @staticmethod
    def _key_to_str(key: int) -> str:
        """Qt.Key → строка совместимая с keyboard-библиотекой."""
        # Буквы
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key).lower()
        # Цифры
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            return chr(key)
        # F-клавиши
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            n = key - Qt.Key.Key_F1 + 1
            return f"f{n}"
        # Специальные
        _MAP = {
            Qt.Key.Key_Space:       "space",
            Qt.Key.Key_Return:      "enter",
            Qt.Key.Key_Enter:       "enter",
            Qt.Key.Key_Tab:         "tab",
            Qt.Key.Key_Backspace:   "backspace",
            Qt.Key.Key_Delete:      "delete",
            Qt.Key.Key_Insert:      "insert",
            Qt.Key.Key_Home:        "home",
            Qt.Key.Key_End:         "end",
            Qt.Key.Key_PageUp:      "page up",
            Qt.Key.Key_PageDown:    "page down",
            Qt.Key.Key_Left:        "left",
            Qt.Key.Key_Right:       "right",
            Qt.Key.Key_Up:          "up",
            Qt.Key.Key_Down:        "down",
            Qt.Key.Key_BracketLeft:  "[",
            Qt.Key.Key_BracketRight: "]",
            Qt.Key.Key_Semicolon:   ";",
            Qt.Key.Key_Apostrophe:  "'",
            Qt.Key.Key_Comma:       ",",
            Qt.Key.Key_Period:      ".",
            Qt.Key.Key_Slash:       "/",
            Qt.Key.Key_Backslash:   "\\",
            Qt.Key.Key_Minus:       "-",
            Qt.Key.Key_Equal:       "=",
            Qt.Key.Key_QuoteLeft:   "`",
            Qt.Key.Key_NumLock:     "num lock",
            Qt.Key.Key_ScrollLock:  "scroll lock",
            Qt.Key.Key_CapsLock:    "caps lock",
            Qt.Key.Key_Print:       "print screen",
            Qt.Key.Key_Pause:       "pause",
        }
        return _MAP.get(key, "")


# ──────────────────────────────────────────────────────────────────────────────
# Диалог настроек
# ──────────────────────────────────────────────────────────────────────────────
class SettingsDialog(QDialog):
    def __init__(self, audio_engine, parent):
        super().__init__(parent)
        self.audio = audio_engine
        self.mw = parent  # MainWindow
        self.app_settings = QSettings("MyVoiceChat", "GlobalSettings")

        # ── Безрамочный стеклянный дизайн (единый стиль с SoundboardPanel) ──
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Настройки")
        self.resize(780, 660)
        self.setMinimumSize(480, 520)

        # ── FIX MEM: удаляем C++ объект при закрытии (accept/reject/X). ─────
        # Без WA_DeleteOnClose Qt скрывает диалог, но не уничтожает его.
        # audio_engine держит сильную ссылку на mic_vad через сигнальный слот →
        # весь диалог остаётся в памяти. Каждое открытие настроек добавляло
        # ~20–30 МБ виджетов, которые никогда не освобождались.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # ── Корневой layout: прозрачный фон, карточка с border-radius ─────────
        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # Карточка — полупрозрачный тёмный фон, скруглённые углы
        self._card = QFrame(self)
        self._card.setObjectName("settingsCard")
        self._card.setStyleSheet("""
            QFrame#settingsCard {
                background-color: rgba(26, 28, 38, 252);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }
            QLabel {
                color: #c8d0e0;
                background: transparent;
                border: none;
            }
            QGroupBox {
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 6px;
                color: #8899bb;
                font-weight: bold;
            }
            QComboBox {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.13);
                border-radius: 6px;
                padding: 5px 10px;
                color: #c8d0e0;
            }
            QComboBox QAbstractItemView {
                background-color: #1e2130;
                color: #c8d0e0;
                border: 1px solid #333648;
                selection-background-color: #2c3252;
                selection-color: #ffffff;
                outline: none;
            }
            QComboBox::drop-down { border: none; }
            QLineEdit {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.13);
                border-radius: 6px;
                padding: 5px 10px;
                color: #c8d0e0;
            }
            QCheckBox { color: #c8d0e0; background: transparent; }
            QCheckBox::indicator {
                width: 16px; height: 16px;
                border: 1px solid rgba(255,255,255,0.20);
                border-radius: 4px;
                background: rgba(255,255,255,0.06);
            }
            QCheckBox::indicator:checked {
                background: #5b8ef5;
                border-color: #5b8ef5;
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
            QTabWidget::pane {
                border: 1px solid rgba(255,255,255,0.10);
                background-color: rgba(255,255,255,0.03);
                border-radius: 6px;
            }
            QTabBar::tab {
                background-color: rgba(255,255,255,0.05);
                color: #8899bb;
                padding: 8px 16px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                margin-right: 2px;
                border: 1px solid rgba(255,255,255,0.07);
                border-bottom: none;
            }
            QTabBar::tab:selected {
                background-color: rgba(255,255,255,0.10);
                color: #cdd6f4;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background-color: rgba(255,255,255,0.08);
                color: #aabbcc;
            }
            QTabBar::scroller { width: 20px; }
            QTabBar QToolButton {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 4px;
                color: #cccccc;
            }
            QTabBar QToolButton:hover { background: rgba(255,255,255,0.14); }
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 6px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.13);
                border-color: rgba(255,255,255,0.22);
            }
            QPushButton:checked {
                background-color: rgba(220,60,60,0.35);
                border-color: rgba(220,60,60,0.6);
                color: #ff9090;
            }
            #btn_nr { background-color: rgba(214,93,78,0.30); color: #ff9090; }
            #btn_nr:checked { background-color: rgba(39,174,96,0.30); color: #82e0aa; }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.04);
                width: 6px; border-radius: 3px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 3px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal {
                background: rgba(255,255,255,0.04);
                height: 6px; border-radius: 3px; margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: rgba(255,255,255,0.18);
                border-radius: 3px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
            QScrollArea { background: transparent; border: none; }
            QFrame[frameShape="4"], QFrame[frameShape="5"] {
                background: rgba(255,255,255,0.08);
                border: none;
                max-height: 1px;
            }
            QProgressBar {
                background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 4px;
                color: #c8d0e0;
                text-align: center;
            }
            QProgressBar::chunk {
                background: #5b8ef5;
                border-radius: 3px;
            }
        """)
        root_lay.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # ── Кастомный title bar ───────────────────────────────────────────────
        self._title_bar = _DialogTitleBar(self, "⚙  Настройки")
        card_lay.addWidget(self._title_bar)

        # Разделитель под title bar
        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.HLine)
        _sep.setFixedHeight(1)
        _sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(_sep)

        # ── Основной контент ──────────────────────────────────────────────────
        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        content_lay = QVBoxLayout(content_w)
        content_lay.setContentsMargins(16, 14, 16, 14)
        content_lay.setSpacing(10)
        card_lay.addWidget(content_w, stretch=1)

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)

        # 1. Профиль
        self.setup_profile_tab()

        # 2. Аудио
        self.setup_audio_tab()

        # 3. Персонализация (Хоткеи + бывший Шёпот)
        self.setup_personalization_tab()

        # 4. SoundBoard — кастомные звуки + громкость
        self.setup_soundboard_tab()

        # 5. Версия
        self.setup_version_tab()

        content_lay.addWidget(self.tabs)

        # Кнопка «Сохранить» внизу карточки
        btn_save = QPushButton("✔  Сохранить")
        btn_save.setStyleSheet("""
            QPushButton {
                background-color: rgba(46,204,113,0.25);
                color: #82e0aa;
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 7px;
                padding: 8px 20px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(46,204,113,0.40);
                border-color: rgba(46,204,113,0.75);
                color: #ffffff;
            }
        """)
        btn_save.clicked.connect(self.save_all)
        content_lay.addWidget(btn_save)

        # ── Фикс прозрачности выпадающих списков на Windows ──────────────────
        # QComboBox-popup — отдельное top-level окно. Если родитель имеет
        # WA_TranslucentBackground, Windows-compositor рендерит popup тоже
        # прозрачным, игнорируя background-color из CSS.
        # Решение: явно задаём solid-stylesheet непосредственно на view-виджете
        # каждого комбобокса и снимаем флаг TranslucentBackground с его окна.
        QTimer.singleShot(0, self._fix_combo_popups)

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

        # ── Шумоподавление: ComboBox с тремя режимами ──────────────────────────
        nr_label = QLabel("🎛  Шумоподавление микрофона:")
        nr_label.setStyleSheet("font-weight: bold; font-size: 12px; margin-top: 6px;")

        self.cb_nr = QComboBox()
        self.cb_nr.setObjectName("cb_nr")
        self.cb_nr.setToolTip(
            "Выкл — без обработки\n"
            "RNNoise — лёгкое NN-подавление (низкая нагрузка)\n"
            "DeepFilterNet — глубокая фильтрация (средняя нагрузка)"
        )

        # Пункт 0: всегда доступен
        self.cb_nr.addItem("🔇  Выкл", 0)

        # Пункт 1: RNNoise
        rnn_item_text = "🟢  RNNoise" if PYRNNOISE_AVAILABLE else "🔴  RNNoise (модуль не установлен)"
        self.cb_nr.addItem(rnn_item_text, 1)
        if not PYRNNOISE_AVAILABLE:
            # Блокируем пункт через setData на роль Qt.ItemDataRole.UserRole+1
            from PyQt6.QtGui import QStandardItem
            model = self.cb_nr.model()
            item = model.item(1)
            if item:
                item.setEnabled(False)

        # Пункт 2: DeepFilterNet
        dfn_ok = getattr(self.audio, 'dfn_available', False)
        dfn_item_text = "🔵  DeepFilterNet" if dfn_ok else "🔴  DeepFilterNet (dll не найдена)"
        self.cb_nr.addItem(dfn_item_text, 2)
        if not dfn_ok:
            model = self.cb_nr.model()
            item = model.item(2)
            if item:
                item.setEnabled(False)

        # Восстанавливаем сохранённый режим
        saved_nr = getattr(self.audio, 'nr_mode', 0)
        idx = self.cb_nr.findData(saved_nr)
        if idx != -1:
            self.cb_nr.setCurrentIndex(idx)

        self.cb_nr.currentIndexChanged.connect(self._on_nr_mode_changed)

        aud_lay.addWidget(QLabel("Качество звука (Битрейт):"))
        self.cb_bitrate = QComboBox()
        bitrate_options = {
            "24 kbps (Рация)": 24,
            "48 kbps (Стандарт)": 48,
            "64 kbps (Хорошее)": 64,
            "128 kbps (Наилучшее)": 128
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
        aud_lay.addWidget(nr_label)
        aud_lay.addWidget(self.cb_nr)

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

        aud_lay.addStretch()
        self.tabs.addTab(aud_tab, "Аудио")

    # ── Вкладка «Персонализация» (Горячие клавиши) ────────────────────────────
    def setup_personalization_tab(self):
        """
        Вкладка объединяет:
        • Горячие клавиши для mute/deafen (раньше были статическими QLineEdit)
        • PTT-шёпот по нику (раньше вкладка «Шёпот»)
        • Горячие клавиши для звуков Soundboard

        Дизайн: динамическая таблица строк.
        Каждая строка = [Функция (ComboBox)] + [Горячая клавиша (HotkeyCaptureEdit)] + [✕]
        По умолчанию — 1 пустая строка. Кнопка «+» добавляет ещё (макс 8).
        """
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setSpacing(10)
        outer.setContentsMargins(16, 16, 16, 16)

        # ── GroupBox «Горячие клавиши» (как «Громкость Soundboard» на вкладке SoundBoard) ─
        hk_group = QGroupBox("🎹  Горячие клавиши")
        hk_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        hk_group_lay = QVBoxLayout(hk_group)
        hk_group_lay.setSpacing(8)
        hk_group_lay.setContentsMargins(10, 14, 10, 10)
        outer.addWidget(hk_group, stretch=1)

        # ── Заголовки колонок ─────────────────────────────────────────────────
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(4, 0, 36, 0)   # 36 = ширина кнопки «✕»
        hdr_row.setSpacing(8)
        lbl_func = QLabel("Действие")
        lbl_func.setStyleSheet("font-weight: bold; font-size: 12px;")
        lbl_key  = QLabel("Горячая клавиша (кликни для записи)")
        lbl_key.setStyleSheet("font-weight: bold; font-size: 12px;")
        hdr_row.addWidget(lbl_func, stretch=4)
        hdr_row.addWidget(lbl_key,  stretch=5)
        hk_group_lay.addLayout(hdr_row)

        # ── Скролл-область со строками ────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._hk_rows_container = QWidget()
        self._hk_rows_container.setStyleSheet("background: transparent;")
        self._hk_rows_layout = QVBoxLayout(self._hk_rows_container)
        self._hk_rows_layout.setSpacing(5)
        self._hk_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._hk_rows_layout.addStretch()   # прижимаем строки сверху
        scroll.setWidget(self._hk_rows_container)
        hk_group_lay.addWidget(scroll, stretch=1)

        # ── Кнопка «Добавить» — жёстко прибита к низу вкладки (вне GroupBox) ──
        # Находится в outer, ПОСЛЕ группы → всегда видна в одном месте,
        # не зависит от количества строк и не уезжает вверх при пустом списке.
        self._btn_hk_add = QPushButton("＋  Добавить назначение")
        self._btn_hk_add.setStyleSheet("""
            QPushButton {
                background: rgba(88,101,242,0.20);
                color: #a0b0ff;
                border: 1px solid rgba(88,101,242,0.50);
                border-radius: 7px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background: rgba(88,101,242,0.38);
                color: #ffffff;
            }
            QPushButton:disabled {
                background: rgba(255,255,255,0.04);
                color: #555;
                border-color: rgba(255,255,255,0.08);
            }
        """)
        self._btn_hk_add.clicked.connect(self._add_hk_row)
        outer.addWidget(self._btn_hk_add, alignment=Qt.AlignmentFlag.AlignLeft)

        # ── Список строк (модель) ─────────────────────────────────────────────
        # Каждый элемент: {"cb": QComboBox, "hk": HotkeyCaptureEdit, "frame": QFrame}
        self._hk_rows: list[dict] = []

        # ── Загружаем сохранённые строки ──────────────────────────────────────
        self._load_hk_rows()

        self.tabs.addTab(tab, "Персонализация")

    # ── Вспомогательные методы новой таблицы горячих клавиш ──────────────────

    def _build_function_options(self) -> list[tuple[str, str, str]]:
        """
        Возвращает список (display_text, func_type, func_data) для ComboBox.

        func_type:
          "none"      — не задано
          "mute_mic"  — замутить микрофон
          "deafen"    — замутить динамики
          "whisper"   — шёпот; func_data = IP пользователя
          "sound"     — soundboard; func_data = имя звука (строка из QSettings)
        """
        opts: list[tuple[str, str, str]] = [
            ("— не задано —",                  "none",     ""),
            ("🎙  Замутить микрофон",           "mute_mic", ""),
            ("🔇  Замутить динамики (Deafen)",  "deafen",   ""),
        ]

        # ── Пользователи из known_users.json (для шёпота) ─────────────────────
        try:
            if os.path.exists(KNOWN_USERS_PATH):
                with open(KNOWN_USERS_PATH, "r", encoding="utf-8") as f:
                    registry: dict = json.load(f)
                users = sorted(
                    ((v.get("nick", ""), ip)
                     for ip, v in registry.items() if v.get("nick", "")),
                    key=lambda x: x[0].lower()
                )
                for nick, ip in users:
                    opts.append((f"🤫  Шёпот → {nick}", "whisper", ip))
        except Exception:
            pass

        # ── Кастомные звуки soundboard ────────────────────────────────────────
        s = self.app_settings
        for i in range(CUSTOM_SOUND_SLOTS):
            name = s.value(f"custom_sound_{i}_name", "")
            if name:
                opts.append((f"🎵  Звук: {name}", "sound", name))

        return opts

    def _add_hk_row(self, func_type: str = "none", func_data: str = "",
                    hotkey: str = "") -> None:
        """Добавляет одну строку в таблицу горячих клавиш."""
        MAX_ROWS = 7
        if len(self._hk_rows) >= MAX_ROWS:
            self._btn_hk_add.setEnabled(False)
            return

        opts = self._build_function_options()

        # ── Фрейм строки ──────────────────────────────────────────────────────
        frame = QFrame()
        frame.setStyleSheet("""
            QFrame {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.09);
                border-radius: 8px;
            }
        """)
        row_lay = QHBoxLayout(frame)
        row_lay.setContentsMargins(8, 5, 8, 5)
        row_lay.setSpacing(8)

        # Колонка 1: выбор функции
        cb = QComboBox()
        cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for text, ftype, fdata in opts:
            cb.addItem(text, (ftype, fdata))

        # Восстанавливаем выбор
        selected_idx = 0
        for j in range(cb.count()):
            d = cb.itemData(j)
            if d and d[0] == func_type and d[1] == func_data:
                selected_idx = j
                break
        cb.setCurrentIndex(selected_idx)

        # ── Фикс прозрачности выпадающего списка на Windows ──────────────────
        # QComboBox popup — отдельное top-level окно, которое при
        # WA_TranslucentBackground родителя рендерится прозрачным.
        # Решение: явно задаём solid-фон на view-виджете и убираем флаг у его окна.
        def _fix_this_cb_popup(combo=cb):
            try:
                v = combo.view()
                v.setStyleSheet(
                    "QAbstractItemView {"
                    "  background-color: #1e2130;"
                    "  color: #c8d0e0;"
                    "  selection-background-color: #2c3252;"
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
        QTimer.singleShot(0, _fix_this_cb_popup)

        # Колонка 2: захват клавиши
        hk_edit = HotkeyCaptureEdit()
        hk_edit.set_hotkey(hotkey)

        # Кнопка удаления
        btn_del = QPushButton("✕")
        btn_del.setFixedSize(28, 28)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setStyleSheet("""
            QPushButton {
                background: rgba(220,60,60,0.15);
                color: #e87070;
                border: 1px solid rgba(220,60,60,0.35);
                border-radius: 6px;
                font-size: 13px;
                padding: 0;
            }
            QPushButton:hover {
                background: rgba(220,60,60,0.35);
                color: #ffffff;
            }
        """)

        row_lay.addWidget(cb, stretch=4)
        row_lay.addWidget(hk_edit, stretch=5)
        row_lay.addWidget(btn_del)

        slot = {"cb": cb, "hk": hk_edit, "frame": frame}
        self._hk_rows.append(slot)

        # Вставляем перед последним stretch
        stretch_idx = self._hk_rows_layout.count() - 1
        self._hk_rows_layout.insertWidget(stretch_idx, frame)

        # Кнопка «+» — недоступна при максимуме
        self._btn_hk_add.setEnabled(len(self._hk_rows) < MAX_ROWS)

        # ── Удаление строки ───────────────────────────────────────────────────
        # ВАЖНО: btn_del.clicked передаёт checked:bool первым аргументом.
        # Принимаем его явно, чтобы он не попал в _slot и list.remove() не падал.
        def _remove(checked: bool = False, _slot=slot):
            if _slot not in self._hk_rows:
                return   # защита от двойного срабатывания
            self._hk_rows.remove(_slot)
            _slot["frame"].setParent(None)
            _slot["frame"].deleteLater()
            # Если строк не осталось — добавляем одну пустую
            if not self._hk_rows:
                self._add_hk_row()
            self._btn_hk_add.setEnabled(len(self._hk_rows) < MAX_ROWS)

        btn_del.clicked.connect(_remove)

    def _load_hk_rows(self) -> None:
        """
        Загружает строки горячих клавиш из QSettings.
        Если сохранённых строк нет (первый запуск или всё удалено) —
        добавляет одну пустую строку-шаблон.
        """
        s = self.app_settings
        count = s.value("hk_table_count", None)

        if count is None or int(count) == 0:
            # Первый запуск или пустая таблица — одна пустая строка
            self._add_hk_row()
            return

        for i in range(int(count)):
            ftype = s.value(f"hk_table_{i}_type", "none")
            fdata = s.value(f"hk_table_{i}_data", "")
            fhk   = s.value(f"hk_table_{i}_key",  "")
            self._add_hk_row(ftype, fdata, fhk)

    # ── Старая вкладка «Шёпот» — удалена (логика перенесена в Персонализацию) ─
    # setup_whisper_tab — метод намеренно отсутствует.
    # _clear_whisper_slots — метод намеренно отсутствует.


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
        cust_group = QGroupBox("🎵  Мои звуки")
        cust_group.setStyleSheet("QGroupBox { font-weight: bold; }")
        cust_lay = QVBoxLayout(cust_group)

        desc = QLabel(
            "Добавьте собственные звуки (.mp3 / .wav), максимум 1 МБ (~7 сек)."
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
        btn_del = QPushButton("✕")
        btn_del.setFixedSize(28, 28)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setEnabled(bool(saved_path))
        btn_del.setStyleSheet("""
            QPushButton {
                background: rgba(220,60,60,0.15);
                color: #e87070;
                border: 1px solid rgba(220,60,60,0.35);
                border-radius: 6px;
                font-size: 13px;
                padding: 0;
            }
            QPushButton:hover {
                background: rgba(220,60,60,0.35);
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

    # ── Вспомогательные методы профиля ───────────────────────────────────────

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

    def _on_nr_mode_changed(self, index: int):
        """Вызывается при смене режима шумодава в ComboBox."""
        mode = self.cb_nr.currentData()
        if mode is None:
            return
        self.audio.set_nr_mode(mode)

    def _fix_combo_popups(self):
        """
        Устраняет прозрачность выпадающих меню QComboBox на Windows.

        Причина: диалог имеет WA_TranslucentBackground, и Windows-compositor
        рендерит popup-окно комбобокса тоже прозрачным, несмотря на CSS.
        Решение: для каждого QComboBox явно ставим solid-stylesheet на view-виджет
        и снимаем WA_TranslucentBackground с его top-level окна.
        """
        from PyQt6.QtWidgets import QComboBox as _QCB
        _VIEW_SS = (
            "QAbstractItemView {"
            "  background-color: #1e2130;"
            "  color: #c8d0e0;"
            "  selection-background-color: #2c3252;"
            "  selection-color: #ffffff;"
            "  border: 1px solid #333648;"
            "}"
        )
        for cb in self.findChildren(_QCB):
            try:
                v = cb.view()
                v.setStyleSheet(_VIEW_SS)
                win = v.window()
                win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
                win.setStyleSheet("background-color: #1e2130;")
            except Exception:
                pass

    def get_devices(self):
        return self.cb_in.currentText(), self.cb_out.currentText()

    # ── FIX MEM: отключаем сигнал уровня микрофона перед закрытием ───────────
    # audio_engine.volume_level_signal.connect(mic_vad.set_level) создаёт
    # сильную ссылку: audio_engine → bound-method → mic_vad → SettingsDialog.
    # Без disconnect диалог не освобождается из памяти даже при WA_DeleteOnClose,
    # пока audio_engine жив (то есть всё время работы приложения).
    # done() вызывается для accept(), reject() и кнопки закрытия окна —
    # единственная точка выхода, которая покрывает все сценарии.
    def done(self, result: int):
        try:
            self.audio.volume_level_signal.disconnect(self.mic_vad.set_level)
        except (RuntimeError, TypeError):
            pass  # уже отключён или C++ объект уничтожен
        super().done(result)

    def save_all(self):
        s = self.app_settings
        s.setValue("device_in_name", self.cb_in.currentText())
        s.setValue("device_out_name", self.cb_out.currentText())
        s.setValue("system_sound_volume", self.sl_sys.value())
        s.setValue("soundboard_volume", self.sl_sb.value())
        s.setValue("vad_threshold_slider", self.sl_vad.value())

        # ── Сохраняем таблицу горячих клавиш ─────────────────────────────────
        s.setValue("hk_table_count", len(self._hk_rows))
        whisper_slot_idx = 0   # счётчик для обратносовместимых ключей шёпота

        # Сбрасываем прежние значения mute/deafen — перезапишем из таблицы
        s.setValue("hk_mute", "")
        s.setValue("hk_deafen", "")
        # Сбрасываем старые whisper-слоты
        for i in range(8):
            s.setValue(f"whisper_slot_{i}_nick", "")
            s.setValue(f"whisper_slot_{i}_ip",   "")
            s.setValue(f"whisper_slot_{i}_hk",   "")

        for i, row in enumerate(self._hk_rows):
            data = row["cb"].currentData()   # (func_type, func_data)
            hk   = row["hk"].get_hotkey()
            ftype = data[0] if data else "none"
            fdata = data[1] if data else ""

            s.setValue(f"hk_table_{i}_type", ftype)
            s.setValue(f"hk_table_{i}_data", fdata)
            s.setValue(f"hk_table_{i}_key",  hk)

            # Обратносовместимые ключи для остального кода приложения
            if ftype == "mute_mic" and not s.value("hk_mute", ""):
                s.setValue("hk_mute", hk)
            elif ftype == "deafen" and not s.value("hk_deafen", ""):
                s.setValue("hk_deafen", hk)
            elif ftype == "whisper" and whisper_slot_idx < 8 and hk:
                # Восстанавливаем nick из known_users.json по IP
                nick = ""
                try:
                    if os.path.exists(KNOWN_USERS_PATH):
                        with open(KNOWN_USERS_PATH, "r", encoding="utf-8") as f:
                            reg = json.load(f)
                        nick = reg.get(fdata, {}).get("nick", "")
                except Exception:
                    pass
                s.setValue(f"whisper_slot_{whisper_slot_idx}_ip",   fdata)
                s.setValue(f"whisper_slot_{whisper_slot_idx}_nick", nick)
                s.setValue(f"whisper_slot_{whisper_slot_idx}_hk",   hk)
                whisper_slot_idx += 1

        self.mw.nick = self.ed_nick.text()
        self.mw.avatar = self.cur_av
        self.mw.setWindowTitle(f"{APP_NAME} v{APP_VERSION} — {self.mw.nick}")
        if hasattr(self.mw, 'net'):
            self.mw.net.update_user_info(self.mw.nick, self.mw.avatar)

        # Синхронизируем статус: QSettings обновляется через SelfStatusOverlayPanel
        # (правый клик по нику). Читаем актуальное значение и обновляем MainWindow.
        new_icon = self.app_settings.value("my_status_icon", "")
        new_text = self.app_settings.value("my_status_text", "")
        if hasattr(self.mw, '_my_status_icon'):
            self.mw._my_status_icon = new_icon
            self.mw._my_status_text = new_text
        if hasattr(self.mw, 'net'):
            self.mw.net.send_presence_update(new_icon, new_text)

        if os.path.exists(USER_CONFIG_PATH):
            try:
                with open(USER_CONFIG_PATH, 'r') as f:
                    d = json.load(f)
                d['nick'] = self.mw.nick
                d['avatar'] = self.mw.avatar
                with open(USER_CONFIG_PATH, 'w') as f:
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


# ══════════════════════════════════════════════════════════════════════════════
# StatusDialog — диалог выбора пользовательского статуса
# ══════════════════════════════════════════════════════════════════════════════
class SelfStatusOverlayPanel(QFrame):
    """
    Всплывающий полупрозрачный оверлей выбора собственного статуса.
    Открывается правым кликом по своему никнейму в дереве.

    Дизайн повторяет UserOverlayPanel: тёмный полупрозрачный card,
    скруглённые углы, Qt.Popup (автозакрытие при клике вне).

    Содержимое:
    • Сетка иконок статусов (5 колонок, авто-сканирование assets/status/)
    • Поле описания (макс. 20 символов) + счётчик
    • Кнопки «Убрать статус» и «Применить»

    on_save(icon: str, text: str) — вызывается при нажатии «Применить»
    или «Убрать статус» (с пустыми строками).
    """

    _COLS   = 5    # иконок в строке
    _BTN_SZ = 44   # px — размер кнопки иконки

    def __init__(self, current_icon: str, current_text: str,
                 global_pos, on_save, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self._on_save       = on_save
        self._selected_icon = current_icon
        self._icon_buttons: dict = {}  # filename → QPushButton

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("selfStatusOverlay")

        # ── Внешний layout (отступы = «воздух» под тень) ─────────────────────
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Card ──────────────────────────────────────────────────────────────
        self._card = QFrame(self)
        self._card.setObjectName("statusCard")
        self._card.setStyleSheet("""
            QFrame#statusCard {
                background-color: rgba(18, 20, 28, 225);
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 14px;
            }
            QLabel {
                color: #d0d0d8;
                font-size: 12px;
                background: transparent;
                border: none;
            }
        """)
        outer.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(14, 12, 14, 14)
        card_lay.setSpacing(8)

        # ── Заголовок ─────────────────────────────────────────────────────────
        title = QLabel("✨  Мой статус")
        title.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #e0e0ec; "
            "background: transparent; border: none;"
        )
        card_lay.addWidget(title)

        # ── Тонкий разделитель ─────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(
            "background: rgba(255,255,255,0.09); border: none; max-height: 1px;"
        )
        sep.setMaximumHeight(1)
        card_lay.addWidget(sep)

        # ── Скролл-зона с иконками ─────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setMaximumHeight(200)
        scroll.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.05);
                width: 6px; border-radius: 3px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.22);
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        icons_w = QWidget()
        icons_w.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(icons_w)
        self._grid.setSpacing(5)
        self._grid.setContentsMargins(0, 2, 0, 2)
        self._load_icons(current_icon)
        scroll.setWidget(icons_w)
        card_lay.addWidget(scroll)

        # ── Описание ───────────────────────────────────────────────────────────
        lbl_desc = QLabel("Описание (необязательно):")
        lbl_desc.setStyleSheet(
            "font-size: 11px; color: rgba(200,200,210,0.70); "
            "background: transparent; border: none;"
        )
        card_lay.addWidget(lbl_desc)

        self._text_edit = QLineEdit()
        self._text_edit.setMaxLength(20)
        self._text_edit.setPlaceholderText("Например: ушёл пить чай...")
        self._text_edit.setText(current_text)
        self._text_edit.setStyleSheet("""
            QLineEdit {
                background: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.14);
                border-radius: 7px;
                padding: 5px 9px;
                color: #e0e0ec;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: rgba(91,142,245,0.65);
                background: rgba(255,255,255,0.10);
            }
        """)
        card_lay.addWidget(self._text_edit)

        self._char_counter = QLabel(f"{len(current_text)} / 20")
        self._char_counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._char_counter.setStyleSheet(
            "font-size: 10px; color: rgba(180,180,190,0.55); "
            "background: transparent; border: none;"
        )
        self._text_edit.textChanged.connect(self._on_text_changed)
        card_lay.addWidget(self._char_counter)

        # ── Кнопки ────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_clear = QPushButton("✕  Убрать")
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.setStyleSheet("""
            QPushButton {
                background-color: rgba(192,57,43,0.30);
                color: #ff9090;
                border: 1px solid rgba(192,57,43,0.55);
                border-radius: 7px;
                padding: 5px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(231,76,60,0.45);
                color: #ffffff;
            }
        """)
        btn_clear.clicked.connect(self._on_clear)

        btn_ok = QPushButton("Применить")
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: rgba(46,204,113,0.28);
                color: #82e0aa;
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 7px;
                padding: 5px 16px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(39,174,96,0.45);
                color: #ffffff;
            }
        """)
        btn_ok.clicked.connect(self._on_apply)

        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_ok)
        card_lay.addLayout(btn_row)

        # ── Подгон размера и позиционирование ────────────────────────────────
        self.adjustSize()
        self.setFixedWidth(max(self.sizeHint().width(), 280))

        screen = QGuiApplication.screenAt(global_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()

        x = global_pos.x()
        y = global_pos.y()
        if x + self.width()  > avail.right():
            x = avail.right() - self.width() - 4
        if y + self.height() > avail.bottom():
            y = global_pos.y() - self.height()
        x = max(avail.left() + 4, x)
        y = max(avail.top()  + 4, y)
        self.move(x, y)

    # ── Внутренние методы ─────────────────────────────────────────────────────

    def _load_icons(self, selected: str):
        """Сканирует assets/status/ и заполняет сетку кнопками-иконками."""
        status_dir = resource_path("assets/status")
        svgs = []
        if os.path.isdir(status_dir):
            svgs = sorted(f for f in os.listdir(status_dir) if f.lower().endswith('.svg'))

        if not svgs:
            lbl = QLabel("Иконки не найдены.\nПоложи SVG в assets/status/")
            lbl.setStyleSheet("color: #888888; font-size: 11px; background:transparent;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(lbl, 0, 0)
            return

        for idx, fname in enumerate(svgs):
            row, col = divmod(idx, self._COLS)
            path = resource_path(f"assets/status/{fname}")

            btn = QPushButton()
            btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
            btn.setIconSize(QSize(28, 28))
            btn.setIcon(QIcon(path))
            btn.setCheckable(True)
            btn.setChecked(fname == selected)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # Tooltip = читаемое название иконки (hover-подсказка)
            readable = fname.rsplit('.', 1)[0].replace('_', ' ').capitalize()
            btn.setToolTip(readable)

            base_style = (
                "QPushButton {"
                "  background: rgba(255,255,255,0.05);"
                "  border: 1px solid rgba(255,255,255,0.10);"
                "  border-radius: 8px;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(255,255,255,0.13);"
                "  border-color: rgba(91,142,245,0.55);"
                "}"
            )
            selected_style = (
                "QPushButton {"
                "  background: rgba(46,204,113,0.18);"
                "  border: 2px solid #2ecc71;"
                "  border-radius: 8px;"
                "}"
                "QPushButton:hover {"
                "  background: rgba(46,204,113,0.28);"
                "}"
            )
            btn.setStyleSheet(selected_style if fname == selected else base_style)

            def _make_handler(fn, b, b_style=base_style, s_style=selected_style):
                def _toggled(checked):
                    if checked:
                        # Снимаем все остальные
                        for other_fn, other_btn in self._icon_buttons.items():
                            if other_fn != fn:
                                try:
                                    other_btn.setChecked(False)
                                    other_btn.setStyleSheet(b_style)
                                except RuntimeError:
                                    pass
                        self._selected_icon = fn
                        b.setStyleSheet(s_style)
                    else:
                        # Повторный клик — снимаем статус
                        self._selected_icon = ""
                        b.setStyleSheet(b_style)
                return _toggled

            btn.toggled.connect(_make_handler(fname, btn))
            self._grid.addWidget(btn, row, col)
            self._icon_buttons[fname] = btn

    def _on_text_changed(self, text: str):
        n = len(text)
        self._char_counter.setText(f"{n} / 20")
        self._char_counter.setStyleSheet(
            "font-size: 10px; background: transparent; border: none; "
            f"color: {'rgba(231,76,60,0.90)' if n >= 18 else 'rgba(180,180,190,0.55)'};"
        )

    def _on_clear(self):
        self._selected_icon = ""
        for btn in self._icon_buttons.values():
            try:
                btn.setChecked(False)
                btn.setStyleSheet(
                    "QPushButton {"
                    "  background: rgba(255,255,255,0.05);"
                    "  border: 1px solid rgba(255,255,255,0.10);"
                    "  border-radius: 8px;"
                    "}"
                    "QPushButton:hover {"
                    "  background: rgba(255,255,255,0.13);"
                    "  border-color: rgba(91,142,245,0.55);"
                    "}"
                )
            except RuntimeError:
                pass
        self._text_edit.clear()
        if self._on_save:
            self._on_save("", "")
        self.close()

    def _on_apply(self):
        icon = self._selected_icon
        text = self._text_edit.text().strip()[:20]
        if self._on_save:
            self._on_save(icon, text)
        self.close()


class StatusDialog(QDialog):
    """
    Диалог выбора «статуса дела» пользователя.

    Структура:
      ┌──────────────────────────────────────────┐
      │  Выбери статус                           │
      │  ┌───┐ ┌───┐ ┌───┐ ┌───┐ ┌───┐          │
      │  │SVG│ │SVG│ │SVG│ │SVG│ │SVG│  ...     │
      │  └───┘ └───┘ └───┘ └───┘ └───┘          │
      │  Описание (необязательно):               │
      │  [ Ушёл пить чай__________________ ]    │
      │                          0 / 30         │
      │  [ ✕ Убрать статус ] [Отмена] [Применить]│
      └──────────────────────────────────────────┘

    Иконки: assets/status/*.svg  (авто-сканирование).
    Выбранная иконка подсвечивается зелёной рамкой.
    «Убрать статус» → возвращает ('', '').
    Tooltip каждой иконки = имя файла без расширения.
    """

    _COLS   = 5    # иконок в строке
    _BTN_SZ = 48   # размер кнопки (px)

    def __init__(self, current_icon: str = "", current_text: str = "", parent=None):
        super().__init__(parent)
        # ── Безрамочный стеклянный дизайн ────────────────────────────────────
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Мой статус")
        self.setMinimumWidth(320)
        self.setModal(True)

        self._selected_icon: str = current_icon
        self._icon_buttons: dict = {}   # filename → QPushButton

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QFrame(self)
        self._card.setObjectName("statusCard")
        self._card.setStyleSheet("""
            QFrame#statusCard {
                background-color: rgba(26, 28, 38, 252);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
            }
            QLabel { color: #c8d0e0; background: transparent; border: none; }
            QLineEdit {
                background-color: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.13);
                border-radius: 6px;
                padding: 5px 10px;
                color: #c8d0e0;
            }
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 5px 12px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.13);
                border-color: rgba(255,255,255,0.22);
            }
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: rgba(255,255,255,0.04);
                width: 5px; border-radius: 2px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18); border-radius: 2px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QFrame[frameShape="4"] {
                background: rgba(255,255,255,0.08); border: none; max-height: 1px;
            }
        """)
        outer.addWidget(self._card)

        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # Title bar
        self._title_bar = _DialogTitleBar(self, "😊  Мой статус")
        card_lay.addWidget(self._title_bar)
        _sep0 = QFrame()
        _sep0.setFrameShape(QFrame.Shape.HLine)
        _sep0.setFixedHeight(1)
        _sep0.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        card_lay.addWidget(_sep0)

        # Контент
        content_w = QWidget()
        content_w.setStyleSheet("background: transparent;")
        root = QVBoxLayout(content_w)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        card_lay.addWidget(content_w)

        # ── Заголовок ──────────────────────────────────────────────────────────
        title_lbl = QLabel("Выбери статус")
        title_lbl.setStyleSheet("font-weight: bold; font-size: 14px; color: #cdd6f4; background:transparent;")
        root.addWidget(title_lbl)

        # ── Скролл-зона с иконками ─────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumHeight(220)

        icons_w = QWidget()
        icons_w.setStyleSheet("background: transparent;")
        self._grid = QGridLayout(icons_w)
        self._grid.setSpacing(6)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._load_icons(current_icon)
        scroll.setWidget(icons_w)
        root.addWidget(scroll)
        root.addWidget(QLabel("Описание (необязательно):"))

        self._text_edit = QLineEdit()
        self._text_edit.setMaxLength(30)
        self._text_edit.setPlaceholderText("Например: ушёл пить чай...")
        self._text_edit.setText(current_text)
        self._text_edit.setStyleSheet("padding: 5px 8px; border-radius: 5px;")
        root.addWidget(self._text_edit)

        self._char_counter = QLabel(f"{len(current_text)} / 30")
        self._char_counter.setStyleSheet("font-size: 11px; color: #888888;")
        self._char_counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._text_edit.textChanged.connect(self._on_text_changed)
        root.addWidget(self._char_counter)

        # ── Разделитель ────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # ── Кнопки ─────────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        btn_clear = QPushButton("✕  Убрать статус")
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.setStyleSheet("""
            QPushButton {
                background-color: rgba(192,57,43,0.30);
                color: #ff9090;
                border: 1px solid rgba(231,76,60,0.50);
                border-radius: 6px; padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: rgba(231,76,60,0.45);
                color: #ffffff;
            }
        """)
        btn_clear.clicked.connect(self._on_clear)

        btn_cancel = QPushButton("Отмена")
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.06);
                color: #8899bb;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 6px; padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.11);
                color: #c8d0e0;
            }
        """)
        btn_cancel.clicked.connect(self.reject)

        btn_ok = QPushButton("✔  Применить")
        btn_ok.setDefault(True)
        btn_ok.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: rgba(46,204,113,0.25);
                color: #82e0aa;
                border: 1px solid rgba(46,204,113,0.50);
                border-radius: 6px; padding: 6px 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: rgba(46,204,113,0.40);
                color: #ffffff;
            }
        """)
        btn_ok.clicked.connect(self.accept)

        btn_row.addWidget(btn_clear)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_ok)
        root.addLayout(btn_row)

    # ── Внутренние методы ─────────────────────────────────────────────────────

    def _load_icons(self, selected: str):
        """Сканирует assets/status/ и заполняет сетку кнопками-иконками."""
        status_dir = resource_path("assets/status")
        svgs = []
        if os.path.isdir(status_dir):
            svgs = sorted(f for f in os.listdir(status_dir) if f.lower().endswith('.svg'))

        if not svgs:
            lbl = QLabel("Иконки статусов не найдены.\nПоложи SVG-файлы в assets/status/")
            lbl.setStyleSheet("color: #888888; font-size: 12px;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(lbl, 0, 0)
            return

        for idx, fname in enumerate(svgs):
            row, col = divmod(idx, self._COLS)
            path = resource_path(f"assets/status/{fname}")

            btn = QPushButton()
            btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
            btn.setIconSize(QSize(30, 30))
            btn.setIcon(QIcon(path))
            btn.setCheckable(True)
            btn.setChecked(fname == selected)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(fname.rsplit('.', 1)[0].replace('_', ' ').capitalize())

            if fname == selected:
                btn.setStyleSheet("border: 2px solid #2ecc71; border-radius: 8px;")

            def _make_handler(fn, b):
                def _toggled(checked):
                    if checked:
                        for other_fn, other_btn in self._icon_buttons.items():
                            if other_fn != fn:
                                try:
                                    other_btn.setChecked(False)
                                    other_btn.setStyleSheet("")
                                except RuntimeError:
                                    pass
                        self._selected_icon = fn
                        b.setStyleSheet("border: 2px solid #2ecc71; border-radius: 8px;")
                    else:
                        # Повторный клик по той же иконке → снимаем статус
                        self._selected_icon = ""
                        b.setStyleSheet("")
                return _toggled

            btn.toggled.connect(_make_handler(fname, btn))
            self._grid.addWidget(btn, row, col)
            self._icon_buttons[fname] = btn

    def _on_text_changed(self, text: str):
        n = len(text)
        self._char_counter.setText(f"{n} / 30")
        self._char_counter.setStyleSheet(
            f"font-size: 11px; color: {'#e74c3c' if n >= 28 else '#888888'};"
        )

    def _on_clear(self):
        """Сбросить статус и сразу закрыть диалог с пустым результатом."""
        self._selected_icon = ""
        for btn in self._icon_buttons.values():
            try:
                btn.setChecked(False)
                btn.setStyleSheet("")
            except RuntimeError:
                pass
        self._text_edit.clear()
        self.accept()

    def get_result(self) -> tuple:
        """Возвращает (icon_filename, status_text) после exec()."""
        return self._selected_icon, self._text_edit.text().strip()[:30]