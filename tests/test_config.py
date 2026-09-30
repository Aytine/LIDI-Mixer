import json
import os

import pytest

from midi_mixer import config
from midi_mixer.profile import Profile

APC_PORT = "APC40 mkII:APC40 mkII MIDI 1 24:0"


def test_settings_default_and_roundtrip():
    assert config.load_settings() == {"active_profile": None, "autostart": True}
    config.save_settings({"active_profile": "abc", "autostart": False})
    assert config.load_settings() == {"active_profile": "abc", "autostart": False}


def test_bundled_templates_include_the_generic_one_and_the_apc40():
    templates = config.bundled_templates()
    assert {"generic", "apc40_mk2"} <= set(templates)
    assert config.match_template([APC_PORT]) == "apc40_mk2"
    assert config.match_template(["Some Other Controller"]) is None
    assert config.match_template([]) is None


def test_profile_ids_are_unique_slugs():
    a = config.create_profile("Mon Contrôleur Préféré", "generic")
    b = config.create_profile("Mon Contrôleur Préféré", "generic")
    assert a == "mon-controleur-prefere" and b == "mon-controleur-prefere-2"
    assert {pid for pid, _ in config.list_profiles()} == {a, b}


def test_save_load_delete():
    pid = config.create_profile("Test", "apc40_mk2")
    profile = config.load_profile(pid)
    profile.groups[0]["app"] = "Discord"
    profile.port = "port"
    config.save_profile(pid, profile)
    again = config.load_profile(pid)
    assert again.groups[0]["app"] == "Discord" and again.port == "port" and len(again.groups) == 8
    config.delete_profile(pid)
    assert config.load_profile(pid) is None and config.list_profiles() == []


def test_corrupt_or_missing_profile_files_are_ignored():
    with open(config.profile_path("broken"), "w") as f:
        f.write("{nope")
    assert config.load_profile("broken") is None and config.load_profile("absent") is None
    assert config.list_profiles() == []


def test_duplicate_keeps_the_user_state_under_a_new_name():
    pid = config.create_profile("Original", "apc40_mk2")
    original = config.load_profile(pid)
    original.groups[1]["app"] = "Spotify"
    copy_id = config.duplicate_profile(original, "Copie")
    copy = config.load_profile(copy_id)
    assert copy_id != pid and copy.name == "Copie" and copy.groups[1]["app"] == "Spotify"


def test_export_then_import_shares_a_profile_without_the_machine_specific_port(tmp_path):
    profile = Profile.from_template(config.bundled_templates()["apc40_mk2"], "Partagé")
    profile.port = "my port"
    profile.groups[0]["app"] = "Spotify"
    path = str(tmp_path / "shared.json")
    config.export_profile(profile, path)
    assert json.load(open(path))["port"] is None and profile.port == "my port"   # original untouched
    pid = config.import_profile(path)
    imported = config.load_profile(pid)
    assert imported.name == "Partagé" and imported.port is None and imported.groups[0]["app"] == "Spotify"


def test_importing_something_that_is_not_a_profile_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]")
    with pytest.raises(ValueError):
        config.import_profile(str(bad))
    with pytest.raises(ValueError):
        config.import_profile(str(tmp_path / "missing.json"))


def test_first_run_creates_a_generic_profile_without_a_known_controller():
    settings, pid = config.bootstrap(["Some Controller"])
    profile = config.load_profile(pid)
    assert settings["active_profile"] == pid and profile.name == "Mon contrôleur" and not profile.has_leds
    assert config.load_settings()["active_profile"] == pid


def test_first_run_with_an_apc40_plugged_creates_the_apc40_profile():
    _settings, pid = config.bootstrap([APC_PORT])
    profile = config.load_profile(pid)
    assert profile.name == "APC40 mkII" and profile.has_leds and len(profile.groups) == 8


def test_bootstrap_keeps_the_active_profile_and_recovers_from_a_missing_one():
    pid = config.create_profile("A", "generic")
    other = config.create_profile("B", "generic")
    config.save_settings({"active_profile": other, "autostart": True})
    assert config.bootstrap([])[1] == other
    config.delete_profile(other)                        # active profile vanished
    assert config.bootstrap([])[1] == pid


def test_legacy_single_controller_config_becomes_an_apc40_profile():
    legacy = {"mappings": {"1:20": "Spotify"}, "_port": APC_PORT, "autostart": False,
              "groups": [{"uid": "g1", "name": "Musique", "app": "Spotify", "volume": "1:7", "mute": "1:49",
                          "assign": "1:0", "color": "Bleu"}]}
    with open(config.legacy_config_path(), "w") as f:
        json.dump(legacy, f)
    settings, pid = config.bootstrap([])
    profile = config.load_profile(pid)
    assert profile.name == "APC40 mkII" and profile.port == APC_PORT and profile.has_leds
    assert profile.mappings == {"1:20": "Spotify"}
    assert [(g["uid"], g["name"], g["app"], g["color"]) for g in profile.groups] == [("g1", "Musique", "Spotify", "Bleu")]
    assert settings["autostart"] is False
    assert not os.path.exists(config.legacy_config_path())          # not migrated twice
    assert os.path.exists(config.legacy_config_path() + ".migrated")


def test_oldest_flat_mapping_file_is_migrated_too():
    with open(config.legacy_config_path(), "w") as f:
        json.dump({"56": "MASTER"}, f)
    _settings, pid = config.bootstrap([])
    assert config.load_profile(pid).mappings == {"56": "MASTER"}
