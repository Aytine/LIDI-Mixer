"""Launch an application from a group: find its .desktop entry, or run an explicit command."""
import logging
import os
import shlex
import subprocess
import threading
from dataclasses import dataclass

from .audio import si_matches_target, target_base_name

logger = logging.getLogger(__name__)

FIELD_CODES = set("fFuUdDnNickvm")   # Exec field codes (%f, %U...) : filled in by a file manager, dropped here


@dataclass
class DesktopEntry:
    path: str
    desktop_id: str        # file name without ".desktop", e.g. "com.discordapp.Discord"
    name: str
    exec: str
    wm_class: str = ""
    workdir: str = ""
    terminal: bool = False
    no_display: bool = False


def desktop_dirs():
    """Folders holding .desktop files: user, system, Flatpak and Snap exports."""
    home = os.path.expanduser("~")
    bases = [os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")]
    bases += (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    bases += [os.path.join(home, ".local", "share", "flatpak", "exports", "share"),
              "/var/lib/flatpak/exports/share", "/var/lib/snapd/desktop"]
    folders = []
    for base in bases:
        folder = os.path.join(base, "applications")
        if base and folder not in folders:
            folders.append(folder)
    return folders


def parse_desktop_file(path: str):
    """The launchable application described by `path`, or None (not an application, hidden, unreadable)."""
    values, in_entry = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("["):
                    if in_entry:
                        break                      # only the main [Desktop Entry] group matters
                    in_entry = line == "[Desktop Entry]"
                elif in_entry and "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    values.setdefault(key.strip(), value.strip())   # first wins: unlocalized keys come first
    except OSError:
        return None
    if values.get("Type") != "Application" or values.get("Hidden", "").lower() == "true" or not values.get("Exec"):
        return None
    return DesktopEntry(
        path=path,
        desktop_id=os.path.basename(path)[:-len(".desktop")],
        name=values.get("Name", ""),
        exec=values["Exec"],
        wm_class=values.get("StartupWMClass", ""),
        workdir=values.get("Path", ""),
        terminal=values.get("Terminal", "").lower() == "true",
        no_display=values.get("NoDisplay", "").lower() == "true",
    )


def scan_entries(folders=None):
    entries = []
    for folder in folders if folders is not None else desktop_dirs():
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name.endswith(".desktop"):
                entry = parse_desktop_file(os.path.join(folder, name))
                if entry is not None:
                    entries.append(entry)
    return entries


def exec_argv(command: str):
    """Command line of an Exec= value, without the field codes and Flatpak's @@ markers."""
    argv = []
    for token in shlex.split(command):
        if token.startswith("@@") or (len(token) == 2 and token[0] == "%" and token[1] in FIELD_CODES):
            continue
        argv.append(token.replace("%%", "%"))
    return argv


def _program_name(argv) -> str:
    """What the entry runs, for matching: the Flatpak application's short name, else the binary."""
    args = [a for a in argv[1:] if "=" not in a and not a.startswith("-")] if argv and argv[0] == "env" else argv
    if not args:
        return ""
    if os.path.basename(args[0]) == "flatpak" and "run" in args:
        rest = [a for a in args[args.index("run") + 1:] if not a.startswith("-")]
        return rest[0].rsplit(".", 1)[-1].lower() if rest else ""
    return os.path.basename(args[0]).lower()


def match_score(entry: DesktopEntry, wanted: str) -> int:
    """How well `entry` names application `wanted` (lower case): 0 = no, 3 = its display name, 1 = loose."""
    if not wanted:
        return 0
    if entry.name.lower() == wanted:
        return 3
    try:
        program = _program_name(exec_argv(entry.exec))
    except ValueError:
        program = ""
    names = {entry.wm_class.lower(), entry.desktop_id.lower(), entry.desktop_id.rsplit(".", 1)[-1].lower(), program}
    if wanted in names:
        return 2
    return 1 if entry.name.lower().startswith(wanted + " ") else 0   # "Firefox" -> "Firefox ESR"


def find_entry(app: str, entries):
    """The best entry for audio application `app` (a name or a "Name (binary, pid:1)" label), or None."""
    wanted = target_base_name(app)
    scored = [(match_score(e, wanted), not e.no_display, e) for e in entries]
    scored = [item for item in scored if item[0]]
    return max(scored, key=lambda item: item[:2])[2] if scored else None


_cache = {"entries": None}


def lookup(app: str):
    """find_entry on the installed applications; rescans once on a miss (something may have been installed)."""
    for rescan in (False, True):
        if _cache["entries"] is None or rescan:
            _cache["entries"] = scan_entries()
        entry = find_entry(app, _cache["entries"])
        if entry is not None:
            return entry
    return None


def clean_environment():
    """Our environment without the virtualenv this app runs in, so launched programs are not affected by it."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    if venv:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p != os.path.join(venv, "bin"))
    return env


def launch(app: str, command: str = ""):
    """Start `command` (explicit) or the installed application `app`. Returns (ok, message for the user)."""
    command = (command or "").strip()
    workdir, label = None, command
    try:
        if command:
            argv = shlex.split(command)
        else:
            entry = lookup(app)
            if entry is None:
                return False, f"Aucun lanceur trouvé pour « {app} » : renseigne une commande de lancement."
            if entry.terminal:
                return False, f"« {entry.name} » se lance dans un terminal : renseigne une commande de lancement."
            argv, label = exec_argv(entry.exec), entry.name or app
            workdir = entry.workdir if os.path.isdir(entry.workdir or "") else None
    except ValueError as e:
        return False, f"Commande invalide : {e}"
    if not argv:
        return False, "Commande de lancement vide."
    try:
        process = subprocess.Popen(argv, cwd=workdir or os.path.expanduser("~"), env=clean_environment(),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except FileNotFoundError:
        return False, f"Commande introuvable : {argv[0]}"
    except OSError as e:
        return False, f"Lancement impossible : {e}"
    threading.Thread(target=process.wait, daemon=True).start()   # reap it when it exits (no zombie)
    return True, f"Lancement de {label}…"


def is_running(pulse, app: str) -> bool:
    """True if an audio stream of `app` exists: it is running and playing something."""
    return any(si_matches_target(si, app) for si in pulse.sink_input_list())
