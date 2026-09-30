"""XDG autostart entry (works on KDE, GNOME, XFCE...)."""
import os
import sys

from .constants import APP_NAME


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
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
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
