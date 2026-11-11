# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Review of the multi-game branch (2026-11-10): Satisfactory on the panel side, and the generic
pieces the review found with it.

Item 1: a server whose claim may be lost (a restore, a change under saved/, the agent found it
unclaimed, the claim runs again) is never left published: ``ports_open`` is cleared, the claim,
join password and publishing run again, and the container is recreated without ports.
Items 6-12 and 16-17: see each test.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from apps import games
from apps.backups.models import Backup
from apps.games.satisfactory import SatisfactoryDefinition
from apps.instances.models import Instance
from apps.instances.reconcile import reconcile_instance
from apps.instances.spec import build_spec
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient, AgentError

from .test_games_satisfactory import TOKEN, _claimed_instance, body, game, run  # noqa: F401

ALL_STEPS = ("claim", "access", "new_game", "publish")


def code(resp: Any) -> str:
    return resp.json()["error"]["code"]


@pytest.fixture
def sf(make_instance, fake_agent, game):
    """A claimed, published, running server (ports_open), with its claim file and a game."""
    inst = _claimed_instance(make_instance, fake_agent, steps=ALL_STEPS, running=False)
    state = dict(inst.setup_state)
    state["ports_open"] = True
    Instance.objects.filter(pk=inst.pk).update(setup_state=state)
    inst.refresh_from_db()
    game.claimed, game.admin_password = True, inst.rcon_password
    game.sessions, game.current = {"Base": ["Base_autosave_0"]}, "Base"
    fake_agent.put_file(inst.id, "saved/ServerSettings.7777.sav", b"CLAIM")
    fake_agent.put_file(inst.id, "saved/server/Base_autosave_0.sav", b"SAVE")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running", desired_state="running", installed_at=timezone.now())
    inst.refresh_from_db()
    assert len(fake_agent.containers[str(inst.id)]["spec"]["ports"]) == 3
    return inst


def published(fake_agent, inst) -> bool:
    return bool(fake_agent.containers[str(inst.id)]["spec"]["ports"])


def steps(inst) -> dict[str, str]:
    inst.refresh_from_db()
    return {k: v["status"] for k, v in (inst.setup_state.get("steps") or {}).items()}


def assert_reclaim_pending(inst) -> None:
    inst.refresh_from_db()
    assert not inst.setup_state.get("ports_open")
    s = steps(inst)
    assert s["claim"] != "done" and s["access"] != "done" and s["publish"] != "done", s
    assert build_spec(inst)["ports"] == []


# --- item 1: restores and file changes close the ports ----------------------------------------------------
def test_restore_closes_the_ports_and_runs_the_claim_again(sf, fake_agent, game, django_capture_on_commit_callbacks):
    backup = run("backup", sf, django_capture_on_commit_callbacks)
    assert backup.status == JobStatus.SUCCEEDED, backup.error
    b = Backup.objects.get(job=backup)
    job = run("restore", sf, django_capture_on_commit_callbacks, backup_id=str(b.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(sf.id)]["running"]
    # started again without its ports: the claim ran on an unpublished container
    assert game.ports_seen_at_claim and game.ports_seen_at_claim[-1] == []
    # the setup job the restore queued claimed (logged in), applied the password and published again
    setup = Job.objects.filter(instance=sf, kind="setup").order_by("-created_at").first()
    assert setup is not None
    sf.refresh_from_db()
    assert setup.status == JobStatus.SUCCEEDED, setup.error
    assert sf.setup_state.get("ports_open") is True and published(fake_agent, sf)
    assert "claim" in game.actions()


def test_restore_path_under_saved_closes_the_ports(sf, fake_agent, game, django_capture_on_commit_callbacks):
    b = Backup.objects.get(job=run("backup", sf, django_capture_on_commit_callbacks))
    fake_agent.failed_operations["restore_path"] = ("broken", "restic_failed")  # the restore itself fails
    job = run("restore_path", sf, django_capture_on_commit_callbacks, backup_id=str(b.id), path="saved/ServerSettings.7777.sav")
    assert job.status == JobStatus.FAILED
    assert_reclaim_pending(sf)  # closed before the files were touched (fail closed)
    assert not published(fake_agent, sf)


def test_restore_path_elsewhere_keeps_the_ports(sf, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(sf.id, "logs/x.txt", b"x")
    b = Backup.objects.get(job=run("backup", sf, django_capture_on_commit_callbacks))
    job = run("restore_path", sf, django_capture_on_commit_callbacks, backup_id=str(b.id), path="logs")
    assert job.status == JobStatus.SUCCEEDED, job.error
    sf.refresh_from_db()
    assert sf.setup_state.get("ports_open") is True


def test_snapshot_restore_closes_the_ports(sf, fake_agent, game, django_capture_on_commit_callbacks):
    games.for_instance(sf).files_touched(sf, [""], "a snapshot was restored")
    assert_reclaim_pending(sf)


@pytest.mark.parametrize(
    "op,payload",
    [
        ("delete", {"paths": ["saved/server/Base_autosave_0.sav"]}),
        ("rename", {"source": "saved/server/Base_autosave_0.sav", "destination": "saved/server/x.sav"}),
        ("rename", {"source": "other.sav", "destination": "saved/server/other.sav"}),
        ("mkdir-upload", {}),
    ],
)
def test_file_changes_under_saved_close_the_ports(clients, sf, fake_agent, op, payload):
    import io

    fake_agent.put_file(sf.id, "other.sav", b"x")
    c = clients["owner"]
    if op == "mkdir-upload":
        f = io.BytesIO(b"x")
        f.name = "upload.sav"
        resp = c.post(f"/api/instances/{sf.id}/files/upload?path=saved/server", {"file": f}, format="multipart")
    else:
        resp = c.post(f"/api/instances/{sf.id}/files/{op}", payload, format="json")
    assert resp.status_code in (200, 201), resp.json()
    assert_reclaim_pending(sf)


def test_file_changes_elsewhere_keep_the_ports(clients, sf, fake_agent):
    fake_agent.put_file(sf.id, "logs/a.txt", b"x")
    resp = clients["owner"].post(f"/api/instances/{sf.id}/files/delete", {"paths": ["logs/a.txt"]}, format="json")
    assert resp.status_code == 200
    sf.refresh_from_db()
    assert sf.setup_state.get("ports_open") is True


def test_a_start_after_the_ports_closed_recreates_the_container_unpublished(sf, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.stop(sf.id)
    games.for_instance(sf).files_touched(sf, ["saved"], "files were deleted")
    assert published(fake_agent, sf)  # the old container still has its ports
    # a job that starts the server again without "apply changes" (restore, saves) never starts it as it is
    from apps.instances.handlers import start_server

    class Ctx:
        def progress(self, *a, **k): ...

        def log(self, *a, **k): ...

        def agent_progress(self, *a, **k):
            return None

    start_server(sf, Ctx(), admit=False)
    assert fake_agent.containers[str(sf.id)]["running"] and not published(fake_agent, sf)


def test_the_agents_refusal_closes_the_ports_and_starts_unpublished(
    sf, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    """The panel thought the server was claimed, the agent's guard says no (claim file gone)."""
    from apps.instances.spec import current_spec_hash

    fake_agent.stop(sf.id)
    # the container is up to date: the Start job starts it as it is (no recreate)
    Instance.objects.filter(pk=sf.pk).update(container_spec_hash=current_spec_hash(sf))
    sf.refresh_from_db()
    real_start = fake_agent.start

    def start(instance_id):
        if fake_agent.containers[str(instance_id)]["spec"]["ports"]:
            raise AgentError("This Satisfactory server is not claimed", "server_unclaimed", 409)
        return real_start(instance_id)

    monkeypatch.setattr(fake_agent, "start", start)
    monkeypatch.setattr(AgentClient, "start", fake_agent._proxy("start"))
    job = run("start", sf, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(sf.id)]["running"]
    # the start job's setup claimed it again and published it
    assert published(fake_agent, sf) and steps(sf)["publish"] == "done"


def test_the_agents_guard_stop_closes_the_ports(sf, fake_agent, monkeypatch):
    real_status = fake_agent.status

    def status(instance_id):
        st = real_status(instance_id)
        st.update(running=False, status="exited", exit_code=130)
        st["guard_stop"] = {"code": "server_unclaimed", "message": "The server answered as unclaimed", "at": "x"}
        return st

    monkeypatch.setattr(fake_agent, "status", status)
    monkeypatch.setattr(AgentClient, "status", fake_agent._proxy("status"))
    inst = reconcile_instance(sf.id)
    assert inst is not None and "Stopped by the node agent" in inst.status_detail
    assert_reclaim_pending(sf)


def test_rerunning_the_claim_unpublishes_first(sf, fake_agent, game, clients, django_capture_on_commit_callbacks):
    seen: list[bool] = []
    real = game.__call__

    def watch(client, instance_id, action, params=None, timeout=60):
        if action == "claim":
            seen.append(bool(fake_agent.containers[str(instance_id)]["spec"]["ports"]))
        return real(client, instance_id, action, params, timeout)

    mp = pytest.MonkeyPatch()
    mp.setattr(AgentClient, "game_action", lambda client, *a, **k: watch(client, *a, **k))
    try:
        with django_capture_on_commit_callbacks(execute=True):
            resp = clients["owner"].post(f"/api/instances/{sf.id}/setup", {"step": "claim"}, format="json")
        assert resp.status_code == 202, resp.json()
    finally:
        mp.undo()
    assert seen == [False]  # the claim never ran on a published container
    sf.refresh_from_db()
    assert steps(sf)["claim"] == "done" and steps(sf)["publish"] != "done" and not sf.setup_state.get("ports_open")


# --- item 10: setup steps in order ----------------------------------------------------------------
def test_setup_steps_cannot_be_forced_out_of_order(clients, fake_agent, game, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body(start=False), format="json")
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    for step in ("access", "new_game", "publish"):
        r = clients["owner"].post(f"/api/instances/{inst.id}/setup", {"step": step}, format="json")
        assert r.status_code == 409 and code(r) == "setup_order", step
    inst.refresh_from_db()
    assert not inst.setup_state.get("ports_open") and build_spec(inst)["ports"] == []


def test_the_setup_job_checks_the_order_too(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent, steps=())
    job = run("setup", inst, django_capture_on_commit_callbacks, step="publish")
    assert job.status == JobStatus.FAILED and job.error_code == "setup_order"
    inst.refresh_from_db()
    assert not inst.setup_state.get("ports_open")


# --- item 7: new game / import never overwrite saves ------------------------------------------------
def test_new_game_refuses_an_existing_session(sf, game, django_capture_on_commit_callbacks):
    job = run("saves_reset", sf, django_capture_on_commit_callbacks, settings={"session_name": "Base"}, backup=False)
    assert job.status == JobStatus.FAILED and job.error_code == "session_exists"
    assert "new_game" not in game.actions()


@pytest.mark.parametrize(
    "upload,expected", [("Base_autosave_3.sav", "Base_autosave_3_import"), ("Base_mcpanel.sav", "Base_mcpanel_import")]
)
def test_import_never_takes_a_name_the_game_writes_later(
    sf, fake_agent, game, clients, django_capture_on_commit_callbacks, upload, expected
):
    import io

    f = io.BytesIO(b"SAVEDATA")
    f.name = upload
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{sf.id}/saves/import", {"file": f, "confirm_name": sf.name, "backup": "false"}, format="multipart"
        )
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["save"] == expected
    # no safety backup: the running game was saved before the upload was loaded
    assert game.actions().index("save") < game.actions().index("upload_save")


# --- item 6 / 16: the guards of the saves jobs ------------------------------------------------------
def test_satisfactory_refuses_after_a_safety_backup_with_warnings(sf, fake_agent, game, django_capture_on_commit_callbacks):
    fake_agent.backup_warnings = ["some file could not be read"]
    job = run("saves_reset", sf, django_capture_on_commit_callbacks, settings={"session_name": "New"}, backup=True)
    assert job.status == JobStatus.FAILED and job.error_code == "safety_backup_incomplete"
    assert "new_game" not in game.actions()


def test_a_server_started_for_the_job_is_stopped_again_when_it_never_gets_ready(
    make_instance, fake_agent, game, monkeypatch, django_capture_on_commit_callbacks
):
    from apps.core.errors import ConflictError

    inst = _claimed_instance(make_instance, fake_agent, running=False)

    def never_ready(self, instance, ctx, client, timeout=0):
        raise ConflictError("The server did not become ready in time", code="not_ready")

    monkeypatch.setattr(SatisfactoryDefinition, "_wait_ready", never_ready)
    job = run("saves_reset", inst, django_capture_on_commit_callbacks, settings={"session_name": "X"}, backup=False)
    assert job.status == JobStatus.FAILED and job.error_code == "not_ready"
    assert not fake_agent.containers[str(inst.id)]["running"]  # stopped again


def test_replace_save_refuses_after_a_safety_backup_with_warnings(fake_agent, django_capture_on_commit_callbacks, make_instance):
    from .test_games_phase2 import factorio as _f  # noqa: F401

    inst = make_instance(
        name="F",
        game="factorio",
        server_type="",
        minecraft_version="2.1.23",
        image="factoriotools/factorio:2.1.23-rootless",
        memory_mb=2048,
        jvm_flags_preset="none",
        game_port=34197,
        ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34197}],
        game_options={"space_age": False, "save_name": "world"},
    )
    fake_agent.put_file(inst.id, "saves/world.zip", b"current")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.backup_warnings = ["file changed while it was read"]
    job = run("saves_reset", inst, django_capture_on_commit_callbacks, seed=1, backup=True)
    assert job.status == JobStatus.FAILED and job.error_code == "safety_backup_incomplete"
    assert fake_agent.get_file(inst.id, "saves/world.zip") == b"current"  # nothing moved aside
    assert fake_agent.containers[str(inst.id)]["running"]  # started again: nothing was changed


def test_replace_save_failure_after_moving_aside_leaves_the_server_stopped(
    fake_agent, monkeypatch, django_capture_on_commit_callbacks, make_instance
):
    from apps.games.factorio import FactorioSaves

    inst = make_instance(
        name="F2",
        game="factorio",
        server_type="",
        minecraft_version="2.1.23",
        image="factoriotools/factorio:2.1.23-rootless",
        memory_mb=2048,
        jvm_flags_preset="none",
        game_port=34198,
        ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34198}],
        game_options={"space_age": False, "save_name": "world"},
    )
    fake_agent.put_file(inst.id, "saves/world.zip", b"current")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(desired_state="running")

    def broken(self, client, instance, values):
        raise AgentError("disk full", "write_failed", 500)

    monkeypatch.setattr(FactorioSaves, "write_map_gen", broken)
    job = run("saves_reset", inst, django_capture_on_commit_callbacks, seed=1, backup=False)
    assert job.status == JobStatus.FAILED
    assert not fake_agent.containers[str(inst.id)]["running"]
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"
    assert "left stopped" in "\n".join(job.log)


def test_replace_save_stops_a_restarting_container(fake_agent, django_capture_on_commit_callbacks, make_instance):
    inst = make_instance(
        name="F3",
        game="factorio",
        server_type="",
        minecraft_version="2.1.23",
        image="factoriotools/factorio:2.1.23-rootless",
        memory_mb=2048,
        jvm_flags_preset="none",
        game_port=34199,
        ports=[{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 34199}],
        game_options={"space_age": False, "save_name": "world"},
    )
    fake_agent.put_file(inst.id, "saves/world.zip", b"current")
    fake_agent.put_file(inst.id, "config/map-gen-settings.json", "{}")
    fake_agent.apply(inst.id, build_spec(inst), start=False)
    c = fake_agent.containers[str(inst.id)]
    c["status"], c["restarting"] = "restarting", True  # Docker's restart policy between two attempts
    job = run("saves_reset", inst, django_capture_on_commit_callbacks, seed=1, backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.called("stop")


# --- item 8: clone and restore-as-new finish their setup ------------------------------------------
def test_clone_takes_over_the_claim_password_and_secrets(sf, fake_agent, game, clients, django_capture_on_commit_callbacks):
    fake_agent.stop(sf.id)
    Instance.objects.filter(pk=sf.pk).update(status="stopped")
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{sf.id}/clone", {"name": "Copy"}, format="json")
    assert resp.status_code == 202, resp.json()
    clone = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert clone.rcon_password == sf.rcon_password  # the copied claim was made with it
    assert clone.secret_values == sf.secret_values
    assert not clone.setup_state.get("ports_open") and build_spec(clone)["ports"] == []
    # its setup claims it (logging in with that password) and publishes it
    job = run("start", clone, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    setup = Job.objects.filter(instance=clone, kind="setup").order_by("-created_at").first()
    assert setup is not None and setup.status == JobStatus.SUCCEEDED, setup and setup.error
    assert steps(clone)["publish"] == "done"


def test_restore_as_new_takes_over_the_claim_password(sf, fake_agent, clients, django_capture_on_commit_callbacks):
    b = Backup.objects.get(job=run("backup", sf, django_capture_on_commit_callbacks))
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{sf.id}/backups/{b.id}/restore-as-new", {"name": "Restored"}, format="json")
    assert resp.status_code == 202, resp.json()
    target = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert target.rcon_password == sf.rcon_password and target.secret_values == sf.secret_values
    assert not target.setup_state.get("ports_open")


# --- item 17: clone without the saves, migrate, the agent's game list -------------------------------
def test_clone_without_worlds_leaves_out_the_games_saves(sf, fake_agent, clients, django_capture_on_commit_callbacks):
    fake_agent.stop(sf.id)
    fake_agent.put_file(sf.id, "backups/Base_autosave_0.sav", b"OLD")
    fake_agent.put_file(sf.id, "saved/blueprints/b.sbp", b"BP")
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(f"/api/instances/{sf.id}/clone", {"name": "Empty", "exclude_worlds": True}, format="json")
    clone = Instance.objects.get(pk=resp.json()["instance"]["id"])
    files = fake_agent.fs[str(clone.id)]["files"]
    assert not [p for p in files if p.startswith(("saved/server/", "backups/"))]
    assert "saved/blueprints/b.sbp" in files and "saved/ServerSettings.7777.sav" in files


def test_migrate_refuses_a_target_agent_without_the_game_up_front(sf, fake_agent, clients, django_capture_on_commit_callbacks):
    from apps.nodes.models import Node

    other = Node.objects.create(name="other", agent_url="http://other.invalid:8700", use_settings_token=True)
    fake_agent.game_profiles.pop("satisfactory", None)  # the same fake answers for every node
    resp = clients["owner"].post(f"/api/instances/{sf.id}/migrate", {"node": other.pk, "confirm": True}, format="json")
    assert resp.status_code == 409 and code(resp) == "agent_lacks_game"
    assert not Job.objects.filter(kind="migrate").exists()


def test_require_agent_support_asks_again_before_refusing(fake_agent, node):
    from django.core.cache import cache

    client = AgentClient(node)
    saved = dict(fake_agent.game_profiles)
    fake_agent.game_profiles.pop("satisfactory")
    assert "satisfactory" not in client.games()  # cached now
    fake_agent.game_profiles.update(saved)  # the agent was updated meanwhile
    games.require_agent_support("satisfactory", client)  # no 409: asked again
    cache.clear()


# --- item 11 / 12: network and ports --------------------------------------------------------------
def test_satisfactory_runs_on_the_instance_network_only(sf, clients):
    resp = clients["owner"].patch(f"/api/instances/{sf.id}", {"network": "host"}, format="json")
    assert resp.status_code == 400 and "network" in str(resp.json()), resp.json()
    Instance.objects.filter(pk=sf.pk).update(network="macvlan0")
    sf.refresh_from_db()
    with pytest.raises(Exception) as exc:
        build_spec(sf)
    assert getattr(exc.value, "error_code", "") == "network_not_allowed"


def test_messaging_port_never_takes_the_api_container_port(sf, clients, make_instance):
    resp = clients["owner"].patch(f"/api/instances/{sf.id}", {"ports": [{"name": "messaging", "host_port": 7777}]}, format="json")
    assert resp.status_code == 400 and code(resp) == "invalid_port", resp.json()
    # an existing row with that number is never applied
    ports = [dict(p) for p in sf.ports]
    for p in ports:
        if p["name"] == "messaging":
            p["host_port"] = 7777
    Instance.objects.filter(pk=sf.pk).update(ports=ports, game_port=7778)
    sf.refresh_from_db()
    with pytest.raises(Exception) as exc:
        build_spec(sf)
    assert getattr(exc.value, "error_code", "") == "port_conflict"


def test_automatic_messaging_port_skips_7777(node, make_instance, settings):
    from types import SimpleNamespace

    from apps.games.base import PortSpec
    from apps.instances import services

    definition = games.get("satisfactory")
    ports = services.assign_named_ports(definition, node, {"game": 7778, "messaging": None})
    assert {p["name"]: p["host_port"] for p in ports}["messaging"] != 7777
    # a port whose well-known number is one it must avoid gets the next free one
    spec = PortSpec("messaging", "Messaging", 7777, "tcp", 7777, avoid=frozenset({7777}))
    fake = SimpleNamespace(id="x", label="X", ports=(spec,))
    assert services.assign_named_ports(fake, node)[0]["host_port"] == 7778


# --- item 13: Minecraft's world endpoints -------------------------------------------------------------
@pytest.mark.parametrize(
    "method,url", [("get", "world"), ("post", "world/replace"), ("get", "world/reset"), ("post", "world/reset")]
)
def test_minecraft_world_endpoints_refuse_other_games(sf, clients, method, url):
    resp = getattr(clients["owner"], method)(f"/api/instances/{sf.id}/{url}", {}, format="json")
    assert resp.status_code == 409 and code(resp) == "unsupported_by_game", (url, resp.json())


# --- item 16: install deadline, backup log, tModLoader note --------------------------------------
def test_first_start_deadline_fails_the_install(make_instance, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    from apps.instances import game_handlers

    inst = _claimed_instance(make_instance, fake_agent, running=False)
    monkeypatch.setattr(game_handlers, "FIRST_START_TIMEOUT", 0)
    from apps.jobs.runner import JobContext

    with pytest.raises(Exception) as exc:
        game_handlers.wait_first_start(inst, JobContext.__new__(JobContext), AgentClient(inst.node), span=(0.9, 0.05))
    assert getattr(exc.value, "error_code", "") == "first_start_timeout"


def test_backup_log_says_what_consistent_means(sf, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.stop(sf.id)  # a stopped server's backup is consistent
    job = run("backup", sf, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("Consistent backup: the game was saved through its API" in line for line in job.log), job.log


def test_tmodloader_saves_say_that_twld_is_left_out(clients, make_instance, fake_agent):
    from .test_terraria import make_tr

    inst = make_tr(make_instance, "tmodloader")
    data = clients["owner"].get(f"/api/instances/{inst.id}/saves").json()
    assert any(".twld" in n for n in data.get("notes") or [])
    vanilla = make_tr(
        make_instance,
        "vanilla",
        game_port=7790,
        ports=[{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7790}],
    )
    assert "notes" not in clients["owner"].get(f"/api/instances/{vanilla.id}/saves").json()
