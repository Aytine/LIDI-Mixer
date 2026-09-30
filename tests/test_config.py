import json

from midi_mixer import config
from midi_mixer.constants import DEFAULT_GROUP_COUNT, DEFAULT_MAPPINGS


def test_defaults_for_a_first_run():
    cfg = config.load_config()
    assert cfg["mappings"] == DEFAULT_MAPPINGS
    assert cfg["autostart"] is True
    assert len(cfg["groups"]) == DEFAULT_GROUP_COUNT
    first = cfg["groups"][0]
    assert (first["volume"], first["mute"], first["assign"], first["app"]) == ("1:7", "1:49", "1:0", "")
    assert len({g["uid"] for g in cfg["groups"]}) == DEFAULT_GROUP_COUNT   # uids are unique


def test_save_then_load_roundtrip():
    cfg = config.load_config()
    cfg["mappings"] = {"1:20": "Spotify"}
    cfg["port"] = "APC40 mkII"
    cfg["groups"][0]["app"] = "Discord"
    config.save_config(cfg)
    again = config.load_config()
    assert again["mappings"] == {"1:20": "Spotify"}
    assert again["port"] == "APC40 mkII"
    assert again["groups"][0]["app"] == "Discord"


def test_old_assign_defaults_are_migrated_to_the_top_pad():
    path = config.config_path()
    groups = [
        {"uid": "a", "name": "G1", "app": "", "volume": "1:7", "mute": "1:49", "assign": "1:51"},   # old default
        {"uid": "b", "name": "G2", "app": "", "volume": "2:7", "mute": "2:49", "assign": "2:48"},   # old default
        {"uid": "c", "name": "G3", "app": "", "volume": "3:7", "mute": "3:49", "assign": "3:5"},    # chosen by the user
    ]
    with open(path, "w") as f:
        json.dump({"mappings": {}, "_port": None, "groups": groups}, f)
    migrated = config.load_config()["groups"]
    assert [g["assign"] for g in migrated] == ["1:0", "2:0", "3:5"]
    assert all(g["color"] == "Vert" for g in migrated)   # color added for groups saved before it existed


def test_legacy_flat_mapping_file_is_still_read():
    with open(config.config_path(), "w") as f:
        json.dump({"56": "MASTER"}, f)
    assert config.load_config()["mappings"] == {"56": "MASTER"}


def test_corrupt_file_falls_back_to_defaults():
    with open(config.config_path(), "w") as f:
        f.write("{not json")
    assert config.load_config()["mappings"] == DEFAULT_MAPPINGS
