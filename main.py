import customtkinter as ctk
import mido
import pulsectl
import threading
import json
import os
import time
import re
import tempfile
import logging
import tkinter as tk
from tkinter import messagebox, ttk


class MidiMixerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("MIDI Audio Mixer - APC Mini Edition")
        self.geometry("800x600")
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("MidiMixerApp")

        self.pulse = pulsectl.Pulse('midi-mixer-ui')
        self.is_running = False
        self.config_file = self._get_config_path()
        self.mappings = self.load_config()
        self.midi_thread = None
        self.learning_index = None
        
        self.detected_apps = self.get_running_apps()

        # --- UI SETUP ---
        self.grid_columnconfigure(0, weight=1)

        ports = mido.get_input_names() or ["Aucun port détecté"]
        self.port_menu = ctk.CTkOptionMenu(self, values=ports)
        self.port_menu.grid(row=0, column=0, padx=20, pady=10, sticky="ew")
        # restore previously selected port if available
        try:
            saved = getattr(self, '_saved_port', None)
            if saved and saved in ports:
                self.port_menu.set(saved)
        except Exception:
            pass

        self.btn_refresh = ctk.CTkButton(self, text="Rafraîchir les applications audio", command=self.refresh_app_lists)
        self.btn_refresh.grid(row=1, column=0, padx=20, pady=5)

        # debug button removed

        self.mapping_frame = ctk.CTkScrollableFrame(self, label_text="CC MIDI | Application cible | Apprentissage")
        self.mapping_frame.grid(row=2, column=0, padx=20, pady=10, sticky="nsew")
        # allow columns inside the mapping frame to size appropriately
        try:
            self.mapping_frame.grid_columnconfigure(0, minsize=180)
            self.mapping_frame.grid_columnconfigure(1, weight=1)
        except Exception:
            pass
        self.grid_rowconfigure(2, weight=1)

        self.entries = [] 
        self.render_mappings()

        self.btn_add = ctk.CTkButton(self, text="+ Ajouter une assignation", command=self.add_mapping_row)
        self.btn_add.grid(row=3, column=0, padx=20, pady=5)

        self.btn_run = ctk.CTkButton(self, text="Démarrer le Mixer", fg_color="#2ecc71", command=self.toggle_mixer)
        self.btn_run.grid(row=4, column=0, padx=20, pady=20)

    def get_running_apps(self):
        apps = ["MASTER"]
        try:
            for sink_input in self.pulse.sink_input_list():
                props = getattr(sink_input, 'proplist', {}) or {}
                # Prefer human-friendly names but fall back to process/binary or media name
                candidates = [
                    props.get('application.name'),
                    props.get('application.process.binary'),
                    props.get('media.name'),
                ]
                name = None
                for c in candidates:
                    if c:
                        name = c
                        break

                # include PID/binary info to disambiguate (useful for browsers)
                pid = props.get('application.process.id') or props.get('application.pid')
                binary = props.get('application.process.binary')
                if not name:
                    # fallback to a generated label with sink index
                    idx = getattr(sink_input, 'index', None)
                    name = f"sink_input_{idx or 'unknown'}"

                if pid or binary:
                    suffix = []
                    if binary:
                        suffix.append(binary)
                    if pid:
                        suffix.append(f"pid:{pid}")
                    display = f"{name} ({', '.join(suffix)})"
                else:
                    display = name

                if display and display not in apps:
                    apps.append(display)
            # no media action tokens here (handled externally by user)
        except Exception as e:
            print(f"Erreur de lecture audio : {e}")
        return sorted(apps)

    def _get_display_name(self, si):
        """Return a short display name for a sink_input `si`."""
        props = getattr(si, 'proplist', {}) or {}
        for key in ('application.name', 'application.process.binary', 'media.name'):
            v = props.get(key)
            if v:
                return v
        idx = getattr(si, 'index', None)
        return f"sink_input_{idx or 'unknown'}"

    def show_error(self, title: str, msg: str):
        logging.exception(msg)
        try:
            self.after(0, lambda: messagebox.showerror(title, msg))
        except Exception:
            pass

    def apply_volume(self, target: str, val: float):
        """Apply volume `val` (0..1) to `target` which may be MASTER or a sink_input."""
        try:
            if target == "MASTER":
                sink_name = self.pulse.server_info().default_sink_name
                for s in self.pulse.sink_list():
                    if s.name == sink_name:
                        self.pulse.volume_set_all_chans(s, val)
                        # show overlay in UI thread
                        try:
                            self.after(0, lambda: self.show_volume_overlay("MASTER", val))
                        except Exception:
                            pass
                        return True
            else:
                for si in self.pulse.sink_input_list():
                    if self._si_matches_target(si, target):
                        self.pulse.volume_set_all_chans(si, val)
                        try:
                            display = self._get_display_name(si)
                            self.after(0, lambda d=display, v=val: self.show_volume_overlay(d, v))
                        except Exception:
                            pass
                        return True
        except Exception as e:
            self.show_error("PulseAudio error", f"Failed to set volume: {e}")
        return False

    def show_volume_overlay(self, target: str, val: float, timeout_ms: int = 1600):
        """Show a temporary overlay with a progress bar for the given target.

        Runs on the main/UI thread.
        """
        try:
            # create overlay window if needed
            if getattr(self, '_vol_overlay', None) is None or not getattr(self, '_vol_overlay', None).winfo_exists():
                ov = tk.Toplevel(self)
                ov.overrideredirect(True)
                ov.attributes('-topmost', True)
                frm = ttk.Frame(ov, relief='raised', borderwidth=1)
                frm.pack(fill='both', expand=True)
                lbl = ttk.Label(frm, text='')
                lbl.pack(side='top', padx=8, pady=(6,2))
                pb = ttk.Progressbar(frm, orient='horizontal', length=260, mode='determinate')
                pb.pack(side='bottom', padx=8, pady=(2,8))
                self._vol_overlay = ov
                self._vol_overlay_lbl = lbl
                self._vol_overlay_pb = pb
            else:
                ov = self._vol_overlay

            # update content
            txt = f"{target}: {int(val*100)}%"
            try:
                self._vol_overlay_lbl.config(text=txt)
                self._vol_overlay_pb['value'] = int(val*100)
            except Exception:
                pass

            # place overlay bottom-right of main window
            try:
                self.update_idletasks()
                rw = self.winfo_width()
                rh = self.winfo_height()
                rx = self.winfo_rootx()
                ry = self.winfo_rooty()
                ow = 300
                oh = 60
                x = rx + max(10, rw - ow - 20)
                y = ry + max(10, rh - oh - 20)
                ov.geometry(f"{ow}x{oh}+{x}+{y}")
            except Exception:
                pass

            # show overlay and schedule hide
            try:
                ov.deiconify()
                if getattr(self, '_vol_overlay_after', None):
                    try:
                        self.after_cancel(self._vol_overlay_after)
                    except Exception:
                        pass
                self._vol_overlay_after = self.after(timeout_ms, lambda: self._hide_volume_overlay())
            except Exception:
                pass
        except Exception as e:
            logging.exception("Failed to show volume overlay: %s", e)

    def _hide_volume_overlay(self):
        try:
            if getattr(self, '_vol_overlay', None):
                try:
                    self._vol_overlay.destroy()
                except Exception:
                    pass
                self._vol_overlay = None
                self._vol_overlay_lbl = None
                self._vol_overlay_pb = None
        except Exception:
            pass

    # debug functions removed

    def _si_matches_target(self, si, target: str) -> bool:
        """Return True if sink input `si` matches the selected target label.

        Matching uses several proplist keys and is case-insensitive. It also
        supports targets that include a PID (e.g. "Spotify (spotify, pid:1234)").
        """
        try:
            props = getattr(si, 'proplist', {}) or {}
            target_l = (target or "").lower()

            app_name = (props.get('application.name') or "").lower()
            proc_bin = (props.get('application.process.binary') or "").lower()
            media = (props.get('media.name') or "").lower()
            pid = str(props.get('application.process.id') or props.get('application.pid') or "")

            # If the target contains a pid:123 pattern, match by pid
            m = re.search(r'pid[: ]*(\d+)', target_l)
            if m:
                if pid and pid == m.group(1):
                    return True

            # direct equality checks
            if target_l == app_name or target_l == proc_bin or target_l == media:
                return True

            # partial contains checks (useful when target was saved as a display label)
            if app_name and app_name in target_l:
                return True
            if proc_bin and proc_bin in target_l:
                return True
            if media and media in target_l:
                return True

            # also allow target (display) to contain parts of the proplist
            display = target_l
            if app_name and app_name in display:
                return True
        except Exception:
            return False
        return False

    # media actions removed; user will handle them via system shortcuts

    def _get_config_path(self):
        d = os.path.join(os.path.expanduser("~"), ".config", "midi-mixer")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return os.path.join(d, "midi_config.json")

    def refresh_app_lists(self):
        self.detected_apps = self.get_running_apps()
        for entry in self.entries:
            try:
                combo = entry[1]
                current_val = combo.get()
                combo.configure(values=self.detected_apps)
                combo.set(current_val)
            except Exception:
                pass

    def render_mappings(self):
        for entry_set in self.entries:
            for widget in entry_set: widget.destroy()
        self.entries = []
        for i, (cc, app) in enumerate(self.mappings.items()):
            self.create_row(i, cc, app)

    def _remove_row(self, index: int):
        try:
            entry = self.entries[index]
        except Exception:
            return
        # destroy widgets
        for w in entry:
            try:
                w.destroy()
            except Exception:
                pass
        # remove from list and re-render
        try:
            self.entries.pop(index)
        except Exception:
            pass
        # reset learning index if needed
        if self.learning_index is not None and self.learning_index == index:
            self.learning_index = None
        # re-render to update indices and layout
        self.save_config()
        self.render_mappings()

    def create_row(self, row_idx, cc_val, app_val):
        # wider CC field so full learned text is visible (e.g. "CC 56 (ch 1)")
        cc_entry = ctk.CTkEntry(self.mapping_frame, width=160)
        cc_entry.insert(0, str(cc_val))
        cc_entry.grid(row=row_idx, column=0, padx=5, pady=5, sticky="w")

        app_combo = ctk.CTkComboBox(self.mapping_frame, values=self.detected_apps, width=420)
        app_combo.set(app_val if app_val in self.detected_apps else app_val)
        app_combo.grid(row=row_idx, column=1, padx=5, pady=5, sticky="ew")
        
        idx = len(self.entries)
        learn_btn = ctk.CTkButton(self.mapping_frame, text="Learn", width=80, 
                                  command=lambda i=idx: self.start_learning(i))
        learn_btn.grid(row=row_idx, column=2, padx=5, pady=5)
        remove_btn = ctk.CTkButton(self.mapping_frame, text="Remove", width=80,
                                   command=lambda i=idx: self._remove_row(i))
        remove_btn.grid(row=row_idx, column=3, padx=5, pady=5)

        self.entries.append((cc_entry, app_combo, learn_btn, remove_btn))

    def add_mapping_row(self):
        self.create_row(len(self.entries), "", "MASTER")

    def start_learning(self, index):
        self.learning_index = index
        try:
            self.entries[index][2].configure(text="En attente...", fg_color="#e67e22")
        except Exception:
            pass
        if not self.is_running:
            threading.Thread(target=self.quick_learn_listen, daemon=True).start()

    def update_ui_after_learning(self, cc_number):
        # Accept either a mido.Message-like object or an int control number
        if self.learning_index is not None:
            cc_entry, _, btn, _ = self.entries[self.learning_index]
            # determine control and optional channel
            control = None
            channel = None
            try:
                if hasattr(cc_number, 'control'):
                    control = int(cc_number.control)
                    channel = getattr(cc_number, 'channel', None)
                else:
                    control = int(cc_number)
            except Exception:
                control = None

            if control is not None:
                if channel is not None:
                    display = f"CC {control} (ch {channel+1})"
                else:
                    display = str(control)
                cc_entry.delete(0, 'end')
                cc_entry.insert(0, display)
            try:
                btn.configure(text="Learn", fg_color=["#3a7ebf", "#1f538d"])
            except Exception:
                pass
            self.learning_index = None
            self.save_config()

    def quick_learn_listen(self):
        port = self.port_menu.get()
        if not port or "Aucun" in port:
            self.after(0, lambda: messagebox.showwarning("MIDI", "Aucun port MIDI sélectionné"))
            # restore button text
            if self.learning_index is not None:
                try:
                    _, _, btn, _ = self.entries[self.learning_index]
                    btn.configure(text="Learn")
                except Exception:
                    pass
            self.learning_index = None
            return

        try:
            with mido.open_input(port) as inport:
                t0 = time.time()
                while time.time() - t0 < 8:
                    for msg in inport.iter_pending():
                        if msg.type == 'control_change':
                            # pass the full message so we can display channel if present
                            self.after(0, lambda m=msg: self.update_ui_after_learning(m))
                            return
                    time.sleep(0.01)
        except Exception as e:
            logging.exception("Learn listener error: %s", e)
            self.after(0, lambda: messagebox.showerror("Erreur MIDI", f"Erreur pendant apprentissage: {e}"))
            # restore button appearance
            if self.learning_index is not None:
                try:
                    _, _, btn, _ = self.entries[self.learning_index]
                    btn.configure(text="Learn")
                except Exception:
                    pass
                self.learning_index = None

    def load_config(self):
        # Load config file which may contain {'mappings': {...}, '_port': 'name'}
        self._saved_port = None
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # support legacy where file directly contains mappings
                    if 'mappings' in data:
                        self._saved_port = data.get('_port')
                        return data.get('mappings', {})
                    else:
                        # treat whole dict as mappings
                        return data
            except Exception as e:
                logging.exception("Failed to load config: %s", e)
        # default mapping
        return {"56": "MASTER"}

    def save_config(self):
        new_map = {}
        invalid = []
        for i, entry in enumerate(self.entries):
            # entries now are (cc_entry, app_combo, learn_btn, remove_btn)
            try:
                cc_e, app_c = entry[0], entry[1]
            except Exception:
                continue
            cc, app = cc_e.get().strip(), app_c.get().strip()
            if not cc or not app:
                continue
            try:
                # accept fields like "CC 56 (ch 1)" or "ch1:56" — extract first integer
                m = re.search(r"(\d{1,3})", cc)
                if not m:
                    invalid.append((i + 1, cc))
                    continue
                n = int(m.group(1))
                if not (0 <= n <= 127):
                    invalid.append((i + 1, cc))
                    continue
            except Exception:
                invalid.append((i + 1, cc))
                continue
            new_map[str(n)] = app
        if invalid:
            msg = "Lignes avec CC invalides: " + ", ".join([f"#{r}('{v}')" for r, v in invalid])
            messagebox.showerror("Validation", msg)
            return
        self.mappings = new_map
        # atomic write including selected port
        cfg = { 'mappings': self.mappings, '_port': None }
        try:
            cfg['_port'] = self.port_menu.get()
        except Exception:
            cfg['_port'] = None

        d = os.path.dirname(self.config_file)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=d)
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f)
            os.replace(tmp, self.config_file)
        finally:
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def toggle_mixer(self):
        if not self.is_running:
            self.save_config()
            self.is_running = True
            self.btn_run.configure(text="Arrêter le Mixer", fg_color="#e74c3c")
            # start a non-blocking midi worker thread
            self.midi_thread = threading.Thread(target=self.midi_worker, daemon=True)
            self.midi_thread.start()
        else:
            self.is_running = False
            self.btn_run.configure(text="Démarrer le Mixer", fg_color="#2ecc71")

    def midi_worker(self):
        port = self.port_menu.get()
        if not port or "Aucun" in port:
            self.after(0, lambda: messagebox.showwarning("MIDI", "Aucun port MIDI sélectionné"))
            self.is_running = False
            self.after(0, lambda: self.btn_run.configure(text="Démarrer le Mixer", fg_color="#2ecc71"))
            return

        try:
            with mido.open_input(port) as inport:
                while self.is_running:
                    for msg in inport.iter_pending():
                        if msg.type != 'control_change':
                            continue
                        if self.learning_index is not None:
                            self.after(0, lambda m=msg: self.update_ui_after_learning(m))
                            continue
                        cc_str = str(msg.control)
                        if cc_str in self.mappings:
                            target = self.mappings[cc_str]
                            val = msg.value / 127.0
                            try:
                                # apply volume via helper
                                self.apply_volume(target, val)
                            except Exception as e:
                                logging.exception("PulseAudio error: %s", e)
                    time.sleep(0.01)
        except Exception as e:
            logging.exception("MIDI worker error: %s", e)
            self.after(0, lambda: messagebox.showerror("MIDI", f"Erreur d'écoute MIDI : {e}"))
            self.is_running = False
            self.after(0, lambda: self.btn_run.configure(text="Démarrer le Mixer", fg_color="#2ecc71"))

if __name__ == "__main__":
    app = MidiMixerApp()
    app.mainloop()