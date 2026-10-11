"""Live audio spectrum of what the computer plays, for the pad grid (no Qt, no MIDI).

The sound is read from the monitor of the default output (`parec`, PulseAudio / PipeWire), cut into
frequency bands and turned into bar heights.
"""
import logging
import subprocess
import threading

import numpy as np

from .animations import COLS, OFF, ROWS

logger = logging.getLogger(__name__)

RATE = 24000                 # Hz: plenty for music, light to process
WINDOW = 1024                # samples per FFT (about 43 ms)
HOP = 512                    # new samples between two analyses
LOW_HZ, HIGH_HZ = 60, 10000  # the spectrum shown goes from LOW_HZ to HIGH_HZ, split in COLS log-spaced bands
RANGE_DB = 40                # the tallest bar is the loudest band, the bar is empty RANGE_DB below
SILENCE_DB = -55             # below this the bars stay empty, whatever the volume (no gain on background noise)
REF_FALL_DB = 0.1            # per analysis: how fast the reference level follows a quieter sound
TILT_DB = 1.5                # per band: music has far more bass than treble, the high bands get a boost
FALL = 0.85                  # per analysis: bars fall slowly, rise instantly
BAR_COLORS = "GGYOR"         # color of a bar from its bottom (index 0) to its top

_WINDOW_FUNCTION = np.hanning(WINDOW)
_EDGES = np.geomspace(LOW_HZ, HIGH_HZ, COLS + 1)
_BINS = np.fft.rfftfreq(WINDOW, 1 / RATE)
_BAND_BINS = [np.flatnonzero((_BINS >= lo) & (_BINS < hi)) for lo, hi in zip(_EDGES[:-1], _EDGES[1:])]
_BAND_BINS = [b if len(b) else np.array([int(np.argmin(abs(_BINS - lo)))]) for b, lo in zip(_BAND_BINS, _EDGES)]


def band_db(samples):
    """Level in dB of each of the COLS bands for WINDOW samples in -1..1."""
    magnitude = np.abs(np.fft.rfft(samples * _WINDOW_FUNCTION)) / (WINDOW / 4)
    db = np.array([20 * np.log10(magnitude[b].max() + 1e-9) for b in _BAND_BINS])
    return db + TILT_DB * np.arange(COLS)


class Levels:
    """Turns successive band levels (dB) into bar heights 0..1: automatic gain, fast rise, slow fall."""

    def __init__(self):
        self.reference = SILENCE_DB
        self.bars = np.zeros(COLS)

    def update(self, db):
        self.reference = max(self.reference - REF_FALL_DB, float(db.max()), SILENCE_DB)
        level = np.clip((db - (self.reference - RANGE_DB)) / RANGE_DB, 0.0, 1.0)
        self.bars = np.maximum(level, self.bars * FALL)
        return self.bars


def bars_to_frame(bars, colors=BAR_COLORS):
    """A pad frame (see animations.py): column c is lit from the bottom up to round(bars[c] * ROWS) pads.

    `colors` gives the color key of each pad of a bar, from the bottom one up."""
    grid = [[OFF] * COLS for _ in range(ROWS)]
    for col, height in enumerate(np.rint(np.asarray(bars) * ROWS).astype(int)):
        for k in range(min(int(height), ROWS)):
            grid[ROWS - 1 - k][col] = colors[k]
    return tuple("".join(row) for row in grid)


class SpectrumAnalyzer:
    """Reads the default output's monitor in a thread; `bars` always holds the latest heights."""

    def __init__(self):
        self.bars = np.zeros(COLS)
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def sync(self, pulse, groups):
        """Nothing to follow: the spectrum is that of the whole output (same interface as vumeter.GroupMeters)."""

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._thread = None

    def _run(self):
        try:
            proc = subprocess.Popen(
                ["parec", "-d", "@DEFAULT_MONITOR@", "--format=s16le", f"--rate={RATE}", "--channels=1",
                 "--latency-msec=30"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.error("Spectre audio indisponible (parec) : %s", e)
            return
        self._proc = proc
        levels = Levels()
        window = np.zeros(WINDOW)
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read(HOP * 2)
                if len(chunk) < HOP * 2:
                    break                      # parec ended (sound server gone)
                new = np.frombuffer(chunk, dtype="<i2").astype(np.float64) / 32768.0
                window = np.concatenate((window[HOP:], new))
                self.bars = levels.update(band_db(window))
        except Exception as e:
            logger.error("Spectre audio : %s", e)
        finally:
            self.bars = np.zeros(COLS)
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                pass
