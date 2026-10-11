import os
import subprocess

import pytest

from midi_mixer import launcher
from midi_mixer.launcher import (
    clean_environment, exec_argv, find_entry, is_running, launch, match_score, parse_desktop_file, scan_entries,
)
from conftest import FakePulse, FakeStream


def write(folder, filename, **fields):
    """Write a .desktop file; returns its path."""
    body = {"Type": "Application", "Name": filename[:-8], "Exec": "true", **fields}
    path = os.path.join(folder, filename)
    os.makedirs(folder, exist_ok=True)
    with open(path, "w") as f:
        f.write("[Desktop Entry]\n" + "".join(f"{k}={v}\n" for k, v in body.items() if v is not None))
        f.write("\n[Desktop Action new-window]\nName=Other action\nExec=should-not-be-used\n")
    return path


@pytest.fixture
def apps_dir(tmp_path):
    return str(tmp_path / "applications")


@pytest.fixture(autouse=True)
def fresh_cache():
    launcher._cache["entries"] = None
    yield
    launcher._cache["entries"] = None


# ----------------------------------------------------------------- parsing

def test_parse_reads_the_main_group_only(apps_dir):
    entry = parse_desktop_file(write(apps_dir, "spotify.desktop", Name="Spotify", Exec="spotify %U",
                                     StartupWMClass="Spotify", Path="/tmp"))
    assert (entry.desktop_id, entry.name, entry.exec, entry.wm_class) == ("spotify", "Spotify", "spotify %U", "Spotify")
    assert entry.exec != "should-not-be-used" and entry.workdir == "/tmp"


def test_entries_that_are_not_launchable_applications_are_skipped(apps_dir):
    assert parse_desktop_file(write(apps_dir, "link.desktop", Type="Link")) is None
    assert parse_desktop_file(write(apps_dir, "hidden.desktop", Hidden="true")) is None
    assert parse_desktop_file(write(apps_dir, "noexec.desktop", Exec=None)) is None
    assert parse_desktop_file(os.path.join(apps_dir, "missing.desktop")) is None


def test_localized_names_do_not_override_the_plain_name(apps_dir):
    path = os.path.join(apps_dir, "x.desktop")
    os.makedirs(apps_dir)
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Plain\nName[fr]=Français\nExec=x\n")
    assert parse_desktop_file(path).name == "Plain"


def test_scan_reads_every_folder_and_ignores_missing_ones(tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    write(a, "one.desktop")
    write(b, "two.desktop")
    names = {e.desktop_id for e in scan_entries([a, b, str(tmp_path / "nope")])}
    assert names == {"one", "two"}


# ------------------------------------------------------------- Exec parsing

def test_exec_field_codes_and_flatpak_markers_are_dropped():
    assert exec_argv("spotify --disable-gpu %U") == ["spotify", "--disable-gpu"]
    assert exec_argv('"/opt/My App/run" --x %f %%') == ["/opt/My App/run", "--x", "%"]
    assert exec_argv("/usr/bin/flatpak run --branch=stable --command=discord com.discordapp.Discord @@u %U @@") == \
        ["/usr/bin/flatpak", "run", "--branch=stable", "--command=discord", "com.discordapp.Discord"]


# ---------------------------------------------------------------- matching

def entry(**kw):
    defaults = {"path": "/x.desktop", "desktop_id": "x", "name": "X", "exec": "x"}
    return launcher.DesktopEntry(**{**defaults, **kw})


def test_matching_by_name_binary_wm_class_and_desktop_id():
    spotify = entry(desktop_id="spotify", name="Spotify", exec="spotify %U")
    assert match_score(spotify, "spotify") == 3                                  # display name
    assert match_score(entry(name="Music", exec="/usr/bin/musicplayer %U"), "musicplayer") == 2      # binary
    assert match_score(entry(name="Music", wm_class="Player"), "player") == 2                          # window class
    assert match_score(entry(desktop_id="org.kde.konsole", name="Terminal", exec="x"), "konsole") == 2
    assert match_score(entry(name="Firefox ESR"), "firefox") == 1                                     # loose: name prefix
    assert match_score(spotify, "spot") == 0 and match_score(spotify, "") == 0


def test_flatpak_apps_match_on_their_application_id():
    discord = entry(desktop_id="com.discordapp.Discord", name="Chat",
                    exec="/usr/bin/flatpak run --branch=stable --command=discord com.discordapp.Discord @@u %U @@")
    assert match_score(discord, "discord") == 2


def test_env_prefix_does_not_hide_the_program():
    assert match_score(entry(name="Game", exec="env MESA_GLTHREAD=true steam steam://run/1"), "steam") == 2


def test_the_best_entry_wins_and_visible_ones_beat_hidden_ones():
    exact = entry(desktop_id="a", name="Discord", exec="discord-a")
    loose = entry(desktop_id="b", name="Discord Canary", exec="canary")
    assert find_entry("Discord", [loose, exact]) is exact
    hidden = entry(desktop_id="c", name="Discord", exec="hidden", no_display=True)
    assert find_entry("Discord", [hidden, exact]) is exact


def test_an_audio_label_with_binary_and_pid_is_understood():
    konsole = entry(desktop_id="org.kde.konsole", name="Konsole", exec="konsole")
    assert find_entry("Konsole (konsole, pid:12)", [konsole]) is konsole
    assert find_entry("Nothing", [konsole]) is None and find_entry("MASTER", [konsole]) is None


# ------------------------------------------------------------------ launch

class FakePopen:
    calls = []

    def __init__(self, argv, **kwargs):
        FakePopen.calls.append((argv, kwargs))

    def wait(self):
        return 0


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    return FakePopen


def test_an_explicit_command_is_run_detached_from_our_environment(popen, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/home/me/venv")
    monkeypatch.setenv("PATH", "/home/me/venv/bin:/usr/bin")
    ok, message = launch("Whatever", 'my-app --flag "two words"')
    argv, kwargs = popen.calls[0]
    assert ok and argv == ["my-app", "--flag", "two words"] and "my-app" in message
    assert kwargs["start_new_session"] is True and kwargs["stdin"] == subprocess.DEVNULL
    assert "VIRTUAL_ENV" not in kwargs["env"] and kwargs["env"]["PATH"] == "/usr/bin"


def test_the_app_launcher_is_used_when_there_is_no_command(popen, monkeypatch, apps_dir):
    write(apps_dir, "spotify.desktop", Name="Spotify", Exec="spotify --x %U")
    monkeypatch.setattr(launcher, "desktop_dirs", lambda: [apps_dir])
    ok, message = launch("Spotify (spotify, pid:3)")
    assert ok and popen.calls[0][0] == ["spotify", "--x"] and message == "Lancement de Spotify…"


def test_an_app_installed_after_the_first_scan_is_found(popen, monkeypatch, apps_dir):
    monkeypatch.setattr(launcher, "desktop_dirs", lambda: [apps_dir])
    assert launch("Newapp")[0] is False                    # the first scan finds nothing...
    write(apps_dir, "newapp.desktop", Name="Newapp", Exec="newapp")
    assert launch("Newapp")[0] is True                     # ...a miss triggers a rescan


def test_unknown_app_explains_what_to_do(popen, monkeypatch, apps_dir):
    monkeypatch.setattr(launcher, "desktop_dirs", lambda: [apps_dir])
    ok, message = launch("Ghost")
    assert not ok and "Ghost" in message and "commande" in message and popen.calls == []


def test_terminal_apps_need_an_explicit_command(popen, monkeypatch, apps_dir):
    write(apps_dir, "htop.desktop", Name="htop", Exec="htop", Terminal="true")
    monkeypatch.setattr(launcher, "desktop_dirs", lambda: [apps_dir])
    ok, message = launch("htop")
    assert not ok and "terminal" in message and popen.calls == []


def test_bad_commands_are_reported_not_raised(monkeypatch):
    assert launch("x", 'unbalanced "quote')[0] is False
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    ok, message = launch("x", "does-not-exist --flag")
    assert not ok and "does-not-exist" in message
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(PermissionError("nope")))
    assert launch("x", "thing")[0] is False


def test_clean_environment_without_a_virtualenv_is_unchanged(monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert clean_environment()["PATH"] == os.environ["PATH"]


# -------------------------------------------------------------- is_running

def test_is_running_means_an_audio_stream_matches():
    pulse = FakePulse(FakeStream("Spotify"), FakeStream("WEBRTC VoiceEngine", binary="Discord"))
    assert is_running(pulse, "Spotify") and is_running(pulse, "Discord (Discord, pid:5)")
    assert not is_running(pulse, "Firefox") and not is_running(FakePulse(), "Spotify")
