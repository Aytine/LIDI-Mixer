import numpy as np

from midi_mixer import spectrum
from midi_mixer.animations import COLS, OFF, ROWS


def tone(freq, amplitude=0.5):
    t = np.arange(spectrum.WINDOW) / spectrum.RATE
    return amplitude * np.sin(2 * np.pi * freq * t)


def test_a_tone_lights_its_own_band():
    bands = [int(np.argmax(spectrum.band_db(tone(f)))) for f in (100, 400, 1500, 8000)]
    assert bands == sorted(bands) and len(set(bands)) == 4          # low to high, left to right
    assert bands[0] == 0 and bands[-1] == COLS - 1


def test_silence_stays_empty_and_a_sound_rises_whatever_its_volume():
    levels = spectrum.Levels()
    assert not levels.update(spectrum.band_db(np.zeros(spectrum.WINDOW))).any()
    for amplitude in (0.5, 0.01):                                   # loud, then 34 dB quieter: still visible
        levels = spectrum.Levels()
        bars = levels.update(spectrum.band_db(tone(1500, amplitude)))
        assert bars[5] > 0.99 and bars[0] < 0.2


def test_bars_fall_slowly():
    levels = spectrum.Levels()
    loud = levels.update(spectrum.band_db(tone(1500))).copy()
    after = levels.update(spectrum.band_db(np.zeros(spectrum.WINDOW)))
    assert 0 < after[5] < loud[5]


def test_bars_to_frame_draws_columns_from_the_bottom():
    frame = spectrum.bars_to_frame([0, 0.2, 0.4, 0.6, 0.8, 1, 0.7, 0.3])
    assert len(frame) == ROWS and all(len(row) == COLS for row in frame)
    heights = [sum(frame[r][c] != OFF for r in range(ROWS)) for c in range(COLS)]
    assert heights == [0, 1, 2, 3, 4, 5, 4, 2]
    assert frame[ROWS - 1][1] == "G" and frame[0][5] == "R"          # green at the bottom, red at the top
    assert all(frame[r][0] == OFF for r in range(ROWS))


def test_bars_can_use_another_palette():
    frame = spectrum.bars_to_frame([1] * COLS, "BBCCW")
    assert [row[0] for row in frame] == ["W", "C", "C", "B", "B"]
