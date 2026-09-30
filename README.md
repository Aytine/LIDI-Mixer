# MIDI Audio Mixer - APC Mini Edition

A lightweight audio mixer for controlling PulseAudio volumes using MIDI controllers (specifically APC Mini).

## Features

- Control system audio volumes via MIDI input
- Dynamic application detection
- Custom MIDI-to-audio mappings
- Learning mode for easy MIDI control assignment
- Persistent configuration storage
- System tray icon (open / start-stop / quit); closing the window keeps the mixer running in the tray
- Launches at session start (XDG autostart, toggle in the tray menu) and auto-starts with an APC40
- Styled volume popup with fade-out
- **Groups**: a strip = one app + its fader (CC), Mute button and Assign button (notes).
  Pressing Assign gives the group the app that currently has the focus (X11); the app can also be
  picked from a dropdown. Defaults are the 8 APC40 columns (fader CC 7, S / Solo-Cue note 49 = mute,
  top clip-grid pad note 0 = assign, channel = column); every control can be re-learned. Buttons are toggles (each press = one note_on *or* note_off,
  both count as a press) the Mute LED is lit while the group is muted, and the Assign pad is lit (in a per-group color)
  only while an app is assigned to the group.
  The list of running audio apps refreshes by itself every 3 s.

## Requirements

- Python 3.x
- PySide6 (PySide6-Essentials)
- mido
- python-rtmidi
- pulsectl
- python-xlib (focused-window detection for Assign; X11 only)

## Installation

```bash
git clone <repo-url>
cd MIDI_Mixer
chmod +x ./mixer.sh
./mixer.sh
```

On first launch an autostart entry is created in `~/.config/autostart/midi-mixer.desktop`
(disable it from the tray menu).

## Note

⚠️ **This project is vibe coded** 

---

Enjoy mixing!


## Project layout

```
main.py                 entry point (single instance, Qt application)
midi_mixer/
  constants.py          defaults (ports, APC40 controls, LED colors)
  midi_utils.py         CC/note keys, message kinds          (pure, no Qt)
  audio.py              PulseAudio helpers: matching, mute   (pure, no Qt)
  focus.py              focused X11 window -> app name       (pure, no Qt)
  config.py             load/save ~/.config/midi-mixer, group defaults
  autostart.py          XDG autostart entry
  engine.py             MIDI worker threads, mute/assign/LED logic
  theme.py              colors, stylesheet, icons
  widgets.py            volume overlay, group panel/list, mapping row
  window.py             main window + tray
tests/                  pytest suite (runs headless)
```

## Development

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```
