"""Main window: tabs, tray icon, wiring between the UI and the engine."""
import logging
import uuid

import pulsectl
from PySide6.QtCore import QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QStackedWidget, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from .audio import list_running_apps, set_mute, target_base_name
from .autostart import is_autostart_enabled, set_autostart
from .config import default_group, load_config, save_config
from .constants import APP_NAME, ASSIGN_COLORS, DEFAULT_ASSIGN_COLOR, DEFAULT_GROUP_COUNT, DEFAULT_PORT_HINT, NO_PORT_LABEL, PORT_POLL_MS
from .engine import MixerEngine
from .focus import XLIB_AVAILABLE, focused_app_name
from .midi_utils import format_key, input_port_names, mapping_key, parse_cc_field
from .theme import dot_icon, make_app_icon
from .widgets import GroupList, GroupRow, MappingRow, NoScrollComboBox, VolumeOverlay

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, start_hidden: bool):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} - APC40")
        self.resize(1020, 660)
        self.icon = make_app_icon()
        self.setWindowIcon(self.icon)

        self.cfg = load_config()
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

        self._build_ui()
        self._build_tray()

        # Keep the autostart entry in line with the saved preference (on by default).
        if self.cfg["autostart"] != is_autostart_enabled():
            try:
                set_autostart(self.cfg["autostart"])
            except OSError as e:
                logger.error("Autostart: %s", e)
        self.act_autostart.setChecked(is_autostart_enabled())

        self._refresh_apps()
        for key, app in self.cfg["mappings"].items():
            self._add_row(format_key(key), app)
        self.engine.mappings = dict(self.cfg["mappings"])
        for group in self.cfg["groups"]:
            self._add_group_row(group)
        self.engine.set_groups([dict(g) for g in self.cfg["groups"]])
        if self.group_rows:
            self.group_list.setCurrentRow(0)

        self._refresh_ports()
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
        self.port_combo = NoScrollComboBox()
        self.port_combo.currentIndexChanged.connect(self._on_port_changed)
        self.btn_refresh = QPushButton("Rafraîchir les applications")
        self.btn_refresh.clicked.connect(self._refresh_apps)
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
        menu.addAction(act_open)
        menu.addAction(self.act_toggle)
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
        # Pulse keeps the mute flag after we exit (notably on the MASTER sink): restore the sound.
        for g in list(self.engine.groups):
            if g["uid"] in self.engine.muted:
                self._unmute(g["uid"], g.get("app", ""))
        self.engine.stop()
        self._apply_live()
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
        self.cfg["autostart"] = enabled
        save_config(self.cfg)

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
            for field, label, _kind, _msg in GroupRow.FIELDS:
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
        old_apps = {g["uid"]: g.get("app", "") for g in self.cfg["groups"]}
        for g in groups:
            if g["uid"] in self.engine.muted and old_apps.get(g["uid"], g["app"]) != g["app"]:
                self._unmute(g["uid"], old_apps[g["uid"]])
        self.engine.mappings = mappings
        self.engine.set_groups(groups)
        self.cfg["mappings"] = mappings
        self.cfg["groups"] = groups
        self.cfg["port"] = self.port_combo.currentData()
        try:
            save_config(self.cfg)
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
        row = GroupRow(group, self._apps)
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
            item.setIcon(dot_icon(ASSIGN_COLORS[row.color.currentText()][1] if app else "#5c6070"))

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
        """Next free APC40 column's default controls, or an unbound group if all 8 are taken."""
        groups = self._collect_groups()[0]
        used = {g["volume"] for g in groups}
        names = {g["name"] for g in groups}
        n = 1
        while f"Groupe {n}" in names:
            n += 1
        for i in range(DEFAULT_GROUP_COUNT):
            group = default_group(i)
            if group["volume"] not in used:
                group["name"] = f"Groupe {n}"
                return group
        return {"uid": uuid.uuid4().hex[:8], "name": f"Groupe {n}", "app": "", "volume": "",
                "mute": "", "assign": "", "color": DEFAULT_ASSIGN_COLOR}

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

    @staticmethod
    def _default_port(ports, preferred=None):
        """Preferred/saved port if present, otherwise the APC40, otherwise the first port."""
        if preferred in ports:
            return preferred
        for p in ports:
            if DEFAULT_PORT_HINT in p.lower():
                return p
        return ports[0] if ports else None

    def _refresh_ports(self):
        ports = input_port_names()
        if ports == self._ports:
            return
        self._ports = ports
        current = self.port_combo.currentData() or self.cfg["port"]
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        if ports:
            for p in ports:
                self.port_combo.addItem(p, p)
            self.port_combo.setCurrentIndex(self.port_combo.findData(self._default_port(ports, current)))
        else:
            self.port_combo.addItem(NO_PORT_LABEL, None)
        self.port_combo.blockSignals(False)
        self._update_status()

    @Slot()
    def _poll(self):
        """Every few seconds: refresh the audio apps, watch for the controller being (un)plugged and autostart for the APC40."""
        self._refresh_ports()
        self._refresh_apps()
        self._maybe_autostart()

    @Slot(int)
    def _on_port_changed(self, _index):
        self._apply_live()
        self._update_status()

    def _maybe_autostart(self):
        port = self.port_combo.currentData()
        if (port and DEFAULT_PORT_HINT in port.lower()
                and not self.engine.running and not self._user_stopped):
            self._start_mixer(interactive=False)

    def _start_mixer(self, interactive: bool) -> bool:
        port = self.port_combo.currentData()
        if not port:
            if interactive:
                QMessageBox.warning(self, "MIDI", "Aucun port MIDI sélectionné")
            return False
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
        self.tray.setToolTip(f"{APP_NAME} — {text}")

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
