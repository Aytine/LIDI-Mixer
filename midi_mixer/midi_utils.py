"""Pure MIDI helpers: CC/note keys, message kinds, port listing."""
import logging
import re

import mido

logger = logging.getLogger(__name__)


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


def input_port_names():
    try:
        return mido.get_input_names()
    except Exception as e:
        logger.error("Impossible de lister les ports MIDI : %s", e)
        return []
