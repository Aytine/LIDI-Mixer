import numpy as np

from midi_mixer import vumeter
from midi_mixer.vumeter import GroupMeters, group_column, level_of
from conftest import FakePulse, FakeStream


def test_level_follows_loudness():
    assert level_of(np.zeros(256)) == 0.0
    quiet, loud = level_of(0.01 * np.ones(256)), level_of(0.3 * np.ones(256))
    assert 0 < quiet < loud < 1
    assert level_of(np.ones(256)) == 1.0


def test_a_group_sits_in_the_column_of_its_fader_channel():
    assert group_column({"volume": "1:7"}) == 0 and group_column({"volume": "8:7"}) == 7
    assert group_column({"volume": "9:7"}) is None and group_column({"volume": "7"}) is None
    assert group_column({}) is None


def test_meters_follow_the_streams_of_the_groups_apps(monkeypatch):
    created = []

    class FakeMeter:
        def __init__(self, index):
            self.index, self.level, self.stopped = index, 0.0, False
            created.append(self)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(vumeter, "StreamMeter", FakeMeter)
    spotify, firefox, other = FakeStream("Spotify"), FakeStream("firefox"), FakeStream("Other")
    for i, stream in enumerate((spotify, firefox, other), start=10):
        stream.index = i
    groups = [{"app": "Spotify", "volume": "1:7"}, {"app": "firefox", "volume": "3:7"},
              {"app": "", "volume": "2:7"}, {"app": "Missing", "volume": "4:7"}, {"app": "Other", "volume": ""}]
    meters = GroupMeters()
    meters.sync(FakePulse(spotify, firefox, other), groups)
    assert sorted(m.index for m in created) == [10, 11]          # no group app / unknown app / no fader: no meter
    created[0].level, created[1].level = 0.4, 0.9
    bars = meters.bars
    assert bars[0] == 0.4 and bars[2] == 0.9 and bars[1] == bars[3] == 0

    meters.sync(FakePulse(firefox), groups)                      # Spotify closed
    assert created[0].stopped and not created[1].stopped
    meters.stop()
    assert created[1].stopped and not meters.bars.any()
