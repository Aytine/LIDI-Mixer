"""Persistent configuration (~/.config/midi-mixer/midi_config.json) and group defaults."""
import json
import logging
import os
import re
import tempfile
import uuid

from .constants import (
    ASSIGN_COLORS, DEFAULT_ASSIGN_COLOR, DEFAULT_GROUP_COUNT, DEFAULT_MAPPINGS, GROUP_DEFAULT_ASSIGN_NOTE,
    GROUP_DEFAULT_CC, GROUP_DEFAULT_MUTE_NOTE,
)
from .midi_utils import mapping_key

logger = logging.getLogger(__name__)


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
