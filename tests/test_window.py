import pulsectl
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from midi_mixer import config
from midi_mixer import window as window_module
from midi_mixer.engine import MixerEngine
from midi_mixer.window import MainWindow

APC_PORT = "APC40 mkII:APC40 mkII MIDI 1 24:0"


@pytest.fixture
def ports(monkeypatch):
    """The MIDI input ports the app sees; tests change the list to simulate plugging controllers."""
    current = []
    monkeypatch.setattr(window_module, "input_port_names", lambda: list(current))
    return current


@pytest.fixture
def started(monkeypatch):
    """Record engine starts instead of opening real MIDI ports."""
    calls = []
    monkeypatch.setattr(MixerEngine, "start", lambda self, port: calls.append(port))
    return calls


@pytest.fixture
def make_window(qapp, monkeypatch, ports, started):
    """Factory for windows with no PulseAudio; config and autostart live in a temp HOME."""
    monkeypatch.setattr(pulsectl, "Pulse", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pulse")))
    created = []

    def make():
        w = MainWindow(start_hidden=True)
        created.append(w)
        return w

    yield make
    for w in created:
        w.tray.hide()
        w.overlay.hide()
        w.close()


@pytest.fixture
def win(make_window, ports):
    """A window on the APC40 mkII profile (8 groups)."""
    ports.append(APC_PORT)
    return make_window()


def listing(w):
    return [w.group_list.item(i).text().split("\n")[0] for i in range(w.group_list.count())]


def consistent(w):
    """The detail panel shows the group selected in the list, and the saved order is the list order."""
    shown = w.group_stack.currentWidget().name.text()
    selected = w.group_list.currentItem().text().split("\n")[0]
    saved = [g["name"] for g in config.load_profile(w.profile_id).groups]
    return shown == selected and saved == listing(w)


# ------------------------------------------------------------------ groups

def test_starts_with_the_eight_default_groups(win):
    assert listing(win) == [f"Groupe {i}" for i in range(1, 9)]
    assert win.group_list.currentRow() == 0 and consistent(win)


def test_selecting_a_group_shows_its_panel(win):
    win.group_list.setCurrentRow(3)
    assert win.group_stack.currentWidget() is win.group_rows[3]


def test_move_up_and_down_keeps_selection_and_saved_order(win):
    win.group_list.setCurrentRow(2)
    win._move_group(-1)
    assert listing(win)[:3] == ["Groupe 1", "Groupe 3", "Groupe 2"] and consistent(win)
    win._move_group(-1)
    win._move_group(-1)                                    # already first: no-op
    assert listing(win)[0] == "Groupe 3" and win.group_list.currentRow() == 0 and consistent(win)
    win._move_group(1)
    assert consistent(win)


def test_drag_and_drop_reorders_like_the_buttons(win):
    item = win.group_list.takeItem(0)
    win.group_list.insertItem(5, item)
    win.group_list.setCurrentRow(5)
    win.group_list.reordered.emit()                        # what GroupList.dropEvent does
    assert listing(win)[5] == "Groupe 1" and consistent(win)


def test_edits_and_removal_hit_the_right_group_after_reordering(win):
    win.group_list.setCurrentRow(4)
    win._move_group(-3)
    row = win.group_stack.currentWidget()
    row.name.setText("Musique")
    row.changed.emit()
    assert listing(win)[1] == "Musique" and consistent(win)
    win._on_group_remove_requested(row)
    assert "Musique" not in listing(win) and len(win.group_rows) == 7 and consistent(win)


def test_new_groups_take_the_first_free_position_and_name(win):
    win._on_group_remove_requested(win.group_rows[2])      # frees "Groupe 3" / position 3
    win._add_group_row(win._new_group(), user=True)
    added = win.engine.groups[-1]
    assert added["name"] == "Groupe 3" and added["volume"] == "3:7" and added["assign"] == "3:0"
    assert win.group_list.currentRow() == win.group_list.count() - 1


def test_changing_an_app_reaches_the_engine_without_restarting(win):
    win.group_rows[0].set_app("Spotify")
    win._apply_live()
    assert win.engine._lookup_target(0, 7) == "Spotify"


def test_assigning_moves_an_app_out_of_its_previous_group(win, monkeypatch):
    monkeypatch.setattr(window_module, "focused_app_name", lambda pulse: "Discord")
    monkeypatch.setattr(win, "_ui_pulse", lambda: object())
    first, second = win.group_rows[0], win.group_rows[1]
    win._on_assign_requested(first.uid)
    win._on_assign_requested(second.uid)
    assert first.app.currentText() == "" and second.app.currentText() == "Discord"


def test_assign_reports_when_no_app_is_detected(win, monkeypatch):
    monkeypatch.setattr(window_module, "focused_app_name", lambda pulse: None)
    monkeypatch.setattr(win, "_ui_pulse", lambda: object())
    win._on_assign_requested(win.group_rows[0].uid)
    assert win.group_rows[0].app.currentText() == ""


def test_learning_a_button_fills_the_field(win):
    row = win.group_rows[0]
    win._on_learned((row, "mute"), 6, 60)
    assert row.fields["mute"].text() == "Note 60 (ch 6)" and win.engine.groups[0]["mute"] == "6:60"


def test_app_list_refreshes_only_when_it_changes(win, monkeypatch):
    lists = iter([["MASTER", "A"], ["MASTER", "A"], ["MASTER", "A", "B"]])
    monkeypatch.setattr(window_module, "list_running_apps", lambda pulse: next(lists))
    monkeypatch.setattr(win, "_ui_pulse", lambda: object())
    win._refresh_apps()
    count = win.group_rows[0].app.count()
    win._refresh_apps()
    assert win.group_rows[0].app.count() == count          # unchanged list: dropdown untouched
    win._refresh_apps()
    assert win.group_rows[0].app.count() == count + 1


@pytest.mark.parametrize("pick", [
    lambda w: w.port_combo,
    lambda w: w.profile_bar.combo,
    lambda w: w.group_rows[0].color,
    lambda w: w.group_rows[0].app,
])
def test_mouse_wheel_never_changes_a_combo_box(win, pick):
    combo = pick(win)
    before = combo.currentIndex()
    event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    combo.wheelEvent(event)
    assert combo.currentIndex() == before and not event.isAccepted()


# ---------------------------------------------------------------- profiles

def test_no_controller_knowledge_in_the_window_defaults(make_window):
    """Without a known controller the app starts on the generic profile: no LED colors, unbound groups."""
    w = make_window()
    assert w.profile.name == "Mon contrôleur" and not w.profile.has_leds
    assert len(w.group_rows) == 4 and w.group_rows[0].fields["volume"].text() == ""
    assert not w.group_rows[0].color.isVisibleTo(w.group_rows[0])   # no palette: no color selector


def test_apc40_profile_is_used_when_the_controller_is_plugged_at_first_run(win):
    assert win.profile.name == "APC40 mkII" and win.windowTitle().endswith("APC40 mkII")
    assert win.group_rows[0].color.isVisibleTo(win.group_rows[0])
    assert win.profile.mappings == {"14": "MASTER"}


def test_mixer_autostarts_only_for_the_profiles_own_controller(make_window, ports, started):
    ports.append(APC_PORT)
    make_window()
    assert started == [APC_PORT]                           # port name matches the profile's hint
    started.clear()
    ports[:] = ["Some Other Controller"]
    w = make_window()
    assert started == []                                   # nothing known about this device: no autostart
    w._start_mixer(interactive=True)                       # the user starts it once...
    assert started == ["Some Other Controller"] and w.profile.port == "Some Other Controller"
    started.clear()
    w.engine.running = False
    assert w._port_belongs_to(w.profile, "Some Other Controller")   # ...so next time it is recognised


def test_creating_a_profile_switches_to_it_and_keeps_the_previous_one(win):
    win.group_rows[0].set_app("Spotify")
    win._apply_live()
    old_id = win.profile_id
    new_id = win._create_profile("Mon Korg", "generic")
    assert win.profile_id == new_id and win.profile.name == "Mon Korg" and win.windowTitle().endswith("Mon Korg")
    assert len(win.group_rows) == 4 and win.settings["active_profile"] == new_id
    assert config.load_settings()["active_profile"] == new_id
    win._switch_profile(old_id)
    assert win.group_rows[0].app.currentText() == "Spotify" and len(win.group_rows) == 8


def test_switching_profiles_rebuilds_the_ui_and_the_engine(win):
    win._create_profile("Autre", "generic")
    assert win.engine.profile is win.profile and not win.engine.profile.has_leds
    assert [g["volume"] for g in win.engine.groups] == ["", "", "", ""]
    assert win.engine.mappings == {}


def test_switching_switches_the_controller_leds_off(win):
    sent = []
    win.engine._out = type("Out", (), {"send": staticmethod(sent.append)})()
    win.group_rows[0].set_app("Discord")
    win._apply_live()
    sent.clear()
    win._create_profile("Autre", "generic")
    assert any(m.type == "note_on" and m.note == 0 and m.velocity == 0 for m in sent)   # assign pad off


def test_port_choice_is_remembered_per_profile(win, ports):
    ports.append("Other Port")
    win._refresh_ports()
    win.port_combo.setCurrentIndex(win.port_combo.findData("Other Port"))
    win._on_port_activated(win.port_combo.currentIndex())
    assert config.load_profile(win.profile_id).port == "Other Port"


def test_rename_profile(win):
    win._rename_profile("Mon APC")
    assert win.profile.name == "Mon APC" and win.windowTitle().endswith("Mon APC")
    assert dict(config.list_profiles())[win.profile_id] == "Mon APC"


def test_duplicate_profile_copies_the_settings(win):
    win.group_rows[1].set_app("Discord")
    win._apply_live()
    copy_id = win._duplicate_profile("Copie")
    assert win.profile_id == copy_id and win.group_rows[1].app.currentText() == "Discord"


def test_delete_profile_switches_to_another_and_the_last_one_is_kept(win):
    first = win.profile_id
    second = win._create_profile("Second", "generic")
    assert win._delete_profile() is True
    assert win.profile_id == first and second not in dict(config.list_profiles())
    assert win._delete_profile() is False                  # never delete the last profile
    assert win.profile_id == first and config.load_profile(first) is not None
    assert not win.profile_bar.actions["delete"].isEnabled()


def test_export_and_import_roundtrip(win, tmp_path):
    win.group_rows[0].set_app("Spotify")
    path = str(tmp_path / "apc.json")
    win._export_profile(path)
    win._import_profile(path)
    assert win.profile.name == "APC40 mkII" and win.profile_id != "apc40-mkii"
    assert win.group_rows[0].app.currentText() == "Spotify" and win.profile.port is None


def test_importing_garbage_does_not_change_the_active_profile(win, tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(window_module.QMessageBox, "warning", lambda *a: warnings.append(a))
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    before = win.profile_id
    win._import_profile(str(bad))
    assert win.profile_id == before and len(warnings) == 1


def test_profile_selector_and_tray_menu_list_every_profile(win):
    win._create_profile("Autre", "generic")
    names = [win.profile_bar.combo.itemText(i) for i in range(win.profile_bar.combo.count())]
    assert names == ["APC40 mkII", "Autre"]
    assert [a.text() for a in win.tray_profiles.actions()] == names
    assert [a.isChecked() for a in win.tray_profiles.actions()] == [False, True]


def test_choosing_a_profile_in_the_selector_switches(win):
    first = win.profile_id
    win._create_profile("Autre", "generic")
    combo = win.profile_bar.combo
    combo.setCurrentIndex(combo.findData(first))
    win.profile_bar.profileSelected.emit(combo.currentData())
    assert win.profile_id == first


def test_plugging_another_controller_switches_to_its_profile(win, ports):
    korg = win._create_profile("Mon Korg", "generic")
    win.profile.port = "Korg Port"
    win._apply_live()
    win._switch_profile("apc40-mkii")
    win.engine.running = False
    ports[:] = ["Korg Port"]                                # the APC40 is unplugged, the Korg plugged
    win._refresh_ports()
    assert win.profile_id == korg


def test_no_automatic_switch_while_the_mixer_runs_or_after_a_manual_stop(win, ports):
    other = win._create_profile("Mon Korg", "generic")
    win.profile.port = "Korg Port"
    win._apply_live()
    win._switch_profile("apc40-mkii")
    ports[:] = ["Korg Port"]
    win._user_stopped = True
    win._refresh_ports()
    assert win.profile_id != other
