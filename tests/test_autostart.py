import os

from midi_mixer import autostart


def test_autostart_entry_is_created_and_removed():
    assert not autostart.is_autostart_enabled()
    autostart.set_autostart(True)
    assert autostart.is_autostart_enabled()
    content = open(autostart.autostart_file()).read()
    assert "--minimized" in content
    main_py = content.split('Exec="')[1].split('" "')[1].split('"')[0]
    assert os.path.basename(main_py) == "main.py" and os.path.exists(main_py)
    autostart.set_autostart(False)
    assert not autostart.is_autostart_enabled()
