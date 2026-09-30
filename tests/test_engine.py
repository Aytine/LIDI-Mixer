import types

import pytest

from midi_mixer.engine import MixerEngine
from midi_mixer.profile import Profile
from conftest import FakePulse, FakeStream, cc, note


def group(uid="u1", app="Fake", color="Vert", **keys):
    return {"uid": uid, "name": uid, "app": app, "color": color,
            "volume": keys.get("volume", "1:7"), "mute": keys.get("mute", "1:49"), "assign": keys.get("assign", "1:0")}


def make_engine(profile):
    e = MixerEngine()
    e.profile = profile
    e.sent = []          # raw MIDI messages sent to the controller's LEDs
    e._out = types.SimpleNamespace(send=e.sent.append)
    e.events = {"volume": [], "assign": []}
    e.volumeChanged.connect(lambda n, v, muted: e.events["volume"].append((n, muted)))
    e.assignRequested.connect(lambda uid: e.events["assign"].append(uid))
    return e


@pytest.fixture
def engine(apc_profile):
    return make_engine(apc_profile)


def leds(e):
    """LED messages as (channel, number, value) whatever their type."""
    return [(m.channel, m.note if m.type == "note_on" else m.control,
             m.velocity if m.type == "note_on" else m.value) for m in e.sent]


# ------------------------------------------------------------------ faders

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


# ------------------------------------------------------- buttons (APC: toggle notes)

def test_every_message_of_a_toggle_button_is_one_press(engine):
    """Profile trigger "any": note_off, note_on, note_off = three presses = mute, unmute, mute."""
    messages = [note("note_off", 0, 49), note("note_on", 0, 49), note("note_off", 0, 49)]
    assert all(engine._button_press(m) is True for m in messages)
    engine.set_groups([group()])
    stream = FakeStream("Fake")
    pulse = FakePulse(stream)
    states = []
    for m in messages:
        engine._handle_button(pulse, m.channel, m.note)
        states.append(stream.muted)
    assert states == [True, False, True]
    assert engine.events["volume"] == [("Fake", True), ("Fake", False), ("Fake", True)]


def test_mute_led_follows_the_mute_state(engine):
    engine.set_groups([group()])
    engine.sent.clear()
    pulse = FakePulse(FakeStream("Fake"))
    engine._handle_button(pulse, 0, 49)
    engine._handle_button(pulse, 0, 49)
    assert leds(engine) == [(0, 49, 127), (0, 49, 0)]    # lit while muted, off after


def test_mute_does_nothing_when_the_app_is_not_playing(engine):
    engine.set_groups([group(app="Ghost")])
    engine.sent.clear()
    engine._handle_button(FakePulse(FakeStream("Other")), 0, 49)
    assert not engine.muted and engine.sent == [] and engine.events["volume"] == []


def test_assign_button_asks_the_ui_every_time(engine):
    engine.set_groups([group()])
    for _ in range(3):
        engine._handle_button(FakePulse(), 0, 0)
    assert engine.events["assign"] == ["u1", "u1", "u1"]


# ------------------------------------------------------------------- LEDs

def test_assign_led_is_lit_only_when_an_app_is_assigned_in_the_group_color(engine):
    engine.set_groups([group(app="", color="Bleu")])
    assert (0, 0, 0) in leds(engine) and (0, 0, 45) not in leds(engine)
    engine.sent.clear()
    engine.set_groups([group(app="Discord", color="Bleu")])
    assert (0, 0, 45) in leds(engine)
    engine.sent.clear()
    engine.set_groups([group(app="Discord", color="Rouge")])
    assert (0, 0, 5) in leds(engine)


def test_leds_of_removed_or_moved_buttons_are_switched_off(engine):
    engine.set_groups([group(app="Discord")])
    engine.sent.clear()
    engine.set_groups([group(app="Discord", assign="1:5")])      # pad changed
    assert (0, 0, 0) in leds(engine) and (0, 5, 21) in leds(engine)
    engine.sent.clear()
    engine.set_groups([])                                        # group removed
    assert (0, 5, 0) in leds(engine) and (0, 49, 0) in leds(engine)


def test_clear_mute_unlights_the_button(engine):
    engine.set_groups([group()])
    engine.muted.add("u1")
    engine.sent.clear()
    engine.clear_mute("u1")
    assert "u1" not in engine.muted and leds(engine) == [(0, 49, 0)]


def test_no_led_without_an_output_or_without_a_channel(engine):
    engine._out = None
    engine.set_groups([group()])            # must not raise
    engine.groups = []
    engine._out = types.SimpleNamespace(send=engine.sent.append)
    engine.sent.clear()
    engine.set_groups([group(mute="49", assign="0")])   # keys without a channel cannot be lit
    assert engine.sent == []


def test_profile_without_leds_never_sends_anything_nor_opens_an_output():
    e = make_engine(Profile())                       # neutral profile: no LEDs
    e.set_groups([group(app="Discord")])
    e._handle_button(FakePulse(FakeStream("Discord")), 0, 49)
    assert e.sent == [] and e._open_output("any port") is None


def test_leds_can_be_driven_with_control_changes():
    e = make_engine(Profile({"leds": {"message": "cc", "on": 127, "off": 0}}))
    e.set_groups([group(app="Discord", mute="1:48", assign="1:64")])
    sent = {(m.type, m.channel, m.control, m.value) for m in e.sent}
    assert ("control_change", 0, 64, 127) in sent       # assign lit (no palette: the plain "on" value)
    assert ("control_change", 0, 48, 0) in sent         # mute off


# ------------------------------------------------ profile dependent button types

def test_momentary_note_buttons_only_count_the_press():
    e = make_engine(Profile({"buttons": {"message": "note", "trigger": "press"}}))
    assert e._button_press(note("note_on", 0, 5)) is True
    assert e._button_press(note("note_on", 0, 5, velocity=0)) is False     # note_on vel 0 is a release
    assert e._button_press(note("note_off", 0, 5)) is False
    assert e._button_press(cc(0, 5)) is None                                # a CC is not a note button


def test_cc_buttons_are_told_apart_from_faders_by_their_configured_keys():
    e = make_engine(Profile({"buttons": {"message": "cc", "trigger": "press"}}))
    e.set_groups([group(volume="1:0", mute="1:32", assign="1:48")])
    assert e._button_press(cc(0, 32, 127)) is True and e._button_press(cc(0, 32, 0)) is False
    assert e._button_press(note("note_on", 0, 32)) is None
    assert e._is_button_key(0, 32) and e._is_button_key(0, 48)
    assert not e._is_button_key(0, 0)                   # the fader's CC is not a button


def test_a_cc_button_press_mutes_while_fader_cc_keeps_controlling_volume():
    e = make_engine(Profile({"buttons": {"message": "cc", "trigger": "press"}}))
    e.set_groups([group(volume="1:0", mute="1:32", assign="1:48")])
    stream = FakeStream("Fake")
    e._handle_button(FakePulse(stream), 0, 32)
    assert stream.muted is True
    assert e._lookup_target(0, 0) == "Fake"


# -------------------------------------------------------------------- learn

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
