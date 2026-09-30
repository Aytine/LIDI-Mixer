import pulsectl
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from midi_mixer import window as window_module
from midi_mixer.config import load_config
from midi_mixer.window import MainWindow


@pytest.fixture
def win(qapp, monkeypatch):
    """A window with no MIDI port and no PulseAudio, config and autostart in a temp HOME."""
    monkeypatch.setattr(window_module, "input_port_names", lambda: [])
    monkeypatch.setattr(pulsectl, "Pulse", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pulse")))
    w = MainWindow(start_hidden=True)
    yield w
    w.tray.hide()
    w.overlay.hide()
    w.close()


def listing(w):
    return [w.group_list.item(i).text().split("\n")[0] for i in range(w.group_list.count())]


def consistent(w):
    """The detail panel shows the group selected in the list, and the saved order is the list order."""
    shown = w.group_stack.currentWidget().name.text()
    selected = w.group_list.currentItem().text().split("\n")[0]
    saved = [g["name"] for g in w.cfg["groups"]]
    return shown == selected and saved == listing(w)


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
    assert [g["name"] for g in load_config()["groups"]] == listing(win)    # persisted on disk


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


def test_new_groups_take_the_first_free_column_and_name(win):
    win._on_group_remove_requested(win.group_rows[2])      # frees "Groupe 3" / column 3
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
    token = (row, "mute")
    win._on_learned(token, 6, 60)
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
