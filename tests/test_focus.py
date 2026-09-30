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
