"""MIDI listener + PulseAudio volume/mute control, with its worker threads."""
import logging
import threading
import time

import mido
import pulsectl
from PySide6.QtCore import QObject, Signal

from .animations import ANIMATIONS, LIVE_ANIMATIONS, OFF
from .audio import display_name, set_mute, si_matches_target
from .constants import LEARN_TIMEOUT_S
from .focus import focused_app_name, same_app
from .midi_utils import key_channel_number, key_matches, mapping_key, midi_number, msg_kind
from .profile import Profile
from .spectrum import SpectrumAnalyzer, bars_to_frame
from .vumeter import GroupMeters

logger = logging.getLogger(__name__)

VOLUME_TOLERANCE = 0.01  # a stream within 1% of the fader is left alone (also stops our own writes from looping)
LED_RESYNC_SECONDS = 3  # how often the LEDs are re-sent (the controller loses them after a suspend)


_UNSET = object()
LIVE_SOURCES = {"spectrum": SpectrumAnalyzer, "vu": GroupMeters}   # live animation -> what feeds its bars
FOCUS_POLL_SECONDS = 0.5   # how often the focused window is looked at (strip status LEDs)
VU_SYNC_SECONDS = 1.0   # how often the group meters look for new / vanished audio streams


class MixerEngine(QObject):
    """MIDI listener + PulseAudio volume control. Owns the worker/learn threads.

    Signals are emitted from worker threads; Qt queues them to receivers living
    in the UI thread. Every thread uses its own Pulse connection (pulsectl is
    not thread-safe).
    """

    volumeChanged = Signal(str, float, bool)  # name, volume 0..1, muted
    assignRequested = Signal(str)             # group uid: Assign button pressed
    launchRequested = Signal(str)             # group uid: Launch button pressed
    stateChanged = Signal(bool)
    learned = Signal(object, int, int)  # token, channel (1-16), control/note number
    learnEnded = Signal(object)         # token: timeout, error or cancel
    errorOccurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.profile = Profile()  # how this controller's buttons and LEDs behave; set by the window
        self.mappings = {}
        self.groups = []      # list of group dicts (see Profile.make_group)
        self.muted = set()    # uids of muted groups
        self.running = False
        self._thread = None
        self._learn_lock = threading.Lock()
        self._learn_token = None
        self._learn_deadline = 0.0
        self._learn_kinds = ("control_change",)
        self._out = None              # MIDI output to light the Mute buttons
        self._out_lock = threading.Lock()
        self.pad_animation = ""       # name in ANIMATIONS shown on the pad grid ("" = none); played by the worker
        self._pad_current = _UNSET    # Animation being played (None = none; _UNSET = grid not cleared yet)
        self._pad_index = 0           # next frame to show
        self._pad_next = 0.0          # when to show it
        self._pad_shown = {}          # (row, col) -> LED value last sent
        self._pad_frame = None        # frame currently on the pads
        self._focused = set()         # uids of the groups whose app has the focus
        self._live = None             # source of the live animation shown (see LIVE_SOURCES), else None
        self._live_sync = 0.0
        self._pad_resync = 0.0
        self._wanted = {}             # app target -> last fader volume (0..1), re-imposed when the app resets it
        self._wanted_lock = threading.Lock()

    def set_groups(self, groups):
        """Replace the groups; LEDs of buttons that are no longer used are switched off."""
        new = {g["uid"]: g for g in groups}
        for old in self.groups:
            g = new.get(old["uid"])
            if old.get("mute") and (g is None or g.get("mute") != old["mute"]):
                self._send_led(old["mute"], False)
            old_light = self.profile.indicator_key(old)
            if old_light and (g is None or self.profile.indicator_key(g) != old_light):
                self._send_value(old_light, 0)
        self.groups = groups
        self._sync_leds()

    def clear_mute(self, uid: str):
        """Forget a group's muted state (its app was re-targeted or the group removed) and unlight its LED."""
        self.muted.discard(uid)
        for g in self.groups:
            if g["uid"] == uid:
                self._send_led(g.get("mute", ""), False)

    # -- mute button LEDs ----------------------------------------------------

    def _open_output(self, port: str):
        """Open the MIDI output of the same device as input `port`, or None (no LEDs on this profile / no output)."""
        if not self.profile.has_leds:
            return None
        try:
            names = mido.get_output_names()
            name = port if port in names else next((n for n in names if self.profile.matches_port(n)), None)
            return mido.open_output(name) if name else None
        except Exception as e:
            logger.error("Sortie MIDI indisponible (LED désactivées) : %s", e)
            return None

    def _send_led(self, key: str, on: bool):
        """Light (the profile's plain 'on' value) or switch off the LED of the button at `key`."""
        leds = self.profile.leds
        if leds:
            self._send_value(key, leds["on"] if on else leds["off"])

    def _send_value(self, key: str, value: int):
        """Set the LED of the button at `key` to `value` (what the value means depends on the controller)."""
        leds = self.profile.leds
        target = key_channel_number(key) if key else None
        if not leds or target is None or self._out is None:
            return
        channel, number = target
        if leds["message"] == "cc":
            message = mido.Message("control_change", channel=channel, control=number, value=value)
        else:
            message = mido.Message("note_on", channel=channel, note=number, velocity=value)
        self._send(message)

    def _send(self, message):
        try:
            with self._out_lock:
                if self._out is not None:
                    self._out.send(message)
        except Exception as e:
            logger.error("Envoi LED impossible : %s", e)

    # -- pad grid animations ---------------------------------------------------

    def set_pad_animation(self, name: str):
        """Choose the animation shown on the pad grid ("" or unknown name = none); the worker applies it."""
        self.pad_animation = name if name in ANIMATIONS or name in LIVE_ANIMATIONS else ""

    def release_pad(self):
        """Switch the whole pad grid off right now (the controller is being released)."""
        self.pad_animation = ""
        self._stop_live()
        if self.profile.pad and self._out is not None:
            self._pad_clear()

    def _stop_live(self):
        live, self._live = self._live, None
        if live is not None:
            live.stop()

    def _pad_cells(self):
        """[(row, col, channel, note)] for every pad of the grid (empty without a grid)."""
        pad = self.profile.pad
        if not pad:
            return []
        return [(r, c, pad["channel"] + c * pad["channel_step"] - 1,
                 pad["note"] + r * pad["note_step"] + c * pad["note_col_step"])
                for r in range(pad["rows"]) for c in range(pad["cols"])]

    def _pad_show(self, frame, force: bool):
        """Light the pads like `frame` (rows of color keys); only the pads that changed unless `force`."""
        colors = self.profile.pad["colors"]
        for r, c, channel, number in self._pad_cells():
            char = frame[r][c] if frame and r < len(frame) and c < len(frame[r]) else OFF
            value = colors.get(char, self.profile.leds["on"] if self.profile.leds else 127) if char != OFF else 0
            if force or self._pad_shown.get((r, c), 0) != value:
                self._send(mido.Message("note_on", channel=channel, note=number, velocity=value))
                self._pad_shown[(r, c)] = value
        self._pad_frame = frame

    def _pad_clear(self):
        self._pad_show(None, force=True)
        self._pad_shown = {}

    def _pad_reset(self):
        """Forget what is on the pads (new connection): the next tick draws everything again."""
        self._pad_current, self._pad_shown, self._pad_frame, self._pad_next = _UNSET, {}, None, 0.0

    def _pad_tick(self, now: float, pulse=None):
        """Called by the worker loop: advance the pad animation when its next frame is due."""
        if not self.profile.pad or self._out is None:
            return
        animation = ANIMATIONS.get(self.pad_animation) or LIVE_ANIMATIONS.get(self.pad_animation)
        if animation is not self._pad_current:
            self._stop_live()
            if animation is not None and animation.frames is None:
                self._live = LIVE_SOURCES[animation.source]()
                self._live.start()
                self._live_sync = now
            self._pad_clear()
            self._pad_current, self._pad_index, self._pad_next = animation, 0, now
            self._pad_resync = now + LED_RESYNC_SECONDS
        if animation is None:
            return
        if now >= self._pad_next:
            if animation.frames is None:     # live animation: drawn from the sound being played
                if pulse is not None and now >= self._live_sync:
                    self._live.sync(pulse, self.groups, self.profile)
                    self._live_sync = now + VU_SYNC_SECONDS
                frame = bars_to_frame(self._live.bars, animation.colors)
            else:
                frame = animation.frames[self._pad_index % len(animation.frames)]
            self._pad_show(frame, force=False)
            self._pad_index += 1
            self._pad_next = now + animation.interval
        if now >= self._pad_resync:   # the controller forgets its LEDs after a suspend: draw them all again
            self._pad_show(self._pad_frame, force=True)
            self._pad_resync = now + LED_RESYNC_SECONDS

    def _sync_leds(self):
        """Mute LED lit = group muted. (The Assign pads are part of the pad grid, left to the animations.)"""
        if not self.profile.has_leds:
            return
        for g in self.groups:
            self._send_led(g.get("mute", ""), g["uid"] in self.muted)
            self._send_indicator(g)

    def _send_indicator(self, group):
        """Strip status LED: off = no app assigned, lit = assigned, `focused` value = its window has the focus."""
        spec = self.profile.indicator
        if spec is None:
            return
        if not group.get("app"):
            value = 0
        else:
            value = spec["focused"] if group["uid"] in self._focused else spec["assigned"]
        self._send_value(self.profile.indicator_key(group), value)

    def _poll_focus(self, pulse):
        """Which groups' app has the focus now; the strip LEDs are updated when that changes."""
        try:
            name = focused_app_name(pulse)
            focused = {g["uid"] for g in self.groups if g.get("app") and name and same_app(pulse, g["app"], name)}
        except Exception as e:
            logger.error("Détection de l'application au premier plan : %s", e)
            return
        if focused != self._focused:
            self._focused = focused
            for g in self.groups:
                self._send_indicator(g)

    # -- mixer -------------------------------------------------------------

    def start(self, port: str):
        old = self._thread
        if old is not None and old.is_alive():
            self.running = False
            old.join(timeout=1)
        self.running = True
        self._thread = threading.Thread(target=self._worker, args=(port,), daemon=True)
        self._thread.start()
        threading.Thread(target=self._watch_volumes, args=(self._thread,), daemon=True).start()
        self.stateChanged.emit(True)

    def stop(self):
        if self.running:
            self.running = False
            self.stateChanged.emit(False)

    def _lookup_target(self, channel: int, control: int):
        """App for a CC: direct mappings first, then the groups' faders.

        Within the direct mappings a channel-specific key ("1:56") wins over
        the any-channel one ("56").
        """
        mappings = self.mappings
        target = mappings.get(mapping_key(channel + 1, control)) or mappings.get(str(control))
        if target:
            return target
        for g in self.groups:
            if g.get("app") and key_matches(g.get("volume", ""), channel, control):
                return g["app"]
        return None

    def _button_press(self, msg):
        """None if `msg` is not a button message for this profile, else True/False: does it count as a press?"""
        trigger_any = self.profile.button_trigger == "any"
        if self.profile.buttons_message == "cc":
            if msg.type != "control_change":
                return None
            return trigger_any or msg.value > 0
        if msg.type not in ("note_on", "note_off"):
            return None
        return trigger_any or (msg.type == "note_on" and msg.velocity > 0)

    def _is_button_key(self, channel: int, number: int) -> bool:
        """CC buttons share the message type with faders: tell them apart by the configured keys."""
        return any(key_matches(g.get(field, ""), channel, number)
                   for g in self.groups for field in ("mute", "assign", "launch"))

    def _handle_button(self, pulse, channel: int, number: int):
        """A button was pressed: toggle a group's mute, ask the UI to assign the focused app or to launch the app."""
        for g in self.groups:
            if key_matches(g.get("assign", ""), channel, number):
                self.assignRequested.emit(g["uid"])
                return
            if key_matches(g.get("launch", ""), channel, number):
                self.launchRequested.emit(g["uid"])
                return
            if g.get("app") and key_matches(g.get("mute", ""), channel, number):
                muted = g["uid"] not in self.muted
                try:
                    result = set_mute(pulse, g["app"], muted)
                except Exception as e:
                    logger.exception("Failed to mute: %s", e)
                    return
                if result is None:
                    return  # the app is not playing anything right now
                if muted:
                    self.muted.add(g["uid"])
                else:
                    self.muted.discard(g["uid"])
                self._send_led(g["mute"], muted)  # lit = muted
                self.volumeChanged.emit(result[0], result[1], muted)
                return

    def _worker(self, port):
        me = threading.current_thread()
        try:
            with pulsectl.Pulse("midi-mixer-worker") as pulse, mido.open_input(port) as inport:
                self._out = self._open_output(port)
                self._pad_reset()
                self._sync_leds()
                next_led_sync = time.monotonic() + LED_RESYNC_SECONDS
                next_focus = 0.0
                while self.running and self._thread is me:
                    # The controller forgets its LEDs when it loses power (PC suspend) while
                    # staying plugged in, so no port change tells us: just re-send them regularly.
                    if time.monotonic() >= next_led_sync:
                        self._sync_leds()
                        next_led_sync = time.monotonic() + LED_RESYNC_SECONDS
                    if self.profile.indicator and time.monotonic() >= next_focus:
                        self._poll_focus(pulse)
                        next_focus = time.monotonic() + FOCUS_POLL_SECONDS
                    # Only the last value per control is applied, so a burst of
                    # messages from a fader does not flood PulseAudio.
                    latest = {}
                    for msg in inport.iter_pending():
                        number = midi_number(msg)
                        if number is None:
                            continue
                        if self._learn_step(msg, number):
                            continue
                        pressed = self._button_press(msg)
                        if pressed is not None and (msg.type != "control_change"
                                                    or self._is_button_key(msg.channel, number)):
                            if pressed:
                                self._handle_button(pulse, msg.channel, number)
                        elif msg.type == "control_change":
                            latest[(msg.channel, number)] = msg.value
                    self._learn_step()  # handles the learn timeout

                    for (channel, control), value in latest.items():
                        target = self._lookup_target(channel, control)
                        if target:
                            self._apply_volume(pulse, target, value / 127.0)
                    self._pad_tick(time.monotonic(), pulse)
                    time.sleep(0.01)
        except Exception as e:
            logger.exception("MIDI worker error: %s", e)
            self.errorOccurred.emit(f"Erreur d'écoute MIDI : {e}")
        finally:
            if self._thread is me:
                self._stop_live()
                if self.profile.pad and self._pad_shown:
                    self._pad_clear()
            with self._out_lock:
                out, self._out = self._out, None
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            # Died on its own (device unplugged, Pulse gone...) rather than being stopped.
            if self.running and self._thread is me:
                self.running = False
                self.stateChanged.emit(False)

    def _apply_volume(self, pulse, target: str, val: float):
        try:
            if target == "MASTER":
                sink = pulse.get_sink_by_name(pulse.server_info().default_sink_name)
                pulse.volume_set_all_chans(sink, val)
                self.volumeChanged.emit("MASTER", val, "MASTER" in self._muted_targets())
                return
            with self._wanted_lock:
                self._wanted[target] = val
            matched = [si for si in pulse.sink_input_list() if si_matches_target(si, target)]
            for si in matched:
                pulse.volume_set_all_chans(si, val)
            if matched:
                self.volumeChanged.emit(display_name(matched[0]), val, target in self._muted_targets())
        except Exception as e:
            logger.exception("Failed to set volume: %s", e)

    def _enforce_volumes(self, pulse):
        """Put back the fader volume on streams whose app changed it behind our back (Spotify does on each track)."""
        assigned = {g["app"] for g in self.groups if g.get("app")} | set(self.mappings.values())
        with self._wanted_lock:
            wanted = {t: v for t, v in self._wanted.items() if t in assigned and t != "MASTER"}
        if not wanted:
            return
        for si in pulse.sink_input_list():
            for target, val in wanted.items():
                if si_matches_target(si, target):
                    if abs(pulse.volume_get_all_chans(si) - val) > VOLUME_TOLERANCE:
                        pulse.volume_set_all_chans(si, val)
                    break

    def _watch_volumes(self, worker):
        """Re-apply the fader volume whenever a stream is created or changed while the mixer runs."""
        def on_event(ev):
            if ev.t in ("new", "change"):
                raise pulsectl.PulseLoopStop

        try:
            with pulsectl.Pulse("midi-mixer-watcher") as pulse:
                pulse.event_mask_set("sink_input")
                pulse.event_callback_set(on_event)
                while self.running and self._thread is worker:
                    pulse.event_listen(timeout=0.5)  # returns on PulseLoopStop or timeout
                    self._enforce_volumes(pulse)
        except Exception as e:
            logger.exception("Volume watcher error: %s", e)

    def _muted_targets(self):
        return {g.get("app") for g in self.groups if g["uid"] in self.muted}

    # -- learn -------------------------------------------------------------

    @property
    def learning_token(self):
        with self._learn_lock:
            return self._learn_token

    def start_learning(self, token, port: str, kinds=("control_change",)):
        """Capture the next message of one of `kinds` ("control_change" for faders, "note" for buttons)."""
        with self._learn_lock:
            self._learn_kinds = kinds
            self._learn_token = token
            self._learn_deadline = time.time() + LEARN_TIMEOUT_S
        # When the mixer runs its worker already owns the port and catches messages.
        if not self.running:
            threading.Thread(target=self._learn_listen, args=(port, token), daemon=True).start()

    def cancel_learning(self):
        with self._learn_lock:
            token, self._learn_token = self._learn_token, None
        return token

    def _learn_step(self, msg=None, number=None) -> bool:
        """Handle the learn timeout / capture a message. True if `msg` was consumed."""
        with self._learn_lock:
            token = self._learn_token
            if token is None:
                return False
            if msg is not None and msg_kind(msg) in self._learn_kinds:
                self._learn_token = None
                self.learned.emit(token, msg.channel + 1, number)
                return True
            if time.time() > self._learn_deadline:
                self._learn_token = None
                self.learnEnded.emit(token)
            return False

    def _learn_listen(self, port, token):
        try:
            with mido.open_input(port) as inport:
                while self.learning_token is token and not self.running:
                    for msg in inport.iter_pending():
                        number = midi_number(msg)
                        if number is not None and self._learn_step(msg, number):
                            return
                    self._learn_step()
                    time.sleep(0.01)
        except Exception as e:
            logger.exception("Learn listener error: %s", e)
            with self._learn_lock:
                if self._learn_token is token:
                    self._learn_token = None
            self.learnEnded.emit(token)
            self.errorOccurred.emit(f"Erreur pendant l'apprentissage : {e}")
