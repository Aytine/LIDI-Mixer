"""Controller profiles: everything that depends on the MIDI controller is data (JSON), not code.

A profile describes how to talk to one controller (which port, what kind of
buttons, how LEDs are driven, where the faders/buttons of each group sit) and
also holds the user's settings for it (groups, direct mappings, chosen port).
Bundled templates (midi_mixer/profiles/*.json) are profiles without user state.
"""
import uuid

from .midi_utils import mapping_key

SCHEMA_VERSION = 1
BUTTON_MESSAGES = ("note", "cc")      # do the buttons send notes or control changes?
BUTTON_TRIGGERS = ("press", "any")    # "press": note_on / CC>0 ; "any": every message is a press (toggle buttons)
GROUP_FIELDS = ("volume", "mute", "assign")


def _int(value, default, lo, hi):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if lo <= number <= hi else default


def _normalize_leds(raw):
    """LED description, or None when the controller has no LEDs we can drive."""
    if not isinstance(raw, dict):
        return None
    colors = {}
    for name, spec in (raw.get("colors") or {}).items():
        spec = spec if isinstance(spec, dict) else {"velocity": spec}
        velocity = _int(spec.get("velocity"), None, 0, 127)
        if velocity is not None:
            colors[str(name)] = {"velocity": velocity, "hex": str(spec.get("hex") or "#888888")}
    default = raw.get("default_color")
    return {
        "message": raw.get("message") if raw.get("message") in BUTTON_MESSAGES else "note",
        "on": _int(raw.get("on"), 127, 0, 127),
        "off": _int(raw.get("off"), 0, 0, 127),
        "colors": colors,
        "default_color": default if default in colors else next(iter(colors), None),
    }


def _normalize_spec(raw):
    """Where a control sits for group 0, and how it moves for the next groups (steps)."""
    if not isinstance(raw, dict):
        return None
    number = _int(raw.get("number"), None, 0, 127)
    if number is None:
        return None
    return {
        "number": number,
        "channel": _int(raw.get("channel"), None, 1, 16),   # None = any channel
        "number_step": _int(raw.get("number_step"), 0, -127, 127),
        "channel_step": _int(raw.get("channel_step"), 0, -15, 15),
    }


class Profile:
    """A controller profile plus the user's settings for it. `Profile()` is a neutral profile."""

    def __init__(self, data=None):
        raw = data if isinstance(data, dict) else {}
        buttons = raw.get("buttons") if isinstance(raw.get("buttons"), dict) else {}
        template = raw.get("group_template") if isinstance(raw.get("group_template"), dict) else {}

        self.name = str(raw.get("name") or "Profil")
        self.port = raw.get("port") if isinstance(raw.get("port"), str) else None   # chosen MIDI input
        self.port_hint = str(raw.get("port_hint") or "").lower()                    # substring of the port name
        self.buttons_message = buttons.get("message") if buttons.get("message") in BUTTON_MESSAGES else "note"
        self.button_trigger = buttons.get("trigger") if buttons.get("trigger") in BUTTON_TRIGGERS else "press"
        self.leds = _normalize_leds(raw.get("leds"))
        self.template = {"count": _int(template.get("count"), 4, 0, 64)}
        for field in GROUP_FIELDS:
            self.template[field] = _normalize_spec(template.get(field))
        mappings = raw.get("mappings")
        self.mappings = ({str(k): str(v) for k, v in mappings.items()} if isinstance(mappings, dict) else {})
        groups = raw.get("groups")
        if isinstance(groups, list):
            self.groups = [self._normalize_group(g) for g in groups if isinstance(g, dict)]
        else:
            self.groups = [self.make_group(i) for i in range(self.template["count"])]

    # -- description of the controller ----------------------------------------

    @property
    def has_leds(self) -> bool:
        return self.leds is not None

    @property
    def colors(self) -> dict:
        return self.leds["colors"] if self.leds else {}

    @property
    def default_color(self):
        return self.leds["default_color"] if self.leds else None

    @property
    def button_label(self) -> str:
        return "CC" if self.buttons_message == "cc" else "Note"

    @property
    def button_learn_kind(self) -> str:
        """Message kind the Learn mode must wait for when learning a button."""
        return "control_change" if self.buttons_message == "cc" else "note"

    def matches_port(self, port: str) -> bool:
        return bool(self.port_hint) and self.port_hint in port.lower()

    def color_velocity(self, name):
        """Velocity/value that lights an Assign LED in color `name` (the plain 'on' value without a palette)."""
        if not self.leds:
            return None
        spec = self.colors.get(name) or self.colors.get(self.default_color)
        return spec["velocity"] if spec else self.leds["on"]

    def color_hex(self, name):
        spec = self.colors.get(name) or self.colors.get(self.default_color)
        return spec["hex"] if spec else None

    # -- groups --------------------------------------------------------------

    def template_key(self, field: str, index: int) -> str:
        """Config key ("1:7", "7"...) of `field` for the group at template position `index`, "" if unbound."""
        spec = self.template.get(field)
        if spec is None:
            return ""
        number = spec["number"] + index * spec["number_step"]
        channel = spec["channel"]
        if channel is not None:
            channel += index * spec["channel_step"]
            if not 1 <= channel <= 16:
                return ""
        return mapping_key(channel, number) if 0 <= number <= 127 else ""

    def make_group(self, index: int, name: str = None) -> dict:
        group = {"uid": uuid.uuid4().hex[:8], "name": name or f"Groupe {index + 1}", "app": "",
                 "color": self.default_color or ""}
        for field in GROUP_FIELDS:
            group[field] = self.template_key(field, index)
        return group

    def new_group(self, existing) -> dict:
        """A new group on the first free template position, or an unbound one (use Learn) when none is left."""
        names = {g["name"] for g in existing}
        number = 1
        while f"Groupe {number}" in names:
            number += 1
        name = f"Groupe {number}"
        used = {g["volume"] for g in existing}
        for index in range(self.template["count"]):
            key = self.template_key("volume", index)
            if key and key not in used:
                return self.make_group(index, name)
        group = self.make_group(0, name)
        group.update({field: "" for field in GROUP_FIELDS})
        return group

    def _normalize_group(self, raw: dict) -> dict:
        color = raw.get("color")
        return {
            "uid": str(raw.get("uid") or uuid.uuid4().hex[:8]),
            "name": str(raw.get("name") or ""),
            "app": str(raw.get("app") or ""),
            "color": color if color in self.colors else (self.default_color or ""),
            **{field: str(raw.get(field) or "") for field in GROUP_FIELDS},
        }

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        template = {"count": self.template["count"]}
        template.update({f: self.template[f] for f in GROUP_FIELDS if self.template[f]})
        data = {
            "version": SCHEMA_VERSION,
            "name": self.name,
            "port": self.port,
            "port_hint": self.port_hint,
            "buttons": {"message": self.buttons_message, "trigger": self.button_trigger},
            "group_template": template,
            "mappings": dict(self.mappings),
            "groups": [dict(g) for g in self.groups],
        }
        if self.leds:
            data["leds"] = self.leds
        return data

    @classmethod
    def from_template(cls, data: dict, name: str = None) -> "Profile":
        """A fresh profile from a bundled template: no saved port, groups generated from its layout."""
        fresh = {k: v for k, v in data.items() if k not in ("groups", "port")}
        if name:
            fresh["name"] = name
        return cls(fresh)
