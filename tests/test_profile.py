import json

import pytest

from midi_mixer.config import bundled_templates
from midi_mixer.profile import Profile


def test_neutral_profile_has_safe_defaults():
    p = Profile()
    assert (p.buttons_message, p.button_trigger, p.has_leds) == ("note", "press", False)
    assert len(p.groups) == 4 and all(g[f] == "" for g in p.groups for f in ("volume", "mute", "assign"))
    assert not p.matches_port("anything")                # no hint = matches nothing
    assert p.color_velocity("Vert") is None and p.color_hex("Vert") is None


def test_apc40_template_lays_groups_out_by_column(apc_profile):
    assert len(apc_profile.groups) == 8
    first, last = apc_profile.groups[0], apc_profile.groups[7]
    assert (first["volume"], first["mute"], first["assign"]) == ("1:7", "1:49", "1:0")
    assert (last["volume"], last["mute"], last["assign"]) == ("8:7", "8:49", "8:0")
    assert len({g["uid"] for g in apc_profile.groups}) == 8
    assert apc_profile.mappings == {"14": "MASTER"}
    assert apc_profile.matches_port("APC40 mkII:APC40 mkII MIDI 1 24:0")


def test_template_steps_can_move_the_number_instead_of_the_channel():
    p = Profile({"group_template": {"count": 3, "volume": {"number": 0, "channel": 1, "number_step": 1},
                                    "mute": {"number": 32, "number_step": 1}}})   # no channel: any channel
    assert [p.template_key("volume", i) for i in range(3)] == ["1:0", "1:1", "1:2"]
    assert [p.template_key("mute", i) for i in range(3)] == ["32", "33", "34"]
    assert p.template_key("assign", 0) == ""             # unbound field


def test_template_positions_outside_midi_range_are_unbound():
    p = Profile({"group_template": {"volume": {"number": 120, "channel": 15, "number_step": 4, "channel_step": 1}}})
    assert p.template_key("volume", 0) == "15:120"
    assert p.template_key("volume", 1) == "16:124"
    assert p.template_key("volume", 2) == ""             # channel 17 and number 128 do not exist


def test_new_group_takes_the_first_free_position_and_name(apc_profile):
    groups = [g for g in apc_profile.groups if g["name"] != "Groupe 3"]
    new = apc_profile.new_group(groups)
    assert (new["name"], new["volume"]) == ("Groupe 3", "3:7")
    full = apc_profile.new_group(apc_profile.groups)
    assert full["name"] == "Groupe 9" and (full["volume"], full["mute"], full["assign"]) == ("", "", "")


def test_leds_and_colors(apc_profile):
    assert apc_profile.has_leds and apc_profile.default_color == "Vert"
    assert apc_profile.color_velocity("Bleu") == 45
    assert apc_profile.color_velocity("unknown") == 21   # falls back to the default color
    assert apc_profile.color_hex("Rouge") == "#e74c3c"


def test_colors_may_be_plain_numbers_and_leds_without_palette_use_the_on_value():
    p = Profile({"leds": {"colors": {"Red": 15, "Green": 60}}})
    assert p.color_velocity("Green") == 60 and p.colors["Green"]["hex"].startswith("#")
    plain = Profile({"leds": {"on": 100}})
    assert plain.has_leds and plain.colors == {} and plain.color_velocity("x") == 100


def test_button_options_are_validated():
    p = Profile({"buttons": {"message": "sysex", "trigger": "whenever"}})
    assert (p.buttons_message, p.button_trigger) == ("note", "press")
    cc = Profile({"buttons": {"message": "cc", "trigger": "any"}})
    assert (cc.button_label, cc.button_learn_kind) == ("CC", "control_change")
    assert (Profile().button_label, Profile().button_learn_kind) == ("Note", "note")


def test_garbage_input_never_raises():
    p = Profile({"name": None, "port": 5, "leds": "x", "group_template": {"count": "many", "volume": {"number": "x"}},
                 "mappings": [1], "groups": [1, None, {"name": "ok"}], "buttons": []})
    assert p.name == "Profil" and p.port is None and not p.has_leds
    assert [g["name"] for g in p.groups] == ["ok"] and p.groups[0]["uid"]


def test_saved_groups_with_an_unknown_color_get_the_default_one(apc_profile):
    data = apc_profile.to_dict()
    data["groups"][0]["color"] = "Turquoise"
    assert Profile(data).groups[0]["color"] == "Vert"


def test_to_dict_roundtrip_is_lossless(apc_profile):
    apc_profile.port = "some port"
    apc_profile.groups[2]["app"] = "Discord"
    again = Profile(json.loads(json.dumps(apc_profile.to_dict())))
    assert again.to_dict() == apc_profile.to_dict()


def test_from_template_drops_user_state():
    data = dict(bundled_templates()["apc40_mk2"], port="x", groups=[{"name": "mine"}])
    p = Profile.from_template(data, "Copie")
    assert p.name == "Copie" and p.port is None and len(p.groups) == 8


@pytest.mark.parametrize("template_id", list(bundled_templates()))
def test_every_bundled_template_is_a_valid_profile(template_id):
    p = Profile.from_template(bundled_templates()[template_id])
    assert p.name and p.template["count"] == len(p.groups)
    assert Profile(p.to_dict()).to_dict() == p.to_dict()
