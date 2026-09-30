import os
import sys
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed for the GUI tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    """Never touch the real ~/.config (mixer config, autostart entry)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    from midi_mixer.theme import STYLESHEET

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)
    return app


class FakeStream:
    """Minimal stand-in for a pulsectl sink input."""

    def __init__(self, name="Fake", binary=None, pid=None, media=None):
        self.proplist = {"application.name": name}
        if binary:
            self.proplist["application.process.binary"] = binary
        if pid:
            self.proplist["application.process.id"] = str(pid)
        if media:
            self.proplist["media.name"] = media
        self.muted = False
        self.volume = 0.5


class FakePulse:
    """Minimal stand-in for pulsectl.Pulse: a list of streams, mute and volume."""

    def __init__(self, *streams):
        self.streams = list(streams)

    def sink_input_list(self):
        return self.streams

    def mute(self, obj, value=True):
        obj.muted = value

    def volume_get_all_chans(self, obj):
        return obj.volume

    def volume_set_all_chans(self, obj, value):
        obj.volume = value


def note(type_, channel, number, velocity=127):
    return types.SimpleNamespace(type=type_, channel=channel, note=number, velocity=velocity)


def cc(channel, control, value=64):
    return types.SimpleNamespace(type="control_change", channel=channel, control=control, value=value)
