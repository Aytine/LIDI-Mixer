"""Main window: tabs, tray icon, wiring between the UI and the engine."""
import logging

import pulsectl
from PySide6.QtCore import QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QStackedWidget, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from .audio import list_running_apps, set_mute, target_base_name
from .autostart import is_autostart_enabled, set_autostart
from .config import (
    GENERIC_TEMPLATE, bootstrap, bundled_templates, create_profile, delete_profile, duplicate_profile,
    export_profile, import_profile, list_profiles, load_profile, save_profile, save_settings,
)
from .constants import APP_NAME, NO_PORT_LABEL, PORT_POLL_MS
from .engine import MixerEngine
from .focus import XLIB_AVAILABLE, focused_app_name
from .midi_utils import format_key, input_port_names, mapping_key, parse_cc_field
from .theme import GREEN, dot_icon, make_app_icon
from .widgets import GroupList, GroupRow, MappingRow, NoScrollComboBox, ProfileBar, VolumeOverlay

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, start_hidden: bool):
        super().__init__()
        self.resize(1020, 660)
        self.icon = make_app_icon()
        self.setWindowIcon(self.icon)

        self._pulse = None
        self._ports = None
        self._apps = ["MASTER"]
        self._apps_error = False
        self._user_stopped = False
        self._quitting = False
        self._tray_hint_shown = False
        self._last_error = None
        self.rows = []
        self.group_rows = []
        self.single_server = None

        self.engine = MixerEngine()
        self.engine.stateChanged.connect(self._on_state_changed)
        self.engine.volumeChanged.connect(self._on_volume)
        self.engine.assignRequested.connect(self._on_assign_requested)
        self.engine.learned.connect(self._on_learned)
        self.engine.learnEnded.connect(self._on_learn_ended)
        self.engine.errorOccurred.connect(self._on_error)

        self.overlay = VolumeOverlay()

        # Active profile: what the controller is and what the user configured for it.
        self.settings, self.profile_id = bootstrap(input_port_names())
        self.profile = load_profile(self.profile_id)

        self._build_ui()
        self._build_tray()

        # Keep the autostart entry in line with the saved preference (on by default).
        if self.settings["autostart"] != is_autostart_enabled():
            try:
                set_autostart(self.settings["autostart"])
            except OSError as e:
                logger.error("Autostart: %s", e)
        self.act_autostart.setChecked(is_autostart_enabled())

        self._refresh_apps()
        self._rebuild_profile_lists()
        self._load_profile_ui()
        self._refresh_ports(force=True)
        self._maybe_switch_profile(self._ports)
        self.port_timer = QTimer(self)
        self.port_timer.timeout.connect(self._poll)
        self.port_timer.start(PORT_POLL_MS)
        self._maybe_autostart()

        if not (start_hidden and self.tray.isVisible()):
            self.show()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)
        self.profile_bar = ProfileBar()
        self.profile_bar.profileSelected.connect(self._switch_profile)
        self.profile_bar.actionRequested.connect(self._on_profile_action)
        self.port_combo = NoScrollComboBox()
        self.port_combo.setToolTip("Port MIDI du contrôleur")
        self.port_combo.activated.connect(self._on_port_activated)
        self.btn_refresh = QPushButton("Rafraîchir les applications")
        self.btn_refresh.clicked.connect(self._refresh_apps)
        top.addWidget(self.profile_bar)
        top.addWidget(self.port_combo, 1)
        top.addWidget(self.btn_refresh)
        root.addLayout(top)

        tabs = QTabWidget()
        self._build_groups_tab(tabs)
        self.rows_layout = self._scroll_tab(
            tabs, "Assignations directes", "+ Ajouter une assignation",
            lambda: self._add_row("", "MASTER"),
            "CC MIDI   |   Application cible   |   Apprentissage")
        root.addWidget(tabs, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("Mixer arrêté")
        self.status.setObjectName("status")
        self.btn_run = QPushButton("Démarrer le Mixer")
        self.btn_run.setObjectName("run")
        self.btn_run.setProperty("running", False)
        self.btn_run.clicked.connect(self._toggle_mixer)
        bottom.addStretch(1)
        bottom.addWidget(self.status)
        bottom.addWidget(self.btn_run)
        root.addLayout(bottom)

    def _build_groups_tab(self, tabs):
        """Groups tab: the list of groups on the left, the selected group's settings on the right."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(8)
        hint = QLabel("Un groupe pilote UNE application. Sélectionne-le pour régler son application, "
                      "sa LED et ses boutons ; le bouton Assign du contrôleur lui attribue l'application "
                      "au premier plan. Glisse un groupe (ou utilise ▲ ▼) pour changer l'ordre.")
        hint.setObjectName("header")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        self.group_list = GroupList()
        self.group_list.setFixedWidth(250)
        self.group_list.setIconSize(QSize(16, 16))
        self.group_list.currentRowChanged.connect(self._on_group_selected)
        self.group_list.reordered.connect(self._on_groups_reordered)
        btn_add = QPushButton("+ Ajouter un groupe")
        btn_add.clicked.connect(lambda: self._add_group_row(self._new_group(), user=True))
        btn_up = QPushButton("▲")
        btn_down = QPushButton("▼")
        for btn, tip, delta in ((btn_up, "Monter le groupe", -1), (btn_down, "Descendre le groupe", 1)):
            btn.setToolTip(tip)
            btn.setFixedWidth(40)
            btn.clicked.connect(lambda _checked=False, d=delta: self._move_group(d))
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        add_row.addWidget(btn_add, 1)
        add_row.addWidget(btn_up)
        add_row.addWidget(btn_down)
        left.addWidget(self.group_list, 1)
        left.addLayout(add_row)
        self.group_stack = QStackedWidget()
        body.addLayout(left)
        body.addWidget(self.group_stack, 1)
        lay.addLayout(body, 1)
        tabs.addTab(page, "Groupes")

    def _scroll_tab(self, tabs, title, add_text, add_callback, hint):
        """Add a tab with a hint, a scrollable list and an add button; return the list layout."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 10, 0, 0)
        lay.setSpacing(8)
        label = QLabel(hint)
        label.setObjectName("header")
        label.setWordWrap(True)
        lay.addWidget(label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        rows_layout = QVBoxLayout(holder)
        rows_layout.setContentsMargins(4, 4, 4, 4)
        rows_layout.setSpacing(8)
        rows_layout.addStretch(1)
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)
        btn = QPushButton(add_text)
        btn.clicked.connect(add_callback)
        lay.addWidget(btn, 0, Qt.AlignLeft)
        tabs.addTab(page, title)
        return rows_layout

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.icon, self)
        menu = QMenu()
        act_open = QAction("Ouvrir", menu)
        act_open.triggered.connect(self._show_window)
        self.act_toggle = QAction("Démarrer le mixer", menu)
        self.act_toggle.triggered.connect(self._toggle_mixer)
        self.act_autostart = QAction("Lancer au démarrage de la session", menu)
        self.act_autostart.setCheckable(True)
        self.act_autostart.toggled.connect(self._on_autostart_toggled)
        act_quit = QAction("Quitter", menu)
        act_quit.triggered.connect(self._quit)
        self.tray_profiles = QMenu("Profil", menu)
        self._profile_actions = None
        menu.addAction(act_open)
        menu.addAction(self.act_toggle)
        menu.addMenu(self.tray_profiles)
        menu.addSeparator()
        menu.addAction(self.act_autostart)
        menu.addSeparator()
        menu.addAction(act_quit)
        self._tray_menu = menu  # keep a reference alive
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip(f"{APP_NAME} — arrêté")
        self.tray.show()

    # -- window / tray -----------------------------------------------------

    @Slot()
    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # left click
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_window()

    @Slot()
    def _on_new_instance(self):
        """A second launch tried to start: bring this window up instead."""
        while self.single_server is not None and self.single_server.hasPendingConnections():
            self.single_server.nextPendingConnection().close()
        self._show_window()

    def closeEvent(self, event):
        if not self._quitting and self.tray.isVisible():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(APP_NAME, "Le mixer continue de tourner dans la barre des tâches.",
                                      QSystemTrayIcon.Information, 3000)
        else:
            event.accept()
            self._quit()

    @Slot()
    def _quit(self):
        self._quitting = True
        self._apply_live()
        self._release_controller()
        self.tray.hide()
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                pass
        QApplication.quit()

    @Slot(bool)
    def _on_autostart_toggled(self, enabled: bool):
        try:
            set_autostart(enabled)
        except OSError as e:
            self._on_error(f"Impossible de modifier le lancement au démarrage : {e}")
            self.act_autostart.blockSignals(True)
            self.act_autostart.setChecked(is_autostart_enabled())
            self.act_autostart.blockSignals(False)
            return
        self.settings["autostart"] = enabled
        save_settings(self.settings)

    # -- audio apps --------------------------------------------------------

    def _ui_pulse(self):
        if self._pulse is None:
            try:
                self._pulse = pulsectl.Pulse("midi-mixer-ui")
            except Exception as e:
                logger.error("PulseAudio indisponible : %s", e)
                return None
        return self._pulse

    @Slot()
    def _refresh_apps(self):
        """Re-read the running audio apps; the dropdowns are only touched when the list changed."""
        pulse = self._ui_pulse()
        if pulse is None:
            return
        try:
            apps = list_running_apps(pulse)
            self._apps_error = False
        except Exception as e:
            if not self._apps_error:  # log once, not every poll
                logger.error("Erreur de lecture audio : %s", e)
            self._apps_error = True
            try:
                pulse.close()
            except Exception:
                pass
            self._pulse = None
            return
        if apps == self._apps:
            return
        self._apps = apps
        for row in self.rows + self.group_rows:
            if not row.app.view().isVisible():  # don't pull the list from under an open dropdown
                row.set_apps(apps)

    # -- mapping rows ------------------------------------------------------

    def _add_row(self, cc_text: str, app: str):
        row = MappingRow(cc_text, app, self._apps)
        row.changed.connect(self._apply_live)
        row.learnRequested.connect(self._on_learn_requested)
        row.removeRequested.connect(self._on_remove_requested)
        self.rows.append(row)
        self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)

    def _collect_rows(self):
        """Return (mappings, invalid) built from the rows currently in the UI."""
        mappings, invalid = {}, []
        for i, row in enumerate(self.rows):
            cc, app = row.values()
            if not cc or not app:
                continue
            parsed = parse_cc_field(cc)
            if parsed is None:
                invalid.append((i + 1, cc))
                continue
            mappings[mapping_key(*parsed)] = app
        return mappings, invalid

    def _collect_groups(self):
        """Return (groups, invalid) built from the group rows currently in the UI."""
        groups, invalid = [], []
        for row in self.group_rows:
            group = {"uid": row.uid, "name": row.name.text().strip(), "app": row.app.currentText().strip(),
                     "color": row.color.currentText()}
            for field, label, _prefix, _kind in row.fields_spec:
                text = row.fields[field].text()
                parsed = parse_cc_field(text) if text else None
                if text and parsed is None:
                    invalid.append((group["name"] or "Groupe", label, text))
                group[field] = mapping_key(*parsed) if parsed else ""
            groups.append(group)
        return groups, invalid

    def _unmute(self, uid: str, app: str):
        """Restore the sound of a group's app (its group was re-targeted or removed)."""
        self.engine.clear_mute(uid)
        pulse = self._ui_pulse()
        if pulse is not None and app:
            try:
                set_mute(pulse, app, False)
            except Exception as e:
                logger.error("Unmute impossible : %s", e)

    @Slot()
    def _apply_live(self):
        """Push the UI rows to the engine right away (mixer running or not) and save them.

        Invalid rows are ignored silently here; they are reported when the
        mixer is started.
        """
        self._refresh_group_items()
        mappings, _ = self._collect_rows()
        groups, _ = self._collect_groups()
        old_apps = {g["uid"]: g.get("app", "") for g in self.profile.groups}
        for g in groups:
            if g["uid"] in self.engine.muted and old_apps.get(g["uid"], g["app"]) != g["app"]:
                self._unmute(g["uid"], old_apps[g["uid"]])
        self.engine.mappings = mappings
        self.engine.set_groups(groups)
        self.profile.mappings = mappings
        self.profile.groups = groups
        self._save_profile()

    def _save_profile(self):
        try:
            save_profile(self.profile_id, self.profile)
        except OSError as e:
            logger.error("Sauvegarde impossible : %s", e)

    @Slot(object)
    def _on_remove_requested(self, row):
        if row not in self.rows:
            return
        if self.engine.learning_token is row:
            self.engine.cancel_learning()
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()
        self._apply_live()

    # -- groups ------------------------------------------------------------

    def _add_group_row(self, group: dict, user: bool = False):
        row = GroupRow(group, self._apps, self.profile)
        row.changed.connect(self._apply_live)
        row.learnRequested.connect(self._on_group_learn_requested)
        row.removeRequested.connect(self._on_group_remove_requested)
        self.group_rows.append(row)
        self.group_stack.addWidget(row)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, row.uid)  # list entries are matched to their panel by uid, never by position
        self.group_list.addItem(item)
        self._refresh_group_items()
        if user:
            self.group_list.setCurrentRow(self.group_list.count() - 1)
            self._apply_live()

    def _group_by_uid(self, uid):
        return next((r for r in self.group_rows if r.uid == uid), None)

    def _list_index_of(self, uid) -> int:
        for i in range(self.group_list.count()):
            if self.group_list.item(i).data(Qt.UserRole) == uid:
                return i
        return -1

    def _refresh_group_items(self):
        """Keep the left-hand list in line with each group's name, app and LED color."""
        for i in range(self.group_list.count()):
            item = self.group_list.item(i)
            row = self._group_by_uid(item.data(Qt.UserRole))
            if row is None:
                continue
            name = row.name.text().strip() or f"Groupe {i + 1}"
            app = row.app.currentText().strip()
            item.setText(f"{name}\n{app or 'non assigné'}")
            color = (self.profile.color_hex(row.color.currentText()) or GREEN) if app else "#5c6070"
            item.setIcon(dot_icon(color))

    @Slot(int)
    def _on_group_selected(self, index: int):
        item = self.group_list.item(index)
        row = self._group_by_uid(item.data(Qt.UserRole)) if item is not None else None
        if row is not None:
            self.group_stack.setCurrentWidget(row)

    @Slot()
    def _on_groups_reordered(self):
        """The list order changed (drag and drop or ▲ ▼): follow it for the saved order."""
        by_uid = {r.uid: r for r in self.group_rows}
        order = [self.group_list.item(i).data(Qt.UserRole) for i in range(self.group_list.count())]
        self.group_rows = [by_uid[u] for u in order if u in by_uid]
        self._on_group_selected(self.group_list.currentRow())
        self._apply_live()

    def _move_group(self, delta: int):
        i = self.group_list.currentRow()
        j = i + delta
        if i < 0 or not 0 <= j < self.group_list.count():
            return
        item = self.group_list.takeItem(i)
        self.group_list.insertItem(j, item)
        self.group_list.setCurrentRow(j)
        self._on_groups_reordered()

    def _new_group(self) -> dict:
        """Next free position of the profile's layout, or an unbound group when the layout is full."""
        return self.profile.new_group(self._collect_groups()[0])

    @Slot(object)
    def _on_group_remove_requested(self, row):
        if row not in self.group_rows:
            return
        token = self.engine.learning_token
        if isinstance(token, tuple) and token[0] is row:
            self.engine.cancel_learning()
        if row.uid in self.engine.muted:
            self._unmute(row.uid, row.app.currentText().strip())
        index = self._list_index_of(row.uid)
        self.group_rows.remove(row)
        self.group_stack.removeWidget(row)
        row.deleteLater()
        if index >= 0:
            self.group_list.takeItem(index)
        self._on_group_selected(self.group_list.currentRow())
        self._apply_live()

    @Slot(str)
    def _on_assign_requested(self, uid: str):
        """Assign button pressed on the controller: give the group the app that has the focus."""
        row = next((r for r in self.group_rows if r.uid == uid), None)
        if row is None:
            return
        title = row.name.text().strip() or "Groupe"
        pulse = self._ui_pulse()
        name = None
        if pulse is not None:
            try:
                name = focused_app_name(pulse)
            except Exception as e:
                logger.error("Détection de l'application au premier plan : %s", e)
        if not name:
            hint = "aucune application détectée" if XLIB_AVAILABLE else "installe python-xlib"
            self.overlay.show_notice(title, f"Assignation impossible : {hint}")
            return
        # One app lives in one group only: take it away from any other group.
        base = target_base_name(name)
        for other in self.group_rows:
            if other is not row and target_base_name(other.app.currentText()) == base:
                other.set_app("")
        row.set_app(name)
        self._apply_live()
        self.overlay.show_notice(title, f"→ {name}")

    # -- learn -------------------------------------------------------------

    def _reset_learn_widget(self, token):
        """Restore the Learn button that `token` (a MappingRow or a (GroupRow, field) tuple) refers to."""
        if isinstance(token, tuple):
            row, field = token
            if row in self.group_rows:
                row.set_learning(field, False)
        elif token in self.rows:
            token.set_learning(False)

    def _begin_learn(self, token, kinds):
        port = self.port_combo.currentData()
        if not port:
            QMessageBox.warning(self, "MIDI", "Aucun port MIDI sélectionné")
            return
        previous = self.engine.cancel_learning()
        if previous is not None:
            self._reset_learn_widget(previous)
        if isinstance(token, tuple):
            token[0].set_learning(token[1], True)
        else:
            token.set_learning(True)
        self.engine.start_learning(token, port, kinds)

    @Slot(object)
    def _on_learn_requested(self, row):
        self._begin_learn(row, ("control_change",))

    @Slot(object, str)
    def _on_group_learn_requested(self, row, field):
        _label, msg_type = row.kind_for(field)
        self._begin_learn((row, field), (msg_type,))

    @Slot(object, int, int)
    def _on_learned(self, token, channel, number):
        if isinstance(token, tuple):
            row, field = token
            if row not in self.group_rows:
                return
            label, _ = row.kind_for(field)
            row.fields[field].set_text(format_key(mapping_key(channel, number), label))
        else:
            if token not in self.rows:
                return
            token.set_cc_text(format_key(mapping_key(channel, number)))
        self._reset_learn_widget(token)
        self._apply_live()

    @Slot(object)
    def _on_learn_ended(self, token):
        self._reset_learn_widget(token)

    # -- ports / mixer state -----------------------------------------------

    def _default_port(self, ports, preferred=None):
        """Preferred/saved port if present, otherwise one matching the profile's hint, otherwise the first port."""
        if preferred in ports:
            return preferred
        for p in ports:
            if self.profile.matches_port(p):
                return p
        return ports[0] if ports else None

    def _refresh_ports(self, force: bool = False):
        ports = input_port_names()
        changed = ports != self._ports
        if not (changed or force):
            return
        self._ports = ports
        preferred = self.profile.port if force else (self.port_combo.currentData() or self.profile.port)
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        if ports:
            for p in ports:
                self.port_combo.addItem(p, p)
            self.port_combo.setCurrentIndex(self.port_combo.findData(self._default_port(ports, preferred)))
        else:
            self.port_combo.addItem(NO_PORT_LABEL, None)
        self.port_combo.blockSignals(False)
        self._update_status()
        if changed and not force:
            self._maybe_switch_profile(ports)

    @Slot()
    def _poll(self):
        """Every few seconds: refresh the audio apps, watch for controllers being (un)plugged, autostart."""
        self._refresh_ports()
        self._refresh_apps()
        self._maybe_autostart()

    @Slot(int)
    def _on_port_activated(self, _index):
        """The user picked a port: remember it for this profile."""
        self.profile.port = self.port_combo.currentData()
        self._save_profile()
        self._update_status()

    def _port_belongs_to(self, profile, port) -> bool:
        return bool(port) and (port == profile.port or profile.matches_port(port))

    def _maybe_autostart(self):
        """Start the mixer by itself when the profile's own controller is there (saved port or name hint)."""
        port = self.port_combo.currentData()
        if (self._port_belongs_to(self.profile, port)
                and not self.engine.running and not self._user_stopped):
            self._start_mixer(interactive=False)

    def _start_mixer(self, interactive: bool) -> bool:
        port = self.port_combo.currentData()
        if not port:
            if interactive:
                QMessageBox.warning(self, "MIDI", "Aucun port MIDI sélectionné")
            return False
        if interactive:
            self.profile.port = port   # a manual start remembers the port, so the next launch can autostart
        _, invalid = self._collect_rows()
        _, invalid_groups = self._collect_groups()
        if (invalid or invalid_groups) and interactive:
            lines = []
            if invalid:
                lines.append("Assignations directes invalides : " + ", ".join(f"#{r} ('{v}')" for r, v in invalid))
            if invalid_groups:
                lines.append("Groupes invalides : " + ", ".join(f"{n} / {f} ('{v}')" for n, f, v in invalid_groups))
            QMessageBox.critical(self, "Validation", "\n".join(lines))
            return False
        # Auto-start runs with the valid rows only.
        self._apply_live()
        self.engine.start(port)
        return True

    @Slot()
    def _toggle_mixer(self):
        if self.engine.running:
            self._user_stopped = True
            self.engine.stop()
        else:
            self._user_stopped = False
            self._start_mixer(interactive=True)

    @Slot(bool)
    def _on_state_changed(self, running: bool):
        self.btn_run.setText("Arrêter le Mixer" if running else "Démarrer le Mixer")
        self.btn_run.setProperty("running", running)
        self.btn_run.style().unpolish(self.btn_run)
        self.btn_run.style().polish(self.btn_run)
        self.act_toggle.setText("Arrêter le mixer" if running else "Démarrer le mixer")
        self._update_status()

    def _update_status(self):
        port = self.port_combo.currentData()
        text = f"Actif — {port}" if self.engine.running else "Mixer arrêté"
        self.status.setText(text)
        self.tray.setToolTip(f"{APP_NAME} ({self.profile.name}) — {text}")

    # -- profiles ------------------------------------------------------------

    def _rebuild_profile_lists(self):
        """Refresh the profile selector and the tray submenu."""
        profiles = list_profiles()
        self.profile_bar.set_profiles(profiles, self.profile_id)
        self.tray_profiles.clear()
        if self._profile_actions is not None:
            self._profile_actions.deleteLater()
        self._profile_actions = QActionGroup(self)
        for pid, name in profiles:
            action = self.tray_profiles.addAction(name)
            action.setCheckable(True)
            action.setChecked(pid == self.profile_id)
            self._profile_actions.addAction(action)
            action.triggered.connect(lambda _checked=False, p=pid: self._switch_profile(p))

    def _clear_rows(self):
        """Remove every mapping row and group panel (their widgets belong to the previous profile)."""
        self.engine.cancel_learning()
        for row in self.rows:
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        self.rows = []
        for row in self.group_rows:
            self.group_stack.removeWidget(row)
            row.deleteLater()
        self.group_rows = []
        self.group_list.clear()

    def _load_profile_ui(self):
        """Show the active profile: title, mapping rows, group panels; hand its description to the engine."""
        self.engine.profile = self.profile
        self._clear_rows()
        self.setWindowTitle(f"{APP_NAME} — {self.profile.name}")
        for key, app in self.profile.mappings.items():
            self._add_row(format_key(key), app)
        self.engine.mappings = dict(self.profile.mappings)
        for group in self.profile.groups:
            self._add_group_row(group)
        self.engine.set_groups([dict(g) for g in self.profile.groups])
        if self.group_rows:
            self.group_list.setCurrentRow(0)

    def _release_controller(self):
        """Give the sound back and switch the controller's LEDs off, then stop listening."""
        for g in list(self.engine.groups):
            if g["uid"] in self.engine.muted:
                self._unmute(g["uid"], g.get("app", ""))
        self.engine.set_groups([])   # switches the group LEDs off
        self.engine.stop()

    @Slot(str)
    def _switch_profile(self, pid: str, persist_current: bool = True):
        if not pid or pid == self.profile_id:
            return
        new = load_profile(pid)
        if new is None:
            self._on_error("Profil introuvable ou illisible.")
            self._rebuild_profile_lists()
            return
        if persist_current:
            self._apply_live()
        self._release_controller()
        self.profile_id, self.profile = pid, new
        self.settings["active_profile"] = pid
        save_settings(self.settings)
        self._load_profile_ui()
        self._user_stopped = False
        self._rebuild_profile_lists()
        self._refresh_ports(force=True)
        self._maybe_autostart()

    def _maybe_switch_profile(self, ports):
        """If the active profile's controller is absent but another profile's controller is plugged, switch to it."""
        if self.engine.running or self._user_stopped or not ports:
            return
        if any(self._port_belongs_to(self.profile, p) for p in ports):
            return
        for pid, _name in list_profiles():
            other = load_profile(pid) if pid != self.profile_id else None
            if other is not None and any(self._port_belongs_to(other, p) for p in ports):
                self._switch_profile(pid)
                return

    @Slot(str)
    def _on_profile_action(self, action: str):
        """Menu "Gérer": ask the user what is needed, then call the matching method."""
        if action == "new":
            templates = bundled_templates()
            labels = [data.get("name", tid) for tid, data in templates.items()]
            name, ok = QInputDialog.getText(self, "Nouveau profil", "Nom du profil :")
            if not ok or not name.strip():
                return
            label, ok = QInputDialog.getItem(self, "Nouveau profil", "Modèle de contrôleur :", labels, 0, False)
            if ok:
                self._create_profile(name.strip(), list(templates)[labels.index(label)])
        elif action == "duplicate":
            name, ok = QInputDialog.getText(self, "Dupliquer le profil", "Nom de la copie :",
                                            text=f"{self.profile.name} (copie)")
            if ok and name.strip():
                self._duplicate_profile(name.strip())
        elif action == "rename":
            name, ok = QInputDialog.getText(self, "Renommer le profil", "Nouveau nom :", text=self.profile.name)
            if ok and name.strip():
                self._rename_profile(name.strip())
        elif action == "delete":
            answer = QMessageBox.question(self, "Supprimer le profil",
                                          f"Supprimer le profil « {self.profile.name} » et ses réglages ?")
            if answer == QMessageBox.Yes:
                self._delete_profile()
        elif action == "import":
            path, _ = QFileDialog.getOpenFileName(self, "Importer un profil", "", "Profils (*.json)")
            if path:
                self._import_profile(path)
        elif action == "export":
            path, _ = QFileDialog.getSaveFileName(self, "Exporter le profil", f"{self.profile.name}.json",
                                                  "Profils (*.json)")
            if path:
                self._export_profile(path)

    def _create_profile(self, name: str, template_id: str = GENERIC_TEMPLATE) -> str:
        self._apply_live()
        pid = create_profile(name, template_id)
        self._switch_profile(pid, persist_current=False)
        return pid

    def _duplicate_profile(self, name: str) -> str:
        self._apply_live()
        pid = duplicate_profile(self.profile, name)
        self._switch_profile(pid, persist_current=False)
        return pid

    def _rename_profile(self, name: str):
        self.profile.name = name
        self._save_profile()
        self.setWindowTitle(f"{APP_NAME} — {name}")
        self._rebuild_profile_lists()
        self._update_status()

    def _delete_profile(self) -> bool:
        """Delete the active profile (never the last one) and switch to another."""
        others = [pid for pid, _name in list_profiles() if pid != self.profile_id]
        if not others:
            return False
        deleted = self.profile_id
        self._switch_profile(others[0], persist_current=False)
        delete_profile(deleted)
        self._rebuild_profile_lists()
        return True

    def _import_profile(self, path: str):
        try:
            pid = import_profile(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Importer un profil", str(e))
            return
        self._switch_profile(pid)

    def _export_profile(self, path: str):
        self._apply_live()
        try:
            export_profile(self.profile, path)
        except OSError as e:
            QMessageBox.warning(self, "Exporter le profil", str(e))

    # -- engine feedback ---------------------------------------------------

    @Slot(str, float, bool)
    def _on_volume(self, name: str, val: float, muted: bool):
        self.overlay.show_volume(name, val, muted)

    @Slot(str)
    def _on_error(self, message: str):
        logger.error(message)
        if message != self._last_error:  # avoid spamming while auto-restart retries
            self._last_error = message
            self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.Warning, 5000)
