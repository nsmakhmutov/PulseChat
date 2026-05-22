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

_fm_cache: dict = {}

def _get_fm(px: int) -> 'QFontMetrics':
    if px not in _fm_cache:
        from PyQt6.QtWidgets import QApplication
        f = QFont(QApplication.font())
        f.setPixelSize(px)
        _fm_cache[px] = QFontMetrics(f)
    return _fm_cache[px]

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    _MEDIA_OK = True
except ImportError:
    _MEDIA_OK = False
    print("[Chat] PyQt6.QtMultimedia не найден — видео воспроизведение отключено")

_CACHE_DIR: str = tempfile.mkdtemp(prefix="inpulse_media_")
atexit.register(shutil.rmtree, _CACHE_DIR, True)

_OPEN_WINDOWS: list = []

def _cache_write(data: bytes, suffix: str) -> str:
    path = os.path.join(_CACHE_DIR, uuid.uuid4().hex + suffix)
    with open(path, 'wb') as f:
        f.write(data)
    return path

def clear_media_cache() -> None:
    try:
        for name in os.listdir(_CACHE_DIR):
            try:
                os.remove(os.path.join(_CACHE_DIR, name))
            except Exception:
                pass
        print(f"[Chat] Кэш очищен ({_CACHE_DIR})")
    except Exception:
        pass

def _gif_to_mp4(gif_data: bytes) -> bytes:
    """
    Конвертирует GIF в MP4 (H.264, CRF 28, preset fast).
    Возвращает bytes mp4 или b'' при ошибке.
    Требует: pip install imageio imageio-ffmpeg
    """
    try:
        import imageio
        import io
        import numpy as np

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
    try:
        import imageio
        path = _cache_write(video_data, suffix)
        reader = imageio.get_reader(path)
        frame  = reader.get_data(0)
        reader.close()
        import numpy as np
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

def _make_round_avatar(avatar: str, size: int) -> 'QPixmap | None':

    raw_pix = None

    if avatar and len(avatar) < 60 and '/' not in avatar and '\\' not in avatar:
        path = resource_path(f"assets/avatars/{avatar}")
        ico  = QIcon(path)
        if not ico.isNull():
            raw_pix = ico.pixmap(size * 2, size * 2)
            if raw_pix and not raw_pix.isNull():
                raw_pix.setDevicePixelRatio(1.0)

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

    scaled = raw_pix.scaled(
        size, size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    scaled.setDevicePixelRatio(1.0)

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

    def update(self, text: str) -> None:
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

class CustomTitleBar(QWidget):

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

        _sep = QFrame()
        _sep.setFrameShape(QFrame.Shape.VLine)
        _sep.setObjectName("titleBtnSep")
        _sep.setFixedSize(1, 18)
        layout.addWidget(_sep)

        self._btn_min = QPushButton("─")
        self._btn_min.setObjectName("titleBtnMin")
        self._btn_min.setFixedSize(34, 30)
        self._btn_min.clicked.connect(parent_window.showMinimized)

        self._btn_close = QPushButton("✕")
        self._btn_close.setObjectName("titleBtnClose")
        self._btn_close.setFixedSize(34, 30)
        self._btn_close.clicked.connect(parent_window.close)

        layout.addWidget(self._btn_min)
        layout.addWidget(self._btn_close)

    def set_title(self, title: str):
        self._title_lbl.setText(title)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()


class ChatMessageWidget(QWidget):
    _AV   = 32
    _BMAX = 440

    def __init__(self, entry: dict, my_uid: int,
                 show_avatar: bool = True,
                 show_header: bool = True,
                 parent=None):

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
            av_lbl.setStyleSheet("background:transparent;border:none;")

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

        t_lbl = None

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
                    display = text
                    t_fmt   = Qt.TextFormat.PlainText

                t_lbl = QLabel(display)
                t_lbl.setTextFormat(t_fmt)
                t_lbl.setWordWrap(True)
                t_lbl.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse |
                    Qt.TextInteractionFlag.LinksAccessibleByMouse)
                t_lbl.setOpenExternalLinks(True)

                if has_url:
                    first_url = url_re.search(text)
                    if first_url:
                        self._preview_card = _LinkPreviewCard(first_url.group(0))
                        b_lay.addWidget(self._preview_card)
                t_lbl.setStyleSheet(
                    "font-size:13px;background:transparent;border:none;padding:0;")
                if is_own:
                    t_lbl.setAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                else:
                    t_lbl.setAlignment(
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

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

        _PAD = 20 + 20
        if file_data_b64 or (text and self._extract_youtube_id(text)):
            bubble.setMaximumWidth(self._BMAX)
            bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        elif text:
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

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 2, 8, 2)
        outer.setSpacing(8)
        self._av_lbl = av_lbl
        if is_own:
            outer.addStretch(); outer.addWidget(bubble)
            outer.addWidget(av_lbl, alignment=Qt.AlignmentFlag.AlignTop)
        else:
            outer.addWidget(av_lbl, alignment=Qt.AlignmentFlag.AlignTop)
            outer.addWidget(bubble); outer.addStretch()

    def hide_avatar(self) -> None:
        self._av_lbl.clear()
        self._av_lbl.setStyleSheet("background:transparent;border:none;")

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
                        if pix.width() > 320:
                            pix = pix.copy((pix.width()-320)//2, 0, 320, pix.height())
                        if pix.height() > 180:
                            pix = pix.copy(0, (pix.height()-180)//2, pix.width(), 180)
                        pix_res = pix
                        break
                except Exception:
                    continue
            try:
                oe = (f"https://www.youtube.com/oembed?"
                      f"url=https://youtu.be/{_vid}&format=json")
                req2 = urllib.request.Request(
                    oe, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req2, timeout=4) as r2:
                    title_str = _json.loads(r2.read()).get('title', 'YouTube')[:60]
            except Exception:
                pass
            if pix_res is not None:
                _px = pix_res
                QTimer.singleShot(0, lambda: _t_lb.setPixmap(_px))
            _ts = title_str
            QTimer.singleShot(0, lambda: _n_lb.setText(_ts))

        threading.Thread(target=_fetch, daemon=True).start()

    def _add_media(self, layout, file_data_b64, file_name, file_type, is_own):
        if file_type == 'image':
            try:
                raw      = base64.b64decode(file_data_b64)
                img      = QImage.fromData(raw)
                del raw
                if img.isNull():
                    raise ValueError
                orig_pix = QPixmap.fromImage(img)
                del img
                thumb    = orig_pix
                if thumb.width() > 320 or thumb.height() > 320:
                    thumb = orig_pix.scaled(320, 320,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                img_lbl = QLabel()
                img_lbl.setPixmap(thumb)
                del thumb
                img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                img_lbl.setStyleSheet("border-radius:8px;background:transparent;border:none;")
                img_lbl.setCursor(Qt.CursorShape.PointingHandCursor)

                _p = orig_pix
                img_lbl.mousePressEvent = lambda e, p=_p: (
                    self._open_image_viewer(p)
                    if e.button() == Qt.MouseButton.LeftButton else None
                )
                layout.addWidget(img_lbl)
                _d_img, _n_img = file_data_b64, file_name
                save_btn_img = QPushButton("💾  Сохранить")
                save_btn_img.setFixedHeight(26)
                save_btn_img.setStyleSheet(
                    "font-size:11px;border-radius:5px;padding:0 8px;"
                    "background:rgba(255,255,255,0.07);color:#a0b0c8;"
                    "border:1px solid rgba(255,255,255,0.12);"
                )
                save_btn_img.clicked.connect(lambda: self._save_file(_d_img, _n_img))
                layout.addWidget(save_btn_img)
                return
            except Exception as ex:
                print(f"[Chat] image: {ex}")

        if file_type in ('video', 'gif'):
            try:
                raw  = base64.b64decode(file_data_b64)

                _cache_path = _cache_write(raw, '.mp4')
                thumb_pix   = _extract_first_frame(raw, '.mp4')
                del raw

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
                    del thumb_pix
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

                def _make_click_handler(path, n):
                    def _click(e):
                        if e.button() == Qt.MouseButton.LeftButton:
                            if os.path.exists(path):
                                ChatMessageWidget._open_video_player_impl(path, n)
                    return _click
                container.mousePressEvent = _make_click_handler(_cache_path, file_name)
                thumb_lbl.mousePressEvent = _make_click_handler(_cache_path, file_name)

                layout.addWidget(container)
                _d_vid, _n_vid = file_data_b64, file_name
                save_btn_vid = QPushButton("💾  Сохранить")
                save_btn_vid.setFixedHeight(26)
                save_btn_vid.setStyleSheet(
                    "font-size:11px;border-radius:5px;padding:0 8px;"
                    "background:rgba(255,255,255,0.07);color:#a0b0c8;"
                    "border:1px solid rgba(255,255,255,0.12);"
                )
                save_btn_vid.clicked.connect(lambda: self._save_file(_d_vid, _n_vid))
                layout.addWidget(save_btn_vid)
                return
            except Exception as ex:
                print(f"[Chat] video thumb: {ex}")

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

    @staticmethod
    def _open_image_viewer(pix: 'QPixmap') -> None:

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

    @staticmethod
    def _open_video_player(video_data: bytes, file_name: str) -> None:
        path = _cache_write(video_data, os.path.splitext(file_name)[1] or '.mp4')
        ChatMessageWidget._open_video_player_impl(path, file_name)

    @staticmethod
    def _open_video_player_impl(path: str, file_name: str) -> None:
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

        video_w = QVideoWidget()
        video_w.setStyleSheet("background:#000;")
        vlay.addWidget(video_w, stretch=1)

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

        win._player = player
        win._audio  = audio

        _OPEN_WINDOWS.append(win)
        def _on_win_close(ev, _w=win):
            try:
                _OPEN_WINDOWS.remove(_w)
            except ValueError:
                pass
            ev.accept()
        win.closeEvent = _on_win_close

        player.play()

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


class _ChatInput(QLineEdit):

    image_pasted = pyqtSignal(QImage)

    typing_started = pyqtSignal()

    def keyPressEvent(self, e):
        if (e.key() == Qt.Key.Key_V
                and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            cb  = QApplication.clipboard()
            img = cb.image()
            if not img.isNull():
                self.image_pasted.emit(img)
                return
        if e.text() and not e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.typing_started.emit()
        super().keyPressEvent(e)

class _LinkPreviewCard(QFrame):

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.setObjectName("linkPreviewCard")
        self.setStyleSheet("""
            #linkPreviewCard {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.08);
                border-left: 3px solid #5b8ef5;
                border-radius: 6px;
                padding: 8px 10px;
                margin-top: 4px;
            }
        """)
        self.setMaximumWidth(400)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._title_lbl = QLabel("Загрузка...")
        self._title_lbl.setStyleSheet(
            "font-size:12px; font-weight:bold; color:#5b8ef5;"
            " background:transparent; border:none;"
        )
        self._title_lbl.setWordWrap(True)
        lay.addWidget(self._title_lbl)

        self._desc_lbl = QLabel("")
        self._desc_lbl.setStyleSheet(
            "font-size:11px; color:rgba(200,208,224,0.7);"
            " background:transparent; border:none;"
        )
        self._desc_lbl.setWordWrap(True)
        self._desc_lbl.hide()
        lay.addWidget(self._desc_lbl)

        self._domain_lbl = QLabel(self._extract_domain(url))
        self._domain_lbl.setStyleSheet(
            "font-size:10px; color:rgba(140,148,175,0.6);"
            " background:transparent; border:none;"
        )
        lay.addWidget(self._domain_lbl)

        threading.Thread(
            target=self._fetch_og, args=(url,), daemon=True
        ).start()

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc
        except Exception:
            return url[:40]

    def _fetch_og(self, url: str) -> None:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (InPulse LinkPreview)',
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read(32768).decode('utf-8', errors='ignore')

            title = self._parse_meta(html, 'og:title') or self._parse_title(html) or url[:60]
            desc  = self._parse_meta(html, 'og:description') or ''

            _title_safe = title[:120]
            QTimer.singleShot(0, lambda t=_title_safe: self._title_lbl.setText(t))
            if desc:
                _desc_safe = desc[:200]
                _lbl_d = self._desc_lbl
                QTimer.singleShot(0, lambda d=_desc_safe, l=_lbl_d: (l.setText(d), l.show()))

        except Exception:
            _domain = self._extract_domain(url)
            QTimer.singleShot(0, lambda d=_domain: self._title_lbl.setText(d))

    @staticmethod
    def _parse_meta(html: str, prop: str) -> str:
        import re
        pat = re.compile(
            rf'<meta[^>]+property=["\']{prop}["\'"][^>]+content=["\']([^"\'>]+)',
            re.IGNORECASE,
        )
        m = pat.search(html)
        if m:
            return m.group(1).strip()
        pat2 = re.compile(
            rf'<meta[^>]+content=["\']([^"\'>]+)["\'"][^>]+property=["\']{prop}',
            re.IGNORECASE,
        )
        m2 = pat2.search(html)
        return m2.group(1).strip() if m2 else ''

    @staticmethod
    def _parse_title(html: str) -> str:
        import re
        m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        return m.group(1).strip() if m else ''


class ChatPanel(QFrame):
    message_sent         = pyqtSignal(str)
    media_send_requested = pyqtSignal()
    typing_started       = pyqtSignal()

    PANEL_WIDTH = 600

    def __init__(self, my_uid: int, current_room_fn, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setFixedWidth(self.PANEL_WIDTH)
        self._my_uid          = my_uid
        self._current_room_fn = current_room_fn
        self._msg_widgets: dict[tuple, ChatMessageWidget] = {}
        self._pending_media   = None
        self._last_uid: 'int | None'             = None
        self._last_widget: 'ChatMessageWidget | None' = None

        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        self._room_lbl = QLabel()

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

        self._typing_lbl = QLabel("")
        self._typing_lbl.setObjectName("chatTypingLbl")
        self._typing_lbl.setFixedHeight(18)
        self._typing_lbl.setStyleSheet(
            "#chatTypingLbl { color: rgba(140,148,175,0.7); font-size: 11px;"
            " font-style: italic; background: transparent; border: none;"
            " padding-left: 14px; }"
        )
        self._typing_lbl.hide()
        main_lay.addWidget(self._typing_lbl)

        self._typing_timer = QTimer(self)
        self._typing_timer.setSingleShot(True)
        self._typing_timer.setInterval(5000)
        self._typing_timer.timeout.connect(self._hide_typing)
        self._typing_nicks: dict[int, str] = {}  # uid → nick

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
        self._input.setPlaceholderText("Сообщение ...")
        self._input.setMaxLength(CHAT_MSG_MAX_LEN)
        self._input.setFixedHeight(32)
        self._input.returnPressed.connect(self._on_send)
        self._input.typing_started.connect(self.typing_started)
        self._input.image_pasted.connect(self._on_image_pasted)
        i_lay.addWidget(self._input, stretch=1)

        self._send_btn = QToolButton()
        self._send_btn.setText("➤")
        self._send_btn.setObjectName("chatSendBtn")
        self._send_btn.setFixedSize(30, 30)
        self._send_btn.clicked.connect(self._on_send)
        i_lay.addWidget(self._send_btn)
        main_lay.addWidget(input_bar)
        self.setAcceptDrops(True)
        self._drop_overlay = QLabel("📎  Перетащите файлы сюда", self)
        self._drop_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_overlay.setObjectName("chatDropOverlay")
        self._drop_overlay.setStyleSheet("""
            #chatDropOverlay {
                background: rgba(91, 142, 245, 0.12);
                border: 2px dashed rgba(91, 142, 245, 0.6);
                border-radius: 12px;
                color: rgba(91, 142, 245, 0.9);
                font-size: 15px;
                font-weight: 600;
                letter-spacing: 0.3px;
            }
        """)
        self._drop_overlay.hide()

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
            key_to_del = next((k for k, v in self._msg_widgets.items() if v is w), None)
            if key_to_del is not None:
                del self._msg_widgets[key_to_del]
            w.deleteLater()

    def set_my_uid(self, uid: int) -> None:
        self._my_uid = uid

    def update_room_label(self, room: str) -> None:
        self._room_lbl.setText("💬  Чат")

    def _group_flags(self, entry: dict) -> 'tuple[bool, bool]':

        uid = entry.get('uid', 0)
        same_author = (uid == self._last_uid)
        show_header = not same_author
        show_avatar = True
        return show_avatar, show_header

    def _hide_prev_avatar(self) -> None:
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

        if uid == self._last_uid and self._last_widget is not None:
            self._hide_prev_avatar()

        w = ChatMessageWidget(entry, self._my_uid,
                              show_avatar=show_avatar,
                              show_header=show_header)
        self._msg_widgets[key] = w
        self._msg_lay.insertWidget(self._msg_lay.count() - 1, w)

        self._last_uid    = uid
        self._last_widget = w

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

            show_header = not same_prev
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
        self.media_send_requested.emit()

    def _on_send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self.message_sent.emit(text)

    def _on_image_pasted(self, img: QImage) -> None:
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
                    mp4 = _gif_to_mp4(data)
                    if mp4:
                        final_data = mp4
                        file_type  = 'video'
                        file_name  = os.path.splitext(os.path.basename(path))[0] + '.mp4'
                    else:
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
                QMetaObject.invokeMethod(
                    self, "_emit_ready",
                    Qt.ConnectionType.QueuedConnection,
                )
            except Exception as ex:
                print(f"[Chat] attach: {ex}")

        threading.Thread(target=_load_and_prepare, daemon=True).start()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._drop_overlay.setGeometry(
                8, 8,
                self.width() - 16,
                self.height() - 16,
            )
            self._drop_overlay.raise_()
            self._drop_overlay.show()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drop_overlay.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drop_overlay.hide()
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        if not path or not os.path.isfile(path):
            return
        event.acceptProposedAction()
        self._process_dropped_file(path)

    def _process_dropped_file(self, path: str) -> None:
        ext = os.path.splitext(path)[1].lower()

        def _load():
            try:
                with open(path, "rb") as f:
                    data = f.read()

                if ext == ".gif":
                    mp4 = _gif_to_mp4(data)
                    if mp4:
                        final_data, file_type = mp4, "video"
                        file_name = os.path.splitext(os.path.basename(path))[0] + ".mp4"
                    else:
                        final_data, file_type = data, "image"
                        file_name = os.path.basename(path)
                elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
                    final_data, file_type, file_name = data, "video", os.path.basename(path)
                elif ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    final_data, file_type, file_name = data, "image", os.path.basename(path)
                else:
                    final_data, file_type, file_name = data, "file", os.path.basename(path)

                self._pending_media = (
                    file_name, file_type,
                    base64.b64encode(final_data).decode("ascii"),
                )
                print(f"[Chat] drop: {file_name} ({file_type})")
                QMetaObject.invokeMethod(
                    self, "_emit_ready",
                    Qt.ConnectionType.QueuedConnection,
                )
            except Exception as ex:
                print(f"[Chat] drop error: {ex}")

        threading.Thread(target=_load, daemon=True).start()

    def show_typing(self, uid: int, nick: str) -> None:
        self._typing_nicks[uid] = nick
        self._update_typing_text()
        self._typing_timer.start()  # перезапуск таймера

    def _hide_typing(self) -> None:
        self._typing_nicks.clear()
        self._typing_lbl.hide()

    def _update_typing_text(self) -> None:
        nicks = list(self._typing_nicks.values())
        if not nicks:
            self._typing_lbl.hide()
            return
        if len(nicks) == 1:
            self._typing_lbl.setText(f"{nicks[0]} печатает...")
        elif len(nicks) <= 3:
            self._typing_lbl.setText(", ".join(nicks) + " печатают...")
        else:
            self._typing_lbl.setText(f"{len(nicks)} человек печатают...")
        self._typing_lbl.show()

    def clear_typing(self, uid: int) -> None:
        self._typing_nicks.pop(uid, None)
        self._update_typing_text()

    def _scroll_to_bottom(self) -> None:
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())