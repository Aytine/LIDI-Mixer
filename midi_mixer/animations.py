"""Pixel-art animations for the controller's pad grid (pure: no Qt, no MIDI).

A frame is a tuple of ROWS strings of COLS characters. "." is an unlit pad; any other character is a color
key that the profile's `pad.colors` turns into a LED value:
Y yellow, R red, B blue, G green, W white, P pink, C cyan, O orange, V violet.
"""
import random
from dataclasses import dataclass

ROWS, COLS = 5, 8
OFF = "."


@dataclass(frozen=True)
class Animation:
    name: str
    label: str
    interval: float   # seconds per frame
    frames: tuple     # tuple of frames, played in a loop


@dataclass(frozen=True)
class LiveAnimation:
    """An animation whose frames are computed while it plays (no fixed `frames`); the engine knows how."""
    name: str
    label: str
    interval: float
    source: str                  # what feeds the bars (a key of engine.LIVE_SOURCES)
    colors: str = "GGYOR"        # color keys of a bar, from its bottom pad to its top pad (5 pads)
    frames = None


def _blank():
    return [[OFF] * COLS for _ in range(ROWS)]


def _freeze(grid):
    return tuple("".join(row) for row in grid)


def _put(grid, row, col, char):
    if 0 <= row < ROWS and 0 <= col < COLS:
        grid[row][col] = char


def _stamp(grid, sprite, row, col):
    """Draw `sprite` (rows of text, "." = transparent) with its top-left corner at (row, col); it may be cut off."""
    for r, line in enumerate(sprite):
        for c, char in enumerate(line):
            if char != OFF:
                _put(grid, row + r, col + c, char)


# ------------------------------------------------------------------- pac-man

PACMAN_OPEN = ("YY.", "Y..", "YY.")
PACMAN_CLOSED = ("YYY", "YYY", "YYY")


def _pacman():
    """Pac-Man crosses the grid left to right and eats the dots on his way."""
    dots = (1, 3, 5, 7)
    frames = []
    for step, x in enumerate(range(-3, COLS + 1)):
        grid = _blank()
        for dot in dots:
            if dot > x + 1:                 # not eaten yet (his mouth reaches x + 1)
                _put(grid, 2, dot, "W")
        _stamp(grid, PACMAN_OPEN if step % 2 == 0 else PACMAN_CLOSED, 1, x)
        frames.append(_freeze(grid))
    return Animation("pacman", "Pac-Man", 0.18, tuple(frames))


# -------------------------------------------------------------------- spiral

def _spiral_path():
    """Every cell of the grid, walking in a spiral from the top-left corner towards the center."""
    top, bottom, left, right = 0, ROWS - 1, 0, COLS - 1
    path = []
    while top <= bottom and left <= right:
        path += [(top, c) for c in range(left, right + 1)]
        path += [(r, right) for r in range(top + 1, bottom + 1)]
        if top < bottom:
            path += [(bottom, c) for c in range(right - 1, left - 1, -1)]
        if left < right:
            path += [(r, left) for r in range(bottom - 1, top, -1)]
        top, bottom, left, right = top + 1, bottom - 1, left + 1, right - 1
    return path


def _spiral():
    """A spiral that winds in towards the center, a bright head leading, then unwinds back out."""
    path = _spiral_path()
    colors = "CCCBBBVVV"                    # the fill changes color as it goes deeper
    frames = []
    for filled in range(1, len(path) + 1):
        grid = _blank()
        for i, (r, c) in enumerate(path[:filled]):
            grid[r][c] = colors[i * len(colors) // len(path)]
        r, c = path[filled - 1]
        grid[r][c] = "W"
        frames.append(_freeze(grid))
    frames += frames[-2::-1]                # unwinding = the same frames backwards
    return Animation("spiral", "Spirale", 0.07, tuple(frames))


# ---------------------------------------------------------------------- rain

def _rain():
    """Digital rain: green drops falling down each column at their own pace, white head, fading tail."""
    rng = random.Random(7)                  # fixed seed: the loop is always the same
    length = 24                             # frames per loop
    tail = 3
    drops = []                              # (column, start row, speed in frames per row)
    for col in range(COLS):
        drops.append((col, -rng.randrange(0, ROWS + 4), rng.choice((1, 2))))
    frames = []
    for t in range(length):
        grid = _blank()
        for col, start, slow in drops:
            head = start + t // slow
            head = (head + tail) % (ROWS + tail + 3) - tail     # wrap around: the drop starts again from the top
            for k in range(tail + 1):
                _put(grid, head - k, col, "W" if k == 0 else "G")
        frames.append(_freeze(grid))
    return Animation("rain", "Pluie", 0.12, tuple(frames))


# --------------------------------------------------------------------- heart

HEART_BIG = (".RR..RR.", "RRRRRRRR", ".RRRRRR.", "..RRRR..", "...RR...")
HEART_SMALL = ("........", "..R..R..", "..RRRR..", "...RR...", "........")


def _heart():
    """A beating heart: two quick beats, then a pause."""
    beat = (HEART_BIG, HEART_SMALL, HEART_BIG, HEART_SMALL)
    return Animation("heart", "Cœur", 0.16, beat + (HEART_SMALL,) * 4)


# ---------------------------------------------------------------------- wave

def _wave():
    """A sine wave scrolling to the left, cyan crest over a blue body."""
    heights = [2, 1, 0, 1, 2, 3, 4, 3]       # one period over the 8 columns (row of the crest)
    frames = []
    for shift in range(COLS):
        grid = _blank()
        for col in range(COLS):
            crest = heights[(col + shift) % COLS]
            _put(grid, crest, col, "C")
            for r in range(crest + 1, ROWS):
                _put(grid, r, col, "B")
        frames.append(_freeze(grid))
    return Animation("wave", "Vague", 0.12, tuple(frames))


# -------------------------------------------------------------------- bounce

def _bounce():
    """A ball bouncing off the edges with a short trail."""
    r, c, dr, dc = 0, 0, 1, 1
    trail, frames, seen = [], [], set()
    while (r, c, dr, dc) not in seen:       # stop when the ball is back on a position it already went through
        seen.add((r, c, dr, dc))
        grid = _blank()
        for age, (tr, tc) in enumerate(reversed(trail[-3:])):
            _put(grid, tr, tc, "V" if age == 0 else "B")
        _put(grid, r, c, "O")
        frames.append(_freeze(grid))
        trail.append((r, c))
        if not 0 <= r + dr < ROWS:
            dr = -dr
        if not 0 <= c + dc < COLS:
            dc = -dc
        r, c = r + dr, c + dc
    return Animation("bounce", "Balle", 0.1, tuple(frames))


ANIMATIONS = {a.name: a for a in (_pacman(), _spiral(), _rain(), _heart(), _wave(), _bounce())}

LIVE_ANIMATIONS = {a.name: a for a in (
    LiveAnimation("spectrum", "Spectre audio", 0.04, "spectrum"),
    LiveAnimation("spectrum_ocean", "Spectre audio (océan)", 0.04, "spectrum", colors="BBCCW"),
    LiveAnimation("spectrum_neon", "Spectre audio (néon)", 0.04, "spectrum", colors="VVPPW"),
    LiveAnimation("vu", "Niveaux des tranches", 0.04, "vu"),
)}
