import os

from midi_mixer import focus
from conftest import FakePulse, FakeStream


def test_stream_found_through_the_window_process_ancestry(monkeypatch):
    # The window belongs to pid 100; the audio comes from its child process 101.
    monkeypatch.setattr(focus, "active_window_info", lambda: (100, ("navigator", "Firefox")))
    monkeypatch.setattr(focus, "_ancestors", lambda pid: [pid, 100] if pid == 101 else [pid])
    pulse = FakePulse(FakeStream("Firefox", pid=101), FakeStream("Spotify", pid=300))
    assert focus.focused_app_name(pulse) == "Firefox"


def test_falls_back_to_the_window_class_when_pulse_gives_no_pid(monkeypatch):
    # Spotify exposes neither a pid nor a binary to PulseAudio.
    monkeypatch.setattr(focus, "active_window_info", lambda: (999999, ("spotify", "Spotify")))
    assert focus.focused_app_name(FakePulse(FakeStream("Spotify"))) == "Spotify"


def test_app_that_plays_nothing_is_named_after_its_window_class(monkeypatch):
    monkeypatch.setattr(focus, "active_window_info", lambda: (os.getppid(), ("konsole", "konsole")))
    assert focus.focused_app_name(FakePulse()) == "konsole"


def test_no_focus_information_or_focus_on_the_mixer_itself(monkeypatch):
    monkeypatch.setattr(focus, "active_window_info", lambda: None)
    assert focus.focused_app_name(FakePulse()) is None
    monkeypatch.setattr(focus, "active_window_info", lambda: (os.getpid(), ("python", "Python")))
    assert focus.focused_app_name(FakePulse()) is None


def test_electron_app_is_named_after_its_binary_not_the_generic_chromium_stream(monkeypatch):
    """Discord plays sound effects through a stream named "Chromium" but its binary is "Discord"."""
    streams = [FakeStream("WEBRTC VoiceEngine", binary="Discord", pid=111),
               FakeStream("Chromium", binary="Discord", pid=112),
               FakeStream("Chromium", binary="chromium", pid=500)]        # a real Chromium browser
    # Flatpak: the window's pid lives in another pid namespace and matches nothing we can see.
    monkeypatch.setattr(focus, "active_window_info", lambda: (7, ("discord", "discord")))
    assert focus.focused_app_name(FakePulse(*streams)) == "Discord"


def test_electron_app_found_through_process_ancestry_is_still_named_after_its_binary(monkeypatch):
    monkeypatch.setattr(focus, "active_window_info", lambda: (100, ("discord", "discord")))
    monkeypatch.setattr(focus, "_ancestors", lambda pid: [pid, 100] if pid == 101 else [pid])
    pulse = FakePulse(FakeStream("Chromium", binary="Discord", pid=101))
    assert focus.focused_app_name(pulse) == "Discord"


def test_the_real_chromium_browser_is_still_named_chromium(monkeypatch):
    monkeypatch.setattr(focus, "active_window_info", lambda: (7, ("chromium", "Chromium")))
    pulse = FakePulse(FakeStream("Chromium", binary="Discord", pid=112), FakeStream("Chromium", binary="chromium", pid=500))
    assert focus.focused_app_name(pulse) == "chromium"


def test_a_generic_binary_is_not_used_when_the_window_does_not_name_it(monkeypatch):
    """Wine apps: the binary is wine64-preloader for every game, the stream name is the game's."""
    monkeypatch.setattr(focus, "active_window_info", lambda: (7, ("nomanssky.exe", "nomanssky.exe")))
    pulse = FakePulse(FakeStream("No Man's Sky", binary="wine64-preloader", media="nomanssky.exe"))
    assert focus.focused_app_name(pulse) == "nomanssky.exe"
