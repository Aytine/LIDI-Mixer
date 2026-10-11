# MIDI Mixer

Control the volume of your applications (PulseAudio / PipeWire) with any MIDI controller: faders, mute
buttons, and LEDs that show what is assigned. Everything that depends on the controller lives in a
**profile** (a small JSON file), so the same app works with an APC40, a nanoKONTROL, a generic pad
controller... and you can switch between them.

> ⚠️ This project is vibe coded.

## Features

- **Groups**: one strip = one application + its fader, Mute button and Assign button. Pressing Assign on the
  controller gives the group the application that currently has the focus (X11); you can also pick the
  application from a dropdown.
- **Launch button**: each group can have a MIDI button that starts the group's application (found through its
  `.desktop` launcher, Flatpak and Snap included, or through an optional command you set on the group). Nothing
  is started if the application is already running (it has an audio stream). Pick the button with *Learn*.
- **Direct mappings**: a CC controls one application (or MASTER) without going through a group.
- **Learn mode**: click *Learn*, move a fader / press a button, done.
- **Profiles**: one per controller, switch from the window or the tray icon; import / export to share them.
  The app switches by itself when you plug another controller.
- **LEDs** (when the controller supports it): Mute LED lit while muted. The pads are left free for animations.
- **Pad animations**: if the profile describes a pad grid (APC40 mkII: 8 x 5), the *Pad* selector plays a pixel-art
  animation on it in a loop: Pac-Man, spiral, rain, heart, wave, bouncing ball. They are defined in
  `midi_mixer/animations.py` (a frame is 5 strings of 8 characters, one letter per color).
- Tray icon, closes to the tray, optional launch at session start, styled volume popup, live changes (no
  restart needed), the list of running audio apps refreshes by itself.

## Requirements

- Linux with PulseAudio or PipeWire (pulse compatible)
- Python 3.10+
- PySide6, mido, python-rtmidi, pulsectl, python-xlib (focused-window detection, X11 only)

## Installation

```bash
git clone <repo-url>
cd MIDI_Mixer
chmod +x ./mixer.sh
./mixer.sh
```

On first launch the app creates a profile: the bundled one matching a connected controller if it knows it
(currently APC40 mkII), otherwise a **generic** profile where you assign every control with *Learn*. An autostart
entry is created in `~/.config/autostart/midi-mixer.desktop` (toggle it from the tray menu).

## Profiles

Profiles live in `~/.config/midi-mixer/profiles/<id>.json` and the active one is stored in
`~/.config/midi-mixer/settings.json`. Use **Gérer ▾** next to the profile selector to create (from a bundled
template), duplicate, rename, delete, import and export profiles. Exported files do not contain your MIDI port
name, which is machine specific.

A profile is plain JSON; to support a new controller, create a profile with the generic template, set it up
with *Learn*, then export it (or write the file by hand):

```jsonc
{
  "version": 1,
  "name": "My controller",
  "port_hint": "nanokontrol",        // part of the MIDI port name, used to recognise the controller
  "buttons": {
    "message": "note",               // "note" or "cc": what Mute / Assign buttons send
    "trigger": "press"               // "press": note_on (CC > 0) only; "any": every message is a press,
  },                                 //          for toggle buttons that alternate note_on / note_off
  "leds": {                          // omit when the controller has no LEDs you can drive
    "message": "note",               // LEDs are set with "note" (velocity) or "cc" (value) messages
    "on": 127, "off": 0,
    "default_color": "Green",
    "colors": { "Green": {"velocity": 21, "hex": "#2ecc71"}, "Red": {"velocity": 5, "hex": "#e74c3c"} }
  },
  "pad": {                           // optional pad grid for animations: the pad at row r (0 = top), column c is
    "rows": 5, "cols": 8,            //   note (note + r*note_step + c*note_col_step) on channel (channel + c*channel_step)
    "channel": 1, "channel_step": 0, "note": 32, "note_step": -8, "note_col_step": 1,
    "colors": {"Y": 13, "R": 5}      // animation color letter -> LED velocity (see animations.py)
  },
  "group_template": {                // where group N's controls are; new groups follow it
    "count": 8,
    "volume": {"number": 7,  "channel": 1, "channel_step": 1},   // group i: channel 1+i, CC 7
    "mute":   {"number": 49, "channel": 1, "channel_step": 1},
    "assign": {"number": 0,  "channel": 1, "channel_step": 1},   // also "number_step"; omit "channel" = any channel
    "launch": {"number": 40, "channel": 1, "channel_step": 1}    // optional, like every control (none = use Learn)
  },
  "mappings": {"14": "MASTER"},      // direct CC mappings: "cc" or "channel:cc" -> application
  "groups": []                       // your groups (filled by the app)
}
```

Limits: faders must be CC messages (pitch-bend faders, as on Mackie-style surfaces, are not supported yet);
the focused-application lookup for Assign needs X11.

## Project layout

```
main.py                 entry point (single instance, Qt application)
midi_mixer/
  constants.py          app constants (nothing controller specific)
  animations.py         pad grid animations (frames)                             (pure, no Qt)
  profile.py            Profile model: what a controller is and how it behaves   (pure, no Qt)
  profiles/*.json       bundled profile templates (APC40 mkII, generic)
  config.py             settings + profile files, import/export, migration       (pure, no Qt)
  midi_utils.py         CC/note keys, message kinds                              (pure, no Qt)
  audio.py              PulseAudio helpers: matching, mute                       (pure, no Qt)
  focus.py              focused X11 window -> app name                           (pure, no Qt)
  autostart.py          XDG autostart entry
  engine.py             MIDI worker threads, mute/assign/LED logic
  theme.py              colors, stylesheet, icons
  widgets.py            volume overlay, group panel/list, profile bar, mapping row
  window.py             main window + tray
tests/                  pytest suite (runs headless)
```

## Development

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```
