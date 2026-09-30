import types

import pytest

from midi_mixer.midi_utils import (
    format_key, key_channel_number, key_matches, mapping_key, midi_number, msg_kind, parse_cc_field,
)
from conftest import cc, note


@pytest.mark.parametrize("text,expected", [
    ("56", (None, 56)),
    ("CC 56 (ch 1)", (1, 56)),
    ("ch1:56", (1, 56)),
    ("Note 49 (ch 16)", (16, 49)),
    ("channel 3 cc 7", (3, 7)),
    ("ch17:5", None),      # channel out of range
    ("200", None),         # number out of range
    ("abc", None),
    ("", None),
])
def test_parse_cc_field(text, expected):
    assert parse_cc_field(text) == expected


def test_key_roundtrip():
    assert mapping_key(None, 56) == "56"
    assert mapping_key(2, 7) == "2:7"
    assert format_key("2:7") == "CC 7 (ch 2)"
    assert format_key("2:49", "Note") == "Note 49 (ch 2)"
    assert format_key("56") == "56"
    assert format_key("") == ""


def test_key_matches_channel_specific_and_any_channel():
    assert key_matches("1:7", 0, 7)          # channels are 0-based on the wire
    assert not key_matches("1:7", 1, 7)
    assert key_matches("7", 5, 7)            # no channel = any channel
    assert not key_matches("", 0, 7)


def test_key_channel_number():
    assert key_channel_number("3:49") == (2, 49)
    assert key_channel_number("49") is None  # LEDs need a channel


def test_buttons_count_note_on_and_note_off():
    """The APC40 toggle buttons alternate note_on / note_off: both are one press."""
    assert midi_number(note("note_on", 0, 49)) == 49
    assert midi_number(note("note_off", 0, 49)) == 49
    assert midi_number(cc(0, 7)) == 7
    assert midi_number(types.SimpleNamespace(type="clock")) is None
    assert msg_kind(note("note_off", 0, 1)) == "note"
    assert msg_kind(cc(0, 1)) == "control_change"
    assert msg_kind(types.SimpleNamespace(type="clock")) is None
