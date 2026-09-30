"""Reusable widgets: volume overlay, mapping rows, group panel and list."""
from PySide6.QtCore import QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QVBoxLayout, QWidget,
)

from .constants import ASSIGN_COLORS, DEFAULT_ASSIGN_COLOR
from .midi_utils import format_key
from .theme import BORDER, MUTED, RED, dot_icon, volume_color


class NoScrollComboBox(QComboBox):
    """Combo box that ignores the mouse wheel, so scrolling a list never changes a selection by accident."""

    def __init__(self, *args):
        super().__init__(*args)
        self.setFocusPolicy(Qt.StrongFocus)  # the wheel must not grab the focus either

    def wheelEvent(self, event):
        event.ignore()  # let the enclosing scroll area handle it


class VolumeOverlay(QWidget):
    """Frameless rounded volume card shown at the bottom-center of the screen."""

    W, H = 340, 84

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.resize(self.W, self.H)
        self._name = ""
        self._val = 0.0
        self._muted = False
        self._text = None  # notice text (no volume bar) when set

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._fade_out)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(300)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.hide)

    def show_volume(self, name: str, val: float, muted: bool = False, hold_ms: int = 1400):
        self._name, self._val, self._muted = name, max(0.0, min(1.0, val)), muted
        self._text = None
        self._present(hold_ms)

    def show_notice(self, title: str, text: str, hold_ms: int = 1800):
        """Text-only card, e.g. "Groupe 1" / "→ Spotify"."""
        self._name, self._text = title, text
        self._present(hold_ms)

    def _present(self, hold_ms: int):
        self._fade.stop()
        self.setWindowOpacity(1.0)
        if not self.isVisible():
            geo = QGuiApplication.primaryScreen().availableGeometry()
            self.move(geo.x() + (geo.width() - self.W) // 2, geo.y() + geo.height() - self.H - 60)
            self.show()
        self.update()
        self._hold.start(hold_ms)

    def _fade_out(self):
        self._fade.start()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        card = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(card, 16, 16)
        p.fillPath(path, QColor(30, 31, 36, 238))
        p.setPen(QPen(QColor(BORDER), 1.5))
        p.drawPath(path)

        color = QColor(RED if self._muted else volume_color(self._val))

        p.setPen(QColor(MUTED))
        font = QFont(self.font())
        font.setPointSize(11)
        font.setBold(True)
        p.setFont(font)
        name = QFontMetrics(font).elidedText(self._name, Qt.ElideRight, self.W - 130)
        p.drawText(QRectF(20, 12, self.W - 130, 26), Qt.AlignLeft | Qt.AlignVCenter, name)

        if self._text is not None:
            font.setPointSize(16)
            p.setFont(font)
            p.setPen(QColor("#e6e8ee"))
            text = QFontMetrics(font).elidedText(self._text, Qt.ElideRight, self.W - 40)
            p.drawText(QRectF(20, 38, self.W - 40, 34), Qt.AlignLeft | Qt.AlignVCenter, text)
            return

        font.setPointSize(20)
        p.setFont(font)
        p.setPen(color)
        p.drawText(QRectF(self.W - 110, 8, 90, 34), Qt.AlignRight | Qt.AlignVCenter,
                   "MUET" if self._muted else f"{round(self._val * 100)}%")

        track = QRectF(20, self.H - 30, self.W - 40, 10)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(BORDER))
        p.drawRoundedRect(track, 5, 5)
        if self._val > 0:
            fill = QRectF(track.x(), track.y(), max(10.0, track.width() * self._val), track.height())
            p.setBrush(QColor("#6b6f7b") if self._muted else color)
            p.drawRoundedRect(fill, 5, 5)


class MappingRow(QWidget):
    changed = Signal()
    learnRequested = Signal(object)
    removeRequested = Signal(object)

    def __init__(self, cc_text: str, app: str, apps):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)

        self.cc = QLineEdit(cc_text)
        self.cc.setFixedWidth(150)
        self.cc.setPlaceholderText("ex: 14 ou CC 7 (ch 1)")
        self.cc.editingFinished.connect(self.changed)

        self.app = NoScrollComboBox()
        self.app.setEditable(True)
        self.app.setInsertPolicy(QComboBox.NoInsert)
        self.app.addItems(apps)
        self.app.setCurrentText(app)
        self.app.activated.connect(lambda _i: self.changed.emit())
        self.app.lineEdit().editingFinished.connect(self.changed)

        self.learn_btn = QPushButton("Learn")
        self.learn_btn.setObjectName("learn")
        self.learn_btn.setFixedWidth(110)
        self.learn_btn.clicked.connect(lambda: self.learnRequested.emit(self))
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("danger")
        self.remove_btn.clicked.connect(lambda: self.removeRequested.emit(self))

        lay.addWidget(self.cc)
        lay.addWidget(self.app, 1)
        lay.addWidget(self.learn_btn)
        lay.addWidget(self.remove_btn)

    def values(self):
        return self.cc.text().strip(), self.app.currentText().strip()

    def set_cc_text(self, text: str):
        self.cc.setText(text)

    def set_apps(self, apps):
        current = self.app.currentText()
        self.app.blockSignals(True)
        self.app.clear()
        self.app.addItems(apps)
        self.app.setCurrentText(current)
        self.app.blockSignals(False)

    def set_learning(self, learning: bool):
        self.learn_btn.setText("En attente..." if learning else "Learn")
        self.learn_btn.setProperty("learning", learning)
        self.learn_btn.style().unpolish(self.learn_btn)
        self.learn_btn.style().polish(self.learn_btn)


class LearnField(QWidget):
    """Editable MIDI number field + Learn button."""

    changed = Signal()
    learnRequested = Signal()

    def __init__(self, text: str, placeholder: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.editingFinished.connect(self.changed)
        self.btn = QPushButton("Learn")
        self.btn.setObjectName("learn")
        self.btn.setFixedWidth(110)
        self.btn.clicked.connect(self.learnRequested)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.btn)

    def text(self) -> str:
        return self.edit.text().strip()

    def set_text(self, text: str):
        self.edit.setText(text)

    def set_learning(self, learning: bool):
        self.btn.setText("En attente..." if learning else "Learn")
        self.btn.setProperty("learning", learning)
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)


class GroupRow(QFrame):
    """Detail panel of one group: ONE target app and its fader / mute / assign controls."""

    FIELDS = (("volume", "Fader (CC)", "CC", "control_change"),
              ("mute", "Mute (note)", "Note", "note"),
              ("assign", "Assign (note)", "Note", "note"))

    changed = Signal()
    learnRequested = Signal(object, str)   # (row, field)
    removeRequested = Signal(object)

    def __init__(self, group: dict, apps):
        super().__init__()
        self.setObjectName("card")
        self.uid = group["uid"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        def label(text):
            lbl = QLabel(text)
            lbl.setMinimumWidth(120)
            return lbl

        self.name = QLineEdit(group.get("name", ""))
        self.name.setPlaceholderText("Nom du groupe")
        self.name.editingFinished.connect(self.changed)

        self.app = NoScrollComboBox()
        self.app.setEditable(True)
        self.app.setInsertPolicy(QComboBox.NoInsert)
        self.app.addItems([""] + list(apps))
        self.app.setCurrentText(group.get("app", ""))
        self.app.lineEdit().setPlaceholderText("Choisir, ou appuyer sur Assign pour l'application au premier plan")
        self.app.activated.connect(lambda _i: self.changed.emit())
        self.app.lineEdit().editingFinished.connect(self.changed)

        self.color = NoScrollComboBox()
        self.color.setToolTip("Couleur de la LED d'Assign quand une application est assignée")
        self.color.setMinimumWidth(170)
        for name, (_vel, hex_color) in ASSIGN_COLORS.items():
            self.color.addItem(dot_icon(hex_color), name)
        self.color.setCurrentText(group.get("color", DEFAULT_ASSIGN_COLOR))
        self.color.activated.connect(lambda _i: self.changed.emit())

        grid.addWidget(label("Nom"), 0, 0)
        grid.addWidget(self.name, 0, 1)
        grid.addWidget(label("Application"), 1, 0)
        grid.addWidget(self.app, 1, 1)
        grid.addWidget(label("Couleur de la LED"), 2, 0)
        grid.addWidget(self.color, 2, 1, Qt.AlignLeft)

        section = QLabel("Boutons MIDI")
        section.setObjectName("section")
        grid.addWidget(section, 3, 0, 1, 2)

        self.fields = {}
        for i, (field, text, kind, _msg) in enumerate(self.FIELDS):
            f = LearnField(format_key(group.get(field, ""), kind), "non assigné")
            f.changed.connect(self.changed)
            f.learnRequested.connect(lambda fld=field: self.learnRequested.emit(self, fld))
            self.fields[field] = f
            grid.addWidget(label(text), 4 + i, 0)
            grid.addWidget(f, 4 + i, 1)
        outer.addLayout(grid)
        outer.addStretch(1)

        self.remove_btn = QPushButton("Supprimer ce groupe")
        self.remove_btn.setObjectName("danger")
        self.remove_btn.clicked.connect(lambda: self.removeRequested.emit(self))
        outer.addWidget(self.remove_btn, 0, Qt.AlignLeft)

    def kind_for(self, field: str):
        for name, _label, label, msg in self.FIELDS:
            if name == field:
                return label, msg

    def set_app(self, app: str):
        self.app.setCurrentText(app)

    def set_apps(self, apps):
        current = self.app.currentText()
        self.app.blockSignals(True)
        self.app.clear()
        self.app.addItems([""] + list(apps))
        self.app.setCurrentText(current)
        self.app.blockSignals(False)

    def set_learning(self, field: str, learning: bool):
        self.fields[field].set_learning(learning)

    def clear_learning(self):
        for f in self.fields.values():
            f.set_learning(False)


class GroupList(QListWidget):
    """Group list whose entries can be dragged up and down; `reordered` fires after a drop."""

    reordered = Signal()

    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.reordered.emit()
