"""B1 of the feature plan of 2026-11-12: the Share card (``GET /api/instances/<id>/share``)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from apps.core.models import SystemSetting
from apps.games import joining
from apps.instances.models import Instance

from .test_games_phase2 import factorio  # noqa: F401  (fixture)
from .test_games_six import create, start
from .test_terraria import make_tr

pytestmark = pytest.mark.django_db


def _share(client: Any, inst: Instance, **params: Any) -> Any:
    return client.get(f"/api/instances/{inst.id}/share", params)


def test_minecraft_card(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    SystemSetting.set("public_address", "play.example.org")
    inst = make_instance(minecraft_version="1.21.4")
    fake_agent.put_file(inst.id, "server.properties", "white-list=false\nonline-mode=true\n")
    card = _share(clients["operator"], inst).json()
    assert card["address"] == {"host": "play.example.org", "source": "panel", "note": ""}
    assert card["connect"] == f"play.example.org:{inst.game_port}"
    assert card["ports"][0]["port"] == inst.game_port and card["ports"][0]["protocol"] == "tcp"
    assert card["protection"] == {"kind": "whitelist", "on": False, "label": "a whitelist"}
    assert "Minecraft: Java Edition 1.21.4" in card["requirements"]
    assert any("Multiplayer" in s for s in card["steps"]) and "password" not in card
    checks = {c["id"]: c for c in card["checklist"]}
    assert checks["protection"]["ok"] is False and "whitelist is off" in checks["protection"]["hint"]
    assert checks["public_address"]["ok"] is True and checks["ports"]["label"].startswith("Forward on your router: TCP")
    fake_agent.put_file(inst.id, "server.properties", "white-list=true\n")
    assert _share(clients["owner"], inst).json()["protection"]["on"] is True


def test_the_password_only_for_configure_and_in_the_invite_only_when_asked(
    clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    SystemSetting.set("public_address", "203.0.113.5")
    vh = create(clients, django_capture_on_commit_callbacks, "valheim", secrets={"server_password": "sesame-42"})
    owner = _share(clients["owner"], vh)
    assert owner.status_code == 200 and owner["Cache-Control"] == "no-store"
    card = owner.json()
    assert card["password"] == "sesame-42" and "sesame-42" not in card["invite_text"]
    assert card["protection"]["on"] is True and "Password: ask me for it" in card["invite_text"]
    asked = _share(clients["owner"], vh, with_password="true").json()
    assert "Password: sesame-42" in asked["invite_text"] and asked["invite_has_password"] is True
    viewer = _share(clients["viewer"], vh)
    assert viewer.status_code == 200 and "password" not in viewer.json() and "sesame-42" not in viewer.content.decode()
    assert _share(clients["viewer"], vh, with_password="true").status_code == 403


def test_no_public_address(make_instance: Any, fake_agent: Any, clients: Any, node: Any) -> None:
    inst = make_instance()
    card = _share(clients["owner"], inst).json()
    assert card["address"]["host"] is None and "Public address" in card["address"]["note"]
    assert card["connect"] is None and "Address: ask me for it" in card["invite_text"]
    assert {c["id"]: c for c in card["checklist"]}["public_address"]["ok"] is False
    node.public_address = "nas.example.net"
    node.save(update_fields=["public_address"])
    assert _share(clients["owner"], inst).json()["address"] == {"host": "nas.example.net", "source": "node", "note": ""}


def test_passwords_read_from_the_games_files(
    make_instance: Any,
    fake_agent: Any,
    clients: Any,
    factorio: Any,  # noqa: F811
) -> None:
    fake_agent.put_file(factorio.id, "config/server-settings.json", json.dumps({"game_password": "fact-pw", "name": "n"}))
    card = _share(clients["owner"], factorio).json()
    assert card["password"] == "fact-pw" and card["protection"]["on"] is True
    vanilla = make_tr(make_instance, "vanilla", game_port=7781)
    fake_agent.put_file(vanilla.id, "serverconfig.txt", "maxplayers=8\npassword=\n")
    card = _share(clients["owner"], vanilla).json()
    assert card["password"] == "" and card["protection"]["on"] is False
    assert "Anyone who knows the address" in {c["id"]: c for c in card["checklist"]}["protection"]["hint"]
    tshock = make_tr(make_instance, "tshock", game_port=7782)
    fake_agent.put_file(tshock.id, "config.json", json.dumps({"Settings": {"ServerPassword": "ts-pw"}}))
    card = _share(clients["owner"], tshock).json()
    assert card["password"] == "ts-pw" and any("/register" in s for s in card["steps"])


@pytest.mark.parametrize("game", ["palworld", "valheim", "project-zomboid", "hkmp", "cs2", "rust"])
def test_every_game_has_steps_and_the_invite_never_holds_a_secret(
    game: str, clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    SystemSetting.set("public_address", "198.51.100.7")
    secrets = {"server_password": "Secret77"} if game in ("valheim", "palworld", "cs2") else {}
    inst = create(clients, django_capture_on_commit_callbacks, game, **({"secrets": secrets} if secrets else {}))
    start(fake_agent, inst)
    card = _share(clients["owner"], inst).json()
    assert card["steps"] and all(isinstance(s, str) and s for s in card["steps"])
    assert "198.51.100.7" in card["invite_text"]
    for value in [*inst.secret_values.values()]:
        assert value not in card["invite_text"], game
    if game == "rust":
        assert card["protection"] == {"kind": "none", "on": False, "label": "nothing"}
    if game == "hkmp":
        assert any("HKMP mod 2.4.3" in r for r in card["requirements"])


def test_the_guides_cover_every_game() -> None:
    from apps import games

    assert {d.id for d in games.all()} <= set(joining.GUIDES)
