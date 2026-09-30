import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid

import mido
import pulsectl
from PySide6.QtCore import QObject, QPropertyAnimation, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

try:
    from Xlib import X, display as xdisplay
except ImportError:  # focus detection (Assign button) is then unavailable
    X = xdisplay = None

APP_NAME = "MIDI Mixer"
SINGLETON_NAME = "midi-mixer-singleton"
NO_PORT_LABEL = "Aucun port détecté"
DEFAULT_PORT_HINT = "apc40"  # profil par défaut : premier port MIDI dont le nom contient "APC40"
# Profil APC40 par défaut : le fader Master de l'APC40 envoie le CC 14 (canal 1)
DEFAULT_MAPPINGS = {"14": "MASTER"}
LEARN_TIMEOUT_S = 8
# Contrôles APC40 par défaut d'un groupe (colonne i, canal MIDI i+1) :
# fader de piste (CC 7), bouton S / Solo-Cue (note 49) = mute, pad du haut de la grille de clips (note 0) = assign.
# Les boutons S / ● sont en mode bascule : un appui = UN message, en alternant note_on / note_off.
DEFAULT_GROUP_COUNT = 8
GROUP_DEFAULT_CC = 7
GROUP_DEFAULT_MUTE_NOTE = 49
GROUP_DEFAULT_ASSIGN_NOTE = 0
# Couleur de la LED d'Assign : nom -> (vélocité de la palette RGB de l'APC40 mkII, couleur d'aperçu dans l'interface)
ASSIGN_COLORS = {
    "Vert": (21, "#2ecc71"),
    "Rouge": (5, "#e74c3c"),
    "Orange": (9, "#e67e22"),
    "Jaune": (13, "#f1c40f"),
    "Cyan": (37, "#22d3ee"),
    "Bleu": (45, "#3a7ebf"),
    "Violet": (53, "#9b59b6"),
    "Rose": (57, "#ff6fae"),
    "Blanc": (3, "#ecf0f1"),
}
DEFAULT_ASSIGN_COLOR = "Vert"
PORT_POLL_MS = 3000

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
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: white; border-bottom-color: {ACCENT}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
"""

logger = logging.getLogger("MidiMixer")


# --------------------------------------------------------------------- helpers

def parse_cc_field(text: str):
    """Parse a CC field such as "56", "CC 56 (ch 1)" or "ch1:56".

    Returns (channel, control) with channel in 1..16 or None (any channel),
    or None if the text is not valid.
    """
    text = (text or "").strip()
    channel = None
    m = re.search(r"ch(?:annel)?\s*(\d{1,2})", text, re.IGNORECASE)
    if m:
        channel = int(m.group(1))
        if not 1 <= channel <= 16:
            return None
        text = text[:m.start()] + " " + text[m.end():]
    m = re.search(r"\d{1,3}", text)
    if not m:
        return None
    control = int(m.group(0))
    if not 0 <= control <= 127:
        return None
    return channel, control


def mapping_key(channel, control) -> str:
    """Config key: "56" (any channel) or "1:56" (channel 1 only)."""
    return f"{channel}:{control}" if channel else str(control)


def format_key(key: str, label: str = "CC") -> str:
    """Human-readable label for a config key ("Note" for buttons)."""
    if ":" in key:
        ch, num = key.split(":", 1)
        return f"{label} {num} (ch {ch})"
    return key


def msg_kind(msg):
    """"control_change", "note" (note_on or note_off) or None for anything else."""
    if msg.type == "control_change":
        return "control_change"
    if msg.type in ("note_on", "note_off"):
        return "note"
    return None


def midi_number(msg):
    """CC number or note number of `msg`, None if it is neither.

    Buttons are toggles that alternate note_on / note_off on successive presses,
    so BOTH count as one press (velocity is ignored).
    """
    kind = msg_kind(msg)
    if kind == "control_change":
        return msg.control
    return msg.note if kind == "note" else None


def key_channel_number(key: str):
    """(0-based channel, number) of a channel-specific key like "1:49", else None."""
    if ":" not in key:
        return None
    ch, num = key.split(":", 1)
    return int(ch) - 1, int(num)


def key_matches(key: str, channel: int, number: int) -> bool:
    """True if config `key` ("56" or "1:56") matches a message (channel is 0-based)."""
    return bool(key) and key in (f"{channel + 1}:{number}", str(number))


def default_group(index: int) -> dict:
    """Group on APC40 column `index` (0-7): its fader, mute and assign buttons."""
    ch = index + 1
    return {
        "uid": uuid.uuid4().hex[:8],
        "name": f"Groupe {ch}",
        "app": "",
        "volume": mapping_key(ch, GROUP_DEFAULT_CC),
        "mute": mapping_key(ch, GROUP_DEFAULT_MUTE_NOTE),
        "assign": mapping_key(ch, GROUP_DEFAULT_ASSIGN_NOTE),
        "color": DEFAULT_ASSIGN_COLOR,
    }


def volume_color(val: float) -> str:
    if val < 0.70:
        return GREEN
    return YELLOW if val < 0.90 else RED


def input_port_names():
    try:
        return mido.get_input_names()
    except Exception as e:
        logger.error("Impossible de lister les ports MIDI : %s", e)
        return []


def display_name(si) -> str:
    props = getattr(si, "proplist", {}) or {}
    for key in ("application.name", "application.process.binary", "media.name"):
        v = props.get(key)
        if v:
            return v
    return f"sink_input_{getattr(si, 'index', None) or 'unknown'}"


def list_running_apps(pulse):
    apps = set()
    for si in pulse.sink_input_list():
        props = getattr(si, "proplist", {}) or {}
        name = display_name(si)
        binary = props.get("application.process.binary")
        pid = props.get("application.process.id") or props.get("application.pid")
        suffix = []
        if binary:
            suffix.append(binary)
        if pid:
            suffix.append(f"pid:{pid}")
        apps.add(f"{name} ({', '.join(suffix)})" if suffix else name)
    return ["MASTER"] + sorted(apps)


def target_base_name(target: str) -> str:
    """Strip the " (binary, pid:123)" suffix added by list_running_apps."""
    m = re.match(r"^(.*?)\s*\([^()]*\)\s*$", target or "")
    return (m.group(1) if m else target or "").strip().lower()


def si_matches_target(si, target: str) -> bool:
    """True if sink input `si` belongs to the app named by `target`.

    Exact (case-insensitive) match on the app name, the process binary or the
    media name, so all streams of an app match (e.g. several Firefox tabs) but
    unrelated apps never do. The pid in a saved label is ignored on purpose: it
    changes every time the app restarts.
    """
    props = getattr(si, "proplist", {}) or {}
    names = {
        (props.get("application.name") or "").lower(),
        (props.get("application.process.binary") or "").lower(),
        (props.get("media.name") or "").lower(),
    }
    names.discard("")
    base = target_base_name(target)
    return bool(base) and (base in names or (target or "").strip().lower() in names)


def set_mute(pulse, target: str, muted: bool):
    """Mute/unmute MASTER or every stream of `target`. Returns (name, volume) or None if nothing matched."""
    if target == "MASTER":
        sink = pulse.get_sink_by_name(pulse.server_info().default_sink_name)
        pulse.mute(sink, muted)
        return "MASTER", pulse.volume_get_all_chans(sink)
    matched = [si for si in pulse.sink_input_list() if si_matches_target(si, target)]
    for si in matched:
        pulse.mute(si, muted)
    if not matched:
        return None
    return display_name(matched[0]), pulse.volume_get_all_chans(matched[0])


# ----------------------------------------------------------------- focus lookup

def _read_ppid(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _ancestors(pid: int):
    """pid and all its ancestors (browsers play audio from child processes)."""
    chain = []
    while pid and pid > 1 and pid not in chain:
        chain.append(pid)
        pid = _read_ppid(pid)
    return chain


def active_window_info():
    """(pid, wm_class tuple) of the focused X11 window, or None (Wayland / no Xlib / no focus)."""
    if xdisplay is None:
        return None
    try:
        d = xdisplay.Display()
        try:
            root = d.screen().root
            prop = root.get_full_property(d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
            if not prop or not prop.value[0]:
                return None
            win = d.create_resource_object("window", prop.value[0])
            pid_prop = win.get_full_property(d.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
            pid = int(pid_prop.value[0]) if pid_prop else None
            return pid, win.get_wm_class() or ()
        finally:
            d.close()
    except Exception as e:
        logger.error("Lecture de la fenêtre active impossible : %s", e)
        return None


def focused_app_name(pulse):
    """Name of the app owning the focused window, in a form `si_matches_target` understands.

    Preference order: an audio stream whose process descends from the window's
    process, then a stream matching the window's process/class name, then the
    bare class name (works later, once the app plays something).
    """
    info = active_window_info()
    if info is None:
        return None
    pid, wm_class = info
    if pid == os.getpid():
        return None  # the mixer itself has the focus

    streams = pulse.sink_input_list()
    if pid:
        for si in streams:
            props = getattr(si, "proplist", {}) or {}
            try:
                si_pid = int(props.get("application.process.id") or 0)
            except ValueError:
                si_pid = 0
            if si_pid and pid in _ancestors(si_pid):
                return props.get("application.name") or display_name(si)

    candidates = {c for c in wm_class if c}
    try:
        with open(f"/proc/{pid}/comm") as f:
            candidates.add(f.read().strip())
    except (OSError, TypeError):
        pass
    for si in streams:
        if any(si_matches_target(si, c) for c in candidates):
            return display_name(si)
    return wm_class[1] if len(wm_class) > 1 else (next(iter(candidates), None))


# ---------------------------------------------------------------------- config

def config_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), ".config", "midi-mixer")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "midi_config.json")


def load_config() -> dict:
    """Load {'mappings': {...}, '_port': 'name', 'autostart': bool}; also accepts the legacy flat format."""
    cfg = {"mappings": dict(DEFAULT_MAPPINGS), "port": None, "autostart": True,
           "groups": [default_group(i) for i in range(DEFAULT_GROUP_COUNT)]}
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if "mappings" in data:
                    cfg["mappings"] = data.get("mappings", {})
                    cfg["port"] = data.get("_port")
                    cfg["autostart"] = bool(data.get("autostart", True))
                    if isinstance(data.get("groups"), list):
                        cfg["groups"] = data["groups"]
                        for g in cfg["groups"]:
                            # earlier default assign buttons (Track Select 51, ● Record arm 48) -> top clip pad
                            m = re.fullmatch(r"(\d+):(?:51|48)", g.get("assign", ""))
                            if m:
                                g["assign"] = f"{m.group(1)}:{GROUP_DEFAULT_ASSIGN_NOTE}"
                            if g.get("color") not in ASSIGN_COLORS:
                                g["color"] = DEFAULT_ASSIGN_COLOR
                else:
                    cfg["mappings"] = data  # legacy: the whole file is the mappings
        except Exception as e:
            logger.exception("Failed to load config: %s", e)
    return cfg


def save_config(cfg: dict):
    data = {"mappings": cfg["mappings"], "_port": cfg["port"], "autostart": cfg["autostart"],
            "groups": cfg["groups"]}
    path = config_path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ------------------------------------------------------------------- autostart

def autostart_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "autostart", "midi-mixer.desktop")


def is_autostart_enabled() -> bool:
    return os.path.exists(autostart_file())


def set_autostart(enabled: bool):
    """Create or remove the XDG autostart entry (works on KDE, GNOME, XFCE...)."""
    path = autostart_file()
    if not enabled:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    script = os.path.abspath(__file__)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Mixeur audio piloté par un contrôleur MIDI\n"
        f'Exec="{sys.executable}" "{script}" --minimized\n'
        f"Path={os.path.dirname(script)}\n"
        "Icon=audio-volume-high\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-KDE-autostart-after=panel\n"
    )
    with open(path, "w") as f:
        f.write(content)


# ----------------------------------------------------------------------- icons

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


# ---------------------------------------------------------------------- engine

class MixerEngine(QObject):
    """MIDI listener + PulseAudio volume control. Owns the worker/learn threads.

    Signals are emitted from worker threads; Qt queues them to receivers living
    in the UI thread. Every thread uses its own Pulse connection (pulsectl is
    not thread-safe).
    """

    volumeChanged = Signal(str, float, bool)  # name, volume 0..1, muted
    assignRequested = Signal(str)             # group uid: Assign button pressed
    stateChanged = Signal(bool)
    learned = Signal(object, int, int)  # token, channel (1-16), control/note number
    learnEnded = Signal(object)         # token: timeout, error or cancel
    errorOccurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.mappings = {}
        self.groups = []      # list of group dicts (see default_group)
        self.muted = set()    # uids of muted groups
        self.running = False
        self._thread = None
        self._learn_lock = threading.Lock()
        self._learn_token = None
        self._learn_deadline = 0.0
        self._learn_kinds = ("control_change",)
        self._out = None              # MIDI output to light the Mute buttons
        self._out_lock = threading.Lock()

    def set_groups(self, groups):
        """Replace the groups; LEDs of buttons that are no longer used are switched off."""
        new = {g["uid"]: g for g in groups}
        for old in self.groups:
            g = new.get(old["uid"])
            for field in ("mute", "assign"):
                if old.get(field) and (g is None or g.get(field) != old[field]):
                    self._send_led(old[field], False)
        self.groups = groups
        self._sync_leds()

    def clear_mute(self, uid: str):
        """Forget a group's muted state (its app was re-targeted or the group removed) and unlight its LED."""
        self.muted.discard(uid)
        for g in self.groups:
            if g["uid"] == uid:
                self._send_led(g.get("mute", ""), False)

    # -- mute button LEDs ----------------------------------------------------

    def _open_output(self, port: str):
        """Open the MIDI output matching input `port` (same device), or None."""
        try:
            names = mido.get_output_names()
            name = port if port in names else next((n for n in names if DEFAULT_PORT_HINT in n.lower()), None)
            return mido.open_output(name) if name else None
        except Exception as e:
            logger.error("Sortie MIDI indisponible (LED désactivées) : %s", e)
            return None

    def _send_led(self, key: str, on: bool, velocity: int = 127):
        target = key_channel_number(key) if key else None
        if target is None or self._out is None:
            return
        try:
            with self._out_lock:
                if self._out is not None:
                    self._out.send(mido.Message("note_on", channel=target[0], note=target[1],
                                                velocity=velocity if on else 0))
        except Exception as e:
            logger.error("Envoi LED impossible : %s", e)

    def _sync_leds(self):
        """Mute LED lit = group muted; Assign LED lit (in the group's color) = an app is assigned."""
        for g in self.groups:
            self._send_led(g.get("mute", ""), g["uid"] in self.muted)
            velocity = ASSIGN_COLORS.get(g.get("color"), ASSIGN_COLORS[DEFAULT_ASSIGN_COLOR])[0]
            self._send_led(g.get("assign", ""), bool(g.get("app")), velocity)

    # -- mixer -------------------------------------------------------------

    def start(self, port: str):
        old = self._thread
        if old is not None and old.is_alive():
            self.running = False
            old.join(timeout=1)
        self.running = True
        self._thread = threading.Thread(target=self._worker, args=(port,), daemon=True)
        self._thread.start()
        self.stateChanged.emit(True)

    def stop(self):
        if self.running:
            self.running = False
            self.stateChanged.emit(False)

    def _lookup_target(self, channel: int, control: int):
        """App for a CC: direct mappings first, then the groups' faders.

        Within the direct mappings a channel-specific key ("1:56") wins over
        the any-channel one ("56").
        """
        mappings = self.mappings
        target = mappings.get(mapping_key(channel + 1, control)) or mappings.get(str(control))
        if target:
            return target
        for g in self.groups:
            if g.get("app") and key_matches(g.get("volume", ""), channel, control):
                return g["app"]
        return None

    def _handle_note(self, pulse, channel: int, note: int):
        """A button was pressed: toggle a group's mute or ask the UI to assign the focused app."""
        for g in self.groups:
            if key_matches(g.get("assign", ""), channel, note):
                self.assignRequested.emit(g["uid"])
                return
            if g.get("app") and key_matches(g.get("mute", ""), channel, note):
                muted = g["uid"] not in self.muted
                try:
                    result = set_mute(pulse, g["app"], muted)
                except Exception as e:
                    logger.exception("Failed to mute: %s", e)
                    return
                if result is None:
                    return  # the app is not playing anything right now
                if muted:
                    self.muted.add(g["uid"])
                else:
                    self.muted.discard(g["uid"])
                self._send_led(g["mute"], muted)  # lit = muted
                self.volumeChanged.emit(result[0], result[1], muted)
                return

    def _worker(self, port):
        me = threading.current_thread()
        try:
            with pulsectl.Pulse("midi-mixer-worker") as pulse, mido.open_input(port) as inport:
                self._out = self._open_output(port)
                self._sync_leds()
                while self.running and self._thread is me:
                    # Only the last value per control is applied, so a burst of
                    # messages from a fader does not flood PulseAudio.
                    latest = {}
                    for msg in inport.iter_pending():
                        number = midi_number(msg)
                        if number is None:
                            continue
                        if self._learn_step(msg, number):
                            continue
                        if msg.type == "control_change":
                            latest[(msg.channel, number)] = msg.value
                        else:
                            self._handle_note(pulse, msg.channel, number)
                    self._learn_step()  # handles the learn timeout

                    for (channel, control), value in latest.items():
                        target = self._lookup_target(channel, control)
                        if target:
                            self._apply_volume(pulse, target, value / 127.0)
                    time.sleep(0.01)
        except Exception as e:
            logger.exception("MIDI worker error: %s", e)
            self.errorOccurred.emit(f"Erreur d'écoute MIDI : {e}")
        finally:
            with self._out_lock:
                out, self._out = self._out, None
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            # Died on its own (device unplugged, Pulse gone...) rather than being stopped.
            if self.running and self._thread is me:
                self.running = False
                self.stateChanged.emit(False)

    def _apply_volume(self, pulse, target: str, val: float):
        try:
            if target == "MASTER":
                sink = pulse.get_sink_by_name(pulse.server_info().default_sink_name)
                pulse.volume_set_all_chans(sink, val)
                self.volumeChanged.emit("MASTER", val, "MASTER" in self._muted_targets())
                return
            matched = [si for si in pulse.sink_input_list() if si_matches_target(si, target)]
            for si in matched:
                pulse.volume_set_all_chans(si, val)
            if matched:
                self.volumeChanged.emit(display_name(matched[0]), val, target in self._muted_targets())
        except Exception as e:
            logger.exception("Failed to set volume: %s", e)

    def _muted_targets(self):
        return {g.get("app") for g in self.groups if g["uid"] in self.muted}

    # -- learn -------------------------------------------------------------

    @property
    def learning_token(self):
        with self._learn_lock:
            return self._learn_token

    def start_learning(self, token, port: str, kinds=("control_change",)):
        """Capture the next message of one of `kinds` ("control_change" for faders, "note" for buttons)."""
        with self._learn_lock:
            self._learn_kinds = kinds
            self._learn_token = token
            self._learn_deadline = time.time() + LEARN_TIMEOUT_S
        # When the mixer runs its worker already owns the port and catches messages.
        if not self.running:
            threading.Thread(target=self._learn_listen, args=(port, token), daemon=True).start()

    def cancel_learning(self):
        with self._learn_lock:
            token, self._learn_token = self._learn_token, None
        return token

    def _learn_step(self, msg=None, number=None) -> bool:
        """Handle the learn timeout / capture a message. True if `msg` was consumed."""
        with self._learn_lock:
            token = self._learn_token
            if token is None:
                return False
            if msg is not None and msg_kind(msg) in self._learn_kinds:
                self._learn_token = None
                self.learned.emit(token, msg.channel + 1, number)
                return True
            if time.time() > self._learn_deadline:
                self._learn_token = None
                self.learnEnded.emit(token)
            return False

    def _learn_listen(self, port, token):
        try:
            with mido.open_input(port) as inport:
                while self.learning_token is token and not self.running:
                    for msg in inport.iter_pending():
                        number = midi_number(msg)
                        if number is not None and self._learn_step(msg, number):
                            return
                    self._learn_step()
                    time.sleep(0.01)
        except Exception as e:
            logger.exception("Learn listener error: %s", e)
            with self._learn_lock:
                if self._learn_token is token:
                    self._learn_token = None
            self.learnEnded.emit(token)
            self.errorOccurred.emit(f"Erreur pendant l'apprentissage : {e}")


# --------------------------------------------------------------------- overlay

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


# ------------------------------------------------------------------ mapping row

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

        self.app = QComboBox()
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


# ------------------------------------------------------------------ group row

class LearnField(QWidget):
    """Label + editable MIDI number field + Learn button."""

    changed = Signal()
    learnRequested = Signal()

    def __init__(self, label: str, text: str, placeholder: str):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumWidth(120)
        self.edit.editingFinished.connect(self.changed)
        self.btn = QPushButton("Learn")
        self.btn.setObjectName("learn")
        self.btn.setFixedWidth(96)
        self.btn.clicked.connect(self.learnRequested)
        lay.addWidget(QLabel(label))
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
    """One mixer strip: a name, ONE target app, and its fader / mute / assign controls."""

    FIELDS = (("volume", "Fader", "CC", "control_change"),
              ("mute", "Mute", "Note", "note"),
              ("assign", "Assign", "Note", "note"))

    changed = Signal()
    learnRequested = Signal(object, str)   # (row, field)
    removeRequested = Signal(object)

    def __init__(self, group: dict, apps):
        super().__init__()
        self.setObjectName("card")
        self.uid = group["uid"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)

        head = QHBoxLayout()
        self.name = QLineEdit(group.get("name", ""))
        self.name.setFixedWidth(130)
        self.name.setPlaceholderText("Nom du groupe")
        self.name.editingFinished.connect(self.changed)
        self.app = QComboBox()
        self.app.setEditable(True)
        self.app.setInsertPolicy(QComboBox.NoInsert)
        self.app.addItems([""] + list(apps))
        self.app.setCurrentText(group.get("app", ""))
        self.app.lineEdit().setPlaceholderText("Application (ou bouton Assign)")
        self.app.activated.connect(lambda _i: self.changed.emit())
        self.app.lineEdit().editingFinished.connect(self.changed)
        self.color = QComboBox()
        self.color.setToolTip("Couleur de la LED d'Assign quand une application est assignée")
        for name, (_vel, hex_color) in ASSIGN_COLORS.items():
            swatch = QPixmap(14, 14)
            swatch.fill(QColor(hex_color))
            self.color.addItem(QIcon(swatch), name)
        self.color.setCurrentText(group.get("color", DEFAULT_ASSIGN_COLOR))
        self.color.activated.connect(lambda _i: self.changed.emit())
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("danger")
        self.remove_btn.clicked.connect(lambda: self.removeRequested.emit(self))
        head.addWidget(self.name)
        head.addWidget(self.app, 1)
        head.addWidget(QLabel("LED"))
        head.addWidget(self.color)
        head.addWidget(self.remove_btn)
        outer.addLayout(head)

        controls = QHBoxLayout()
        self.fields = {}
        for field, label, kind, _msg in self.FIELDS:
            f = LearnField(label, format_key(group.get(field, ""), kind), "non assigné")
            f.changed.connect(self.changed)
            f.learnRequested.connect(lambda fld=field: self.learnRequested.emit(self, fld))
            self.fields[field] = f
            controls.addWidget(f, 1)
        outer.addLayout(controls)

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


# ------------------------------------------------------------------ main window

class MainWindow(QMainWindow):
    def __init__(self, start_hidden: bool):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - APC40")
        self.resize(1080, 680)
        self.icon = make_app_icon()
        self.setWindowIcon(self.icon)

        self.cfg = load_config()
        self._pulse = None
        self._ports = None
        self._apps = ["MASTER"]
        self._apps_error = False
        self._user_stopped = False
        self._quitting = False
        self._tray_hint_shown = False
        self._last_error = None
        self.rows = []
        self.group_rows = []
        self.single_server = None

        self.engine = MixerEngine()
        self.engine.stateChanged.connect(self._on_state_changed)
        self.engine.volumeChanged.connect(self._on_volume)
        self.engine.assignRequested.connect(self._on_assign_requested)
        self.engine.learned.connect(self._on_learned)
        self.engine.learnEnded.connect(self._on_learn_ended)
        self.engine.errorOccurred.connect(self._on_error)

        self.overlay = VolumeOverlay()

        self._build_ui()
        self._build_tray()

        # Keep the autostart entry in line with the saved preference (on by default).
        if self.cfg["autostart"] != is_autostart_enabled():
            try:
                set_autostart(self.cfg["autostart"])
            except OSError as e:
                logger.error("Autostart: %s", e)
        self.act_autostart.setChecked(is_autostart_enabled())

        self._refresh_apps()
        for key, app in self.cfg["mappings"].items():
            self._add_row(format_key(key), app)
        self.engine.mappings = dict(self.cfg["mappings"])
        for group in self.cfg["groups"]:
            self._add_group_row(group)
        self.engine.set_groups([dict(g) for g in self.cfg["groups"]])

        self._refresh_ports()
        self.port_timer = QTimer(self)
        self.port_timer.timeout.connect(self._poll)
        self.port_timer.start(PORT_POLL_MS)
        self._maybe_autostart()

        if not (start_hidden and self.tray.isVisible()):
            self.show()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        top = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.currentIndexChanged.connect(self._on_port_changed)
        self.btn_refresh = QPushButton("Rafraîchir les applications")
        self.btn_refresh.clicked.connect(self._refresh_apps)
        top.addWidget(self.port_combo, 1)
        top.addWidget(self.btn_refresh)
        root.addLayout(top)

        tabs = QTabWidget()
        self.group_layout = self._scroll_tab(
            tabs, "Groupes", "+ Ajouter un groupe",
            lambda: self._add_group_row(self._new_group(), user=True),
            "Chaque groupe pilote UNE application avec son fader, son bouton Mute et son bouton Assign "
            "(qui lui attribue l'application au premier plan).")
        self.rows_layout = self._scroll_tab(
            tabs, "Assignations directes", "+ Ajouter une assignation",
            lambda: self._add_row("", "MASTER"),
            "CC MIDI   |   Application cible   |   Apprentissage")
        root.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("Mixer arrêté")
        self.status.setObjectName("status")
        self.btn_run = QPushButton("Démarrer le Mixer")
        self.btn_run.setObjectName("run")
        self.btn_run.setProperty("running", False)
        self.btn_run.clicked.connect(self._toggle_mixer)
        bottom.addStretch(1)
        bottom.addWidget(self.status)
        bottom.addWidget(self.btn_run)
        root.addLayout(bottom)

    def _scroll_tab(self, tabs, title, add_text, add_callback, hint):
        """Add a tab with a hint, a scrollable list and an add button; return the list layout."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(8)
        label = QLabel(hint)
        label.setObjectName("header")
        label.setWordWrap(True)
        lay.addWidget(label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        rows_layout = QVBoxLayout(holder)
        rows_layout.setContentsMargins(4, 4, 4, 4)
        rows_layout.setSpacing(8)
        rows_layout.addStretch(1)
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)
        btn = QPushButton(add_text)
        btn.clicked.connect(add_callback)
        lay.addWidget(btn, 0, Qt.AlignLeft)
        tabs.addTab(page, title)
        return rows_layout

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.icon, self)
        menu = QMenu()
        act_open = QAction("Ouvrir", menu)
        act_open.triggered.connect(self._show_window)
        self.act_toggle = QAction("Démarrer le mixer", menu)
        self.act_toggle.triggered.connect(self._toggle_mixer)
        self.act_autostart = QAction("Lancer au démarrage de la session", menu)
        self.act_autostart.setCheckable(True)
        self.act_autostart.toggled.connect(self._on_autostart_toggled)
        act_quit = QAction("Quitter", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_open)
        menu.addAction(self.act_toggle)
        menu.addSeparator()
        menu.addAction(self.act_autostart)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray_menu = menu  # keep a reference alive
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip(f"{APP_NAME} — arrêté")
        self.tray.show()

    # -- window / tray -----------------------------------------------------

    @Slot()
    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # left click
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_window()

    @Slot()
    def _on_new_instance(self):
        """A second launch tried to start: bring this window up instead."""
        while self.single_server is not None and self.single_server.hasPendingConnections():
            self.single_server.nextPendingConnection().close()
        self._show_window()

    def closeEvent(self, event):
        if not self._quitting and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Le mixer continue de tourner dans la barre des tâches.",
                                      QSystemTrayIcon.Information, 3000)
        else:
            event.accept()
            self._quit()

    @Slot()
    def _quit(self):
        self._quitting = True
        # Pulse keeps the mute flag after we exit (notably on the MASTER sink): restore the sound.
        for g in list(self.engine.groups):
            if g["uid"] in self.engine.muted:
                self._unmute(g["uid"], g.get("app", ""))
        self.engine.stop()
        self._apply_live()
        self.tray.hide()
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                pass
        QApplication.quit()

    @Slot(bool)
    def _on_autostart_toggled(self, enabled: bool):
        try:
            set_autostart(enabled)
        except OSError as e:
            self._on_error(f"Impossible de modifier le lancement au démarrage : {e}")
            self.act_autostart.blockSignals(True)
            self.act_autostart.setChecked(is_autostart_enabled())
            self.act_autostart.blockSignals(False)
            return
        self.cfg["autostart"] = enabled
        save_config(self.cfg)

    # -- audio apps --------------------------------------------------------

    def _ui_pulse(self):
        if self._pulse is None:
            try:
                self._pulse = pulsectl.Pulse("midi-mixer-ui")
            except Exception as e:
                logger.error("PulseAudio indisponible : %s", e)
                return None
        return self._pulse

    @Slot()
    def _refresh_apps(self):
        """Re-read the running audio apps; the dropdowns are only touched when the list changed."""
        pulse = self._ui_pulse()
        if pulse is None:
            return
        try:
            apps = list_running_apps(pulse)
            self._apps_error = False
        except Exception as e:
            if not self._apps_error:  # log once, not every poll
                logger.error("Erreur de lecture audio : %s", e)
            self._apps_error = True
            try:
                pulse.close()
            except Exception:
                pass
            self._pulse = None
            return
        if apps == self._apps:
            return
        self._apps = apps
        for row in self.rows + self.group_rows:
            if not row.app.view().isVisible():  # don't pull the list from under an open dropdown
                row.set_apps(apps)

    # -- mapping rows ------------------------------------------------------

    def _add_row(self, cc_text: str, app: str):
        row = MappingRow(cc_text, app, self._apps)
        row.changed.connect(self._apply_live)
        row.learnRequested.connect(self._on_learn_requested)
        row.removeRequested.connect(self._on_remove_requested)
        self.rows.append(row)
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)

    def _collect_rows(self):
        """Return (mappings, invalid) built from the rows currently in the UI."""
        mappings, invalid = {}, []
        for i, row in enumerate(self.rows):
            cc, app = row.values()
            if not cc or not app:
                continue
            parsed = parse_cc_field(cc)
            if parsed is None:
                invalid.append((i + 1, cc))
                continue
            mappings[mapping_key(*parsed)] = app
        return mappings, invalid

    def _collect_groups(self):
        """Return (groups, invalid) built from the group rows currently in the UI."""
        groups, invalid = [], []
        for row in self.group_rows:
            group = {"uid": row.uid, "name": row.name.text().strip(), "app": row.app.currentText().strip(),
                     "color": row.color.currentText()}
            for field, label, _kind, _msg in GroupRow.FIELDS:
                text = row.fields[field].text()
                parsed = parse_cc_field(text) if text else None
                if text and parsed is None:
                    invalid.append((group["name"] or "Groupe", label, text))
                group[field] = mapping_key(*parsed) if parsed else ""
            groups.append(group)
        return groups, invalid

    def _unmute(self, uid: str, app: str):
        """Restore the sound of a group's app (its group was re-targeted or removed)."""
        self.engine.clear_mute(uid)
        pulse = self._ui_pulse()
        if pulse is not None and app:
            try:
                set_mute(pulse, app, False)
            except Exception as e:
                logger.error("Unmute impossible : %s", e)

    @Slot()
    def _apply_live(self):
        """Push the UI rows to the engine right away (mixer running or not) and save them.

        Invalid rows are ignored silently here; they are reported when the
        mixer is started.
        """
        mappings, _ = self._collect_rows()
        groups, _ = self._collect_groups()
        old_apps = {g["uid"]: g.get("app", "") for g in self.cfg["groups"]}
        for g in groups:
            if g["uid"] in self.engine.muted and old_apps.get(g["uid"], g["app"]) != g["app"]:
                self._unmute(g["uid"], old_apps[g["uid"]])
        self.engine.mappings = mappings
        self.engine.set_groups(groups)
        self.cfg["mappings"] = mappings
        self.cfg["groups"] = groups
        self.cfg["port"] = self.port_combo.currentData()
        try:
            save_config(self.cfg)
        except OSError as e:
            logger.error("Sauvegarde impossible : %s", e)

    @Slot(object)
    def _on_remove_requested(self, row):
        if row not in self.rows:
            return
        if self.engine.learning_token is row:
            self.engine.cancel_learning()
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._apply_live()

    # -- groups ------------------------------------------------------------

    def _add_group_row(self, group: dict, user: bool = False):
        row = GroupRow(group, self._apps)
        row.changed.connect(self._apply_live)
        row.learnRequested.connect(self._on_group_learn_requested)
        row.removeRequested.connect(self._on_group_remove_requested)
        self.group_rows.append(row)
        self.group_layout.insertWidget(self.group_layout.count() - 1, row)
        if user:
            self._apply_live()

    def _new_group(self) -> dict:
        """Next free APC40 column's default controls, or an unbound group if all 8 are taken."""
        used = {g["volume"] for g in self._collect_groups()[0]}
        for i in range(DEFAULT_GROUP_COUNT):
            group = default_group(i)
            if group["volume"] not in used:
                group["name"] = f"Groupe {len(self.group_rows) + 1}"
                return group
        return {"uid": uuid.uuid4().hex[:8], "name": f"Groupe {len(self.group_rows) + 1}",
                "app": "", "volume": "", "mute": "", "assign": "", "color": DEFAULT_ASSIGN_COLOR}

    @Slot(object)
    def _on_group_remove_requested(self, row):
        if row not in self.group_rows:
            return
        token = self.engine.learning_token
        if isinstance(token, tuple) and token[0] is row:
            self.engine.cancel_learning()
        if row.uid in self.engine.muted:
            self._unmute(row.uid, row.app.currentText().strip())
        self.group_rows.remove(row)
        self.group_layout.removeWidget(row)
        row.deleteLater()
        self._apply_live()

    @Slot(str)
    def _on_assign_requested(self, uid: str):
        """Assign button pressed on the controller: give the group the app that has the focus."""
        row = next((r for r in self.group_rows if r.uid == uid), None)
        if row is None:
            return
        title = row.name.text().strip() or "Groupe"
        pulse = self._ui_pulse()
        name = None
        if pulse is not None:
            try:
                name = focused_app_name(pulse)
            except Exception as e:
                logger.error("Détection de l'application au premier plan : %s", e)
        if not name:
            hint = "installe python-xlib" if xdisplay is None else "aucune application détectée"
            self.overlay.show_notice(title, f"Assignation impossible : {hint}")
            return
        # One app lives in one group only: take it away from any other group.
        base = target_base_name(name)
        for other in self.group_rows:
            if other is not row and target_base_name(other.app.currentText()) == base:
                other.set_app("")
        row.set_app(name)
        self._apply_live()
        self.overlay.show_notice(title, f"→ {name}")

    # -- learn -------------------------------------------------------------

    def _reset_learn_widget(self, token):
        """Restore the Learn button that `token` (a MappingRow or a (GroupRow, field) tuple) refers to."""
        if isinstance(token, tuple):
            row, field = token
            if row in self.group_rows:
                row.set_learning(field, False)
        elif token in self.rows:
            token.set_learning(False)

    def _begin_learn(self, token, kinds):
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "MIDI", "Aucun port MIDI sélectionné")
            return
        previous = self.engine.cancel_learning()
        if previous is not None:
            self._reset_learn_widget(previous)
        if isinstance(token, tuple):
            token[0].set_learning(token[1], True)
        else:
            token.set_learning(True)
        self.engine.start_learning(token, port, kinds)

    @Slot(object)
    def _on_learn_requested(self, row):
        self._begin_learn(row, ("control_change",))

    @Slot(object, str)
    def _on_group_learn_requested(self, row, field):
        _label, msg_type = row.kind_for(field)
        self._begin_learn((row, field), (msg_type,))

    @Slot(object, int, int)
    def _on_learned(self, token, channel, number):
        if isinstance(token, tuple):
            row, field = token
            if row not in self.group_rows:
                return
            label, _ = row.kind_for(field)
            row.fields[field].set_text(format_key(mapping_key(channel, number), label))
        else:
            if token not in self.rows:
                return
            token.set_cc_text(format_key(mapping_key(channel, number)))
        self._reset_learn_widget(token)
        self._apply_live()

    @Slot(object)
    def _on_learn_ended(self, token):
        self._reset_learn_widget(token)

    # -- ports / mixer state -----------------------------------------------

    @staticmethod
    def _default_port(ports, preferred=None):
        """Preferred/saved port if present, otherwise the APC40, otherwise the first port."""
        if preferred in ports:
            return preferred
        for p in ports:
            if DEFAULT_PORT_HINT in p.lower():
                return p
        return ports[0] if ports else None

    def _refresh_ports(self):
        ports = input_port_names()
        if ports == self._ports:
            return
        self._ports = ports
        current = self.port_combo.currentData() or self.cfg["port"]
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        if ports:
            for p in ports:
                self.port_combo.addItem(p, p)
            self.port_combo.setCurrentIndex(self.port_combo.findData(self._default_port(ports, current)))
        else:
            self.port_combo.addItem(NO_PORT_LABEL, None)
        self.port_combo.blockSignals(False)
        self._update_status()

    @Slot()
    def _poll(self):
        """Every few seconds: refresh the audio apps, watch for the controller being (un)plugged and autostart for the APC40."""
        self._refresh_ports()
        self._refresh_apps()
        self._maybe_autostart()

    @Slot(int)
    def _on_port_changed(self, _index):
        self._apply_live()
        self._update_status()

    def _maybe_autostart(self):
        port = self.port_combo.currentData()
        if (port and DEFAULT_PORT_HINT in port.lower()
                and not self.engine.running and not self._user_stopped):
            self._start_mixer(interactive=False)

    def _start_mixer(self, interactive: bool) -> bool:
        port = self.port_combo.currentData()
        if not port:
            if interactive:
                QMessageBox.warning(self, "MIDI", "Aucun port MIDI sélectionné")
            return False
        _, invalid = self._collect_rows()
        _, invalid_groups = self._collect_groups()
        if (invalid or invalid_groups) and interactive:
            lines = []
            if invalid:
                lines.append("Assignations directes invalides : " + ", ".join(f"#{r} ('{v}')" for r, v in invalid))
            if invalid_groups:
                lines.append("Groupes invalides : " + ", ".join(f"{n} / {f} ('{v}')" for n, f, v in invalid_groups))
            QMessageBox.critical(self, "Validation", "\n".join(lines))
            return False
        # Auto-start runs with the valid rows only.
        self._apply_live()
        self.engine.start(port)
        return True

    @Slot()
    def _toggle_mixer(self):
        if self.engine.running:
            self._user_stopped = True
            self.engine.stop()
        else:
            self._user_stopped = False
            self._start_mixer(interactive=True)

    @Slot(bool)
    def _on_state_changed(self, running: bool):
        self.btn_run.setText("Arrêter le Mixer" if running else "Démarrer le Mixer")
        self.btn_run.setProperty("running", running)
        self.btn_run.style().unpolish(self.btn_run)
        self.btn_run.style().polish(self.btn_run)
        self.act_toggle.setText("Arrêter le mixer" if running else "Démarrer le mixer")
        self._update_status()

    def _update_status(self):
        port = self.port_combo.currentData()
        text = f"Actif — {port}" if self.engine.running else "Mixer arrêté"
        self.status.setText(text)
        self.tray.setToolTip(f"{APP_NAME} — {text}")

    # -- engine feedback ---------------------------------------------------

    @Slot(str, float, bool)
    def _on_volume(self, name: str, val: float, muted: bool):
        self.overlay.show_volume(name, val, muted)

    @Slot(str)
    def _on_error(self, message: str):
        logger.error(message)
        if message != self._last_error:  # avoid spamming while auto-restart retries
            self._last_error = message
            self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.Warning, 5000)


# ------------------------------------------------------------------------ main

def main() -> int:
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray
    app.setStyleSheet(STYLESHEET)

    # Single instance: if one is already running, ask it to show its window and leave.
    sock = QLocalSocket()
    sock.connectToServer(SINGLETON_NAME)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(SINGLETON_NAME)  # stale socket from a crashed run
    server = QLocalServer()
    server.listen(SINGLETON_NAME)

    window = MainWindow(start_hidden="--minimized" in sys.argv)
    window.single_server = server
    server.newConnection.connect(window._on_new_instance)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
