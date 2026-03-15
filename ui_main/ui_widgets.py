from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QFrame, QApplication)
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QIcon, QFontMetrics

from config import resource_path


# ──────────────────────────────────────────────────────────────────────────────
# QuickMsgBubble — стеклянный пузырь быстрого сообщения
# ──────────────────────────────────────────────────────────────────────────────
class QuickMsgBubble(QWidget):
    """
    Frameless tool-окно: появляется поверх всего приложения (и поверх дерева).
    Позиционируется слева от аватарки отправителя по глобальным координатам.
    Имеет маленький хвостик ▶ справа — указывает на аватарку.

    Жизненный цикл управляется из MainWindow._quick_bubbles.
    Создаётся один раз на uid, текст обновляется в update().
    """

    MAX_W = 210   # максимальная ширина пузыря, px

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Стеклянный контейнер ──────────────────────────────────────────────
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

        # Текст сообщения с переносом строк
        self._text_lbl = QLabel()
        self._text_lbl.setWordWrap(True)
        self._text_lbl.setStyleSheet(
            "color: #eaf0ff; font-size: 13px; font-weight: 600;"
            "background: transparent; border: none;"
        )
        card_lay.addWidget(self._text_lbl)

        outer.addWidget(self._card)

        # ── Хвостик ▶ справа (указывает на аватарку) ─────────────────────────
        self._tail = QLabel("▶")
        self._tail.setFixedWidth(12)
        self._tail.setStyleSheet(
            "color: rgba(91, 142, 245, 0.60);"
            "font-size: 11px; background: transparent; border: none;"
            "padding: 0; margin: 0;"
        )
        self._tail.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._tail)

    def update(self, text: str) -> None:  # type: ignore[override]
        self._text_lbl.setText(text)

        # QFontMetrics напрямую измеряет пиксельную ширину текста —
        # независимо от setFixedWidth/wordWrap/sizeHint лейбла.
        # Это единственный надёжный способ: sizeHint() у wordWrap-лейбла
        # всегда возвращает ширину контейнера, а не текста.
        padding_h = 10 * 2          # card_lay contentsMargins left+right
        fm = QFontMetrics(self._text_lbl.font())
        text_w = fm.horizontalAdvance(text) + 8   # +8px запас на сглаживание
        content_w = min(text_w, self.MAX_W - padding_h)

        self._text_lbl.setFixedWidth(content_w)
        self._card.setFixedWidth(content_w + padding_h)
        self.adjustSize()

    def place_left_of(self, global_item_tl, item_h: int) -> None:
        """
        Позиционирует пузырь слева от аватарки.
        global_item_tl — глобальные экранные координаты верхнего-левого угла
        строки пользователя в дереве.
        Аватарка — первые 32px ширины строки.
        """
        bw = self.width()
        bh = self.height()
        # Правый край пузыря (вместе с хвостиком) = левый край аватарки − 2 px
        x = global_item_tl.x() - bw - 2
        # Вертикальный центр = центр строки
        y = global_item_tl.y() + (item_h - bh) // 2

        # Защита от выхода за левый край экрана
        screen = QApplication.screenAt(global_item_tl)
        if screen:
            sg = screen.geometry()
            if x < sg.left():
                # Не влезает слева → показываем справа от аватарки (32px)
                x = global_item_tl.x() + 32 + 6
            # Защита по вертикали
            y = max(sg.top() + 4, min(y, sg.bottom() - bh - 4))

        self.move(x, y)


# ──────────────────────────────────────────────────────────────────────────────
# Кастомная строка заголовка окна (вместо системного title bar Windows)
# ──────────────────────────────────────────────────────────────────────────────
class CustomTitleBar(QWidget):
    """
    Кастомный title bar для безрамочного окна.
    Поддерживает: перетаскивание окна, сворачивание, разворачивание/восстановление,
    закрытие, двойной клик для maximize/restore.
    """

    def __init__(self, parent_window, title=""):
        super().__init__(parent_window)
        self._win = parent_window
        self._drag_pos = None
        self.setFixedHeight(40)
        self.setObjectName("customTitleBar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(6)

        # Иконка приложения (logo.ico — единый источник иконки по всему проекту)
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(22, 22)
        self._icon_lbl.setPixmap(
            QIcon(resource_path("assets/icon/logo.ico")).pixmap(22, 22)
        )
        # Inline-стиль намеренно НЕ устанавливается: у QLabel без своего
        # setStyleSheet() родительский stylesheet (#customTitleBar *) применяется
        # корректно и задаёт прозрачный фон через CSS.
        layout.addWidget(self._icon_lbl)

        # Текст заголовка
        # ВАЖНО: не вызываем self._title_lbl.setStyleSheet() здесь.
        # Если у виджета есть собственный stylesheet (даже без color:), Qt полностью
        # блокирует наследование цвета из родительского stylesheet — именно поэтому
        # #titleBarText { color: ... } в apply_theme не работал в светлой теме.
        # Всё оформление делается через apply_theme CSS-правила.
        self._title_lbl = QLabel(title)
        self._title_lbl.setObjectName("titleBarText")
        layout.addWidget(self._title_lbl, stretch=1)

        # ── Кнопки управления окном ──────────────────────────────────────────
        # Размеры задаём через setFixedSize, а не через inline stylesheet —
        # по той же причине: inline stylesheet блокирует цвет из apply_theme.
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

    def _toggle_maximize(self):
        if self._win.isMaximized():
            self._win.showNormal()
            self._btn_max.setText("□")
        else:
            self._win.showMaximized()
            self._btn_max.setText("❐")

    # ── Drag to move ─────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            if self._win.isMaximized():
                self._win.showNormal()
                self._btn_max.setText("□")
                # Пересчитываем drag_pos после восстановления нормального размера
                self._drag_pos = QPoint(self._win.width() // 2, 20)
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()