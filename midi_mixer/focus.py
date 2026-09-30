"""Find which application owns the focused window (X11)."""
import logging
import os

from .audio import display_name, si_matches_target

try:
    from Xlib import X, display as xdisplay
except ImportError:  # focus detection (Assign button) is then unavailable
    X = xdisplay = None

XLIB_AVAILABLE = xdisplay is not None

logger = logging.getLogger(__name__)


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


def _identity(si, candidates):
    """The label of stream `si` that best names the app the user is looking at.

    A stream's `application.name` can be generic or shared: Electron apps such as
    Discord play through streams named "Chromium" while their process binary is
    "Discord". So when one of the window's own names (class, process name)
    equals the stream's binary, name or media, return that field, binary first;
    otherwise the usual application name.
    """
    props = getattr(si, "proplist", {}) or {}
    wanted = {c.lower() for c in candidates}
    for key in ("application.process.binary", "application.name", "media.name"):
        value = props.get(key)
        if value and value.lower() in wanted:
            return value
    return props.get("application.name") or display_name(si)


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

    candidates = {c for c in wm_class if c}
    try:
        with open(f"/proc/{pid}/comm") as f:
            candidates.add(f.read().strip())
    except (OSError, TypeError):
        pass

    streams = pulse.sink_input_list()
    if pid:
        for si in streams:
            props = getattr(si, "proplist", {}) or {}
            try:
                si_pid = int(props.get("application.process.id") or 0)
            except ValueError:
                si_pid = 0
            if si_pid and pid in _ancestors(si_pid):
                return _identity(si, candidates)

    matching = [si for si in streams if any(si_matches_target(si, c) for c in candidates)]
    if matching:
        # A stream whose binary is the window's program is a surer match than one that merely has the same name
        # (Discord's "Chromium" streams versus the real Chromium browser).
        wanted = {c.lower() for c in candidates}
        best = next((si for si in matching
                     if (getattr(si, "proplist", {}) or {}).get("application.process.binary", "").lower() in wanted),
                    matching[0])
        return _identity(best, candidates)
    return wm_class[1] if len(wm_class) > 1 else (next(iter(candidates), None))
