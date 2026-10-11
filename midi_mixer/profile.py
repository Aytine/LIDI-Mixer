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
GROUP_FIELDS = ("volume", "mute", "assign", "launch")   # MIDI controls of a group


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


def _normalize_pad(raw):
    """Where the pad grid sits (first pad = top-left, steps to the next column / row), or None without a grid.

    Pad (row r, column c) is the note `note + r * note_step + c * note_col_step` on channel
    `channel + c * channel_step`.
    `colors` maps the animation color keys (see animations.py) to the LED value that lights them.
    """
    if not isinstance(raw, dict):
        return None
    pad = {"rows": _int(raw.get("rows"), 5, 1, 16), "cols": _int(raw.get("cols"), 8, 1, 16),
           "channel": _int(raw.get("channel"), 1, 1, 16), "note": _int(raw.get("note"), None, 0, 127),
           "channel_step": _int(raw.get("channel_step"), 1, -15, 15),
           "note_step": _int(raw.get("note_step"), 1, -127, 127),
           "note_col_step": _int(raw.get("note_col_step"), 0, -127, 127)}
    if pad["note"] is None:
        return None
    last_channel = pad["channel"] + (pad["cols"] - 1) * pad["channel_step"]
    notes = [pad["note"] + r * pad["note_step"] + c * pad["note_col_step"]
             for r in (0, pad["rows"] - 1) for c in (0, pad["cols"] - 1)]
    if not (1 <= last_channel <= 16 and all(0 <= n <= 127 for n in notes)):
        return None
    colors = raw.get("colors") if isinstance(raw.get("colors"), dict) else {}
    pad["colors"] = {str(k): v for k, v in ((k, _int(v, None, 1, 127)) for k, v in colors.items()) if v is not None}
    return pad


def _normalize_indicator(raw):
    """One status LED per fader strip (APC40: the Clip Stop buttons), or None.

    The LED of strip N is the note / CC `number + N * number_step` on channel `channel + N * channel_step`;
    `assigned` / `focused` are the LED values for "an app is assigned" / "and its window has the focus".
    """
    spec = _normalize_spec(raw)
    if spec is None or spec["channel"] is None:
        return None
    spec["assigned"] = _int(raw.get("assigned"), 1, 1, 127)
    spec["focused"] = _int(raw.get("focused"), 2, 1, 127)
    return spec


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
        self.pad = _normalize_pad(raw.get("pad"))                                  # LED grid for animations
        self.indicator = _normalize_indicator(raw.get("indicator"))                # status LED of each strip
        self.pad_animation = str(raw.get("pad_animation") or "")                   # name in animations.ANIMATIONS
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

    def strip_column(self, volume_key: str):
        """0-based position of the fader strip whose volume control is `volume_key`, from the template; else None.

        Strips are told apart by MIDI channel (APC40: one channel per strip) or, when they share a
        channel, by control number (APC mini: CC 48, 49, 50...).
        """
        spec = self.template.get("volume")
        if spec is None or not volume_key:
            return None
        channel, _, number = volume_key.rpartition(":")
        try:
            number = int(number)
            channel = int(channel) if channel else None
        except ValueError:
            return None
        for step, value, first in ((spec["channel_step"], channel, spec["channel"]),
                                   (spec["number_step"], number, spec["number"])):
            if step and value is not None and first is not None and (value - first) % step == 0:
                return (value - first) // step
        return None

    def indicator_key(self, group: dict) -> str:
        """Config key ("1:52"...) of the status LED above the group's fader strip, "" if there is none."""
        spec = self.indicator
        column = self.strip_column(group.get("volume", ""))
        if spec is None or column is None:
            return ""
        channel = spec["channel"] + column * spec["channel_step"]
        number = spec["number"] + column * spec["number_step"]
        return mapping_key(channel, number) if 1 <= channel <= 16 and 0 <= number <= 127 else ""

    def make_group(self, index: int, name: str = None) -> dict:
        group = {"uid": uuid.uuid4().hex[:8], "name": name or f"Groupe {index + 1}", "app": "", "command": ""}
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
        return {
            "uid": str(raw.get("uid") or uuid.uuid4().hex[:8]),
            "name": str(raw.get("name") or ""),
            "app": str(raw.get("app") or ""),
            "command": str(raw.get("command") or ""),   # optional launch command; empty = find the app's launcher
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
        if self.indicator:
            data["indicator"] = self.indicator
        if self.pad:
            data["pad"] = self.pad
            data["pad_animation"] = self.pad_animation
        return data

    @classmethod
    def from_template(cls, data: dict, name: str = None) -> "Profile":
        """A fresh profile from a bundled template: no saved port, groups generated from its layout."""
        fresh = {k: v for k, v in data.items() if k not in ("groups", "port")}
        if name:
            fresh["name"] = name
        return cls(fresh)
