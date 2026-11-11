"""Multi-game phase 1: the game definition layer (apps/games) with Minecraft as the only game.

The registry, Minecraft's capabilities per server type (they reproduce the gating that existed
before), the API fields and the game catalog, the agent gate for other games (409
agent_lacks_game), capability gating of the Minecraft-only endpoints for a game without them
(a test game registered here), and the game in the software record of backups.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from apps import games
from apps.backups.handlers import backup_game
from apps.backups.models import Backup
from apps.games.base import CAPABILITIES, GameDefinition, Variant
from apps.games.minecraft import MinecraftDefinition
from apps.instances.models import Instance, ServerType
from apps.instances.spec import build_minecraft_spec, build_spec, current_spec_hash
from apps.jobs import runner
from apps.jobs.models import JobStatus

pytestmark = pytest.mark.django_db

ALL = frozenset(CAPABILITIES)


class FakeGame(GameDefinition):
    """A game without any optional feature (what Factorio & co. will look like before their features)."""

    id = "testgame"
    label = "Test Game"
    profile = "testgame"
    variants = (Variant("vanilla", "Vanilla"),)
    memory_model = "plain"

    def build_spec(self, instance: Instance) -> dict[str, Any]:
        return {
            "name": instance.name,
            "game": self.id,
            "image": "example/testgame:1",
            "env": {},
            "memory_limit_mb": instance.container_memory_mb,
            "ports": [{"container_port": 7777, "host_port": instance.game_port, "protocol": "tcp", "host_ip": instance.host_ip}],
            "restart_policy": instance.restart_policy,
            "stop_timeout": instance.stop_timeout,
            "rcon_password": "x" * 32,
        }


@pytest.fixture
def testgame():
    definition = games.register(FakeGame())
    yield definition
    games.unregister(definition.id)


@pytest.fixture
def other(make_instance, testgame):
    return make_instance(name="Other", game="testgame", server_type="vanilla", image="example/testgame:1", memory_mb=1024)


def err(resp) -> str:
    return resp.json()["error"]["code"]


def run(kind, inst, capture, **params):
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


# --- registry -------------------------------------------------------------------------------------
def test_registry_has_the_built_in_games():
    # phase 1 had Minecraft only; phase 3 adds Factorio (tests/test_games_phase2.py); later phases add
    # more games after them (Terraria: tests/test_terraria.py)
    assert games.ids()[:2] == ["minecraft", "factorio"] and "terraria" in games.ids()
    assert [d.id for d in games.all()][:2] == ["minecraft", "factorio"]
    mc = games.get("minecraft")
    assert isinstance(mc, MinecraftDefinition) and games.get(None) is mc and games.get("") is mc
    assert games.get_or_none("nogame") is None
    with pytest.raises(games.UnknownGame) as exc:
        games.get("nogame")
    assert exc.value.status_code == 409 and exc.value.error_code == "unknown_game"


def test_register_and_unregister(testgame):
    # (Minecraft first, then the built-in games, then the ones tests register)
    ids = games.ids()
    assert ids[:2] == ["minecraft", "factorio"] and ids[-1] == "testgame" and games.get("testgame") is testgame
    with pytest.raises(ValueError, match="already registered"):
        games.register(FakeGame())
    with pytest.raises(ValueError, match="already registered"):
        games.register(MinecraftDefinition())
    with pytest.raises(ValueError, match="cannot be removed"):
        games.unregister("minecraft")


def test_for_instance(make_instance):
    inst = make_instance()
    assert inst.game == "minecraft" and games.for_instance(inst).id == "minecraft"
    inst.game = "nosuchgame"
    with pytest.raises(games.UnknownGame):
        games.for_instance(inst)
    assert games.capabilities(inst) == frozenset()


def test_game_column_defaults_to_minecraft_in_the_database(node):
    """db_default: rows inserted without the column (an older release after a rollback) are Minecraft's."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_default FROM information_schema.columns WHERE table_name = %s AND column_name = 'game'",
            ["instances_instance"],
        )
        (default,) = cursor.fetchone()
    assert "minecraft" in default
    assert Instance(name="x", node=node, game_port=1).game == "minecraft"


# --- Minecraft's definition: today's gating and behaviour ------------------------------------------
@pytest.mark.parametrize("server_type", ServerType.values)
def test_minecraft_capabilities_match_todays_gating(server_type):
    inst = Instance(server_type=server_type)
    caps = games.capabilities(inst)
    assert ("content" in caps) == (inst.content_kind != "none")
    assert ("modpacks" in caps) == inst.is_modpack
    assert caps == ALL - ({"content"} if inst.content_kind == "none" else set()) - (set() if inst.is_modpack else {"modpacks"})


@pytest.mark.parametrize(
    ("server_type", "content", "modpacks"),
    [
        ("VANILLA", False, False),
        ("CUSTOM", False, False),
        ("PAPER", True, False),
        ("PURPUR", True, False),
        ("FOLIA", True, False),
        ("FABRIC", True, False),
        ("QUILT", True, False),
        ("FORGE", True, False),
        ("NEOFORGE", True, False),
        ("MODRINTH", True, True),
        ("AUTO_CURSEFORGE", True, True),
        ("FTBA", True, True),
    ],
)
def test_minecraft_capabilities_per_server_type(server_type, content, modpacks):
    caps = games.get("minecraft").capabilities(Instance(server_type=server_type))
    assert ("content" in caps, "modpacks" in caps) == (content, modpacks)
    assert ALL - {"content", "modpacks"} <= caps


def test_minecraft_definition_data():
    mc = games.get("minecraft")
    assert (mc.label, mc.memory_model, mc.profile, mc.control) == ("Minecraft", "jvm", "minecraft", "minecraft-rcon")
    assert [v.id for v in mc.variants] == list(ServerType.values)
    assert [v.label for v in mc.variants] == list(ServerType.labels)
    assert mc.capabilities() == ALL
    assert mc.stop_words == {"stop"} and mc.flush_command == "save-all flush"
    assert mc.normalise_command("//say hi") == "say hi" and mc.normalise_command("list") == "list"
    assert mc.is_stop_command(" /STOP ") and not mc.is_stop_command("stop now")
    (config,) = mc.config_files()
    assert (config.path, config.codec) == ("server.properties", "java-properties")
    assert {"server-port", "rcon.port", "enable-rcon", "rcon.password"} == config.managed_keys


def test_minecraft_build_spec_is_todays_builder(make_instance):
    inst = make_instance(server_type="PAPER", loader_version="130")
    assert games.for_instance(inst).build_spec(inst) == build_minecraft_spec(inst) == build_spec(inst)
    assert "game" not in build_spec(inst) and "variant" not in build_spec(inst)


def test_minecraft_initial_files_and_memory(make_instance):
    from apps.instances import properties

    inst = make_instance(memory_mb=4096)
    mc = games.for_instance(inst)
    assert mc.initial_files(inst, {}) == {}
    assert mc.initial_files(inst, {"server_properties": {"motd": "Hi"}}) == {
        "server.properties": properties.render({"motd": "Hi"})
    }
    assert inst.container_memory_mb == inst.jvm_container_memory_mb == 4096 + int(4096 * 0.3)


def test_minecraft_software_record(make_instance):
    from apps.instances.handlers import describe_software, software_state

    inst = make_instance(server_type="PAPER", minecraft_version="1.21.1", loader_version="130", java_version=21)
    state = software_state(inst)
    assert next(iter(state)) == "game" and state["game"] == "minecraft"
    assert state["server_type"] == "PAPER" and state["java_version"] == 21
    text = "Paper 1.21.1 (loader 130) on Java 21"
    assert describe_software(state) == text
    assert describe_software({k: v for k, v in state.items() if k != "game"}) == text  # records from before games


# --- API --------------------------------------------------------------------------------------------
def test_instance_api_fields(clients, make_instance):
    inst = make_instance(server_type="FORGE")
    data = clients["owner"].get(f"/api/instances/{inst.id}").json()
    assert data["game"] == "minecraft" and data["game_label"] == "Minecraft" and data["memory_model"] == "jvm"
    assert data["capabilities"] == sorted(ALL - {"modpacks"})
    row = next(r for r in clients["owner"].get("/api/instances/").json() if r["id"] == str(inst.id))
    assert {k: row[k] for k in ("game", "game_label", "capabilities", "memory_model")} == {
        k: data[k] for k in ("game", "game_label", "capabilities", "memory_model")
    }


def test_capabilities_list_is_sorted_and_per_server(clients, make_instance):
    vanilla = clients["owner"].get(f"/api/instances/{make_instance().id}").json()
    pack = clients["owner"].get(f"/api/instances/{make_instance(server_type='MODRINTH', modpack_loader='fabric').id}").json()
    assert vanilla["capabilities"] == sorted(vanilla["capabilities"]) == sorted(ALL - {"content", "modpacks"})
    assert pack["capabilities"] == sorted(ALL)


def test_game_is_read_only(clients, make_instance, testgame):
    inst = make_instance()
    resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"game": "testgame", "description": "x"}, format="json")
    assert resp.status_code == 200 and resp.json()["game"] == "minecraft"
    inst.refresh_from_db()
    assert inst.game == "minecraft" and inst.description == "x"


def test_unknown_game_still_renders(clients, make_instance):
    inst = make_instance()
    Instance.objects.filter(pk=inst.pk).update(game="nosuchgame")
    data = clients["owner"].get(f"/api/instances/{inst.id}").json()
    assert (data["game"], data["game_label"], data["capabilities"], data["memory_model"]) == (
        "nosuchgame",
        "nosuchgame",
        [],
        "plain",
    )
    resp = clients["owner"].get(f"/api/instances/{inst.id}/properties")
    assert resp.status_code == 409 and err(resp) == "unknown_game"


def test_other_game_api_fields(clients, other):
    data = clients["owner"].get(f"/api/instances/{other.id}").json()
    assert (data["game"], data["game_label"], data["capabilities"], data["memory_model"]) == (
        "testgame",
        "Test Game",
        [],
        "plain",
    )
    # memory model "plain": the field is the container limit (no JVM overhead)
    assert data["container_memory_mb"] == 1024


def test_game_catalog(clients):
    resp = clients["viewer"].get("/api/catalog/games")
    assert resp.status_code == 200
    # Minecraft's entry is exactly what phase 1 answered; Factorio's is tested in tests/test_games_phase2.py
    # (later games follow: tests/test_terraria.py, ...)
    assert [g["id"] for g in resp.json()][:2] == ["minecraft", "factorio"]
    assert resp.json()[:1] == [
        {
            "id": "minecraft",
            "label": "Minecraft",
            "variants": [{"id": v, "label": label} for v, label in ServerType.choices],
            "capabilities": sorted(ALL),
            "memory_model": "jvm",
            "default_memory_mb": 4096,
            "notes": list(MinecraftDefinition.notes),
        }
    ]
    assert clients["anon"].get("/api/catalog/games").status_code in (401, 403)


def test_game_catalog_default_memory_follows_the_setting(clients, testgame):
    from apps.core.models import SystemSetting

    SystemSetting.set("default_memory_mb", 6144)
    body = clients["owner"].get("/api/catalog/games").json()
    assert [g["id"] for g in body][:2] == ["minecraft", "factorio"] and body[-1]["id"] == "testgame"
    assert body[0]["default_memory_mb"] == 6144
    assert body[-1] == {
        "id": "testgame",
        "label": "Test Game",
        "variants": [{"id": "vanilla", "label": "Vanilla"}],
        "capabilities": [],
        "memory_model": "plain",
        "default_memory_mb": 2048,
        "notes": [],
    }


# --- the agent gate -----------------------------------------------------------------------------------
def test_minecraft_starts_on_an_agent_without_games(make_instance, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.without_games()
    inst = make_instance()
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"]


@pytest.mark.parametrize("agent", ["older", "without_the_game"])
def test_other_game_needs_an_agent_with_its_profile(agent, other, fake_agent, django_capture_on_commit_callbacks):
    if agent == "older":
        fake_agent.without_games()
    job = run("start", other, django_capture_on_commit_callbacks)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "agent_lacks_game")
    assert "cannot run Test Game servers" in job.error
    assert not fake_agent.called("apply") and str(other.id) not in fake_agent.containers


def test_other_game_starts_on_an_agent_with_its_profile(other, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.add_game("testgame", ["example/testgame"])
    job = run("start", other, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    spec = fake_agent.containers[str(other.id)]["spec"]
    assert spec["game"] == "testgame" and spec["image"] == "example/testgame:1"
    other.refresh_from_db()
    assert other.container_spec_hash == current_spec_hash(other)


def test_agent_games_are_read_from_its_info(node, fake_agent):
    from apps.nodes.client import AgentClient

    agent_games = AgentClient(node).games()
    assert {k: agent_games[k] for k in ("minecraft", "factorio")} == {
        "minecraft": {"variants": [], "images": ["itzg/minecraft-server"], "control": "minecraft-rcon"},
        "factorio": {"variants": [], "images": ["factoriotools/factorio"], "control": "rcon"},
    }
    fake_agent.without_games()
    assert AgentClient(node).games(refresh=True) == {}
    assert "games" not in fake_agent.info()


def test_create_with_a_game(clients, node, fake_agent, mojang_java, testgame):
    body = {"name": "S", "accept_eula": True, "server_type": "VANILLA", "minecraft_version": "1.20.4", "memory_mb": 1024}
    resp = clients["owner"].post("/api/instances/", {**body, "game": "minecraft"}, format="json")
    assert resp.status_code == 201 and resp.json()["instance"]["game"] == "minecraft"
    resp = clients["owner"].post("/api/instances/", {**body, "game": "nosuchgame"}, format="json")
    assert resp.status_code == 400 and "game" in resp.json()["error"]["details"]["fields"]
    fake_agent.without_games()
    resp = clients["owner"].post("/api/instances/", {**body, "game": "testgame"}, format="json")
    assert resp.status_code == 409 and err(resp) == "agent_lacks_game"
    fake_agent.features.append("games")
    fake_agent.add_game("testgame", ["example/testgame"])
    from django.core.cache import cache

    cache.clear()  # the agent's features and games are cached for a few minutes
    resp = clients["owner"].post("/api/instances/", {**body, "game": "testgame"}, format="json")
    assert resp.status_code == 409 and err(resp) == "unsupported_by_game"
    assert Instance.objects.count() == 1


def test_clone_copies_the_game_and_checks_the_agent(clients, make_instance, other, fake_agent):
    mc = make_instance(name="Source")
    fake_agent.prepare(mc.id, {"server.properties": "motd=x\n"})
    fake_agent.prepare(other.id, {"config.txt": "x\n"})
    resp = clients["owner"].post(f"/api/instances/{mc.id}/clone", {"name": "Copy"}, format="json")
    assert resp.status_code == 202 and resp.json()["instance"]["game"] == "minecraft"
    fake_agent.without_games()
    resp = clients["owner"].post(f"/api/instances/{other.id}/clone", {"name": "Copy 2"}, format="json")
    assert resp.status_code == 409 and err(resp) == "agent_lacks_game"
    assert not Instance.objects.filter(name="Copy 2").exists()
    fake_agent.features.append("games")
    fake_agent.add_game("testgame", ["example/testgame"])
    from django.core.cache import cache

    cache.clear()
    resp = clients["owner"].post(f"/api/instances/{other.id}/clone", {"name": "Copy 3"}, format="json")
    assert resp.status_code == 202 and Instance.objects.get(name="Copy 3").game == "testgame"


# --- capability gating for a game without the Minecraft features ----------------------------------------
def test_minecraft_only_endpoints_refuse_another_game(clients, other, fake_agent):
    fake_agent.prepare(other.id, {"server.properties": "motd=x\n"})
    client = clients["owner"]
    base = f"/api/instances/{other.id}"
    refused = [
        client.get(f"{base}/properties"),
        client.patch(f"{base}/properties", {"values": {"motd": "y"}}, format="json"),
        client.get(f"{base}/world"),
        client.post(f"{base}/world/replace", {"confirm": True}, format="json"),
        client.get(f"{base}/world/reset"),
        client.get(f"{base}/players"),
        client.post(f"{base}/players", {"action": "kick", "name": "Steve"}, format="json"),
        client.post(f"{base}/command", {"command": "say hi"}, format="json"),
        client.get(f"{base}/diagnostics"),
        client.post(f"{base}/software", {"minecraft_version": "1.21.1"}, format="json"),
    ]
    for resp in refused:
        assert resp.status_code == 409 and err(resp) == "unsupported_by_game", resp.request["PATH_INFO"]
        assert resp.json()["error"]["details"]["game"] == "testgame"
    assert err(client.get(f"{base}/content/")) == "content_unsupported"
    assert err(client.get(f"{base}/modpack")) == "not_modpack"
    schedule = {"name": "s", "cron": "0 4 * * *"}
    resp = client.post(f"{base}/schedules", {**schedule, "action": "command", "payload": {"command": "x"}}, format="json")
    assert resp.status_code == 400 and "action" in resp.json()["error"]["details"]["fields"]
    resp = client.post(f"{base}/schedules", {**schedule, "action": "restart"}, format="json")
    assert resp.status_code == 201
    assert not fake_agent.commands


def test_world_download_does_not_send_minecraft_commands_to_another_game(clients, other, fake_agent):
    fake_agent.add_game("testgame", ["example/testgame"])
    fake_agent.apply(other.id, build_spec(other), start=True)
    fake_agent.mkdir(other.id, "saves")
    Instance.objects.filter(pk=other.pk).update(status="running")
    resp = clients["owner"].get(f"/api/instances/{other.id}/files/archive?path=saves&flush=true")
    assert resp.status_code == 200
    assert not fake_agent.commands


def test_players_are_not_polled_for_another_game(other, fake_agent):
    from apps.metrics.tasks import player_count
    from apps.nodes.client import AgentClient

    fake_agent.add_game("testgame", ["example/testgame"])
    fake_agent.apply(other.id, build_spec(other), start=True)
    assert player_count(other, AgentClient(other.node)) is None
    assert not fake_agent.called("ping") and not fake_agent.commands


def test_console_stop_words_come_from_the_game(testgame):
    from apps.console.consumers import is_stop_command

    assert is_stop_command("/stop") and not is_stop_command("/stop", testgame)
    testgame.stop_words = frozenset({"exit"})
    try:
        assert is_stop_command(" EXIT ", testgame) and not is_stop_command("/exit", testgame)
    finally:
        del testgame.stop_words


def test_scheduled_command_is_refused_for_a_game_without_commands(other, fake_agent):
    from apps.instances.models import Schedule
    from apps.instances.scheduling import execute

    sched = Schedule.objects.create(
        instance=other, name="c", action="command", cron="* * * * *", payload={"command": "x"}, only_when_running=False
    )
    with pytest.raises(games.CapabilityUnavailable):
        execute(sched)
    assert not fake_agent.commands


# --- backups: the game in the software record ---------------------------------------------------------
@pytest.fixture
def server(make_instance, fake_agent):
    inst = make_instance(name="Survival", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.put_file(inst.id, "world/level.dat", b"current")
    Instance.objects.filter(pk=inst.pk).update(
        container_spec_hash=current_spec_hash(inst), installed_at=timezone.now(), status="running", container_state="running"
    )
    inst.refresh_from_db()
    return inst


def backup_of(inst, capture) -> Backup:
    job = run("backup", inst, capture)
    assert job.status == JobStatus.SUCCEEDED, job.error
    return Backup.objects.get(job=job)


def test_backup_records_the_game(server, django_capture_on_commit_callbacks):
    backup = backup_of(server, django_capture_on_commit_callbacks)
    software = backup.meta["software"]
    assert software["game"] == "minecraft" and software["server_type"] == "VANILLA"
    assert backup_game(backup) == "minecraft"


def test_backups_from_before_games_are_minecraft(server, django_capture_on_commit_callbacks):
    backup = backup_of(server, django_capture_on_commit_callbacks)
    backup.meta = {**backup.meta, "software": {k: v for k, v in backup.meta["software"].items() if k != "game"}}
    backup.save(update_fields=["meta"])
    assert backup_game(backup) == "minecraft"
    backup.meta = {}
    assert backup_game(backup) is None


def _foreign(backup: Backup) -> Backup:
    backup.meta = {**backup.meta, "software": {**backup.meta["software"], "game": "testgame"}}
    backup.save(update_fields=["meta"])
    return backup


def test_restore_across_games_is_refused(server, fake_agent, clients, testgame, django_capture_on_commit_callbacks):
    backup = _foreign(backup_of(server, django_capture_on_commit_callbacks))
    base = f"/api/instances/{server.id}/backups/{backup.id}"
    resp = clients["owner"].post(f"{base}/restore", {"confirm": True}, format="json")
    assert resp.status_code == 409 and err(resp) == "backup_game_mismatch"
    assert resp.json()["error"]["details"] == {"backup_game": "testgame", "game": "minecraft"}
    resp = clients["owner"].post(f"{base}/restore-as-new", {"name": "New"}, format="json")
    assert resp.status_code == 409 and err(resp) == "backup_game_mismatch"
    assert not Instance.objects.filter(name="New").exists()
    # the job refuses it too (queued before, or by another way), before it stops anything
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "backup_game_mismatch")
    assert not fake_agent.called("restore_backup") and fake_agent.containers[str(server.id)]["running"]


def test_restore_of_a_minecraft_backup_is_unchanged(server, fake_agent, django_capture_on_commit_callbacks):
    backup = backup_of(server, django_capture_on_commit_callbacks)
    job = run("restore", server, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["software_restored"] is None


@pytest.fixture
def console_env(monkeypatch):
    """Console without the agent's log relay; commands that would go to the agent are recorded."""
    import asyncio

    from apps.console import consumers

    async def idle_relay(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(consumers.ConsoleConsumer, "_relay", idle_relay)
    sent: list[str] = []
    monkeypatch.setattr(consumers, "_agent_command", lambda node, instance_id, command: sent.append(command) or "ok")
    return sent


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_console_refuses_commands_for_a_game_without_them(console_env, testgame):
    from asgiref.sync import sync_to_async

    from apps.jobs.models import Job

    from .test_security_audit_authz import WsClient
    from .test_web_audit_2026_11_09 import _close_db, _ws_setup

    inst, _user, key = await sync_to_async(_ws_setup)(
        ["instance.view", "instance.console", "instance.command", "instance.control"]
    )
    await sync_to_async(lambda: Instance.objects.filter(pk=inst.pk).update(game="testgame"))()
    comm = WsClient(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()  # hello
    for n, text in enumerate(("list", "stop")):
        await comm.send_json_to({"type": "command", "id": n, "command": text})
        result = await comm.receive_json_from()
        assert result["type"] == "command_result" and result["ok"] is False and result["code"] == "unsupported_by_game"
        assert result["id"] == n and "Test Game servers have no console commands" in result["error"]
    assert console_env == [] and not await sync_to_async(Job.objects.exists)()
    await comm.disconnect()
    await _close_db()


def test_the_fake_agent_games_match_the_agent_profile():
    """The fake agent's "games" map names the game and control driver the agent's Minecraft profile has,
    and the Minecraft definition asks for the same ones."""
    import re
    from pathlib import Path

    from .conftest import CURRENT_AGENT_GAMES

    source = (Path(__file__).resolve().parents[2] / "agent" / "mcagent" / "profiles" / "minecraft.py").read_text()
    game = re.search(r'^GAME = "([a-z0-9-]+)"', source, re.M)
    control = re.search(r'^CONTROL = "([a-z0-9-]+)"', source, re.M)
    assert game and control, "GAME / CONTROL not found in agent/mcagent/profiles/minecraft.py"
    mc = games.get("minecraft")
    assert (game.group(1), control.group(1)) == (mc.profile, mc.control)
    assert CURRENT_AGENT_GAMES[mc.id]["control"] == mc.control
