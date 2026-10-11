import pytest

from midi_mixer.animations import ANIMATIONS, COLS, OFF, ROWS


@pytest.mark.parametrize("name", list(ANIMATIONS))
def test_every_frame_fits_the_grid(name):
    animation = ANIMATIONS[name]
    assert animation.frames and animation.interval > 0
    for frame in animation.frames:
        assert len(frame) == ROWS and all(len(row) == COLS for row in frame)


@pytest.mark.parametrize("name", list(ANIMATIONS))
def test_colors_exist_in_the_bundled_palette(name, apc_profile):
    used = {c for frame in ANIMATIONS[name].frames for row in frame for c in row} - {OFF}
    assert used and used <= set(apc_profile.pad["colors"])


@pytest.mark.parametrize("name", list(ANIMATIONS))
def test_animations_move(name):
    assert len(set(ANIMATIONS[name].frames)) > 1


def test_pacman_eats_the_dots_and_leaves_the_screen():
    frames = ANIMATIONS["pacman"].frames
    assert any("W" in row for row in frames[0])
    assert not any("W" in row or "Y" in row for row in frames[-1])


def test_live_animations_have_no_fixed_frames():
    from midi_mixer.animations import LIVE_ANIMATIONS

    assert LIVE_ANIMATIONS["spectrum"].frames is None and LIVE_ANIMATIONS["spectrum"].interval > 0


def test_live_animations_use_known_sources_and_palette_colors(apc_profile):
    from midi_mixer.animations import LIVE_ANIMATIONS
    from midi_mixer.engine import LIVE_SOURCES

    for animation in LIVE_ANIMATIONS.values():
        assert animation.source in LIVE_SOURCES
        assert len(animation.colors) == ROWS and set(animation.colors) <= set(apc_profile.pad["colors"])
    assert LIVE_ANIMATIONS["spectrum_ocean"].colors != LIVE_ANIMATIONS["spectrum"].colors
