"""Persistent data under ~/.config/midi-mixer/: app settings and one JSON file per profile.

    settings.json            {"active_profile": "<id>", "autostart": true}
    profiles/<id>.json       a Profile (controller description + the user's groups/mappings)

Bundled templates live next to the code (midi_mixer/profiles/*.json).
"""
import json
import logging
import os
import re
import tempfile
import unicodedata

from .profile import Profile

logger = logging.getLogger(__name__)

BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
GENERIC_TEMPLATE = "generic"
DEFAULT_SETTINGS = {"active_profile": None, "autostart": True}


# ------------------------------------------------------------------- locations

def config_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), ".config", "midi-mixer")
    os.makedirs(d, exist_ok=True)
    return d


def profiles_dir() -> str:
    d = os.path.join(config_dir(), "profiles")
    os.makedirs(d, exist_ok=True)
    return d


def settings_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def legacy_config_path() -> str:
    """Single-controller config used before profiles existed."""
    return os.path.join(config_dir(), "midi_config.json")


def _write_json(path: str, data: dict):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_json(path: str):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.error("Lecture impossible de %s : %s", path, e)
        return None


# -------------------------------------------------------------------- settings

def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    data = _read_json(settings_path()) if os.path.exists(settings_path()) else None
    if isinstance(data, dict):
        if isinstance(data.get("active_profile"), str):
            settings["active_profile"] = data["active_profile"]
        settings["autostart"] = bool(data.get("autostart", True))
    return settings


def save_settings(settings: dict):
    _write_json(settings_path(), {"active_profile": settings.get("active_profile"),
                                  "autostart": bool(settings.get("autostart", True))})


# ------------------------------------------------------------------- templates

def bundled_templates() -> dict:
    """{template id: raw dict} of the profiles shipped with the app, sorted by name."""
    templates = {}
    for filename in sorted(os.listdir(BUNDLED_DIR)):
        if filename.endswith(".json"):
            data = _read_json(os.path.join(BUNDLED_DIR, filename))
            if isinstance(data, dict):
                templates[filename[:-5]] = data
    return dict(sorted(templates.items(), key=lambda kv: str(kv[1].get("name", kv[0])).lower()))


def match_template(ports):
    """Id of the first bundled template whose port hint matches one of `ports`, else None."""
    for template_id, data in bundled_templates().items():
        profile = Profile(data)
        if any(profile.matches_port(p) for p in ports):
            return template_id
    return None


# -------------------------------------------------------------------- profiles

def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "profil"


def _unique_id(name: str) -> str:
    base, candidate, n = _slug(name), _slug(name), 2
    while os.path.exists(os.path.join(profiles_dir(), candidate + ".json")):
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def profile_path(pid: str) -> str:
    return os.path.join(profiles_dir(), pid + ".json")


def list_profiles():
    """[(id, name)] of the user's profiles, sorted by name."""
    found = []
    for filename in os.listdir(profiles_dir()):
        if filename.endswith(".json"):
            profile = load_profile(filename[:-5])
            if profile is not None:
                found.append((filename[:-5], profile.name))
    return sorted(found, key=lambda item: item[1].lower())


def load_profile(pid: str):
    path = profile_path(pid)
    data = _read_json(path) if os.path.exists(path) else None
    return Profile(data) if isinstance(data, dict) else None


def save_profile(pid: str, profile: Profile):
    _write_json(profile_path(pid), profile.to_dict())


def delete_profile(pid: str):
    if os.path.exists(profile_path(pid)):
        os.remove(profile_path(pid))


def add_profile(profile: Profile) -> str:
    """Store a new profile under a fresh id and return that id."""
    pid = _unique_id(profile.name)
    save_profile(pid, profile)
    return pid


def create_profile(name: str, template_id: str = None) -> str:
    """New profile from a bundled template (the generic one by default)."""
    templates = bundled_templates()
    data = templates.get(template_id) or templates.get(GENERIC_TEMPLATE) or {}
    return add_profile(Profile.from_template(data, name))


def duplicate_profile(profile: Profile, name: str) -> str:
    copy = Profile(profile.to_dict())
    copy.name = name
    return add_profile(copy)


def import_profile(path: str) -> str:
    """Add the profile stored in file `path`; raises ValueError if it is not a profile."""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError("Ce fichier n'est pas un profil valide.")
    profile = Profile(data)
    profile.port = None          # port names are machine specific
    return add_profile(profile)


def export_profile(profile: Profile, path: str):
    data = profile.to_dict()
    data["port"] = None          # port names are machine specific
    _write_json(path, data)


# ------------------------------------------------------------------- bootstrap

def _migrate_legacy():
    """Turn the old single-controller file into an APC40 profile (it was APC40 only). None if absent."""
    path = legacy_config_path()
    if not os.path.exists(path):
        return None
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    templates = bundled_templates()
    base = templates.get("apc40_mk2") or templates.get(GENERIC_TEMPLATE) or {}
    merged = {k: v for k, v in base.items() if k not in ("groups", "port")}
    if "mappings" in data:       # current legacy format
        merged["mappings"] = data.get("mappings") or {}
        merged["port"] = data.get("_port")
        if isinstance(data.get("groups"), list):
            merged["groups"] = data["groups"]
    else:                        # oldest format: the whole file was the mappings
        merged["mappings"] = data
    os.replace(path, path + ".migrated")
    return Profile(merged), bool(data.get("autostart", True))


def bootstrap(ports):
    """Make sure an active profile exists; returns (settings, profile id).

    In order: keep the active profile; else the first existing one; else migrate
    the legacy file; else create one from the template matching a connected
    controller (or the generic one).
    """
    settings = load_settings()
    active = settings["active_profile"]
    if active and load_profile(active) is not None:
        return settings, active

    existing = list_profiles()
    if existing:
        pid = existing[0][0]
    else:
        migrated = _migrate_legacy()
        if migrated:
            profile, settings["autostart"] = migrated
            pid = add_profile(profile)
        else:
            template_id = match_template(ports)
            template = bundled_templates().get(template_id) or bundled_templates().get(GENERIC_TEMPLATE) or {}
            name = template.get("name") if template_id else "Mon contrôleur"
            pid = create_profile(name, template_id)
    settings["active_profile"] = pid
    save_settings(settings)
    return settings, pid
