from midi_mixer.audio import set_mute, si_matches_target, target_base_name
from conftest import FakePulse, FakeStream


def test_target_base_name_strips_the_label_suffix():
    assert target_base_name("Firefox (firefox, pid:12)") == "firefox"
    assert target_base_name("Spotify") == "spotify"
    assert target_base_name("") == ""


def test_matching_is_exact_not_substring():
    stream = FakeStream("Firefox", binary="firefox", media="Some Track Title")
    assert si_matches_target(stream, "Firefox")
    assert si_matches_target(stream, "firefox")
    assert si_matches_target(stream, "Firefox (firefox, pid:12)")   # pid in the label is ignored
    assert not si_matches_target(stream, "Spotify (spotify, pid:3)")
    assert not si_matches_target(stream, "track")                   # no substring match on the media name
    assert not si_matches_target(stream, "")


def test_set_mute_applies_to_every_stream_of_the_app():
    a, b, other = FakeStream("Firefox"), FakeStream("Firefox"), FakeStream("Spotify")
    pulse = FakePulse(a, b, other)
    assert set_mute(pulse, "Firefox", True) == ("Firefox", 0.5)
    assert a.muted and b.muted and not other.muted
    set_mute(pulse, "Firefox", False)
    assert not a.muted and not b.muted


def test_set_mute_returns_none_when_nothing_plays():
    assert set_mute(FakePulse(FakeStream("Spotify")), "Discord", True) is None
