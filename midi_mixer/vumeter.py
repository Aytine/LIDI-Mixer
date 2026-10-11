"""Live level of what each group's application plays, for the pad grid (no Qt, no MIDI).

Every audio stream of a group's application is read with `parec --monitor-stream`; the loudness of
the latest samples gives the bar of the column above that group's fader.
"""
import logging
import subprocess
import threading

import numpy as np

from .animations import COLS
from .audio import si_matches_target
from .midi_utils import key_channel_number

logger = logging.getLogger(__name__)

RATE = 8000        # Hz: only the loudness matters
CHUNK = 256        # samples per measure (32 ms)
FLOOR_DB = -50     # RMS level of an empty bar
CEIL_DB = -8       # RMS level of a full bar
FALL = 0.85        # per measure: bars rise instantly and fall slowly


def level_of(samples) -> float:
    """Bar height 0..1 for samples in -1..1, from their RMS loudness in dB."""
    rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
    db = 20 * np.log10(rms + 1e-9)
    return float(np.clip((db - FLOOR_DB) / (CEIL_DB - FLOOR_DB), 0.0, 1.0))


class StreamMeter:
    """Follows the loudness of one audio stream (a PulseAudio sink input) in a thread."""

    def __init__(self, index: int):
        self.index = index
        self.level = 0.0
        self._proc = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _run(self):
        try:
            proc = subprocess.Popen(
                ["parec", f"--monitor-stream={self.index}", "--format=s16le", f"--rate={RATE}", "--channels=1",
                 "--latency-msec=30"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.error("Vu-mètre indisponible (parec) : %s", e)
            return
        self._proc = proc
        try:
            while not self._stop.is_set():
                chunk = proc.stdout.read(CHUNK * 2)
                if len(chunk) < CHUNK * 2:
                    break                      # the stream is gone
                samples = np.frombuffer(chunk, dtype="<i2").astype(np.float64) / 32768.0
                self.level = max(level_of(samples), self.level * FALL)
        except Exception as e:
            logger.error("Vu-mètre : %s", e)
        finally:
            self.level = 0.0
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                pass


def group_column(group):
    """Pad column (0-based) of the fader strip a group is on, from its volume control's MIDI channel; else None."""
    target = key_channel_number(group.get("volume", ""))
    return target[0] if target and 0 <= target[0] < COLS else None


class GroupMeters:
    """One StreamMeter per audio stream of the groups' applications; `bars` = loudest stream of each column."""

    def __init__(self):
        self._meters = {}      # sink input index -> (column, StreamMeter)

    def start(self):
        pass

    def sync(self, pulse, groups):
        """Follow the streams the groups' applications have right now (call it every second or so)."""
        wanted = {}
        try:
            streams = pulse.sink_input_list()
        except Exception as e:
            logger.error("Vu-mètres : %s", e)
            return
        for group in groups:
            column = group_column(group)
            if column is None or not group.get("app"):
                continue
            for si in streams:
                if si_matches_target(si, group["app"]):
                    wanted[si.index] = column
        for index in list(self._meters):
            if wanted.get(index) != self._meters[index][0]:
                self._meters.pop(index)[1].stop()
        for index, column in wanted.items():
            if index not in self._meters:
                self._meters[index] = (column, StreamMeter(index))

    @property
    def bars(self):
        bars = np.zeros(COLS)
        for column, meter in self._meters.values():
            bars[column] = max(bars[column], meter.level)
        return bars

    def stop(self):
        meters, self._meters = self._meters, {}
        for _column, meter in meters.values():
            meter.stop()
