import os
import base64
import re
import shutil
import tempfile
import threading
import uuid
import atexit

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QApplication, QScrollArea, QLineEdit, QSizePolicy, QToolButton,
    QFileDialog, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QSlider,
)
from PyQt6.QtCore import (
    Qt, QPoint, QSize, QTimer, pyqtSignal, pyqtSlot, QDateTime, QUrl, QRectF,
    QMetaObject,
)
from PyQt6.QtGui import (
    QIcon, QFont, QFontMetrics, QPixmap, QImage, QPainter, QBrush, QPainterPath,
)

from config import resource_path, CHAT_MSG_MAX_LEN

# ── Кэш QFontMetrics на уровне модуля ────────────────────────────────────────
# Создаём один раз при старте модуля — не аллоцируем на каждое сообщение.
# _fm_cache[px] → QFontMetrics для шрифта размером px пикселей.
# Заполняется лениво при первом запросе.
_fm_cache: dict = {}

def _get_fm(px: int) -> 'QFontMetrics':
    """Возвращает закэшированный QFontMetrics для шрифта размером px px."""
    if px not in _fm_cache:
        from PyQt6.QtWidgets import QApplication
        f = QFont(QApplication.font())
        f.setPixelSize(px)
        _fm_cache[px] = QFontMetrics(f)
    return _fm_cache[px]

# ── QMediaPlayer (опциональный — PyQt6.QtMultimedia) ─────────────────────────
try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    _MEDIA_OK = True
except ImportError:
    _MEDIA_OK = False
    print("[Chat] PyQt6.QtMultimedia не найден — видео воспроизведение отключено")

# ── Кэш медиафайлов: временная папка, очищается при выходе ───────────────────
_CACHE_DIR: str = tempfile.mkdtemp(prefix="inpulse_media_")
atexit.register(shutil.rmtree, _CACHE_DIR, True)

# ── Keep-alive: предотвращает GC top-level окон (viewer/player) ───────────────
# QWidget без родителя — GC Python может собрать его СРАЗУ после возврата
# из статического метода. Список удерживает сильную ссылку до закрытия окна.
_OPEN_WINDOWS: list = []

def _cache_write(data: bytes, suffix: str) -> str:
    """Записывает bytes во временный файл кэша, возвращает путь."""
    path = os.path.join(_CACHE_DIR, uuid.uuid4().hex + suffix)
    with open(path, 'wb') as f:
        f.write(data)
    return path

def clear_media_cache() -> None:
    """Удаляет все файлы кэша (вызывается при закрытии сервера)."""
    try:
        for name in os.listdir(_CACHE_DIR):
            try:
                os.remove(os.path.join(_CACHE_DIR, name))
            except Exception:
                pass
        print(f"[Chat] Кэш очищен ({_CACHE_DIR})")
    except Exception:
        pass

# ── GIF → MP4 конвертация ─────────────────────────────────────────────────────
def _gif_to_mp4(gif_data: bytes) -> bytes:
    """
    Конвертирует GIF в MP4 (H.264, CRF 28, preset fast).
    Возвращает bytes mp4 или b'' при ошибке.
    Требует: pip install imageio imageio-ffmpeg
    """
    try:
        import imageio                              # type: ignore
        import io
        import numpy as np                         # type: ignore

        reader  = imageio.get_reader(io.BytesIO(gif_data), format='gif')
        meta    = reader.get_meta_data()
        fps     = float(meta.get('fps', 15) or 15)

        out_path = _cache_write(b'', '.mp4')
        writer   = imageio.get_writer(
            out_path, fps=fps, codec='libx264',
            output_params=[
                '-crf',    '28',
                '-preset', 'fast',
                '-pix_fmt','yuv420p',
                '-movflags', '+faststart',
            ],
        )
        for frame in reader:
            arr = np.asarray(frame)
            if arr.ndim == 3 and arr.shape[2] == 4:
                arr = arr[:, :, :3]
            # H.264 требует чётных размеров
            h, w = arr.shape[:2]
            arr = arr[:(h & ~1), :(w & ~1)]
            writer.append_data(arr)
        writer.close()
        reader.close()

        with open(out_path, 'rb') as f:
            return f.read()

    except ImportError:
        print("[Chat] imageio не установлен: pip install imageio imageio-ffmpeg")
        return b''
    except Exception as ex:
        print(f"[Chat] gif_to_mp4: {ex}")
        return b''


def _extract_first_frame(video_data: bytes, suffix: str = '.mp4') -> 'QPixmap | None':
    """Извлекает первый кадр видео как QPixmap для превью."""
    try:
        import imageio    # type: ignore
        path = _cache_write(video_data, suffix)
        reader = imageio.get_reader(path)
        frame  = reader.get_data(0)
        reader.close()
        import numpy as np  # type: ignore
        arr = np.asarray(frame)
        if arr.ndim == 3 and arr.shape[2] == 4:
            arr = arr[:, :, :3]
        img = QImage(
            arr.tobytes(), arr.shape[1], arr.shape[0],
            arr.shape[1] * 3, QImage.Format.Format_RGB888,
        )
        return QPixmap.fromImage(img)
    except Exception as ex:
        print(f"[Chat] extract_first_frame: {ex}")
        return None


# ── Вспомогательная функция: SVG/base64 → круглый QPixmap ───────────────────
def _make_round_avatar(avatar: str, size: int) -> 'QPixmap | None':
    """
    SVG-имя ('1.svg') из assets/avatars/ или base64 PNG → круглый QPixmap.
    Рендерим SVG в 2× размере и масштабируем вниз — антиалиасинг как в дереве.
    """
    raw_pix = None

    # SVG из assets/avatars/
    if avatar and len(avatar) < 60 and '/' not in avatar and '\\' not in avatar:
        path = resource_path(f"assets/avatars/{avatar}")
        ico  = QIcon(path)
        if not ico.isNull():
            # 2× рендер для HiDPI-качества, затем плавное уменьшение
            raw_pix = ico.pixmap(size * 2, size * 2)
            if raw_pix and not raw_pix.isNull():
                raw_pix.setDevicePixelRatio(1.0)

    # base64 PNG/JPEG
    if (raw_pix is None or raw_pix.isNull()) and avatar and len(avatar) > 60:
        try:
            raw = base64.b64decode(avatar)
            img = QImage.fromData(raw)
            if not img.isNull():
                raw_pix = QPixmap.fromImage(img)
                if raw_pix:
                    raw_pix.setDevicePixelRatio(1.0)
        except Exception:
            pass

    if raw_pix is None or raw_pix.isNull():
        return None

    # Масштабируем плавно до size×size
    scaled = raw_pix.scaled(
        size, size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scaled.setDevicePixelRatio(1.0)

    # Круглая маска через clip-path (без QBrush-тайлинга)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addEllipse(0.0, 0.0, float(size), float(size))
    p.setClipPath(clip)
    p.drawPixmap(0, 0, size, size, scaled)
    p.end()
    return result


# ──────────────────────────────────────────────────────────────────────────────
# QuickMsgBubble
# ──────────────────────────────────────────────────────────────────────────────
class QuickMsgBubble(QWidget):
    MAX_W = 210

    def __init__(self, parent=None):
        super().__init__(parent,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._card = QFrame()
        self._card.setObjectName("qbCard")
        self._card.setStyleSheet("""
            QFrame#qbCard {
                background-color: rgba(16, 18, 32, 220);
                border: 1px solid rgba(91, 142, 245, 0.60);
                border-radius: 10px;
            }
        """)
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(10, 7, 10, 7)
        card_lay.setSpacing(0)
        self._text_lbl = QLabel()
        self._text_lbl.setWordWrap(True)
        self._text_lbl.setStyleSheet(
            "color:#eaf0ff;font-size:13px;font-weight:600;"
            "background:transparent;border:none;")
        card_lay.addWidget(self._text_lbl)
        outer.addWidget(self._card)

        self._tail = QLabel("▶")
        self._tail.setFixedWidth(12)
        self._tail.setStyleSheet(
            "color:rgba(91,142,245,0.60);font-size:11px;"
            "background:transparent;border:none;padding:0;margin:0;")
        self._tail.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._tail)

    def update(self, text: str) -> None:  # type: ignore[override]
        self._text_lbl.setText(text)
        fm = QFontMetrics(self._text_lbl.font())
        text_w = fm.horizontalAdvance(text) + 8
        content_w = min(text_w, self.MAX_W - 20)
        self._text_lbl.setFixedWidth(content_w)
        self._card.setFixedWidth(content_w + 20)
        self.adjustSize()

    def place_left_of(self, global_item_tl, item_h: int) -> None:
        bw, bh = self.width(), self.height()
        x = global_item_tl.x() - bw - 2
        y = global_item_tl.y() + (item_h - bh) // 2
        screen = QApplication.screenAt(global_item_tl)
        if screen:
            sg = screen.geometry()
            if x < sg.left():
                x = global_item_tl.x() + 32 + 6
            y = max(sg.top() + 4, min(y, sg.bottom() - bh - 4))
        self.move(x, y)


# ──────────────────────────────────────────────────────────────────────────────
# CustomTitleBar
# ──────────────────────────────────────────────────────────────────────────────
class CustomTitleBar(QWidget):
    """Кастомный title bar. Кнопка 💬 эмитит chat_toggled(bool)."""

    chat_toggled = pyqtSignal(bool)

    def __init__(self, parent_window, title=""):
        super().__init__(parent_window)
        self._win = parent_window
        self._drag_pos = None
        self.setFixedHeight(40)
        self.setObjectName("customTitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(4)

        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(22, 22)
        self._icon_lbl.setPixmap(QIcon(resource_path("assets/icon/logo.ico")).pixmap(22, 22))
        layout.addWidget(self._icon_lbl)

        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("titleBarText")
        layout.addWidget(self._title_lbl, stretch=1)

        self._btn_chat = QPushButton("💬")
        self._btn_chat.setObjectName("titleBtnChat")
        self._btn_chat.setFixedSize(30, 26)
        self._btn_chat.setCheckable(True)
        self._btn_chat.setToolTip("Чат (Ctrl+T)")
        self._btn_chat.clicked.connect(lambda checked: self.chat_toggled.emit(checked))
        layout.addWidget(self._btn_chat)

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.VLine)
        _sep.setObjectName("titleBtnSep")
        _sep.setFixedSize(1, 18)
        layout.addWidget(_sep)

        self._btn_min = QPushButton("─")
        self._btn_min.setObjectName("titleBtnMin")
        self._btn_min.setFixedSize(34, 30)
        self._btn_min.clicked.connect(parent_window.showMinimized)

        self._btn_max = QPushButton("□")
        self._btn_max.setObjectName("titleBtnMax")
        self._btn_max.setFixedSize(34, 30)
        self._btn_max.clicked.connect(self._toggle_maximize)

        self._btn_close = QPushButton("✕")
        self._btn_close.setObjectName("titleBtnClose")
        self._btn_close.setFixedSize(34, 30)
        self._btn_close.clicked.connect(parent_window.close)

        layout.addWidget(self._btn_min)
        layout.addWidget(self._btn_max)
        layout.addWidget(self._btn_close)

    def set_title(self, title: str):
        self._title_lbl.setText(title)

    def set_chat_checked(self, checked: bool) -> None:
        self._btn_chat.setChecked(checked)

    def _toggle_maximize(self):
        if self._win.isMaximized():
            self._win.showNormal(); self._btn_max.setText("□")
        else:
            self._win.showMaximized(); self._btn_max.setText("❐")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            if self._win.isMaximized():
                self._win.showNormal(); self._btn_max.setText("□")
                self._drag_pos = QPoint(self._win.width() // 2, 20)
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()


# ──────────────────────────────────────────────────────────────────────────────
# ChatMessageWidget — одно сообщение (текст / картинка / видео / файл)
# ──────────────────────────────────────────────────────────────────────────────
class ChatMessageWidget(QWidget):
    _AV   = 32    # совпадает с tree.setIconSize(32,32)
    _BMAX = 440

    def __init__(self, entry: dict, my_uid: int,
                 show_avatar: bool = True,
                 show_header: bool = True,
                 parent=None):
        """
        show_avatar — показывать ли аватарку (False для всех сообщений группы
                      кроме последнего; вместо аватарки — пустой отступ).
        show_header — показывать ли строку ник+время (True только для первого
                      сообщения в группе или для одиночных сообщений).
        """
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("chatMsgWidget")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        is_own        = (entry.get('uid', 0) == my_uid)
        nick          = entry.get('nick', '?')
        text          = entry.get('text', '')
        ts            = entry.get('ts', 0.0)
        avatar        = entry.get('avatar', '1.svg')
        file_data_b64 = entry.get('file_data_b64', '')
        file_name     = entry.get('file_name', 'file')
        file_type     = entry.get('file_type', 'file')
        time_str      = QDateTime.fromSecsSinceEpoch(int(ts)).toString('HH:mm')

        # ── Аватарка / пустой отступ ──────────────────────────────────────────
        # Если аватарку не показываем — ставим прозрачный spacer той же ширины,
        # чтобы пузырь не прыгал по горизонтали внутри группы.
        av_lbl = QLabel()
        av_lbl.setFixedSize(self._AV, self._AV)
        av_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        av_lbl.setScaledContents(False)
        if show_avatar:
            pix = _make_round_avatar(avatar, self._AV)
            if pix and not pix.isNull():
                av_lbl.setPixmap(pix)
                av_lbl.setStyleSheet("background:transparent;border:none;")
            else:
                initials = nick[:2].upper() if len(nick) >= 2 else (nick[:1].upper() or '?')
                color    = "#2ecc71" if is_own else "#5b8ef5"
                av_lbl.setText(initials)
                av_lbl.setStyleSheet(
                    f"background:rgba(91,142,245,0.18);border-radius:{self._AV//2}px;"
                    f"font-size:10px;font-weight:bold;color:{color};"
                )
        else:
            # Прозрачный держатель — место аватарки остаётся, но пустое
            av_lbl.setStyleSheet("background:transparent;border:none;")

        # ── Пузырь — скругление углов по позиции в группе (Telegram-стиль) ──
        # R = 12px — полное скругление изолированных углов
        # r =  4px — «хвост» (угол со стороны стека сообщений)
        #
        # Собственные сообщения (правый край = сторона стека):
        #   single : все=12, кроме TR=4 и BR=4
        #   first  : все=12, кроме TR=4 (начало группы — верхний правый острый)
        #   middle : все=12, кроме TR=4 и BR=4 (оба правых — острые)
        #   last   : все=12, кроме BR=4 (конец группы — нижний правый острый у аватарки)
        #
        # Чужие сообщения (левый край = сторона стека): зеркально.
        R, r = 12, 4
        if show_header and show_avatar:
            _pos = 'single'
        elif show_header and not show_avatar:
            _pos = 'first'
        elif not show_header and not show_avatar:
            _pos = 'middle'
        else:
            _pos = 'last'

        if is_own:
            _br = {
                'single': f"{R}px {r}px {r}px {R}px",
                'first' : f"{R}px {r}px {R}px {R}px",
                'middle': f"{R}px {r}px {r}px {R}px",
                'last'  : f"{R}px {R}px {r}px {R}px",
            }[_pos]
            _bg   = "rgba(91,142,245,0.16)"
            _bord = "rgba(91,142,245,0.28)"
        else:
            _br = {
                'single': f"{r}px {R}px {R}px {r}px",
                'first' : f"{r}px {R}px {R}px {R}px",
                'middle': f"{r}px {R}px {R}px {r}px",
                'last'  : f"{R}px {R}px {R}px {r}px",
            }[_pos]
            _bg   = "rgba(255,255,255,0.07)"
            _bord = "rgba(255,255,255,0.10)"

        bubble = QFrame()
        # objectName сохраняем для совместимости, стиль ставим напрямую
        bubble.setObjectName("chatBubbleOwn" if is_own else "chatBubbleOther")
        bubble.setStyleSheet(
            f"QFrame#{bubble.objectName()} {{"
            f"  background-color:{_bg};"
            f"  border:1px solid {_bord};"
            f"  border-radius:{_br};"
            f"}}"
        )
        b_lay = QVBoxLayout(bubble)
        b_lay.setContentsMargins(10, 6, 10, 6)
        b_lay.setSpacing(4)

        # Шапка (ник + время) — только для первого сообщения в группе
        if show_header:
            hdr   = QWidget()
            hdr.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            h_lay = QHBoxLayout(hdr)
            h_lay.setContentsMargins(0, 0, 0, 0)
            h_lay.setSpacing(5)
            nick_lbl = QLabel(nick)
            nick_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            nick_lbl.setStyleSheet(
                f"font-size:11px;font-weight:bold;"
                f"color:{'#2ecc71' if is_own else '#5b8ef5'};"
                "background:transparent;border:none;")
            time_lbl = QLabel(time_str)
            time_lbl.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            time_lbl.setStyleSheet(
                "font-size:10px;color:rgba(140,148,175,0.9);"
                "background:transparent;border:none;")
            if is_own:
                h_lay.addStretch()
                h_lay.addWidget(time_lbl)
                h_lay.addWidget(nick_lbl)
            else:
                h_lay.addWidget(nick_lbl)
                h_lay.addWidget(time_lbl)
                h_lay.addStretch()
            b_lay.addWidget(hdr)

        # t_lbl — инициализируем как None, чтобы проверить ниже без dir()
        t_lbl = None

        # ── Контент ───────────────────────────────────────────────────────────
        if file_data_b64:
            self._add_media(b_lay, file_data_b64, file_name, file_type, is_own)
        elif text:
            yt_id = self._extract_youtube_id(text)
            if yt_id:
                self._add_youtube_card(b_lay, text, yt_id)
            else:
                has_url = bool(re.search(r'https?://', text))
                if has_url:
                    url_re  = re.compile(r'(https?://[^\s]+)')
                    display = url_re.sub(r'<a href="\1" style="color:#5b8ef5;">\1</a>', text)
                    t_fmt   = Qt.TextFormat.RichText
                else:
                    # PlainText переносит любой непрерывный текст
                    display = text
                    t_fmt   = Qt.TextFormat.PlainText

                t_lbl = QLabel(display)
                t_lbl.setTextFormat(t_fmt)
                t_lbl.setWordWrap(True)
                t_lbl.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse |
                    Qt.TextInteractionFlag.LinksAccessibleByMouse)
                t_lbl.setOpenExternalLinks(True)
                # ВАЖНО: не ставим qproperty-alignment в stylesheet —
                # он перекрывает setAlignment() и текст всегда слева
                t_lbl.setStyleSheet(
                    "font-size:13px;background:transparent;border:none;padding:0;")
                # Выравнивание: своё — вправо, чужое — влево
                if is_own:
                    t_lbl.setAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                else:
                    t_lbl.setAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

                # ── Русское контекстное меню ───────────────────────────────────
                _MENU_STYLE = """
                    QMenu {
                        background-color: rgba(14, 16, 28, 245);
                        border: 1px solid rgba(91,142,245,0.35);
                        border-radius: 8px;
                        padding: 4px 0;
                        color: #d8e0f0;
                        font-size: 13px;
                    }
                    QMenu::item {
                        padding: 6px 20px 6px 14px;
                        border-radius: 4px;
                        margin: 1px 4px;
                    }
                    QMenu::item:selected {
                        background-color: rgba(91,142,245,0.30);
                        color: #ffffff;
                    }
                    QMenu::separator {
                        height: 1px;
                        background: rgba(255,255,255,0.10);
                        margin: 3px 10px;
                    }
                """
                def _ctx_menu(event, _lbl=t_lbl, _raw=text):
                    from PyQt6.QtWidgets import QMenu
                    from PyQt6.QtGui import QGuiApplication
                    menu = QMenu()
                    menu.setStyleSheet(_MENU_STYLE)
                    has_sel      = bool(_lbl.hasSelectedText())
                    act_copy     = menu.addAction("📋  Копировать")
                    act_copy.setEnabled(has_sel)
                    act_copy_all = menu.addAction("📄  Копировать всё")
                    menu.addSeparator()
                    act_sel_all  = menu.addAction("⬜  Выделить всё")
                    act_copy.triggered.connect(
                        lambda: QGuiApplication.clipboard().setText(_lbl.selectedText()))
                    act_copy_all.triggered.connect(
                        lambda: QGuiApplication.clipboard().setText(_raw))
                    act_sel_all.triggered.connect(
                        lambda: _lbl.setSelection(0, len(_lbl.text())))
                    menu.exec(event.globalPos())

                t_lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                t_lbl.customContextMenuRequested.connect(
                    lambda pos, _l=t_lbl: _ctx_menu(
                        type('E', (), {'globalPos': lambda s=None: _l.mapToGlobal(pos)})()
                    )
                )
                b_lay.addWidget(t_lbl)

        # ── Ширина пузыря: точно по содержимому ──────────────────────────────
        _PAD = 20 + 20   # b_lay.contentsMargins left+right
        if file_data_b64 or (text and self._extract_youtube_id(text)):
            bubble.setMaximumWidth(self._BMAX)
            bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        elif text:
            # Используем кэшированные метрики — не аллоцируем QFont/QLabel на каждое сообщение
            _fm13 = _get_fm(13)
            _tw   = _fm13.horizontalAdvance(text) + 8

            if show_header:
                _fm11  = _get_fm(11)
                _fm10  = _get_fm(10)
                _hdr_w = (_fm11.horizontalAdvance(nick) +
                          _fm10.horizontalAdvance(time_str) + 10)
            else:
                _hdr_w = 0

            _max_content = self._BMAX - _PAD
            _content_w   = min(max(_tw, _hdr_w), _max_content)
            bubble.setFixedWidth(_content_w + _PAD)
        else:
            bubble.setMaximumWidth(self._BMAX)
            bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        # ── Сборка ────────────────────────────────────────────────────────────
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 2, 8, 2)
        outer.setSpacing(8)
        self._av_lbl = av_lbl   # сохраняем для hide_avatar()
        if is_own:
            outer.addStretch(); outer.addWidget(bubble)
            outer.addWidget(av_lbl, alignment=Qt.AlignmentFlag.AlignTop)
        else:
            outer.addWidget(av_lbl, alignment=Qt.AlignmentFlag.AlignTop)
            outer.addWidget(bubble); outer.addStretch()

    def hide_avatar(self) -> None:
        """Скрывает аватарку — вызывается когда следующее сообщение того же автора."""
        self._av_lbl.clear()
        self._av_lbl.setStyleSheet("background:transparent;border:none;")

    # ── YouTube ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_youtube_id(text: str) -> 'str | None':
        for p in [
            r'(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([A-Za-z0-9_-]{11})',
            r'youtube\.com/shorts/([A-Za-z0-9_-]{11})',
            r'youtube\.com/embed/([A-Za-z0-9_-]{11})',
        ]:
            m = re.search(p, text)
            if m:
                return m.group(1)
        return None

    def _add_youtube_card(self, layout, full_url: str, video_id: str) -> None:
        import urllib.request, json as _json

        card = QFrame()
        card.setStyleSheet(
            "background:rgba(0,0,0,0.30);border-radius:8px;"
            "border:1px solid rgba(255,255,255,0.12);")
        c_lay = QVBoxLayout(card)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(0)

        thumb_lbl = QLabel()
        thumb_lbl.setFixedSize(320, 180)
        thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_lbl.setStyleSheet("background:#111;border-radius:8px 8px 0 0;border:none;")
        # Иконка воспроизведения поверх превью (CSS overlay не работает в Qt — нарисуем)
        play_overlay = QLabel("▶", thumb_lbl)
        play_overlay.setFixedSize(60, 60)
        play_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        play_overlay.move(130, 60)
        play_overlay.setStyleSheet(
            "background:rgba(0,0,0,0.65);border-radius:30px;"
            "color:#fff;font-size:26px;border:none;")
        c_lay.addWidget(thumb_lbl)

        title_lbl = QLabel("YouTube")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        title_lbl.setWordWrap(True)
        title_lbl.setContentsMargins(8, 4, 8, 4)
        title_lbl.setStyleSheet(
            "font-size:12px;font-weight:bold;color:#eaedf5;"
            "background:rgba(0,0,0,0.22);border:none;padding:4px 8px;")
        c_lay.addWidget(title_lbl)

        open_btn = QPushButton("▶  Открыть в браузере")
        open_btn.setFixedHeight(30)
        open_btn.setStyleSheet(
            "background:rgba(200,0,0,0.85);color:#fff;font-size:12px;"
            "font-weight:bold;border:none;border-radius:0 0 8px 8px;")
        _url = full_url
        open_btn.clicked.connect(lambda: __import__('webbrowser').open(_url))
        c_lay.addWidget(open_btn)
        layout.addWidget(card)

        _vid  = video_id
        _t_lb = thumb_lbl
        _n_lb = title_lbl

        def _fetch():
            pix_res   = None
            title_str = "YouTube"
            # Превью: пробуем качество от лучшего к худшему
            for q in ("maxresdefault", "hqdefault", "mqdefault", "0"):
                try:
                    req = urllib.request.Request(
                        f"https://img.youtube.com/vi/{_vid}/{q}.jpg",
                        headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        data = r.read()
                    img = QImage.fromData(data)
                    if not img.isNull() and img.width() > 120:
                        pix = QPixmap.fromImage(img).scaled(
                            320, 180,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
                        # Центральная обрезка до 320×180
                        if pix.width() > 320:
                            pix = pix.copy((pix.width()-320)//2, 0, 320, pix.height())
                        if pix.height() > 180:
                            pix = pix.copy(0, (pix.height()-180)//2, pix.width(), 180)
                        pix_res = pix
                        break
                except Exception:
                    continue
            # Заголовок через oEmbed
            try:
                oe = (f"https://www.youtube.com/oembed?"
                      f"url=https://youtu.be/{_vid}&format=json")
                req2 = urllib.request.Request(
                    oe, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req2, timeout=4) as r2:
                    title_str = _json.loads(r2.read()).get('title', 'YouTube')[:60]
            except Exception:
                pass
            # Обновляем UI строго через QTimer.singleShot (из главного потока)
            if pix_res is not None:
                _px = pix_res
                QTimer.singleShot(0, lambda: _t_lb.setPixmap(_px))
            _ts = title_str
            QTimer.singleShot(0, lambda: _n_lb.setText(_ts))

        threading.Thread(target=_fetch, daemon=True).start()

    # ── Медиа ─────────────────────────────────────────────────────────────────

    def _add_media(self, layout, file_data_b64, file_name, file_type, is_own):
        # ── Изображение ───────────────────────────────────────────────────────
        if file_type == 'image':
            try:
                raw      = base64.b64decode(file_data_b64)
                img      = QImage.fromData(raw)
                del raw   # bytes больше не нужны — QImage уже скопировал
                if img.isNull():
                    raise ValueError
                orig_pix = QPixmap.fromImage(img)
                del img   # QPixmap скопировал данные
                thumb    = orig_pix
                if thumb.width() > 320 or thumb.height() > 320:
                    thumb = orig_pix.scaled(320, 320,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                img_lbl = QLabel()
                img_lbl.setPixmap(thumb)
                del thumb   # скопирован в лейбл
                img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                img_lbl.setStyleSheet("border-radius:8px;background:transparent;border:none;")
                img_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                # orig_pix держим для полноэкранного просмотра — это нормально,
                # это единственная копия изображения в памяти
                _p = orig_pix
                img_lbl.mousePressEvent = lambda e, p=_p: (
                    self._open_image_viewer(p)
                    if e.button() == Qt.MouseButton.LeftButton else None
                )
                layout.addWidget(img_lbl)
                return
            except Exception as ex:
                print(f"[Chat] image: {ex}")

        # ── Видео / GIF-конвертированный ──────────────────────────────────────
        if file_type in ('video', 'gif'):
            try:
                raw  = base64.b64decode(file_data_b64)
                # Записываем в кэш-файл ОДИН РАЗ — замыкание держит путь (строку),
                # а не raw bytes (могут быть MB). raw освобождается сразу.
                _cache_path = _cache_write(raw, '.mp4')
                thumb_pix   = _extract_first_frame(raw, '.mp4')
                del raw   # освобождаем байты — путь уже сохранён

                container = QFrame()
                container.setFixedWidth(320)
                container.setStyleSheet(
                    "background:rgba(0,0,0,0.40);border-radius:12px;border:none;")
                container.setCursor(Qt.CursorShape.PointingHandCursor)
                c_lay = QVBoxLayout(container)
                c_lay.setContentsMargins(0, 0, 0, 0)
                c_lay.setSpacing(0)

                thumb_lbl = QLabel()
                thumb_lbl.setFixedSize(320, 180)
                thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                thumb_lbl.setStyleSheet(
                    "border-radius:12px;border:none;background:#0a0a0a;")
                if thumb_pix and not thumb_pix.isNull():
                    t = thumb_pix.scaled(320, 180,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    thumb_lbl.setPixmap(t)
                    del thumb_pix   # thumbnail уже скопирован в лейбл
                else:
                    thumb_lbl.setText("🎬")
                    thumb_lbl.setStyleSheet(
                        "font-size:48px;background:#111;"
                        "border-radius:12px;border:none;")

                play_overlay = QLabel("▶", thumb_lbl)
                play_overlay.setFixedSize(64, 64)
                play_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
                play_overlay.move(128, 58)
                play_overlay.setStyleSheet(
                    "background: rgba(0,0,0,0.60);"
                    "border-radius: 32px;"
                    "color: #ffffff;"
                    "font-size: 26px;"
                    "border: 2px solid rgba(255,255,255,0.30);"
                    "padding-left: 4px;"
                )
                play_overlay.show()
                play_overlay.setAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents)

                c_lay.addWidget(thumb_lbl)

                # Замыкание держит только строку пути, не bytes
                def _make_click_handler(path, n):
                    def _click(e):
                        if e.button() == Qt.MouseButton.LeftButton:
                            if os.path.exists(path):
                                ChatMessageWidget._open_video_player_impl(path, n)
                    return _click
                container.mousePressEvent = _make_click_handler(_cache_path, file_name)
                thumb_lbl.mousePressEvent = _make_click_handler(_cache_path, file_name)

                layout.addWidget(container)
                return
            except Exception as ex:
                print(f"[Chat] video thumb: {ex}")

        # ── Прочие файлы ──────────────────────────────────────────────────────
        try:
            raw_size = len(base64.b64decode(file_data_b64))
            size_str = (f"{raw_size/1_048_576:.1f} МБ" if raw_size >= 1_048_576
                        else f"{raw_size//1024} КБ")
        except Exception:
            size_str = "?"
        card_lbl = QLabel(f"📁  {size_str}")
        card_lbl.setStyleSheet("font-size:13px;background:transparent;border:none;padding:2px 0;")
        layout.addWidget(card_lbl)
        save_btn = QPushButton("💾  Сохранить")
        save_btn.setFixedHeight(28)
        save_btn.setStyleSheet("font-size:12px;border-radius:6px;padding:0 10px;")
        _d, _n = file_data_b64, file_name
        save_btn.clicked.connect(lambda: self._save_file(_d, _n))
        layout.addWidget(save_btn)

    # ── Просмотр изображения ──────────────────────────────────────────────────

    @staticmethod
    def _open_image_viewer(pix: 'QPixmap') -> None:
        """
        Полноэкранный просмотр: QGraphicsView + зум колесом,
        панорама перетаскиванием, кнопки +/−/⊡/✕, Esc.
        """
        screen = QApplication.primaryScreen().availableGeometry()

        viewer = QWidget(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        viewer.setStyleSheet("background:#0d0f1a;")
        viewer.resize(screen.width(), screen.height())
        viewer.move(screen.topLeft())

        vlay = QVBoxLayout(viewer)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        # Тулбар
        toolbar = QWidget()
        toolbar.setFixedHeight(44)
        toolbar.setStyleSheet("background:rgba(0,0,0,0.60);")
        t_lay = QHBoxLayout(toolbar)
        t_lay.setContentsMargins(12, 0, 12, 0)
        t_lay.setSpacing(6)
        hint = QLabel("🔍  Колесо мыши — зум  •  Перетащить — панорама  •  Esc / ✕ — закрыть")
        hint.setStyleSheet("color:rgba(200,210,230,0.60);font-size:12px;background:transparent;border:none;")
        t_lay.addWidget(hint, stretch=1)

        def _tbtn(txt):
            b = QPushButton(txt)
            b.setFixedSize(34, 30)
            b.setStyleSheet(
                "background:rgba(255,255,255,0.10);border:none;"
                "border-radius:6px;color:#c8d0e0;font-size:15px;"
            )
            return b

        btn_plus  = _tbtn("＋")
        btn_minus = _tbtn("−")
        btn_reset = _tbtn("⊡")
        btn_close = _tbtn("✕")
        for b in (btn_plus, btn_minus, btn_reset, btn_close):
            t_lay.addWidget(b)
        vlay.addWidget(toolbar)

        # Scene + View
        scene = QGraphicsScene()
        item  = QGraphicsPixmapItem(pix)
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        scene.addItem(item)

        ZOOM_STEP = 1.20
        ZOOM_MIN  = 0.03
        ZOOM_MAX  = 20.0

        class _View(QGraphicsView):
            def __init__(self):
                super().__init__(scene)
                self._s = 1.0
                self.setRenderHint(QPainter.RenderHint.Antialiasing)
                self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
                self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
                self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                self.setStyleSheet("background:#0d0f1a;border:none;")

            def wheelEvent(self, e):
                d = e.angleDelta().y()
                f = ZOOM_STEP if d > 0 else 1.0 / ZOOM_STEP
                ns = self._s * f
                if ZOOM_MIN <= ns <= ZOOM_MAX:
                    self._s = ns
                    self.scale(f, f)

            def zoom(self, f):
                ns = self._s * f
                if ZOOM_MIN <= ns <= ZOOM_MAX:
                    self._s = ns
                    self.scale(f, f)

            def reset(self):
                self.resetTransform()
                self._s = 1.0
                self.fitInView(item, Qt.AspectRatioMode.KeepAspectRatio)
                self._s = self.transform().m11()

        gv = _View()
        vlay.addWidget(gv, stretch=1)
        QTimer.singleShot(15, gv.reset)

        btn_plus .clicked.connect(lambda: gv.zoom(ZOOM_STEP))
        btn_minus.clicked.connect(lambda: gv.zoom(1.0 / ZOOM_STEP))
        btn_reset.clicked.connect(gv.reset)
        btn_close.clicked.connect(viewer.close)
        viewer.keyPressEvent = lambda e: (viewer.close() if e.key() == Qt.Key.Key_Escape else None)

        # Keep-alive: удерживаем ссылку пока окно открыто
        _OPEN_WINDOWS.append(viewer)
        _orig_close_ev = viewer.closeEvent if hasattr(viewer, 'closeEvent') else None
        def _on_viewer_close(ev, _w=viewer):
            try:
                _OPEN_WINDOWS.remove(_w)
            except ValueError:
                pass
            if _orig_close_ev:
                _orig_close_ev(ev)
            else:
                ev.accept()
        viewer.closeEvent = _on_viewer_close

        viewer.show()

    # ── Проигрыватель видео ───────────────────────────────────────────────────

    @staticmethod
    def _open_video_player(video_data: bytes, file_name: str) -> None:
        """Открывает плеер из raw bytes (legacy — для обратной совместимости)."""
        path = _cache_write(video_data, os.path.splitext(file_name)[1] or '.mp4')
        ChatMessageWidget._open_video_player_impl(path, file_name)

    @staticmethod
    def _open_video_player_impl(path: str, file_name: str) -> None:
        """Общая реализация плеера — принимает готовый путь к файлу."""
        if not _MEDIA_OK:
            import subprocess
            try:
                os.startfile(path)
            except AttributeError:
                subprocess.Popen(['xdg-open', path])
            return

        screen = QApplication.primaryScreen().availableGeometry()

        win = QWidget(
            None,
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        win.setStyleSheet("background:#000;")
        win.resize(screen.width(), screen.height())
        win.move(screen.topLeft())

        vlay = QVBoxLayout(win)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        # Тулбар
        toolbar = QWidget()
        toolbar.setFixedHeight(44)
        toolbar.setStyleSheet("background:rgba(0,0,0,0.70);")
        t_lay = QHBoxLayout(toolbar)
        t_lay.setContentsMargins(12, 0, 12, 0)
        t_lay.setSpacing(8)
        title_lbl = QLabel(f"🎬  {file_name}")
        title_lbl.setStyleSheet("color:#c8d0e0;font-size:13px;background:transparent;border:none;")
        t_lay.addWidget(title_lbl, stretch=1)
        hint_lbl = QLabel("Space — пауза  •  ← → — ±10с  •  Esc — закрыть")
        hint_lbl.setStyleSheet("color:rgba(200,210,230,0.55);font-size:11px;background:transparent;border:none;")
        t_lay.addWidget(hint_lbl)
        btn_close = QPushButton("✕")
        btn_close.setFixedSize(34, 30)
        btn_close.setStyleSheet(
            "background:rgba(255,255,255,0.10);border:none;"
            "border-radius:6px;color:#c8d0e0;font-size:15px;")
        t_lay.addWidget(btn_close)
        vlay.addWidget(toolbar)

        # Видеовиджет
        video_w = QVideoWidget()
        video_w.setStyleSheet("background:#000;")
        vlay.addWidget(video_w, stretch=1)

        # Панель управления
        ctrl = QWidget()
        ctrl.setFixedHeight(52)
        ctrl.setStyleSheet("background:rgba(0,0,0,0.70);")
        c_lay = QHBoxLayout(ctrl)
        c_lay.setContentsMargins(14, 0, 14, 0)
        c_lay.setSpacing(10)

        btn_pp = QPushButton("⏸")
        btn_pp.setFixedSize(40, 36)
        btn_pp.setStyleSheet(
            "background:rgba(91,142,245,0.70);border:none;"
            "border-radius:8px;color:#fff;font-size:16px;")

        seek = QSlider(Qt.Orientation.Horizontal)
        seek.setRange(0, 1000)
        seek.setStyleSheet(
            "QSlider::groove:horizontal{background:rgba(255,255,255,0.18);"
            "height:4px;border-radius:2px;}"
            "QSlider::handle:horizontal{background:#5b8ef5;width:12px;height:12px;"
            "border-radius:6px;margin:-4px 0;}"
            "QSlider::sub-page:horizontal{background:#5b8ef5;border-radius:2px;}"
        )

        time_lbl = QLabel("0:00 / 0:00")
        time_lbl.setFixedWidth(100)
        time_lbl.setStyleSheet("color:#c8d0e0;font-size:12px;background:transparent;border:none;")

        c_lay.addWidget(btn_pp)
        c_lay.addWidget(seek, stretch=1)
        c_lay.addWidget(time_lbl)
        vlay.addWidget(ctrl)

        # Плеер
        player = QMediaPlayer()
        audio  = QAudioOutput()
        audio.setVolume(1.0)
        player.setAudioOutput(audio)
        player.setVideoOutput(video_w)
        player.setSource(QUrl.fromLocalFile(path))

        def _fmt(ms):
            s = ms // 1000
            return f"{s//60}:{s%60:02d}"

        def _on_pos(ms):
            dur = player.duration()
            if dur > 0:
                seek.blockSignals(True)
                seek.setValue(int(ms * 1000 / dur))
                seek.blockSignals(False)
            time_lbl.setText(f"{_fmt(ms)} / {_fmt(dur)}")

        def _on_seek(v):
            dur = player.duration()
            if dur > 0:
                player.setPosition(int(v * dur / 1000))

        def _toggle():
            if player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                player.pause(); btn_pp.setText("▶")
            else:
                player.play();  btn_pp.setText("⏸")

        def _on_close():
            player.stop()
            win.close()

        player.positionChanged.connect(_on_pos)
        seek.valueChanged.connect(_on_seek)
        btn_pp.clicked.connect(_toggle)
        btn_close.clicked.connect(_on_close)

        def _keypress(e):
            k = e.key()
            if k == Qt.Key.Key_Escape:
                _on_close()
            elif k == Qt.Key.Key_Space:
                _toggle()
            elif k == Qt.Key.Key_Left:
                player.setPosition(max(0, player.position() - 10_000))
            elif k == Qt.Key.Key_Right:
                player.setPosition(min(player.duration(), player.position() + 10_000))
        win.keyPressEvent = _keypress

        win.show()
        # Привязываем player и audio к win — иначе GC Python соберёт их
        # СРАЗУ после возврата из статического метода (они локальные),
        # и видео остановится без видимой причины.
        win._player = player
        win._audio  = audio

        # Keep-alive: удерживаем ссылку пока окно открыто
        _OPEN_WINDOWS.append(win)
        def _on_win_close(ev, _w=win):
            try:
                _OPEN_WINDOWS.remove(_w)
            except ValueError:
                pass
            ev.accept()
        win.closeEvent = _on_win_close

        player.play()

    # ── Сохранить файл ────────────────────────────────────────────────────────

    @staticmethod
    def _save_file(file_data_b64: str, file_name: str) -> None:
        path, _ = QFileDialog.getSaveFileName(
            None, "Сохранить файл",
            os.path.join(os.path.expanduser("~"), "Downloads", file_name))
        if path:
            try:
                with open(path, 'wb') as f:
                    f.write(base64.b64decode(file_data_b64))
            except Exception as ex:
                print(f"[Chat] save: {ex}")


# ──────────────────────────────────────────────────────────────────────────────
# ChatPanel — полноценная боковая панель (Discord-стиль)
# ──────────────────────────────────────────────────────────────────────────────
class _ChatInput(QLineEdit):
    """
    QLineEdit с перехватом Ctrl+V для изображений из буфера обмена.
    При вставке картинки эмитирует image_pasted(QImage).
    """
    image_pasted = pyqtSignal(QImage)

    def keyPressEvent(self, e):
        if (e.key() == Qt.Key.Key_V
                and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            cb  = QApplication.clipboard()
            img = cb.image()
            if not img.isNull():
                self.image_pasted.emit(img)
                return  # не вставляем текст
        super().keyPressEvent(e)


class ChatPanel(QFrame):
    """
    При show() MainWindow вызывает resize(w + PANEL_WIDTH, h).
    GIF конвертируется в MP4 перед отправкой.
    """
    message_sent         = pyqtSignal(str)
    media_send_requested = pyqtSignal()

    PANEL_WIDTH = 600

    def __init__(self, my_uid: int, current_room_fn, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setFixedWidth(self.PANEL_WIDTH)
        self._my_uid          = my_uid
        self._current_room_fn = current_room_fn
        self._msg_widgets: dict[tuple, ChatMessageWidget] = {}
        self._pending_media   = None
        # Состояние группировки: uid и виджет последнего добавленного сообщения
        self._last_uid: 'int | None'             = None
        self._last_widget: 'ChatMessageWidget | None' = None

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # Заголовок удалён намеренно:
        # Чат закрывается ТОЛЬКО через кнопку 💬 в title bar главного окна.
        # _room_lbl — заглушка для совместимости с update_room_label()
        self._room_lbl = QLabel()

        # Список сообщений
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setObjectName("chatScrollArea")
        self._msg_container = QWidget()
        self._msg_container.setObjectName("chatMsgContainer")
        self._msg_lay = QVBoxLayout(self._msg_container)
        self._msg_lay.setContentsMargins(0, 8, 0, 4)
        self._msg_lay.setSpacing(0)
        self._msg_lay.addStretch()
        self._scroll.setWidget(self._msg_container)
        main_lay.addWidget(self._scroll, stretch=1)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setObjectName("chatPanelSep")
        sep2.setFixedHeight(1)
        main_lay.addWidget(sep2)

        # Поле ввода
        input_bar = QFrame()
        input_bar.setObjectName("chatInputBar")
        input_bar.setFixedHeight(50)
        i_lay = QHBoxLayout(input_bar)
        i_lay.setContentsMargins(10, 0, 10, 0)
        i_lay.setSpacing(6)

        btn_attach = QToolButton()
        btn_attach.setText("📎")
        btn_attach.setObjectName("chatAttachBtn")
        btn_attach.setFixedSize(30, 30)
        btn_attach.setToolTip("Прикрепить файл (GIF конвертируется в MP4)")
        btn_attach.clicked.connect(self._on_attach_clicked)
        i_lay.addWidget(btn_attach)

        self._input = _ChatInput()
        self._input.setObjectName("chatInput")
        self._input.setPlaceholderText("Написать в чат… (Ctrl+V — вставить скриншот)")
        self._input.setMaxLength(CHAT_MSG_MAX_LEN)
        self._input.setFixedHeight(32)
        self._input.returnPressed.connect(self._on_send)
        self._input.image_pasted.connect(self._on_image_pasted)
        i_lay.addWidget(self._input, stretch=1)

        self._send_btn = QToolButton()
        self._send_btn.setText("➤")
        self._send_btn.setObjectName("chatSendBtn")
        self._send_btn.setFixedSize(30, 30)
        self._send_btn.clicked.connect(self._on_send)
        i_lay.addWidget(self._send_btn)
        main_lay.addWidget(input_bar)

    # Максимум сообщений в панели чата.
    # При превышении удаляем самое старое: виджет + запись из _msg_widgets.
    # Выбрано 200 — достаточно контекста для разговора на 30 человек,
    # при этом ОЗУ не растёт бесконечно (каждый ChatMessageWidget ~несколько KB).
    MAX_MESSAGES = 200

    def _trim_oldest(self) -> None:
        """Удаляет самое старое сообщение если превышен MAX_MESSAGES."""
        while len(self._msg_widgets) > self.MAX_MESSAGES:
            # takeAt(0) — первый элемент layout (самый старый, stretch в конце)
            item = self._msg_lay.takeAt(0)
            if item is None:
                break
            w = item.widget()
            if w is None:
                continue
            # Удаляем из dict по значению
            key_to_del = next((k for k, v in self._msg_widgets.items() if v is w), None)
            if key_to_del is not None:
                del self._msg_widgets[key_to_del]
            w.deleteLater()

    def set_my_uid(self, uid: int) -> None:
        self._my_uid = uid

    def update_room_label(self, room: str) -> None:
        self._room_lbl.setText("💬  Чат")

    # ── Вспомогательный метод: определяем параметры группировки ──────────────
    def _group_flags(self, entry: dict) -> 'tuple[bool, bool]':
        """
        Возвращает (show_avatar, show_header) для нового сообщения.

        Правило Discord-стиля:
          • Первое сообщение в группе (или после другого автора):
              show_header=True  (показываем ник+время)
              show_avatar=False (аватарка будет у последнего в группе)
          • Последнее сообщение в группе — определяем ретроспективно
            при добавлении следующего: у предыдущего скрываем аватарку.
          • Одиночное сообщение: show_header=True, show_avatar=True.

        Реализация упрощённая — двухпроходная не нужна:
          при добавлении нового сообщения:
            1. если тот же автор — скрыть аватарку у предыдущего
            2. show_header = (новый автор != предыдущего)
            3. show_avatar = True (аватарка у текущего, пока следующий не придёт)
        """
        uid = entry.get('uid', 0)
        same_author = (uid == self._last_uid)
        show_header = not same_author
        show_avatar = True
        return show_avatar, show_header

    def _hide_prev_avatar(self) -> None:
        """Скрывает аватарку у предыдущего виджета (он больше не последний в группе)."""
        if self._last_widget is not None:
            try:
                self._last_widget.hide_avatar()
            except RuntimeError:
                pass  # виджет уже удалён

    def add_message(self, entry: dict) -> None:
        key = (entry.get('uid', 0), round(entry.get('ts', 0.0), 3))
        if key in self._msg_widgets:
            return

        show_avatar, show_header = self._group_flags(entry)
        uid = entry.get('uid', 0)

        # Предыдущий виджет того же автора больше не последний → скрываем его аватарку
        if uid == self._last_uid and self._last_widget is not None:
            self._hide_prev_avatar()

        w = ChatMessageWidget(entry, self._my_uid,
                              show_avatar=show_avatar,
                              show_header=show_header)
        self._msg_widgets[key] = w
        self._msg_lay.insertWidget(self._msg_lay.count() - 1, w)

        self._last_uid    = uid
        self._last_widget = w

        # Ограничиваем размер чата — удаляем старые сообщения
        self._trim_oldest()

        QTimer.singleShot(40, self._scroll_to_bottom)

    def load_history(self, messages: list) -> None:
        while self._msg_lay.count() > 1:
            item = self._msg_lay.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._msg_widgets.clear()
        self._last_uid    = None
        self._last_widget = None

        for i, entry in enumerate(messages):
            key = (entry.get('uid', 0), round(entry.get('ts', 0.0), 3))
            if key in self._msg_widgets:
                continue

            uid         = entry.get('uid', 0)
            next_uid    = messages[i + 1].get('uid', 0) if i + 1 < len(messages) else None
            same_prev   = (uid == self._last_uid)
            same_next   = (uid == next_uid)

            # Шапка только у первого в группе
            show_header = not same_prev
            # Аватарка только у последнего в группе
            show_avatar = not same_next

            w = ChatMessageWidget(entry, self._my_uid,
                                  show_avatar=show_avatar,
                                  show_header=show_header)
            self._msg_widgets[key] = w
            self._msg_lay.insertWidget(self._msg_lay.count() - 1, w)

            self._last_uid    = uid
            self._last_widget = w

        QTimer.singleShot(60, self._scroll_to_bottom)

    def focus_input(self) -> None:
        self._input.setFocus()

    def take_pending_media(self) -> 'tuple | None':
        m = self._pending_media
        self._pending_media = None
        return m

    @pyqtSlot()
    def _emit_ready(self) -> None:
        """Вызывается из фонового потока через QMetaObject.invokeMethod."""
        self.media_send_requested.emit()

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.message_sent.emit(text)

    def _on_image_pasted(self, img: QImage) -> None:
        """Скриншот из буфера обмена — конвертируем в PNG и отправляем."""
        try:
            from PyQt6.QtCore import QBuffer, QIODevice
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            png_bytes = bytes(buf.data())
            self._pending_media = (
                "screenshot.png",
                "image",
                base64.b64encode(png_bytes).decode('ascii'),
            )
            self.media_send_requested.emit()
        except Exception as ex:
            print(f"[Chat] image_pasted: {ex}")

    def _on_attach_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выбрать файл",
            os.path.expanduser("~"),
            "Изображения (*.png *.jpg *.jpeg *.gif *.webp *.bmp);;"
            "Видео (*.mp4 *.mov *.avi *.mkv *.webm);;"
            "Все файлы (*.*)"
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()

        def _load_and_prepare():
            try:
                with open(path, 'rb') as f:
                    data = f.read()

                if ext == '.gif':
                    # GIF → MP4 в фоне
                    mp4 = _gif_to_mp4(data)
                    if mp4:
                        final_data = mp4
                        file_type  = 'video'
                        file_name  = os.path.splitext(os.path.basename(path))[0] + '.mp4'
                    else:
                        # Fallback: отправить как image (первый кадр)
                        final_data = data
                        file_type  = 'image'
                        file_name  = os.path.basename(path)
                elif ext in ('.mp4', '.mov', '.avi', '.mkv', '.webm'):
                    final_data = data
                    file_type  = 'video'
                    file_name  = os.path.basename(path)
                elif ext in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
                    final_data = data
                    file_type  = 'image'
                    file_name  = os.path.basename(path)
                else:
                    final_data = data
                    file_type  = 'file'
                    file_name  = os.path.basename(path)

                self._pending_media = (
                    file_name,
                    file_type,
                    base64.b64encode(final_data).decode('ascii'),
                )
                print(f"[Chat] attach готово: {file_name} ({file_type}) "
                      f"{len(self._pending_media[2])} символов b64")
                # Строго потокобезопасный вызов — QMetaObject.invokeMethod
                # гарантирует исполнение в GUI-потоке через очередь событий Qt.
                QMetaObject.invokeMethod(
                    self, "_emit_ready",
                    Qt.ConnectionType.QueuedConnection,
                )
            except Exception as ex:
                print(f"[Chat] attach: {ex}")

        # GIF конвертация может занять время — делаем в фоне
        threading.Thread(target=_load_and_prepare, daemon=True).start()

    def _scroll_to_bottom(self) -> None:
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())