"""server.properties parsing/updating (comments + order preserved, escaping, managed keys)."""

from __future__ import annotations

import pytest

from apps.instances import properties

SAMPLE = """#Minecraft server properties
#Mon Nov 03 17:00:00 UTC 2026
! bang comment
enable-rcon=true
motd=A Minecraft Server
max-players = 20
level-seed=
gamemode:survival
motd2=Line\\nTwo\\tTabbed \\\\ back\\u00A7aColour
custom.key=keep me
"""


def test_parse_values():
    values = properties.parse(SAMPLE)
    assert values["motd"] == "A Minecraft Server"
    assert values["max-players"] == "20"
    assert values["level-seed"] == ""
    assert values["gamemode"] == "survival"
    assert values["custom.key"] == "keep me"
    assert values["motd2"] == "Line\nTwo\tTabbed \\ back§aColour"
    assert not any(k.startswith(("#", "!")) for k in values)


def test_parse_key_without_separator_and_blank_lines():
    assert properties.parse("\n   \nlonely\n  # indented comment\n") == {"lonely": ""}


def test_parse_last_duplicate_wins_like_java():
    assert properties.parse("pvp=true\npvp=false\n")["pvp"] == "false"


def test_update_preserves_comments_order_and_unknown_keys():
    out = properties.update(SAMPLE, {"motd": "Welcome", "difficulty": "hard"})
    lines = out.splitlines()
    assert lines[:4] == SAMPLE.splitlines()[:4]  # comments + untouched keys verbatim
    assert lines[4] == "motd=Welcome"
    assert "max-players = 20" in lines  # untouched formatting kept
    assert "custom.key=keep me" in lines
    assert lines[-1] == "difficulty=hard"  # new keys appended
    assert out.endswith("\n")
    values = properties.parse(out)
    assert values["motd"] == "Welcome" and values["difficulty"] == "hard" and values["gamemode"] == "survival"


def test_update_none_deletes_key():
    out = properties.update(SAMPLE, {"custom.key": None, "not-present": None})
    assert "custom.key" not in properties.parse(out)
    assert "not-present" not in out


def test_update_replaces_colon_separated_and_spaced_keys():
    out = properties.update(SAMPLE, {"gamemode": "creative", "max-players": "50"})
    values = properties.parse(out)
    assert values["gamemode"] == "creative"
    assert values["max-players"] == "50"
    assert "gamemode:survival" not in out


def test_update_escapes_values_and_round_trips():
    tricky = "Line1\nLine2\r\tC:\\server\\path = x : y"
    out = properties.update("", {"motd": tricky})
    assert out.count("\n") == 1  # value stays on one line
    assert properties.parse(out)["motd"] == tricky


def test_update_cannot_inject_additional_keys_via_newlines():
    out = properties.update("server-port=25565\n", {"motd": "hi\nserver-port=1\nenable-rcon=false"})
    values = properties.parse(out)
    assert values["server-port"] == "25565"
    assert "enable-rcon" not in values


def test_update_handles_duplicate_keys():
    out = properties.update("pvp=true\nmotd=x\npvp=true\n", {"pvp": "false"})
    assert properties.parse(out)["pvp"] == "false"
    assert out == "pvp=false\nmotd=x\n"


def test_render_initial_file():
    out = properties.render({"motd": "Hi", "pvp": False, "max-players": 10})
    assert out.startswith("#Minecraft server properties\n")
    assert properties.parse(out) == {"motd": "Hi", "pvp": "false", "max-players": "10"}


# --- validate_changes -------------------------------------------------------------------
@pytest.mark.parametrize("key", sorted(properties.MANAGED_KEYS))
def test_managed_keys_are_rejected(key):
    with pytest.raises(ValueError, match="managed by MCPanel"):
        properties.validate_changes({key: "x"})


@pytest.mark.parametrize("key", ["", "a=b", "a:b", "a b", "#x", "line\nbreak", "cr\r", "x" * 101])
def test_invalid_property_names(key):
    with pytest.raises(ValueError, match="Invalid property name"):
        properties.validate_changes({key: "1"})


def test_non_string_key_rejected():
    with pytest.raises(ValueError):
        properties.validate_changes({5: "1"})


@pytest.mark.parametrize(("value", "expected"), [(True, "true"), (False, "false"), ("TRUE", "true"), ("False", "false")])
def test_bool_normalisation(value, expected):
    assert properties.validate_changes({"pvp": value}) == {"pvp": expected}


def test_bool_rejects_other_values():
    with pytest.raises(ValueError, match="true or false"):
        properties.validate_changes({"pvp": "yes"})


def test_int_validation_and_bounds():
    assert properties.validate_changes({"max-players": "25"}) == {"max-players": "25"}
    assert properties.validate_changes({"max-players": 7}) == {"max-players": "7"}
    assert properties.validate_changes({"max-tick-time": -1}) == {"max-tick-time": "-1"}
    with pytest.raises(ValueError, match="whole number"):
        properties.validate_changes({"max-players": "ten"})
    with pytest.raises(ValueError, match="at least 1"):
        properties.validate_changes({"max-players": 0})
    with pytest.raises(ValueError, match="at most 32"):
        properties.validate_changes({"view-distance": 33})
    with pytest.raises(ValueError, match="at least 1"):
        properties.validate_changes({"op-permission-level": 0})


def test_enum_validation():
    assert properties.validate_changes({"difficulty": "hard"}) == {"difficulty": "hard"}
    with pytest.raises(ValueError, match="must be one of"):
        properties.validate_changes({"difficulty": "nightmare"})


def test_none_means_delete_and_unknown_keys_pass_through():
    assert properties.validate_changes({"motd": None, "some-mod.option": 3}) == {"motd": None, "some-mod.option": "3"}


def test_value_length_limit():
    properties.validate_changes({"motd": "x" * 4096})
    with pytest.raises(ValueError, match="too long"):
        properties.validate_changes({"motd": "x" * 4097})


def test_schema_keys_are_unique_and_never_managed():
    keys = [s["key"] for s in properties.SCHEMA]
    assert len(keys) == len(set(keys))
    assert not set(keys) & properties.MANAGED_KEYS
