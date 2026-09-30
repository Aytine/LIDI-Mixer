"""Colors, stylesheet and small painted icons."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

GREEN = "#2ecc71"
YELLOW = "#f1c40f"
RED = "#e74c3c"
ORANGE = "#e67e22"
BG = "#1e1f24"
PANEL = "#272a31"
BORDER = "#3a3d46"
MUTED = "#b8bcc8"
ACCENT = "#3a7ebf"

STYLESHEET = f"""
QMainWindow, QWidget#central {{ background: {BG}; }}
QWidget {{ color: #e6e8ee; font-size: 13px; }}
QLabel#header {{ color: {MUTED}; font-weight: bold; padding: 2px 4px; }}
QLabel#status {{ color: {MUTED}; }}
QLineEdit, QComboBox {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 10px;
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT};
}}
QPushButton {{
    background: {ACCENT}; border: none; border-radius: 8px; padding: 7px 14px; color: white;
}}
QPushButton:hover {{ background: #4a8fd0; }}
QPushButton#danger {{ background: {PANEL}; border: 1px solid {BORDER}; }}
QPushButton#danger:hover {{ background: {RED}; }}
QPushButton#run {{ background: {GREEN}; font-weight: bold; padding: 10px 18px; }}
QPushButton#run[running="true"] {{ background: {RED}; }}
QPushButton#learn[learning="true"] {{ background: {ORANGE}; }}
QPushButton#learn {{ padding: 6px 10px; }}
QScrollArea {{ border: 1px solid {BORDER}; border-radius: 10px; background: {BG}; }}
QScrollArea > QWidget > QWidget {{ background: {BG}; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame#card QLineEdit, QFrame#card QComboBox {{ background: {BG}; }}
QFrame#card QLabel {{ color: {MUTED}; }}
QFrame#card QLabel#section {{ color: #e6e8ee; font-weight: bold; padding-top: 10px; }}
QListWidget {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 10px; padding: 4px; outline: 0; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; }}
QListWidget::item:hover:!selected {{ background: {PANEL}; }}
QListWidget::item:selected {{ background: {ACCENT}; color: white; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #555a66; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ height: 0; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: white; border-bottom-color: {ACCENT}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
"""


def volume_color(val: float) -> str:
    if val < 0.70:
        return GREEN
    return YELLOW if val < 0.90 else RED


def dot_icon(hex_color: str) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(hex_color))
    p.drawEllipse(2, 2, 12, 12)
    p.end()
    return QIcon(pm)


def make_app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(BORDER), 2))
    p.setBrush(QColor(BG))
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    for x, knob_y in ((18, 38), (32, 22), (46, 30)):
        p.setPen(QPen(QColor("#5c6070"), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(x, 14, x, 50)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(GREEN))
        p.drawRoundedRect(x - 8, knob_y - 4, 16, 8, 3, 3)
    p.end()
    return QIcon(pm)
