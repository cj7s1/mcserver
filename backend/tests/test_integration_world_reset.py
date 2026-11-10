"""World reset through the panel against a REAL node agent + Docker (+ restic when the agent has it).

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set, e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration -s tests/test_integration_world_reset.py

Shares ``MCPANEL_IT_AGENT_TOKEN`` / ``MCPANEL_IT_AGENT_DATA_ROOT`` / ``MCPANEL_IT_READY_TIMEOUT`` with
``test_integration_agent.py``. Both tests go through the panel's own API (``GET`` / ``POST
/api/instances/<id>/world/reset``), so the plan, the ``world_reset`` job, the agent's reset
operation, Docker and restic are exercised together. Everything they create is named ``it-…``
and removed even when an assertion fails.

* ``test_reset_world_of_a_running_vanilla_server`` starts a real vanilla server, resets its world
  with a new seed, the safety backup and "stop if running", then resets the stopped server
  once more with the seed kept and no backup. The world's identity is the seed (the RCON command
  ``seed``, and the ``seed`` tag in level.dat) plus a block placed in it.
* ``test_reset_world_bukkit_layout_carry_over`` starts no Minecraft server: a Paper-style
  world (``bruh``, ``bruh_nether``, ``bruh_the_end``) is written through the agent file API.

Without restic on the agent the safety-backup parts are skipped (and say so); the resets then
run without a backup.
"""

from __future__ import annotations

import gzip
import os
import re
import struct
import time
from typing import Any

import pytest

from .integration_support import (
    READY_TIMEOUT,
    InstanceTracker,
    Timer,
    make_node,
    rcon,
    restic_available,
    run_job,
    wait_status,
)

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
PORTS = (25600, 25650)
BLOCK = (0, 100, 0)  # air in a flat world
NEW_SEED = 2026110923
SEED_ANSWER = re.compile(r"Seed: \[(-?\d+)\]")
STAGING_PREFIX = ".mcpanel-world-"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not AGENT_URL, reason="set MCPANEL_IT_AGENT_URL to run against a real agent"),
    pytest.mark.django_db(transaction=True),
]


@pytest.fixture
def it_node(transactional_db: Any) -> Any:
    return make_node("integration-agent", AGENT_URL, AGENT_TOKEN, ports=PORTS)


@pytest.fixture
def tracker(transactional_db: Any) -> Any:
    t = InstanceTracker(DATA_ROOT)
    yield t
    t.cleanup()


@pytest.fixture
def owner_api(make_user: Any, client_for: Any) -> Any:
    return client_for(make_user("it-owner", owner=True))


# --- helpers ---------------------------------------------------------------------------------------------
def _plan(api: Any, instance: Any) -> dict[str, Any]:
    resp = api.get(f"/api/instances/{instance.id}/world/reset")
    assert resp.status_code == 200, resp.content
    plan = resp.json()
    print(
        f"[it]   plan: delete {plan['delete']}, running {plan['running']}, seed {plan['seed']}, "
        f"blockers {[b['code'] for b in plan['blockers']]}",
        flush=True,
    )
    return plan


def _post(api: Any, instance: Any, **body: Any) -> Any:
    return api.post(f"/api/instances/{instance.id}/world/reset", {"confirm_name": instance.name, **body}, format="json")


def _reset(api: Any, instance: Any, **body: Any) -> Any:
    """Queue the ``world_reset`` job through the API (eager Celery: it has run when this returns)."""
    from apps.jobs.models import Job, JobStatus

    resp = _post(api, instance, **body)
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.kind == "world_reset"
    assert job.status == JobStatus.SUCCEEDED, f"world_reset {job.status}: [{job.error_code}] {job.error}\n" + "\n".join(
        job.log[-25:]
    )
    print("[it]   job log:\n" + "\n".join(f"[it]     {line}" for line in job.log), flush=True)
    return job


def _seed(instance: Any) -> int:
    """The seed of the world the server is running, as its own ``seed`` command reports it."""
    out = ""
    for _ in range(10):
        out = rcon(instance, "seed")
        match = SEED_ANSWER.search(out)
        if match:
            return int(match.group(1))
        time.sleep(3)
    raise AssertionError(f"unexpected answer to 'seed': {out!r}")


def _block_is(instance: Any, block: str, *, attempts: int = 10) -> bool:
    x, y, z = BLOCK
    out = ""
    for _ in range(attempts):
        out = rcon(instance, f"execute if block {x} {y} {z} minecraft:{block}")
        if "Test passed" in out:
            return True
        if "Test failed" in out:
            return False
        time.sleep(3)  # e.g. "That position is not loaded" right after start
    raise AssertionError(f"unexpected answer to block test: {out!r}")


def _place(instance: Any, block: str) -> None:
    """Put a block into the running world and save it, so the world on disk is recognisable."""
    x, y, z = BLOCK
    rcon(instance, "forceload add 0 0")  # keep the test chunk loaded regardless of the spawn location
    out = rcon(instance, f"setblock {x} {y} {z} minecraft:{block}")
    assert "Changed the block" in out or "Could not set the block" in out, out
    assert "Saved the game" in rcon(instance, "save-all flush")
    assert _block_is(instance, block)


def _has_seed(level_dat: bytes, seed: int) -> bool:
    """Whether a level.dat (gzip NBT) holds ``seed`` as its world seed (``WorldGenSettings.seed``, a long)."""
    return b"\x04\x00\x04seed" + struct.pack(">q", seed) in gzip.decompress(level_dat)


def _file(client: Any, instance_id: Any, path: str) -> bytes:
    chunks, _length, _resp = client.download(instance_id, path)
    return b"".join(chunks)


def _hashes(client: Any, instance_id: Any, folder: str) -> dict[str, str]:
    return {f["name"]: f["hash"] for f in client.hash_files(instance_id, folder)}


def _texts(client: Any, instance_id: Any, paths: tuple[str, ...]) -> dict[str, str]:
    return {path: client.read_text(instance_id, path)["content"] for path in paths}


def _properties(client: Any, instance_id: Any) -> dict[str, str]:
    from apps.instances import properties

    return properties.parse(client.read_text(instance_id, "server.properties")["content"])


def _staging_folders(client: Any, instance_id: Any) -> list[str]:
    return sorted(e["name"] for e in client.list_files(instance_id, "")["entries"] if e["name"].startswith(STAGING_PREFIX))


def _states(client: Any, instance_id: Any, level: str, layout: str) -> dict[str, str]:
    info = client.world_info(instance_id, level, layout, sizes=False)
    assert info["leftovers"] == [], info["leftovers"]
    return {f["path"]: f["state"] for f in info["folders"]}


def _snapshot_names(client: Any, instance_id: Any, snapshot_id: str, folder: str) -> set[str]:
    return {e["name"] for e in client.backup_files(instance_id, snapshot_id, folder)["entries"]}


# --- a running vanilla server: new seed + safety backup, then keep-seed on the stopped server ---------------
def test_reset_world_of_a_running_vanilla_server(it_node, tracker, owner_api):
    from apps.audit.models import AuditEvent
    from apps.backups.models import Backup, BackupKind, BackupStatus
    from apps.nodes.client import AgentClient

    timer = Timer()
    client = AgentClient(it_node)
    backup = restic_available(it_node)
    if not backup:
        print("[it] restic not available on the agent: the safety backup of the reset is SKIPPED", flush=True)
    # Mojang: 1.20.4 requires Java 17 (docs/reference/version-metadata.md)
    instance = tracker.create(it_node, "reset", PORTS, minecraft_version="1.20.4", java_version=17)
    iid = instance.id
    try:
        # --- a real world, with something recognisable in it -----------------------------------------
        run_job(
            "install",
            instance,
            start=True,
            server_properties={
                "motd": "MCPanel world reset test",
                "level-type": "minecraft:flat",
                "generate-structures": "false",
                "view-distance": "4",
                "simulation-distance": "4",
                "spawn-protection": "0",
            },
        )
        inst = wait_status(instance, "running", READY_TIMEOUT)
        timer.mark("install + first start until healthy")
        old_seed = _seed(inst)
        assert old_seed != NEW_SEED
        _place(inst, "gold_block")
        old_hash = _hashes(client, iid, "world")["level.dat"]
        assert _has_seed(_file(client, iid, "world/level.dat"), old_seed)
        # things outside the world folder, among them a stale Bukkit-style folder that is not this server's world
        client.mkdir(iid, "it-notes", parents=True)
        client.write_text(iid, "it-notes/keep.txt", "outside the world")
        client.mkdir(iid, "world_nether", parents=True)
        client.write_text(iid, "world_nether/level.dat", "not part of a vanilla-layout world")
        outside = ("ops.json", "eula.txt", "it-notes/keep.txt", "world_nether/level.dat")
        before = _texts(client, iid, outside)
        properties_before = _properties(client, iid)
        assert properties_before.get("level-seed", "") == ""
        timer.mark("world identified (seed, block, level.dat hash)")

        # --- the plan the dialog shows -----------------------------------------------------------------
        plan = _plan(owner_api, inst)
        assert plan["can_reset"] is True and plan["blockers"] == []
        assert (plan["level_name"], plan["layout"], plan["delete"]) == ("world", "vanilla", ["world"])
        assert plan["running"] is True
        assert plan["seed"] == {"value": "", "source": "unset"}
        (folder,) = plan["folders"]
        assert folder["state"] == "world" and {"level.dat", "region", "session.lock"} <= set(folder["markers"])
        assert folder["bytes"] > 0 and folder["files"] > 0 and folder["modified"]
        assert plan["leftovers"] == []

        # a reset is refused without the server's name, without consent to stop, or for other folders
        assert _post(owner_api, inst, folders=["world"]).json()["error"]["code"] == "must_stop"
        assert _post(owner_api, inst, folders=["world_nether"], stop_if_running=True).json()["error"]["code"] == "world_changed"
        wrong_name = owner_api.post(
            f"/api/instances/{iid}/world/reset",
            {"confirm_name": inst.name.upper(), "folders": ["world"], "stop_if_running": True},
            format="json",
        )
        assert wrong_name.status_code == 400 and wrong_name.json()["error"]["code"] == "confirmation_required"
        assert client.status(iid)["running"] is True

        # --- case 1: running server, new custom seed, safety backup, stop if running -------------------
        job = _reset(
            owner_api,
            inst,
            folders=plan["delete"],
            stop_if_running=plan["running"],
            pre_restore_backup=backup,
            seed_mode="custom",
            seed=str(NEW_SEED),
            keep=[],
        )
        timer.mark("world_reset job (stop, safety backup, seed, delete, start)")
        result = job.result
        assert result["deleted"] == ["world"] and result["kept"] == [] and result["skipped"] == []
        assert result["leftover"] is None and result["warnings"] == []
        assert result["seed_mode"] == "custom" and result["seed_changed"] is True
        assert result["restarted"] is True
        assert bool(result["safety_backup"]) is backup
        inst = wait_status(inst, "running", READY_TIMEOUT)
        timer.mark("start after the reset until healthy (generates the new world)")

        # the server runs a new world with the new seed
        assert _seed(inst) == NEW_SEED
        rcon(inst, "forceload add 0 0")
        assert not _block_is(inst, "gold_block"), "the block of the old world is still there: the world was not reset"
        assert "Saved the game" in rcon(inst, "save-all flush")
        new_level_dat = _file(client, iid, "world/level.dat")
        assert _has_seed(new_level_dat, NEW_SEED) and not _has_seed(new_level_dat, old_seed)
        assert _hashes(client, iid, "world")["level.dat"] != old_hash

        # the old world is in the safety backup
        if backup:
            safety = Backup.objects.get(pk=result["safety_backup"])
            assert safety.kind == BackupKind.PRE_RESTORE and safety.status == BackupStatus.SUCCEEDED
            assert safety.name == "Before resetting the world 'world'"
            assert safety.warnings == [] and safety.snapshot_id
            assert {"world", "server.properties", "ops.json", "eula.txt"} <= _snapshot_names(client, iid, safety.snapshot_id, "")
            assert set(folder["markers"]) <= _snapshot_names(client, iid, safety.snapshot_id, "world")
            chunks, _resp = client.backup_download(iid, safety.snapshot_id, "world/level.dat")
            old_level_dat = b"".join(chunks)
            assert _has_seed(old_level_dat, old_seed) and not _has_seed(old_level_dat, NEW_SEED)
            assert Backup.objects.filter(instance=inst, kind=BackupKind.PRE_RESTORE).count() == 1
        else:
            assert not Backup.objects.filter(instance=inst).exists()

        # nothing is left behind, and nothing outside the world changed
        assert _staging_folders(client, iid) == []
        assert _states(client, iid, "world", "vanilla") == {"world": "world"}
        assert _texts(client, iid, outside) == before
        properties_after = _properties(client, iid)
        assert properties_after.pop("level-seed") == str(NEW_SEED)
        properties_before.pop("level-seed", None)
        assert properties_after == properties_before
        event = AuditEvent.objects.filter(action="instance.world_reset").latest("created_at")
        assert event.details["folders"] == ["world"] and event.details["seed_mode"] == "custom"
        assert str(NEW_SEED) not in str(event.details)  # the seed itself is not recorded
        timer.mark("verified the new world, the safety backup and the untouched files")

        # --- case 2: stopped server, seed kept, no safety backup ---------------------------------------
        _place(inst, "diamond_block")  # recognisable content of the second world
        run_job("stop", inst)
        inst = wait_status(inst, "stopped", 120)
        assert not client.status(iid).get("running")  # its container may already be gone (a stopped server has none)
        timer.mark("stop")
        properties_text = client.read_text(iid, "server.properties")["content"]
        backups = Backup.objects.filter(instance=inst).count()
        plan = _plan(owner_api, inst)
        assert plan["can_reset"] is True and plan["running"] is False and plan["delete"] == ["world"]
        assert plan["seed"] == {"value": str(NEW_SEED), "source": "server.properties"}
        job = _reset(
            owner_api, inst, folders=plan["delete"], stop_if_running=False, pre_restore_backup=False, seed_mode="keep", keep=[]
        )
        timer.mark("world_reset job on the stopped server (no backup)")
        result = job.result
        assert result["deleted"] == ["world"] and result["leftover"] is None and result["warnings"] == []
        assert result["seed_mode"] == "keep" and result["seed_changed"] is False
        assert result["restarted"] is False and result["safety_backup"] is None
        assert not client.status(iid).get("running")  # a stopped server stays stopped
        assert _states(client, iid, "world", "vanilla") == {"world": "missing"}
        assert _staging_folders(client, iid) == []
        assert client.read_text(iid, "server.properties")["content"] == properties_text  # keep: not touched at all
        assert Backup.objects.filter(instance=inst).count() == backups
        assert _texts(client, iid, outside) == before
        # with the world gone there is nothing to reset any more
        plan = _plan(owner_api, inst)
        assert plan["can_reset"] is False and plan["delete"] == [] and [b["code"] for b in plan["blockers"]] == ["no_world"]
        refused = _post(owner_api, inst, folders=["world"])
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "no_world"

        # the next start generates a new world from the seed that was kept
        run_job("start", inst)
        inst = wait_status(inst, "running", READY_TIMEOUT)
        timer.mark("start after the second reset until healthy")
        assert _seed(inst) == NEW_SEED
        rcon(inst, "forceload add 0 0")
        assert not _block_is(inst, "diamond_block"), "the block of the second world is still there"
        assert _states(client, iid, "world", "vanilla") == {"world": "world"}
        timer.mark("verified the second new world")
    finally:
        timer.summary()


# --- Paper-style folders on a stopped server: three folders, carry-overs, a folder that is no world ----------
def test_reset_world_bukkit_layout_carry_over(it_node, tracker, owner_api):
    from apps.backups.models import Backup, BackupKind
    from apps.nodes.client import AgentClient, AgentError

    timer = Timer()
    client = AgentClient(it_node)
    backup = restic_available(it_node)
    if not backup:
        print("[it] restic not available on the agent: the safety backup of the reset is SKIPPED", flush=True)
    inst = tracker.create(it_node, "reset-bukkit", PORTS)
    iid = inst.id
    worlds = ["bruh", "bruh_nether", "bruh_the_end"]
    try:
        # never started: the world is written through the agent file API (bukkit.yml makes it a Bukkit layout)
        run_job("install", inst, start=False, server_properties={"level-name": "bruh", "level-seed": "4242"})
        client.write_text(iid, "bukkit.yml", "settings:\n  allow-end: true\n  shutdown-message: Server closed\n")
        for name in worlds:
            client.mkdir(iid, f"{name}/region", parents=True)
            client.write_text(iid, f"{name}/level.dat", f"level.dat of {name}")
            client.upload(iid, f"{name}/region/r.0.0.mca", os.urandom(32 * 1024))
        client.mkdir(iid, "bruh/datapacks", parents=True)
        client.write_text(iid, "bruh/datapacks/it-pack.zip", "a datapack")
        client.mkdir(iid, "bruh/serverconfig", parents=True)
        client.write_text(iid, "bruh/serverconfig/it-mod.toml", "per-world mod settings")
        client.write_text(iid, "bruh/paper-world.yml", "_version: 31\n")
        client.write_text(iid, "bruh_nether/paper-world.yml", "_version: 31\n")
        client.mkdir(iid, "bluemap/web", parents=True)
        client.write_text(iid, "bluemap/web/index.html", "<html>map</html>")
        client.mkdir(iid, "bruh_old", parents=True)
        client.write_text(iid, "bruh_old/level.dat", "another world next to it")
        outside = ("bluemap/web/index.html", "bruh_old/level.dat", "bukkit.yml", "server.properties")
        before = _texts(client, iid, outside)
        carried = (
            "bruh/datapacks/it-pack.zip",
            "bruh/serverconfig/it-mod.toml",
            "bruh/paper-world.yml",
            "bruh_nether/paper-world.yml",
        )
        carried_before = _texts(client, iid, carried)
        timer.mark("install (stopped) + world folders written")

        plan = _plan(owner_api, inst)
        assert plan["can_reset"] is True and plan["running"] is False
        assert (plan["level_name"], plan["level_name_source"], plan["layout"]) == ("bruh", "server.properties", "bukkit")
        assert plan["delete"] == worlds
        assert plan["seed"] == {"value": "4242", "source": "server.properties"}
        by_path = {f["path"]: f for f in plan["folders"]}
        assert [f["role"] for f in plan["folders"]] == ["overworld", "nether", "end"]
        assert all(
            f["state"] == "world" and f["markers"] == ["level.dat", "region"] and f["bytes"] > 32 * 1024 for f in plan["folders"]
        )
        assert by_path["bruh"]["keepable"] == {"datapacks": ["datapacks"], "config": ["serverconfig", "paper-world.yml"]}
        assert by_path["bruh_nether"]["keepable"] == {"config": ["paper-world.yml"]}
        assert by_path["bruh_the_end"]["keepable"] == {}

        job = _reset(
            owner_api,
            inst,
            folders=plan["delete"],
            stop_if_running=False,
            pre_restore_backup=backup,
            seed_mode="keep",
            keep=["datapacks", "config"],
        )
        timer.mark("world_reset job (safety backup, delete three folders, carry over)")
        result = job.result
        assert result["layout"] == "bukkit" and result["deleted"] == worlds
        assert result["kept"] == ["bruh/datapacks", "bruh/serverconfig", "bruh/paper-world.yml", "bruh_nether/paper-world.yml"]
        assert result["skipped"] == [] and result["leftover"] is None and result["warnings"] == []
        assert result["restarted"] is False and result["seed_changed"] is False
        assert client.status(iid).get("running") in (False, None)

        # only what was carried over is left of the three folders
        assert _states(client, iid, "bruh", "bukkit") == {"bruh": "pending", "bruh_nether": "pending", "bruh_the_end": "missing"}
        assert _texts(client, iid, carried) == carried_before
        assert sorted(e["name"] for e in client.list_files(iid, "bruh")["entries"]) == [
            "datapacks",
            "paper-world.yml",
            "serverconfig",
        ]
        assert [e["name"] for e in client.list_files(iid, "bruh_nether")["entries"]] == ["paper-world.yml"]
        for gone in ("bruh/level.dat", "bruh/region", "bruh_nether/region", "bruh_the_end"):
            with pytest.raises(AgentError) as missing:
                client.stat_file(iid, gone)
            assert missing.value.agent_status == 404, gone
        assert _staging_folders(client, iid) == []
        assert _texts(client, iid, outside) == before

        if backup:
            safety = Backup.objects.get(pk=result["safety_backup"])
            assert safety.kind == BackupKind.PRE_RESTORE and safety.warnings == []
            assert safety.name == "Before resetting the world 'bruh'"
            for name in worlds:
                assert {"level.dat", "region"} <= _snapshot_names(client, iid, safety.snapshot_id, name), name
            chunks, _resp = client.backup_download(iid, safety.snapshot_id, "bruh_the_end/level.dat")
            assert b"".join(chunks) == b"level.dat of bruh_the_end"
        else:
            assert result["safety_backup"] is None

        # a second reset finds no world: the carried-over folders are not deleted
        plan = _plan(owner_api, inst)
        assert plan["can_reset"] is False and plan["delete"] == []
        assert [b["code"] for b in plan["blockers"]] == ["no_world"]
        assert _post(owner_api, inst, folders=worlds).json()["error"]["code"] == "no_world"

        # a level-name that points at a folder which is no world is refused, and the folder stays
        client.write_text(iid, "server.properties", "level-name=bluemap\n")
        plan = _plan(owner_api, inst)
        assert plan["can_reset"] is False and plan["delete"] == []
        assert [b["code"] for b in plan["blockers"]] == ["not_a_world"]
        refused = _post(owner_api, inst, folders=["bluemap"])
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_a_world"
        # the agent itself refuses it too, whatever the panel says
        op = client.world_reset(iid, level_name="bluemap", layout="bukkit", expect=["bluemap"], keep=[])
        with pytest.raises(AgentError) as not_a_world:
            client.wait_operation(op["id"], timeout=60)
        assert not_a_world.value.error_code == "not_a_world"
        assert client.read_text(iid, "bluemap/web/index.html")["content"] == "<html>map</html>"
        assert _staging_folders(client, iid) == []
        timer.mark("verified carry-overs, the safety backup and the refusals")
    finally:
        timer.summary()
