import types

import pytest

from midi_mixer.engine import MixerEngine
from conftest import FakePulse, FakeStream, cc, note


def group(uid="u1", app="Fake", color="Vert", **keys):
    return {"uid": uid, "name": uid, "app": app, "color": color,
            "volume": keys.get("volume", "1:7"), "mute": keys.get("mute", "1:49"), "assign": keys.get("assign", "1:0")}


@pytest.fixture
def engine():
    e = MixerEngine()
    e.leds = []
    e._out = types.SimpleNamespace(send=lambda m: e.leds.append((m.channel, m.note, m.velocity)))
    e.events = {"volume": [], "assign": []}
    e.volumeChanged.connect(lambda n, v, muted: e.events["volume"].append((n, muted)))
    e.assignRequested.connect(lambda uid: e.events["assign"].append(uid))
    return e


def test_fader_resolves_to_the_group_app_and_channel(engine):
    engine.set_groups([group(app="Spotify")])
    assert engine._lookup_target(0, 7) == "Spotify"
    assert engine._lookup_target(1, 7) is None          # other channel
    engine.set_groups([group(app="")])
    assert engine._lookup_target(0, 7) is None          # no app assigned = inert


def test_direct_mappings_take_priority_over_groups(engine):
    engine.mappings = {"1:7": "MASTER"}
    engine.set_groups([group(app="Spotify")])
    assert engine._lookup_target(0, 7) == "MASTER"


def test_every_message_of_a_toggle_button_is_one_press(engine):
    """note_off, note_on, note_off = three presses = mute, unmute, mute."""
    engine.set_groups([group()])
    stream = FakeStream("Fake")
    pulse = FakePulse(stream)
    states = []
    for kind in ("note_off", "note_on", "note_off"):
        engine._handle_note(pulse, 0, 49)
        states.append(stream.muted)
    assert states == [True, False, True]
    assert engine.events["volume"] == [("Fake", True), ("Fake", False), ("Fake", True)]


def test_mute_led_follows_the_mute_state(engine):
    engine.set_groups([group()])
    engine.leds.clear()
    pulse = FakePulse(FakeStream("Fake"))
    engine._handle_note(pulse, 0, 49)
    engine._handle_note(pulse, 0, 49)
    assert engine.leds == [(0, 49, 127), (0, 49, 0)]    # lit while muted, off after


def test_mute_does_nothing_when_the_app_is_not_playing(engine):
    engine.set_groups([group(app="Ghost")])
    engine.leds.clear()
    engine._handle_note(FakePulse(FakeStream("Other")), 0, 49)
    assert not engine.muted and engine.leds == [] and engine.events["volume"] == []


def test_assign_button_asks_the_ui_every_time(engine):
    engine.set_groups([group()])
    for _ in range(3):
        engine._handle_note(FakePulse(), 0, 0)
    assert engine.events["assign"] == ["u1", "u1", "u1"]


def test_assign_led_is_lit_only_when_an_app_is_assigned_in_the_group_color(engine):
    engine.set_groups([group(app="", color="Bleu")])
    assert (0, 0, 0) in engine.leds and (0, 0, 45) not in engine.leds
    engine.leds.clear()
    engine.set_groups([group(app="Discord", color="Bleu")])
    assert (0, 0, 45) in engine.leds
    engine.leds.clear()
    engine.set_groups([group(app="Discord", color="Rouge")])
    assert (0, 0, 5) in engine.leds


def test_leds_of_removed_or_moved_buttons_are_switched_off(engine):
    engine.set_groups([group(app="Discord")])
    engine.leds.clear()
    engine.set_groups([group(app="Discord", assign="1:5")])      # pad changed
    assert (0, 0, 0) in engine.leds and (0, 5, 21) in engine.leds
    engine.leds.clear()
    engine.set_groups([])                                        # group removed
    assert (0, 5, 0) in engine.leds and (0, 49, 0) in engine.leds


def test_clear_mute_unlights_the_button(engine):
    engine.set_groups([group()])
    engine.muted.add("u1")
    engine.leds.clear()
    engine.clear_mute("u1")
    assert "u1" not in engine.muted and engine.leds == [(0, 49, 0)]


def test_no_led_without_an_output_or_without_a_channel(engine):
    engine._out = None
    engine.set_groups([group()])            # must not raise
    engine.groups = []
    engine._out = types.SimpleNamespace(send=lambda m: engine.leds.append(m))
    engine.leds.clear()
    engine.set_groups([group(mute="49", assign="0")])   # keys without a channel cannot be lit
    assert engine.leds == []


def test_learn_captures_the_requested_kind_only(engine):
    learned = []
    engine.learned.connect(lambda token, ch, n: learned.append((token, ch, n)))
    engine._learn_token, engine._learn_kinds = "row", ("note",)
    engine._learn_deadline = 1e18
    assert not engine._learn_step(cc(0, 7), 7)            # a fader move is not a button
    assert engine._learn_step(note("note_off", 5, 60), 60)  # note_off counts too (toggle buttons)
    assert learned == [("row", 6, 60)] and engine.learning_token is None


def test_learn_times_out(engine):
    ended = []
    engine.learnEnded.connect(ended.append)
    engine._learn_token, engine._learn_deadline = "row", 0.0
    engine._learn_step()
    assert ended == ["row"] and engine.learning_token is None


def test_cancel_learning_returns_the_pending_token(engine):
    engine._learn_token = "row"
    assert engine.cancel_learning() == "row"
    assert engine.cancel_learning() is None
