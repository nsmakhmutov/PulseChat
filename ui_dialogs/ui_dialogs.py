import os
import json
import math
import base64
import socket
import secrets
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
                             QWidget, QGridLayout, QLabel, QSlider, QFrame,
                             QSizePolicy, QProgressBar, QLineEdit)
from PyQt6.QtCore import (Qt, QSize, QPoint, QTimer, pyqtSignal, QThread)
from PyQt6.QtGui import QIcon, QGuiApplication, QPainter, QColor, QPen, QPainterPath, QBrush
from config import (resource_path, FILE_CHUNK_SIZE, FILE_TRANSFER_TIMEOUT)


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


# ══════════════════════════════════════════════════════════════════════════════
# Кнопка с удержанием (3 секунды)
# ══════════════════════════════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════════════════════════════
# Всплывающий оверлей управления пользователем
# ══════════════════════════════════════════════════════════════════════════════
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
                color: #c8d0e0;
                background: transparent;
                border: none;
            }
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 6px 14px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: rgba(255,255,255,0.14);
                border-color: rgba(255,255,255,0.22);
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
        card_lay.setContentsMargins(14, 12, 14, 12)
        card_lay.setSpacing(8)

        # ── Никнейм (заголовок) ───────────────────────────────────────────────
        nick_lbl = QLabel(self._nick)
        nick_lbl.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #cdd6f4; "
            "background: transparent; border: none;"
        )
        card_lay.addWidget(nick_lbl)

        # Тонкий разделитель
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: rgba(255,255,255,0.08); border: none; max-height: 1px;")
        sep.setMaximumHeight(1)
        card_lay.addWidget(sep)

        # ── Блок громкости ────────────────────────────────────────────────────
        vol_lbl = QLabel("🔊  Громкость")
        vol_lbl.setStyleSheet("font-size: 12px; font-weight: bold; color: #a0b0cc;")
        card_lay.addWidget(vol_lbl)

        self.sl_vol = QSlider(Qt.Orientation.Horizontal)
        self.sl_vol.setRange(0, 200)
        self.sl_vol.setValue(_vol_to_slider(current_vol))
        self.sl_vol.setMinimumWidth(200)
        # slider 0 → 0.0 (полная тишина, _slider_to_vol гарантирует это).
        self.sl_vol.valueChanged.connect(
            lambda v: self.audio.set_user_volume(self.uid, _slider_to_vol(v))
        )
        card_lay.addWidget(self.sl_vol)

        # Метки под слайдером
        marks_row = QHBoxLayout()
        marks_row.setContentsMargins(0, 0, 0, 0)
        for txt in ["0", "50", "100 (норм)", "150", "200"]:
            lbl = QLabel(txt)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-size: 9px; color: rgba(180,190,210,0.45); background:transparent; border:none;")
            marks_row.addWidget(lbl)
        card_lay.addLayout(marks_row)

        # ── Кнопка буста ─────────────────────────────────────────────────────
        # slider 0 → 0.0 (полная тишина, _slider_to_vol гарантирует это).
        # ставим флаг vol_boost_{uid}="true", применяем _BOOST_VOL (15x).
        is_boosted = self.audio.app_settings.value(f"vol_boost_{uid}", "") == "true" \
            if hasattr(self.audio, 'app_settings') else False

        self._btn_boost = QPushButton(
            "🔊  Усилить ×15 (OFF)" if not is_boosted else "🔊  Усилить ×15 (ON)"
        )
        self._btn_boost.setCheckable(True)
        self._btn_boost.setChecked(is_boosted)
        self._btn_boost.setStyleSheet("""
            QPushButton {
                background-color: rgba(255,255,255,0.07);
                color: #c8d0e0;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px;
                padding: 6px 14px;
            }
            QPushButton:checked {
                background-color: rgba(46,204,113,0.25);
                color: #82e0aa;
                border-color: rgba(46,204,113,0.50);
            }
        """)
        self._btn_boost.toggled.connect(self._on_boost_toggled)
        card_lay.addWidget(self._btn_boost)

        # ── Кнопки действий ───────────────────────────────────────────────────
        # Шёпот (hold)
        self.btn_nudge = NudgeHoldButton("👟  Пнуть  (держи 3с)")
        self.btn_nudge.hold_complete.connect(self._on_nudge)
        card_lay.addWidget(self.btn_nudge)

        if net is not None:
            btn_whisper = QPushButton("🤫  Шепнуть (Push-to-Talk)")
            btn_whisper.setCheckable(True)
            btn_whisper.toggled.connect(self._on_whisper_toggled)
            card_lay.addWidget(btn_whisper)

        # Смотреть стрим
        if is_streaming and on_watch_stream:
            btn_watch = QPushButton("📺  Смотреть стрим")
            btn_watch.setStyleSheet("""
                QPushButton {
                    background-color: rgba(91,142,245,0.25);
                    color: #a0c0ff;
                    border: 1px solid rgba(91,142,245,0.50);
                    border-radius: 7px;
                    padding: 6px 14px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: rgba(91,142,245,0.40);
                    color: #ffffff;
                }
            """)
            btn_watch.clicked.connect(lambda: (on_watch_stream(), self.close()))
            card_lay.addWidget(btn_watch)

        # Передача сервера (только для хоста)
        if on_transfer_server:
            btn_transfer = QPushButton("👑  Сделать хостом")
            btn_transfer.clicked.connect(lambda: (on_transfer_server(uid), self.close()))
            card_lay.addWidget(btn_transfer)

        # Заглушить на сервере (только для хоста)
        if on_host_mute:
            btn_hmute = QPushButton("🔇  Заглушить (сервер)")
            btn_hmute.clicked.connect(lambda: (on_host_mute(uid), self.close()))
            card_lay.addWidget(btn_hmute)

        # ── Позиционирование ──────────────────────────────────────────────────
        self.adjustSize()

        screen = QGuiApplication.screenAt(global_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()

        x = global_pos.x()
        y = global_pos.y()
        if x + self.width()  > avail.right():
            x = avail.right()  - self.width()  - 4
        if y + self.height() > avail.bottom():
            y = global_pos.y() - self.height()
        x = max(avail.left() + 4, x)
        y = max(avail.top()  + 4, y)
        self.move(x, y)

    def _on_boost_toggled(self, checked: bool):
        """Включает/выключает буст ×15 для пользователя."""
        pre_slider = self.sl_vol.value()
        if checked:
            self._btn_boost.setText("🔊  Усилить ×15 (ON)")
            self.audio.set_user_volume(self.uid, _BOOST_VOL)
            if hasattr(self.audio, 'app_settings'):
                self.audio.app_settings.setValue(f"vol_boost_{self.uid}", "true")
        else:
            self._btn_boost.setText("🔊  Усилить ×15 (OFF)")
            self.audio.set_user_volume(self.uid, _slider_to_vol(pre_slider))
            if hasattr(self.audio, 'app_settings'):
                self.audio.app_settings.setValue(f"vol_boost_{self.uid}", "")

    def _on_nudge(self):
        """Отправляет «пнуть» через сеть."""
        if self._net is not None:
            try:
                self._net.send_nudge_vote(self.uid)
            except Exception as e:
                print(f"[UserOverlay] nudge error: {e}")
        self.close()

    def _on_whisper_toggled(self, checked: bool):
        """PTT-шёпот: включает/выключает режим шёпота к этому пользователю."""
        self._whisper_active = checked
        try:
            if checked:
                self.audio.start_whisper(self.uid)
            else:
                self.audio.stop_whisper()
        except Exception as e:
            print(f"[UserOverlay] whisper error: {e}")

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
                14.0, 14.0
            )
            p.drawPath(path)


# ══════════════════════════════════════════════════════════════════════════════
# Диалог выбора аватарки
# ══════════════════════════════════════════════════════════════════════════════
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
            from config import resource_path as _rp
            ico_lbl.setPixmap(QIcon(_rp("assets/icon/logo.ico")).pixmap(18, 18))
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


# ══════════════════════════════════════════════════════════════════════════════
# Оверлей выбора собственного статуса
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


# ══════════════════════════════════════════════════════════════════════════════
# StatusDialog — диалог выбора пользовательского статуса
# ══════════════════════════════════════════════════════════════════════════════
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
        return self._selected_icon, self._text_edit.text().strip()