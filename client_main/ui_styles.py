# ui_styles.py
# ──────────────────────────────────────────────────────────────────────────────
# Общие CSS/StyleSheet константы для стартовых экранов InPulse.
# Импортируется в: ui_titlebar.py, ui_login.py, ui_connecting.py,
#                  ui_server_select.py
# ──────────────────────────────────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
# Карточка (стеклянный фон)
# ══════════════════════════════════════════════════════════════════════════════

GLASS_CARD_SS = """
    QWidget#glassCard {
        background-color: rgba(22, 24, 35, 252);
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 14px;
    }
    QLabel {
        color: #c8d0e0;
        background: transparent;
        border: none;
    }
    QLineEdit {
        background-color: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 7px;
        padding: 7px 11px;
        color: #dde3f0;
        font-size: 14px;
    }
    QLineEdit:focus { border-color: rgba(91,142,245,0.70); }
    QCheckBox { color: #9aa5bb; font-size: 13px; background: transparent; }
    QCheckBox::indicator {
        width: 16px; height: 16px;
        border: 1px solid rgba(255,255,255,0.20);
        border-radius: 4px;
        background: rgba(255,255,255,0.06);
    }
    QCheckBox::indicator:checked { background: #5b8ef5; border-color: #5b8ef5; }
    QProgressBar {
        background: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 5px;
        color: #c8d0e0;
        text-align: center;
        font-size: 12px;
    }
    QProgressBar::chunk {
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
            stop:0 #2ecc71, stop:1 #27ae60);
        border-radius: 4px;
    }
"""

# ══════════════════════════════════════════════════════════════════════════════
# Блок ошибки
# ══════════════════════════════════════════════════════════════════════════════

GLASS_ERROR_SS = """
    QFrame {
        background: rgba(192,57,43,0.18);
        border: 1px solid rgba(231,76,60,0.55);
        border-radius: 8px;
    }
"""

# ══════════════════════════════════════════════════════════════════════════════
# Кнопки
# ══════════════════════════════════════════════════════════════════════════════

BTN_PRIMARY_SS = (
    "QPushButton {"
    "  background-color: rgba(39,174,96,0.30);"
    "  color: #82e0aa;"
    "  border: 1px solid rgba(46,204,113,0.55);"
    "  border-radius: 8px;"
    "  font-size: 15px;"
    "  font-weight: bold;"
    "  padding: 10px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(39,174,96,0.50);"
    "  border-color: rgba(46,204,113,0.85);"
    "  color: #ffffff;"
    "}"
    "QPushButton:pressed { background-color: rgba(39,174,96,0.70); }"
)

BTN_SECONDARY_SS = (
    "QPushButton {"
    "  background-color: rgba(41,128,185,0.28);"
    "  color: #7ec8e3;"
    "  border: 1px solid rgba(52,152,219,0.55);"
    "  border-radius: 8px;"
    "  font-size: 14px;"
    "  font-weight: bold;"
    "  padding: 9px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(52,152,219,0.45);"
    "  border-color: rgba(52,152,219,0.85);"
    "  color: #ffffff;"
    "}"
)

BTN_SKIP_SS = (
    "QPushButton {"
    "  background-color: rgba(127,140,141,0.22);"
    "  color: #8899aa;"
    "  border: 1px solid rgba(127,140,141,0.40);"
    "  border-radius: 7px;"
    "  font-size: 13px;"
    "  font-weight: bold;"
    "  padding: 8px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(149,165,166,0.35);"
    "  color: #c8d0e0;"
    "}"
)

BTN_CREATE_SS = (
    "QPushButton {"
    "  background-color: rgba(39,174,96,0.28);"
    "  color: #82e0aa;"
    "  border: 1px solid rgba(46,204,113,0.55);"
    "  border-radius: 8px;"
    "  font-size: 14px;"
    "  font-weight: bold;"
    "  padding: 9px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(39,174,96,0.48);"
    "  border-color: rgba(46,204,113,0.85);"
    "  color: #ffffff;"
    "}"
    "QPushButton:pressed { background-color: rgba(39,174,96,0.65); }"
)

BTN_CONNECT_SS = (
    "QPushButton {"
    "  background-color: rgba(52,152,219,0.28);"
    "  color: #7ec8e3;"
    "  border: 1px solid rgba(52,152,219,0.55);"
    "  border-radius: 8px;"
    "  font-size: 14px;"
    "  font-weight: bold;"
    "  padding: 9px 0;"
    "}"
    "QPushButton:hover {"
    "  background-color: rgba(52,152,219,0.48);"
    "  border-color: rgba(52,152,219,0.85);"
    "  color: #ffffff;"
    "}"
    "QPushButton:disabled { opacity: 0.4; }"
)

# ══════════════════════════════════════════════════════════════════════════════
# Карточки серверов
# ══════════════════════════════════════════════════════════════════════════════

SERVER_ITEM_SS_IDLE = """
    QFrame#serverItem {
        background-color: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 10px;
    }
    QFrame#serverItem:hover {
        background-color: rgba(91,142,245,0.16);
        border-color: rgba(91,142,245,0.55);
    }
"""

SERVER_ITEM_SS_SELECTED = """
    QFrame#serverItem {
        background-color: rgba(91,142,245,0.22);
        border: 1px solid rgba(91,142,245,0.80);
        border-radius: 10px;
    }
"""