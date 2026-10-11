import types

import pytest

from midi_mixer import engine as engine_module
from midi_mixer.engine import MixerEngine
from midi_mixer.profile import Profile
from conftest import FakePulse, FakeStream, cc, note


def group(uid="u1", app="Fake", **keys):
    return {"uid": uid, "name": uid, "app": app,
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

def test_assign_pads_are_left_to_the_pad_animations(engine):
    engine.set_groups([group(app="Discord")])
    assert not any(channel == 0 and number == 0 for channel, number, _ in leds(engine))


def test_mute_leds_of_removed_or_moved_buttons_are_switched_off(engine):
    engine.set_groups([group(app="Discord")])
    engine.sent.clear()
    engine.set_groups([group(app="Discord", mute="1:50")])       # button changed
    assert (0, 49, 0) in leds(engine) and (0, 50, 0) in leds(engine)
    engine.sent.clear()
    engine.set_groups([])                                        # group removed
    assert (0, 50, 0) in leds(engine)


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
    engine.set_groups([group(mute="49", assign="0", volume="7")])   # keys without a channel cannot be lit
    assert engine.sent == []


def test_profile_without_leds_never_sends_anything_nor_opens_an_output():
    e = make_engine(Profile())                       # neutral profile: no LEDs
    e.set_groups([group(app="Discord")])
    e._handle_button(FakePulse(FakeStream("Discord")), 0, 49)
    assert e.sent == [] and e._open_output("any port") is None


def test_leds_can_be_driven_with_control_changes():
    e = make_engine(Profile({"leds": {"message": "cc", "on": 127, "off": 0}}))
    e.set_groups([group(app="Discord", mute="1:48")])
    e.muted.add("u1")
    e._sync_leds()
    sent = [(m.type, m.channel, m.control, m.value) for m in e.sent]
    assert ("control_change", 0, 48, 0) in sent         # mute off
    assert sent[-1] == ("control_change", 0, 48, 127)   # then lit while muted


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


# ------------------------------------------------------------------- launch button

def test_launch_button_asks_the_ui_to_launch_the_group_app():
    e = make_engine(Profile({"buttons": {"message": "note", "trigger": "press"}}))
    launched = []
    e.launchRequested.connect(launched.append)
    e.set_groups([dict(group(), launch="1:40")])
    e._handle_button(FakePulse(), 0, 40)
    e._handle_button(FakePulse(), 1, 40)                # another channel: not this group's button
    assert launched == ["u1"]


def test_launch_button_works_with_cc_buttons_and_is_not_mistaken_for_a_fader():
    e = make_engine(Profile({"buttons": {"message": "cc", "trigger": "press"}}))
    launched = []
    e.launchRequested.connect(launched.append)
    e.set_groups([dict(group(volume="1:0", mute="1:32", assign="1:48"), launch="1:64")])
    assert e._is_button_key(0, 64) and not e._is_button_key(0, 0)
    e._handle_button(FakePulse(), 0, 64)
    assert launched == ["u1"]


# ----------------------------------------- apps that reset their own volume (Spotify)

def test_fader_volume_is_put_back_when_the_app_resets_its_stream(engine):
    """Spotify rewrites its stream volume (87%) on each new track: the fader value must win."""
    engine.set_groups([group(app="Spotify")])
    stream = FakeStream("Spotify")
    pulse = FakePulse(stream)
    engine._apply_volume(pulse, "Spotify", 0.31)
    stream.volume = 0.87                                  # the app changes it behind our back
    engine._enforce_volumes(pulse)
    assert stream.volume == 0.31


def test_enforcing_leaves_streams_within_tolerance_alone(engine):
    engine.set_groups([group(app="Spotify")])
    stream = FakeStream("Spotify")
    pulse = FakePulse(stream)
    engine._apply_volume(pulse, "Spotify", 0.31)
    stream.volume = 0.315                                 # rounding noise, e.g. our own write coming back
    engine._enforce_volumes(pulse)
    assert stream.volume == 0.315


def test_enforcing_needs_a_fader_move_first_and_an_assigned_app(engine):
    engine.set_groups([group(app="Spotify")])
    stream = FakeStream("Spotify")
    stream.volume = 0.87
    pulse = FakePulse(stream)
    engine._enforce_volumes(pulse)                        # no fader value known yet: untouched
    assert stream.volume == 0.87
    engine._apply_volume(pulse, "Spotify", 0.31)
    engine.set_groups([group(app="")])                    # app un-assigned: forgotten
    stream.volume = 0.87
    engine._enforce_volumes(pulse)
    assert stream.volume == 0.87


def test_enforcing_only_touches_the_streams_of_the_target(engine):
    engine.set_groups([group(app="Spotify")])
    spotify, other = FakeStream("Spotify"), FakeStream("Discord")
    pulse = FakePulse(spotify, other)
    engine._apply_volume(pulse, "Spotify", 0.31)
    spotify.volume, other.volume = 0.87, 0.9
    engine._enforce_volumes(pulse)
    assert (spotify.volume, other.volume) == (0.31, 0.9)


def test_watcher_reapplies_the_volume_after_a_stream_event(engine, monkeypatch):
    """The watcher thread listens to sink-input events and enforces the fader volume after them."""
    import pulsectl
    import midi_mixer.engine as eng

    engine.set_groups([group(app="Spotify")])
    stream = FakeStream("Spotify")
    fake = FakePulse(stream)
    engine._apply_volume(fake, "Spotify", 0.31)
    stream.volume = 0.87

    def listen(timeout=None):
        if not events:
            engine.running = False
            return
        try:
            fake.callback(types.SimpleNamespace(t=events.pop(0)))
        except pulsectl.PulseLoopStop:
            pass

    events = ["remove", "change"]
    fake.event_mask_set = lambda *masks: None
    fake.event_callback_set = lambda func: setattr(fake, "callback", func)
    fake.event_listen = listen
    monkeypatch.setattr(eng.pulsectl, "Pulse", lambda name: type(
        "Ctx", (), {"__enter__": lambda s: fake, "__exit__": lambda s, *a: None})())

    worker = object()
    engine.running, engine._thread = True, worker
    engine._watch_volumes(worker)
    assert stream.volume == 0.31


# ------------------------------------------------------------ pad animations

def pad_levels(e):
    """{(channel, note): last velocity sent} for the pad grid."""
    return {(m.channel, m.note): m.velocity for m in e.sent if m.type == "note_on" and m.note < 40}


def test_apc_template_describes_the_pad_grid(engine):
    cells = engine._pad_cells()
    assert len(cells) == 40
    assert cells[0] == (0, 0, 0, 32)          # top-left: channel 1, note 32
    assert cells[-1] == (4, 7, 0, 7)          # bottom-right: channel 1, note 7
    assert {n for *_, n in cells} == set(range(40))


def test_animation_lights_only_what_changed(engine):
    from midi_mixer.animations import ANIMATIONS

    engine.set_pad_animation("pacman")
    engine._pad_tick(0.0)
    assert len(engine.sent) == 40 + sum(c != "." for row in ANIMATIONS["pacman"].frames[0] for c in row)  # clear + draw
    engine.sent.clear()
    engine._pad_tick(0.01)                    # next frame not due yet
    assert engine.sent == []
    engine._pad_tick(ANIMATIONS["pacman"].interval + 0.01)
    changed = sum(a != b for ra, rb in zip(ANIMATIONS["pacman"].frames[0], ANIMATIONS["pacman"].frames[1])
                  for a, b in zip(ra, rb))
    assert len(engine.sent) == changed


def test_unknown_animation_and_no_pad_do_nothing(engine):
    engine.set_pad_animation("nope")
    assert engine.pad_animation == ""
    engine._pad_tick(0.0)
    assert not any(pad_levels(engine).values())     # the grid is only switched off (leftovers of a previous run)
    generic = make_engine(Profile())
    generic.set_pad_animation("pacman")
    generic._pad_tick(0.0)
    assert generic.sent == []


def test_stopping_the_animation_or_releasing_switches_the_pads_off(engine):
    engine.set_pad_animation("heart")
    engine._pad_tick(0.0)
    assert any(pad_levels(engine).values())
    engine.set_pad_animation("")
    engine._pad_tick(0.5)
    assert not any(pad_levels(engine).values())
    engine.set_pad_animation("heart")
    engine._pad_tick(1.0)
    engine.release_pad()
    assert not any(pad_levels(engine).values()) and engine.pad_animation == ""


def test_pads_are_drawn_again_regularly(engine):
    engine.set_pad_animation("heart")
    engine._pad_tick(0.0)
    engine.sent.clear()
    engine._pad_tick(0.01)
    engine._pad_tick(100.0)                   # long after: frame advanced AND the whole grid re-sent
    assert len(engine.sent) >= 40


def test_spectrum_animation_starts_and_stops_the_audio_capture(engine, monkeypatch):
    started, stopped = [], []

    class FakeAnalyzer:
        bars = [0, 0.2, 0.4, 0.6, 0.8, 1, 0.5, 0.1]

        def start(self):
            started.append(1)

        def stop(self):
            stopped.append(1)

    monkeypatch.setitem(engine_module.LIVE_SOURCES, "spectrum", FakeAnalyzer)
    engine.set_pad_animation("spectrum")
    engine._pad_tick(0.0)
    assert started == [1] and any(pad_levels(engine).values())
    engine.set_pad_animation("heart")
    engine._pad_tick(1.0)
    assert stopped == [1]
    engine.set_pad_animation("spectrum")
    engine._pad_tick(2.0)
    engine.release_pad()
    assert stopped == [1, 1] and engine.pad_animation == ""


# --------------------------------------------------- strip status LEDs (APC40: Clip Stop buttons)

def strip_led(e, channel=0):
    """Last value sent to the status LED (note 52) of the strip on `channel`, or None."""
    sent = [v for ch, number, v in leds(e) if (ch, number) == (channel, 52)]
    return sent[-1] if sent else None


def test_strip_led_is_off_without_an_app_and_lit_with_one(engine):
    engine.set_groups([group(app="")])
    assert strip_led(engine) == 0
    engine.set_groups([group(app="Discord")])
    assert strip_led(engine) == 1
    engine.set_groups([group(uid="u2", app="Discord", volume="3:7", mute="3:49", assign="1:2")])
    assert strip_led(engine, 2) == 1 and strip_led(engine, 0) == 0          # the LED of the strip the group is on


def test_strip_led_blinks_for_the_group_whose_app_has_the_focus(engine, monkeypatch):
    monkeypatch.setattr(engine_module, "focused_app_name", lambda pulse: "Discord")
    monkeypatch.setattr(engine_module, "same_app", lambda pulse, a, b: a.lower() == b.lower())
    engine.set_groups([group(uid="u1", app="Discord"), group(uid="u2", app="Spotify", volume="2:7", mute="2:49",
                                                           assign="1:1")])
    engine._poll_focus(None)
    assert strip_led(engine, 0) == 2 and strip_led(engine, 1) == 1          # focused blinks, the other stays lit
    engine.sent.clear()
    engine._poll_focus(None)
    assert engine.sent == []                                                  # nothing changed: nothing sent
    monkeypatch.setattr(engine_module, "focused_app_name", lambda pulse: "Spotify")
    engine._poll_focus(None)
    assert strip_led(engine, 0) == 1 and strip_led(engine, 1) == 2
    monkeypatch.setattr(engine_module, "focused_app_name", lambda pulse: None)   # e.g. Wayland, or a desktop click
    engine._poll_focus(None)
    assert strip_led(engine, 0) == 1 and strip_led(engine, 1) == 1


def test_strip_leds_go_off_when_the_group_is_removed_or_moved(engine):
    engine.set_groups([group(app="Discord")])
    engine.set_groups([])
    assert strip_led(engine) == 0
    engine.set_groups([group(app="Discord")])
    engine.set_groups([group(app="Discord", volume="2:7")])                   # moved to strip 2
    assert strip_led(engine, 0) == 0 and strip_led(engine, 1) == 1


def test_profile_without_strip_leds_sends_nothing(engine):
    engine.profile = make_engine(Profile({"leds": {"on": 127, "off": 0}})).profile
    engine.set_groups([group(app="Discord")])
    assert strip_led(engine) is None
