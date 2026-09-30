"""MIDI listener + PulseAudio volume/mute control, with its worker threads."""
import logging
import threading
import time

import mido
import pulsectl
from PySide6.QtCore import QObject, Signal

from .audio import display_name, set_mute, si_matches_target
from .constants import ASSIGN_COLORS, DEFAULT_ASSIGN_COLOR, DEFAULT_PORT_HINT, LEARN_TIMEOUT_S
from .midi_utils import key_channel_number, key_matches, mapping_key, midi_number, msg_kind

logger = logging.getLogger(__name__)


class MixerEngine(QObject):
    """MIDI listener + PulseAudio volume control. Owns the worker/learn threads.

    Signals are emitted from worker threads; Qt queues them to receivers living
    in the UI thread. Every thread uses its own Pulse connection (pulsectl is
    not thread-safe).
    """

    volumeChanged = Signal(str, float, bool)  # name, volume 0..1, muted
    assignRequested = Signal(str)             # group uid: Assign button pressed
    stateChanged = Signal(bool)
    learned = Signal(object, int, int)  # token, channel (1-16), control/note number
    learnEnded = Signal(object)         # token: timeout, error or cancel
    errorOccurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.mappings = {}
        self.groups = []      # list of group dicts (see default_group)
        self.muted = set()    # uids of muted groups
        self.running = False
        self._thread = None
        self._learn_lock = threading.Lock()
        self._learn_token = None
        self._learn_deadline = 0.0
        self._learn_kinds = ("control_change",)
        self._out = None              # MIDI output to light the Mute buttons
        self._out_lock = threading.Lock()

    def set_groups(self, groups):
        """Replace the groups; LEDs of buttons that are no longer used are switched off."""
        new = {g["uid"]: g for g in groups}
        for old in self.groups:
            g = new.get(old["uid"])
            for field in ("mute", "assign"):
                if old.get(field) and (g is None or g.get(field) != old[field]):
                    self._send_led(old[field], False)
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
        """Open the MIDI output matching input `port` (same device), or None."""
        try:
            names = mido.get_output_names()
            name = port if port in names else next((n for n in names if DEFAULT_PORT_HINT in n.lower()), None)
            return mido.open_output(name) if name else None
        except Exception as e:
            logger.error("Sortie MIDI indisponible (LED désactivées) : %s", e)
            return None

    def _send_led(self, key: str, on: bool, velocity: int = 127):
        target = key_channel_number(key) if key else None
        if target is None or self._out is None:
            return
        try:
            with self._out_lock:
                if self._out is not None:
                    self._out.send(mido.Message("note_on", channel=target[0], note=target[1],
                                                velocity=velocity if on else 0))
        except Exception as e:
            logger.error("Envoi LED impossible : %s", e)

    def _sync_leds(self):
        """Mute LED lit = group muted; Assign LED lit (in the group's color) = an app is assigned."""
        for g in self.groups:
            self._send_led(g.get("mute", ""), g["uid"] in self.muted)
            velocity = ASSIGN_COLORS.get(g.get("color"), ASSIGN_COLORS[DEFAULT_ASSIGN_COLOR])[0]
            self._send_led(g.get("assign", ""), bool(g.get("app")), velocity)

    # -- mixer -------------------------------------------------------------

    def start(self, port: str):
        old = self._thread
        if old is not None and old.is_alive():
            self.running = False
            old.join(timeout=1)
        self.running = True
        self._thread = threading.Thread(target=self._worker, args=(port,), daemon=True)
        self._thread.start()
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

    def _handle_note(self, pulse, channel: int, note: int):
        """A button was pressed: toggle a group's mute or ask the UI to assign the focused app."""
        for g in self.groups:
            if key_matches(g.get("assign", ""), channel, note):
                self.assignRequested.emit(g["uid"])
                return
            if g.get("app") and key_matches(g.get("mute", ""), channel, note):
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
                self._sync_leds()
                while self.running and self._thread is me:
                    # Only the last value per control is applied, so a burst of
                    # messages from a fader does not flood PulseAudio.
                    latest = {}
                    for msg in inport.iter_pending():
                        number = midi_number(msg)
                        if number is None:
                            continue
                        if self._learn_step(msg, number):
                            continue
                        if msg.type == "control_change":
                            latest[(msg.channel, number)] = msg.value
                        else:
                            self._handle_note(pulse, msg.channel, number)
                    self._learn_step()  # handles the learn timeout

                    for (channel, control), value in latest.items():
                        target = self._lookup_target(channel, control)
                        if target:
                            self._apply_volume(pulse, target, value / 127.0)
                    time.sleep(0.01)
        except Exception as e:
            logger.exception("MIDI worker error: %s", e)
            self.errorOccurred.emit(f"Erreur d'écoute MIDI : {e}")
        finally:
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
            matched = [si for si in pulse.sink_input_list() if si_matches_target(si, target)]
            for si in matched:
                pulse.volume_set_all_chans(si, val)
            if matched:
                self.volumeChanged.emit(display_name(matched[0]), val, target in self._muted_targets())
        except Exception as e:
            logger.exception("Failed to set volume: %s", e)

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
