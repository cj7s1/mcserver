"""Multi-game phase 1: pins of today's Minecraft outputs (written before the game definition layer).

The container spec and its hash decide "restart required" for every existing server: a single
changed byte recreates every server at its next start. These tests compare the exact
``spec.build_spec`` dict and hash of a set of fixture servers with what the release before the
refactor produced (tests/games_pin_expected.py), the instance API with today's fields and values,
and the gating of today's Minecraft-only endpoints. They must pass unchanged after the refactor.
"""

from __future__ import annotations

import pytest

from apps.core.models import SystemSetting
from apps.instances.models import Instance
from apps.instances.spec import build_spec, current_spec_hash, spec_hash

from .games_pin_expected import API, API_KEYS, HASHES, SPECS
from .games_pin_fixtures import BASE, FIXTURES

pytestmark = pytest.mark.django_db


def _configure(name, settings):
    fields, system, django = FIXTURES[name]
    settings.CURSEFORGE_API_KEY = django.get("CURSEFORGE_API_KEY", "")
    for key, value in system.items():
        SystemSetting.set(key, value)
    return {**BASE, **fields}


def test_every_fixture_is_pinned():
    assert sorted(FIXTURES) == sorted(SPECS) == sorted(HASHES) == sorted(API)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_minecraft_spec_is_unchanged(name, settings):
    instance = Instance(**_configure(name, settings))
    spec = build_spec(instance)
    assert spec == SPECS[name]
    # same key order too: the agent receives the dict as JSON
    assert list(spec) == list(SPECS[name])
    assert list(spec["env"]) == list(SPECS[name]["env"])
    assert "game" not in spec and "variant" not in spec


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_minecraft_spec_hash_is_unchanged(name, settings):
    instance = Instance(**_configure(name, settings))
    current, format1 = HASHES[name]
    assert spec_hash(build_spec(instance)) == current
    assert spec_hash(build_spec(instance), 1) == format1
    assert current_spec_hash(instance) == current


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_saved_minecraft_server_has_the_same_spec(name, settings, node):
    """A server read back from the database (the jobs' copy) builds the same spec."""
    created = Instance.objects.create(node=node, **_configure(name, settings))
    instance = Instance.objects.select_related("node").get(pk=created.pk)
    assert build_spec(instance) == SPECS[name]
    assert current_spec_hash(instance) == HASHES[name][0]


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_instance_api_keeps_todays_fields_and_values(name, settings, node, users, client_for):
    created = Instance.objects.create(node=node, **_configure(name, settings))
    data = client_for(users["owner"]).get(f"/api/instances/{created.id}").json()
    # existing keys keep their order; new keys may only follow
    assert [k for k in data if k in API_KEYS] == API_KEYS
    assert {k: data[k] for k in API[name]} == API[name]


def test_instance_list_keeps_todays_fields(settings, node, users, client_for):
    created = Instance.objects.create(node=node, **_configure("paper", settings))
    rows = client_for(users["owner"]).get("/api/instances/").json()
    row = next(r for r in rows if r["id"] == str(created.id))
    assert [k for k in row if k in API_KEYS] == API_KEYS
    assert {k: row[k] for k in API["paper"]} == API["paper"]


# --- gating of today's Minecraft-only endpoints ------------------------------------------------
def _server(name, settings, node, fake_agent):
    instance = Instance.objects.create(node=node, **_configure(name, settings))
    fake_agent.put_file(instance.id, "server.properties", "motd=Pinned\nlevel-name=world\n")
    return instance


def _code(response):
    return (response.json().get("error") or {}).get("code")


@pytest.mark.parametrize(("name", "supported"), [("vanilla", False), ("imported", False), ("paper", True), ("forge", True)])
def test_content_gate(name, supported, settings, node, fake_agent, users, client_for):
    from apps.content.service import context_for

    instance = _server(name, settings, node, fake_agent)
    response = client_for(users["owner"]).get(f"/api/instances/{instance.id}/content/")
    if supported:
        assert _code(response) != "content_unsupported"
        assert context_for(instance).kind == ("plugins" if name == "paper" else "mods")
    else:
        assert response.status_code == 409 and _code(response) == "content_unsupported"


@pytest.mark.parametrize("name", ["modrinth_pack", "curseforge_pack", "ftb_pack"])
def test_content_gate_for_modpacks(name, settings, node, fake_agent):
    from apps.content.service import context_for

    instance = _server(name, settings, node, fake_agent)
    assert context_for(instance).kind == "mods"


@pytest.mark.parametrize("name", ["vanilla", "paper", "fabric", "imported"])
def test_modpack_endpoints_refuse_standard_servers(name, settings, node, fake_agent, users, client_for):
    instance = _server(name, settings, node, fake_agent)
    client = client_for(users["owner"])
    for response in (
        client.get(f"/api/instances/{instance.id}/modpack"),
        client.post(f"/api/instances/{instance.id}/modpack/check"),
    ):
        assert response.status_code == 409 and _code(response) == "not_modpack"


@pytest.mark.parametrize("name", ["modrinth_pack", "curseforge_pack", "ftb_pack"])
def test_software_update_refuses_modpacks(name, settings, node, fake_agent, users, client_for):
    instance = _server(name, settings, node, fake_agent)
    client = client_for(users["owner"])
    response = client.post(f"/api/instances/{instance.id}/software", {"minecraft_version": "1.21.1"}, format="json")
    assert response.status_code == 400 and _code(response) == "use_modpack_update"
    response = client.patch(f"/api/instances/{instance.id}", {"minecraft_version": "1.12.2"}, format="json")
    assert response.status_code == 400
    assert "pinned modpack release" in str(response.json())


@pytest.mark.parametrize("name", ["vanilla", "paper", "modrinth_pack", "imported"])
def test_minecraft_feature_endpoints_answer(name, settings, node, fake_agent, users, client_for):
    """The endpoints that only make sense for Minecraft answer for every Minecraft server."""
    instance = _server(name, settings, node, fake_agent)
    client = client_for(users["owner"])
    base = f"/api/instances/{instance.id}"
    properties = client.get(f"{base}/properties")
    assert properties.status_code == 200 and properties.json()["values"]["motd"] == "Pinned"
    assert client.get(f"{base}/world").status_code == 200
    assert client.get(f"{base}/world/reset").status_code == 200
    assert client.get(f"{base}/players").status_code == 200
    assert client.get(f"{base}/diagnostics").status_code == 200
    # a stopped server: the command reaches the agent, which answers "not running"
    response = client.post(f"{base}/command", {"command": "/say hi"}, format="json")
    assert response.status_code == 409 and _code(response) == "not_running"
    schedule = client.post(
        f"{base}/schedules",
        {"name": "hello", "action": "command", "cron": "0 4 * * *", "payload": {"command": "/say hi"}},
        format="json",
    )
    assert schedule.status_code == 201 and schedule.json()["payload"] == {"command": "say hi"}


def test_running_server_commands_and_power_warnings(settings, node, fake_agent, users, client_for):
    instance = _server("paper", settings, node, fake_agent)
    fake_agent.apply(instance.id, build_spec(instance), start=True)
    Instance.objects.filter(pk=instance.pk).update(status="running", container_state="running")
    client = client_for(users["owner"])
    response = client.post(f"/api/instances/{instance.id}/command", {"command": "//list"}, format="json")
    assert response.status_code == 200 and response.json()["command"] == "list"
    assert fake_agent.commands[-1] == (str(instance.id), "list")


def test_console_stop_word_and_command_normalisation():
    from apps.console.consumers import is_stop_command

    for text in ("stop", " STOP ", "/stop", "//Stop "):
        assert is_stop_command(text)
    for text in ("stop now", "say stop", "restart", "/"):
        assert not is_stop_command(text)
