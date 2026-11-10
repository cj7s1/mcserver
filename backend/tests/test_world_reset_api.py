"""World reset: GET /world/reset (the plan), POST /world/reset and the world_reset job."""

from __future__ import annotations

import gzip
import uuid

import pytest
from django.utils import timezone

from apps.accounts.models import InstanceAccess, Role
from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupKind, RetentionPolicy
from apps.core.errors import ApiError
from apps.core.models import SystemSetting
from apps.instances import world
from apps.instances.models import Instance
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient, AgentError, AgentUnavailable

pytestmark = pytest.mark.django_db

LEVEL_DAT = gzip.compress(b"\x0a\x00\x00\x00")
PROPS = "#Minecraft server properties\n#Mon Nov 09 12:00:00 PST 2026\nmotd=hello\nlevel-seed=12345\n"
OMIT = object()


def err(resp) -> str:
    return resp.json()["error"]["code"]


def put_world(fake_agent, inst, name="world", marker="old"):
    fake_agent.put_file(inst.id, f"{name}/level.dat", LEVEL_DAT)
    fake_agent.put_file(inst.id, f"{name}/region/r.0.0.mca", marker)


@pytest.fixture
def server(make_instance, fake_agent):
    inst = make_instance(name="Survival")
    fake_agent.prepare(inst.id, {"server.properties": PROPS})
    put_world(fake_agent, inst)
    fake_agent.put_file(inst.id, "mods/a.jar", "jar")
    fake_agent.put_file(inst.id, "ops.json", "[]")
    return inst


@pytest.fixture
def paper(make_instance, fake_agent):
    inst = make_instance(name="Paper", server_type="PAPER")
    fake_agent.prepare(inst.id, {"server.properties": "level-name=bruh\n"})
    for name in ("bruh", "bruh_nether", "bruh_the_end"):
        put_world(fake_agent, inst, name, marker=name)
    return inst


BRUH = ["bruh", "bruh_nether", "bruh_the_end"]


def start_container(fake_agent, inst):
    from apps.instances.spec import build_spec

    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running", desired_state="running")
    inst.refresh_from_db()


def plan(client, inst):
    return client.get(f"/api/instances/{inst.id}/world/reset")


def reset(client, inst, capture, between=None, **fields):
    """POST /world/reset and run the queued job; ``between`` runs after the request, before the job."""
    body = {"confirm_name": inst.name, "folders": ["world"], **fields}
    body = {k: v for k, v in body.items() if v is not OMIT}
    with capture(execute=True):
        resp = client.post(f"/api/instances/{inst.id}/world/reset", body, format="json")
        if between is not None:
            between()
    return resp


def last_job(inst) -> Job:
    return Job.objects.filter(instance=inst, kind="world_reset").latest("created_at")


def log_of(job) -> str:
    return "\n".join(job.log)


def named(fake_agent, *names) -> list[str]:
    return [n for n, _args, _kwargs in fake_agent.calls if n in names]


def abandon(inst):
    """A hook that marks the running world_reset job as failed, as POST /jobs/{id}/abandon does."""

    def _hook(*_args, **_kwargs):
        job = last_job(inst)
        if job.status == JobStatus.RUNNING:
            runner.finish(job, JobStatus.FAILED, error="Marked as failed by owner", error_code="abandoned")

    return _hook


# --- GET /world/reset: the plan ------------------------------------------------------------------
def test_plan_vanilla(clients, server, fake_agent):
    resp = plan(clients["manager"], server)
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert (data["level_name"], data["level_name_source"], data["layout"]) == ("world", "default", "vanilla")
    assert data["delete"] == ["world"] and data["can_reset"] is True and data["blockers"] == []
    (folder,) = data["folders"]
    assert folder["modified"] is not None
    assert {k: v for k, v in folder.items() if k != "modified"} == {
        "role": "overworld",
        "path": "world",
        "state": "world",
        "markers": ["level.dat", "region"],
        "keepable": {},
        "bytes": len(LEVEL_DAT) + 3,
        "files": 2,
    }
    assert data["seed"] == {"value": "12345", "source": "server.properties"}
    assert data["level_type"] == "" and data["running"] is False
    assert data["pre_restore_backup_default"] is True
    assert data["safety_backup_keep_days"] == 14 and data["backup_exclusions"] == 0
    assert data["leftovers"] == [] and data["warnings"] == []
    # looking at the plan measures the folders, and does not create the retention row
    assert fake_agent.called("world_info")[-1][1] == {"sizes": True}
    assert not RetentionPolicy.objects.filter(instance=server).exists()


def test_plan_bukkit(clients, paper, fake_agent):
    fake_agent.put_file(paper.id, "bruh/datapacks/pack.zip", "dp")
    fake_agent.put_file(paper.id, "bruh/paper-world.yml", "a: 1")
    fake_agent.put_file(paper.id, "bruh_nether/paper-world.yml", "b: 2")
    fake_agent.put_file(paper.id, "bruh_the_end/datapacks/ignored.zip", "x")  # datapacks live in the main world only
    fake_agent.mkdir(paper.id, ".mcpanel-world-old-abc123")
    fake_agent.put_file(paper.id, "server.properties", "level-name=bruh\nlevel-type=minecraft\\:flat\n")
    RetentionPolicy.objects.create(instance=paper, keep_safety_days=0, excludes=["bruh", "*.zip"])
    Instance.objects.filter(pk=paper.pk).update(env={"WORLD": "https://example.invalid/w.zip", "CF_SET_LEVEL_FROM": "OVERRIDES"})
    start_container(fake_agent, paper)
    data = plan(clients["owner"], paper).json()
    assert (data["level_name"], data["level_name_source"], data["layout"]) == ("bruh", "server.properties", "bukkit")
    assert data["delete"] == BRUH and data["can_reset"] is True
    assert [(f["role"], f["path"], f["state"]) for f in data["folders"]] == [
        ("overworld", "bruh", "world"),
        ("nether", "bruh_nether", "world"),
        ("end", "bruh_the_end", "world"),
    ]
    assert [f["keepable"] for f in data["folders"]] == [
        {"datapacks": ["datapacks"], "config": ["paper-world.yml"]},
        {"config": ["paper-world.yml"]},
        {},
    ]
    assert data["seed"] == {"value": "", "source": "unset"} and data["level_type"] == "minecraft:flat"
    assert data["running"] is True
    assert data["safety_backup_keep_days"] == 0 and data["backup_exclusions"] == 2
    assert data["leftovers"] == [".mcpanel-world-old-abc123"]
    assert len(data["warnings"]) == 2 and "WORLD" in data["warnings"][0] and "CF_SET_LEVEL_FROM" in data["warnings"][1]


def test_plan_seed_sources(clients, server, fake_agent):
    c = clients["owner"]
    assert plan(c, server).json()["seed"] == {"value": "12345", "source": "server.properties"}
    fake_agent.put_file(server.id, "server.properties", "motd=hello\nlevel-seed=\n")
    assert plan(c, server).json()["seed"] == {"value": "", "source": "unset"}
    fake_agent.put_file(server.id, "server.properties", "motd=hello\n")
    assert plan(c, server).json()["seed"] == {"value": "", "source": "unset"}
    Instance.objects.filter(pk=server.pk).update(env={"SEED": "-42"})
    fake_agent.put_file(server.id, "server.properties", PROPS)
    assert plan(c, server).json()["seed"] == {"value": "-42", "source": "env"}


def test_plan_permissions(clients, server, make_user, client_for):
    for name in ("operator", "viewer"):
        assert plan(clients[name], server).status_code == 403, name
    assert plan(clients["norole"], server).status_code == 404
    assert plan(clients["anon"], server).status_code in (401, 403)
    restore_only = Role.objects.create(key="p-restorer", name="Restorer", permissions=["instance.view", "instance.restore"])
    files_only = Role.objects.create(key="p-files", name="Files", permissions=["instance.view", "instance.files.write"])
    for role in (restore_only, files_only):
        user = make_user(f"u-{role.key}")
        InstanceAccess.objects.create(user=user, instance=server, role=role)
        assert plan(client_for(user), server).status_code == 403, role.key
    assert plan(clients["owner"], server).status_code == 200
    assert clients["owner"].get(f"/api/instances/{uuid.uuid4()}/world/reset").status_code == 404


def test_plan_level_name_from_env(clients, server, fake_agent):
    """LEVEL wins when server.properties agrees or does not name a world folder at all."""
    Instance.objects.filter(pk=server.pk).update(env={"LEVEL": "hardcore"})
    put_world(fake_agent, server, "hardcore")
    data = plan(clients["owner"], server).json()
    assert (data["level_name"], data["level_name_source"]) == ("hardcore", "env")
    assert data["delete"] == ["hardcore"] and data["can_reset"] is True
    fake_agent.put_file(server.id, "server.properties", "level-name=hardcore\n")
    assert plan(clients["owner"], server).json()["can_reset"] is True


@pytest.mark.parametrize(
    ("props", "env"),
    [
        ("level-name=world \n", {}),  # Java keeps the trailing space: the folder is "world "
        ("level-name=\\ world\n", {}),  # an escaped leading space
        ("motd=x\n", {"LEVEL": "world "}),
        ("motd=x\n", {"LEVEL": " "}),
        ("motd=x\n", {"LEVEL": ""}),  # set but empty: what the runtime writes into level-name is not known
        ("level-name=world\n", {"LEVEL": ""}),
        ("level-name=plugins\n", {}),
        ("level-name=../escape\n", {}),
        ("level-name=.hidden\n", {}),
        ("level-name=\n", {}),  # present but empty: Minecraft keeps the world in the server folder itself
    ],
)
def test_blocker_invalid_level_name(clients, server, fake_agent, django_capture_on_commit_callbacks, props, env):
    fake_agent.put_file(server.id, "server.properties", props)
    Instance.objects.filter(pk=server.pk).update(env=env)
    data = plan(clients["owner"], server).json()
    assert data["can_reset"] is False and data["delete"] == [] and data["folders"] == []
    assert [b["code"] for b in data["blockers"]] == ["invalid_level_name"]
    assert not fake_agent.called("world_info")  # an unusable name is never sent to the agent
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 400 and err(resp) == "invalid_level_name"
    assert not Job.objects.filter(kind="world_reset").exists()
    assert fake_agent.get_file(server.id, "world/level.dat") is not None


def test_blocker_level_name_conflict(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """LEVEL was changed but the server was not started since: the world can be in either folder."""
    fake_agent.put_file(server.id, "server.properties", "level-name=survival\n")
    put_world(fake_agent, server, "survival")
    put_world(fake_agent, server, "hardcore")
    Instance.objects.filter(pk=server.pk).update(env={"LEVEL": "hardcore"})
    data = plan(clients["owner"], server).json()
    assert [b["code"] for b in data["blockers"]] == ["level_name_conflict"]
    assert "hardcore" in data["blockers"][0]["message"] and "survival" in data["blockers"][0]["message"]
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks, folders=["hardcore"])
    assert resp.status_code == 409 and err(resp) == "level_name_conflict"
    assert not Job.objects.filter(kind="world_reset").exists()


@pytest.mark.parametrize("value", ["worlds", "'worlds'", '"/data/worlds"', "worlds # where the worlds are", "./"])
def test_blocker_world_container(clients, paper, fake_agent, django_capture_on_commit_callbacks, value):
    fake_agent.put_file(
        paper.id, "bukkit.yml", f"settings:\n  allow-end: true\n  world-container: {value}\n  shutdown-message: bye\n"
    )
    data = plan(clients["owner"], paper).json()
    assert [b["code"] for b in data["blockers"]] == ["world_location_overridden"]
    assert "world-container" in data["blockers"][0]["message"]
    resp = reset(clients["owner"], paper, django_capture_on_commit_callbacks, folders=BRUH)
    assert resp.status_code == 409 and err(resp) == "world_location_overridden"
    assert not Job.objects.filter(kind="world_reset").exists()


@pytest.mark.parametrize(
    "text",
    [
        "settings:\n  world-container: .\n",
        "settings:\n  world-container: '.'\n",
        'settings:\n  world-container: ""\n',
        "settings:\n  world-container:\n  shutdown-message: bye\n",
        "settings:\n  world-container: # not set\n",
        "settings:\n  # world-container: worlds\n  allow-end: true\n",
        "settings:\n  allow-end: true\n",
    ],
)
def test_default_world_container_is_not_a_blocker(clients, paper, fake_agent, text):
    fake_agent.put_file(paper.id, "bukkit.yml", text)
    data = plan(clients["owner"], paper).json()
    assert data["blockers"] == [] and data["delete"] == BRUH


def test_world_container_of_a_bukkit_yml_on_a_custom_server(clients, server, fake_agent):
    """A server folder with a bukkit.yml is a bukkit layout whatever the server type says."""
    fake_agent.put_file(server.id, "bukkit.yml", "settings:\n  world-container: worlds\n")
    data = plan(clients["owner"], server).json()
    assert data["layout"] == "bukkit" and [b["code"] for b in data["blockers"]] == ["world_location_overridden"]


@pytest.mark.parametrize(
    "args", ["--universe /data/u", "--nogui -W worlds", "--world-dir=worlds", "--level-name other", "-w other", "--world other"]
)
def test_blocker_extra_args(clients, server, fake_agent, django_capture_on_commit_callbacks, args):
    Instance.objects.filter(pk=server.pk).update(env={"EXTRA_ARGS": args})
    data = plan(clients["owner"], server).json()
    assert [b["code"] for b in data["blockers"]] == ["world_location_overridden"]
    assert "EXTRA_ARGS" in data["blockers"][0]["message"]
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "world_location_overridden"


def test_other_extra_args_are_not_a_blocker(clients, server):
    Instance.objects.filter(pk=server.pk).update(env={"EXTRA_ARGS": "--nogui --forceUpgrade --safeMode"})
    assert plan(clients["owner"], server).json()["can_reset"] is True


def test_blocker_not_a_directory(clients, make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance(name="FileWorld")
    fake_agent.prepare(inst.id, {"server.properties": PROPS})
    fake_agent.put_file(inst.id, "world", "this is a file")
    data = plan(clients["owner"], inst).json()
    assert data["folders"][0]["state"] == "not_a_directory" and data["delete"] == []
    assert [b["code"] for b in data["blockers"]] == ["not_a_directory"]
    resp = reset(clients["owner"], inst, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "not_a_directory"
    assert fake_agent.text(inst.id, "world") == "this is a file"


def test_blocker_not_a_world(clients, paper, fake_agent, django_capture_on_commit_callbacks):
    """Any folder level-name points at that is not a world is left alone, and it blocks the whole reset."""
    fake_agent.put_file(paper.id, "server.properties", "level-name=bluemap\n")
    fake_agent.put_file(paper.id, "bluemap/web/index.html", "not a world")
    put_world(fake_agent, paper, "bluemap_nether")
    data = plan(clients["owner"], paper).json()
    assert [f["state"] for f in data["folders"]] == ["unknown", "world", "missing"]
    assert [b["code"] for b in data["blockers"]] == ["not_a_world"] and "'bluemap'" in data["blockers"][0]["message"]
    resp = reset(clients["owner"], paper, django_capture_on_commit_callbacks, folders=["bluemap_nether"])
    assert resp.status_code == 409 and err(resp) == "not_a_world"
    assert fake_agent.text(paper.id, "bluemap/web/index.html") == "not a world"
    assert fake_agent.get_file(paper.id, "bluemap_nether/level.dat") is not None


def test_blocker_no_world(clients, make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance(name="Fresh", server_type="PAPER")
    fake_agent.prepare(inst.id, {"server.properties": PROPS})
    fake_agent.put_file(inst.id, "world/datapacks/pack.zip", "dp")  # what an earlier reset carried over
    fake_agent.mkdir(inst.id, "world_nether")
    data = plan(clients["owner"], inst).json()
    assert [f["state"] for f in data["folders"]] == ["pending", "empty", "missing"]
    assert data["delete"] == [] and [b["code"] for b in data["blockers"]] == ["no_world"]
    resp = reset(clients["owner"], inst, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "no_world"
    assert fake_agent.text(inst.id, "world/datapacks/pack.zip") == "dp"


def test_plan_of_a_server_that_was_never_installed(clients, make_instance, fake_agent):
    inst = make_instance(name="Empty")
    fake_agent.failures["world_info"] = AgentError("Instance directory does not exist", "not_found", 404)
    data = plan(clients["owner"], inst).json()
    assert data["folders"] == [
        {
            "role": "overworld",
            "path": "world",
            "state": "missing",
            "markers": [],
            "keepable": {},
            "bytes": 0,
            "files": 0,
            "modified": None,
        }
    ]
    assert [b["code"] for b in data["blockers"]] == ["no_world"] and data["running"] is False


def test_agent_errors_are_not_read_as_a_vanilla_layout(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """Only "bukkit.yml does not exist" means vanilla: any other agent error stops the plan."""
    fake_agent.failures["stat_file"] = AgentError("Input/output error", "io_error", 500)
    resp = plan(clients["owner"], server)
    assert resp.status_code == 502 and err(resp) == "io_error"
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 502 and err(resp) == "io_error"
    fake_agent.failures["stat_file"] = AgentUnavailable("The node agent did not respond in time")
    assert plan(clients["owner"], server).status_code == 503
    assert not Job.objects.filter(kind="world_reset").exists()
    # world replace keeps its lenient behaviour
    assert world.layout(server, AgentClient(server.node)) == "vanilla"
    with pytest.raises(ApiError):
        world.layout(server, AgentClient(server.node), strict=True)


@pytest.mark.parametrize("method", ["read_text", "world_info", "status"])
def test_agent_errors_during_the_checks_surface(clients, server, fake_agent, django_capture_on_commit_callbacks, method):
    fake_agent.failures[method] = AgentError("Input/output error", "io_error", 500)
    assert plan(clients["owner"], server).status_code == 502
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 502 and err(resp) == "io_error"
    assert not Job.objects.filter(kind="world_reset").exists()


def test_agent_without_reset_support_is_reported(clients, server, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    """An agent from before this feature answers GET /world without "state": nothing is guessed."""

    def old_world_info(_client, instance_id, level_name, layout, *, sizes=True):
        folder = {"role": "overworld", "path": level_name, "exists": True, "type": "directory", "has_level_dat": True}
        return {"level_name": level_name, "layout": layout, "folders": [folder], "total_bytes": 0, "leftovers": []}

    monkeypatch.setattr(AgentClient, "world_info", old_world_info)
    data = plan(clients["owner"], server).json()
    assert data["delete"] == [] and [b["code"] for b in data["blockers"]] == ["agent_outdated"]
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 502 and err(resp) == "agent_outdated"


# --- POST /world/reset: permissions and validation --------------------------------------------------
def test_reset_permissions(clients, server, fake_agent, make_user, client_for, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    for name in ("operator", "viewer"):
        assert reset(clients[name], server, capture).status_code == 403, name
    assert reset(clients["norole"], server, capture).status_code == 404
    restore_only = Role.objects.create(key="restorer", name="Restorer", permissions=["instance.view", "instance.restore"])
    files_only = Role.objects.create(key="files", name="Files", permissions=["instance.view", "instance.files.write"])
    for role in (restore_only, files_only):
        user = make_user(f"u-{role.key}")
        InstanceAccess.objects.create(user=user, instance=server, role=role)
        assert reset(client_for(user), server, capture).status_code == 403, role.key
    assert not Job.objects.filter(kind="world_reset").exists()
    assert fake_agent.calls == []  # refused before anything is asked of the agent
    # instance.delete is not needed: the two permissions already allow replacing the world
    both = Role.objects.create(
        key="worlds", name="Worlds", permissions=["instance.view", "instance.restore", "instance.files.write"]
    )
    user = make_user("u-both")
    InstanceAccess.objects.create(user=user, instance=server, role=both)
    assert reset(client_for(user), server, capture).status_code == 202
    assert last_job(server).status == JobStatus.SUCCEEDED


def test_abandoning_a_world_reset_needs_the_same_permissions(server, make_user, client_for):
    """Marking the job failed releases the lock while the agent may be deleting: it needs what starting it needs."""
    job = Job.objects.create(kind="world_reset", title="Reset world", instance=server, status=JobStatus.RUNNING, exclusive=True)
    Instance.objects.filter(pk=server.pk).update(active_job=job)
    restore_only = Role.objects.create(
        key="restorer2", name="Restorer", permissions=["instance.view", "instance.restore", "instance.control", "instance.delete"]
    )
    user = make_user("u-restore-only")
    InstanceAccess.objects.create(user=user, instance=server, role=restore_only)
    assert client_for(user).post(f"/api/jobs/{job.id}/abandon").status_code == 403
    both = Role.objects.create(
        key="worlds2", name="Worlds", permissions=["instance.view", "instance.restore", "instance.files.write"]
    )
    user = make_user("u-both-abandon")
    InstanceAccess.objects.create(user=user, instance=server, role=both)
    assert client_for(user).post(f"/api/jobs/{job.id}/abandon").status_code == 200
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error_code == "abandoned"


def test_reset_requires_the_server_name(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    c = clients["manager"]
    resp = reset(c, server, capture, confirm_name=OMIT)
    assert resp.status_code == 400 and err(resp) == "validation_error"
    assert "confirm_name" in resp.json()["error"]["details"]["fields"]
    for typed in ("Creative", "survival", "Survival ", " Survival"):
        resp = reset(c, server, capture, confirm_name=typed)
        assert resp.status_code == 400 and err(resp) == "confirmation_required", typed
    assert reset(c, server, capture, confirm_name="").status_code == 400
    assert not Job.objects.filter(kind="world_reset").exists()
    assert fake_agent.calls == []
    assert fake_agent.get_file(server.id, "world/level.dat") is not None


def test_reset_requires_the_folders_the_dialog_showed(clients, paper, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    c = clients["manager"]
    for folders in (OMIT, [], "bruh", ["a", "b", "c", "d"]):
        resp = reset(c, paper, capture, folders=folders)
        assert resp.status_code == 400 and err(resp) == "validation_error", folders
        assert "folders" in resp.json()["error"]["details"]["fields"]
    for folders in (
        ["bruh"],
        ["bruh", "bruh_nether"],
        ["world", "world_nether", "world_the_end"],
        ["bruh", "bruh_nether", "mods"],
    ):
        resp = reset(c, paper, capture, folders=folders)
        assert resp.status_code == 409 and err(resp) == "world_changed", folders
        assert resp.json()["error"]["details"]["delete"] == BRUH
    assert not Job.objects.filter(kind="world_reset").exists()
    # the order does not matter
    assert reset(c, paper, capture, folders=list(reversed(BRUH))).status_code == 202
    assert last_job(paper).params["folders"] == BRUH


def test_seed_and_keep_validation(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    c = clients["manager"]
    bad = [
        {"seed_mode": "custom"},
        {"seed_mode": "custom", "seed": ""},
        {"seed_mode": "custom", "seed": "   "},
        {"seed_mode": "custom", "seed": "x" * 201},
        {"seed_mode": "custom", "seed": "a\x07b"},
        {"seed_mode": "custom", "seed": "a\nlevel-name=mods"},
    ]
    for fields in bad:
        resp = reset(c, server, capture, **fields)
        assert resp.status_code == 400 and err(resp) == "validation_error", fields
        assert "seed" in resp.json()["error"]["details"]["fields"], fields
    resp = reset(c, server, capture, seed_mode="fixed")
    assert resp.status_code == 400 and "seed_mode" in resp.json()["error"]["details"]["fields"]
    for keep in (["datapacks", "mods"], "datapacks", ["datapacks", "config", "datapacks"]):
        resp = reset(c, server, capture, keep=keep)
        assert resp.status_code == 400 and "keep" in resp.json()["error"]["details"]["fields"], keep
    for field in ("stop_if_running", "pre_restore_backup"):
        resp = reset(c, server, capture, **{field: "maybe"})
        assert resp.status_code == 400 and field in resp.json()["error"]["details"]["fields"]
    assert not Job.objects.filter(kind="world_reset").exists()
    assert fake_agent.calls == []


def test_validate_seed():
    assert world.validate_seed("  -4172144997902289642 ") == "-4172144997902289642"
    assert world.validate_seed("my seed: a=b#c") == "my seed: a=b#c"
    assert world.validate_seed("x" * 200) == "x" * 200
    for value in ("", "  ", "x" * 201, "a\tb", "a" + chr(0x2028) + "b", None):
        with pytest.raises(ApiError) as exc:
            world.validate_seed(value)
        assert exc.value.error_code == "invalid_seed"


def test_archived_server_is_refused(clients, server, fake_agent, django_capture_on_commit_callbacks):
    Instance.objects.filter(pk=server.pk).update(archived_at=timezone.now())
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "archived"
    assert not Job.objects.filter(kind="world_reset").exists() and fake_agent.calls == []


def test_busy_server_is_refused(clients, server, fake_agent, django_capture_on_commit_callbacks):
    first = clients["manager"].post(f"/api/instances/{server.id}/backups", {}, format="json")  # stays queued
    assert first.status_code == 202
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "instance_busy"
    assert not Job.objects.filter(kind="world_reset").exists()
    assert not AuditEvent.objects.filter(action="instance.world_reset").exists()


def test_running_server_needs_consent_to_stop(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "must_stop"
    assert not Job.objects.filter(kind="world_reset").exists()
    assert fake_agent.containers[str(server.id)]["running"] is True


@pytest.mark.parametrize("state", [{"status": "restarting", "running": False, "restarting": True}, {"restarting": True}])
def test_crash_looping_server_counts_as_running(clients, server, fake_agent, django_capture_on_commit_callbacks, state):
    """instance.status says "failed" in a crash loop, but Docker starts the container again by itself."""
    start_container(fake_agent, server)
    fake_agent.set_container_state(server.id, **state)
    Instance.objects.filter(pk=server.pk).update(status="failed")
    assert plan(clients["manager"], server).json()["running"] is True
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "must_stop"
    assert not Job.objects.filter(kind="world_reset").exists()


def test_is_live():
    assert world.is_live({"exists": True, "status": "running", "running": True, "restarting": False})
    assert world.is_live({"exists": True, "status": "restarting", "running": False, "restarting": False})
    assert world.is_live({"exists": True, "status": "exited", "running": False, "restarting": True})
    assert not world.is_live({"exists": True, "status": "exited", "running": False, "restarting": False})
    assert not world.is_live({"exists": True, "status": "created", "running": False})
    assert not world.is_live({"exists": False, "operation": None})


def test_seed_change_is_refused_while_the_seed_variable_is_set(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    Instance.objects.filter(pk=server.pk).update(env={"SEED": "777"})
    for fields in ({"seed_mode": "random"}, {"seed_mode": "custom", "seed": "abc"}):
        resp = reset(clients["manager"], server, capture, **fields)
        assert resp.status_code == 409 and err(resp) == "seed_env_set", fields
    assert not Job.objects.filter(kind="world_reset").exists()
    assert reset(clients["manager"], server, capture, seed_mode="keep").status_code == 202
    assert last_job(server).status == JobStatus.SUCCEEDED
    assert fake_agent.text(server.id, "server.properties") == PROPS


# --- the job ----------------------------------------------------------------------------------------
def test_reset_stopped_server(clients, server, fake_agent, django_capture_on_commit_callbacks):
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.json()
    body = resp.json()
    assert body["kind"] == "world_reset" and body["title"] == "Reset world of Survival" and "params" not in body
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    # the world is gone, everything else is as it was
    files = fake_agent.fs[str(server.id)]
    assert not [p for p in [*files["files"], *files["dirs"]] if p.split("/")[0] == "world"]
    assert not [p for p in files["dirs"] if p.startswith(".mcpanel-world-")]
    assert fake_agent.text(server.id, "server.properties") == PROPS
    assert fake_agent.text(server.id, "mods/a.jar") == "jar" and fake_agent.text(server.id, "ops.json") == "[]"
    assert not fake_agent.called("write_text")
    safety = Backup.objects.get(instance=server)
    assert job.result == {
        "level_name": "world",
        "layout": "vanilla",
        "deleted": ["world"],
        "kept": [],
        "skipped": [],
        "seed_mode": "keep",
        "seed_changed": False,
        "leftover": None,
        "warnings": [],
        "safety_backup": str(safety.id),
        "restarted": False,
    }
    # the safety backup holds the old world and was checked before anything was deleted
    assert safety.kind == BackupKind.PRE_RESTORE and safety.name == "Before resetting the world 'world'"
    assert safety.job_id == job.id and safety.meta["node_id"] == server.node_id
    snap = fake_agent.repos[str(server.id)][-1]
    assert snap["tree"]["files"]["world/region/r.0.0.mca"] == b"old"
    assert fake_agent.called("backup_files") == [((server.id, snap["id"], "world"), {})]
    assert named(fake_agent, "create_backup", "backup_files", "world_reset") == ["create_backup", "backup_files", "world_reset"]
    assert fake_agent.called("world_reset") == [
        ((server.id,), {"level_name": "world", "layout": "vanilla", "expect": ["world"], "keep": ["datapacks", "config"]})
    ]
    # the request and the job check the folders without measuring them
    assert [kwargs for _args, kwargs in fake_agent.called("world_info")] == [{"sizes": False}, {"sizes": False}]
    # a stopped server is neither stopped nor started
    assert not fake_agent.called("stop") and not fake_agent.called("start")
    event = AuditEvent.objects.get(action="instance.world_reset")
    assert event.actor.username == "manager" and event.instance_id == server.id
    assert event.details == {
        "level_name": "world",
        "layout": "vanilla",
        "folders": ["world"],
        "seed_mode": "keep",
        "keep": ["datapacks", "config"],
        "pre_restore_backup": True,
        "stop_if_running": False,
    }
    server.refresh_from_db()
    assert server.active_job_id is None
    assert "Deleted: world" in log_of(job)
    # the server generates the new world on its next start; until then there is nothing to reset
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "no_world"


def test_reset_running_server_call_order(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert named(fake_agent, "stop", "create_backup", "backup_files", "world_reset", "start") == [
        "stop",
        "create_backup",
        "backup_files",
        "world_reset",
        "start",
    ]
    assert fake_agent.called("stop") == [((server.id,), {"timeout": server.stop_timeout})]
    assert job.result["restarted"] is True and job.result["deleted"] == ["world"]
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert fake_agent.get_file(server.id, "world/level.dat") is None
    # the backup was taken of the stopped server, so nothing had to be flushed
    assert (str(server.id), "save-all flush") not in fake_agent.commands
    server.refresh_from_db()
    assert server.desired_state == "running" and server.active_job_id is None
    assert AuditEvent.objects.get(action="instance.world_reset").details["stop_if_running"] is True


def test_reset_restarting_container_is_stopped_first(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.set_container_state(server.id, status="restarting", running=False, restarting=True, exit_code=1)
    Instance.objects.filter(pk=server.pk).update(status="failed")
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert named(fake_agent, "stop", "world_reset", "start") == ["stop", "world_reset", "start"]
    assert job.result["restarted"] is True
    container = fake_agent.containers[str(server.id)]
    assert container["running"] is True and container["restarting"] is False
    assert fake_agent.get_file(server.id, "world/level.dat") is None


def test_server_started_between_request_and_job_is_not_stopped_without_consent(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    resp = reset(
        clients["manager"], server, django_capture_on_commit_callbacks, between=lambda: start_container(fake_agent, server)
    )
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "must_stop"
    assert job.error.startswith("The world was not changed.")
    assert not fake_agent.called("stop") and not fake_agent.called("create_backup") and not fake_agent.called("world_reset")
    assert fake_agent.containers[str(server.id)]["running"] is True


def test_bukkit_reset_deletes_the_dimension_folders_and_carries_over(
    clients, paper, fake_agent, django_capture_on_commit_callbacks
):
    fake_agent.put_file(paper.id, "bruh/datapacks/pack.zip", "dp")
    fake_agent.put_file(paper.id, "bruh/playerdata/a.dat", "player")
    fake_agent.put_file(paper.id, "bruh_nether/paper-world.yml", "b: 2")
    fake_agent.put_file(paper.id, "bruh2/level.dat", LEVEL_DAT)  # another world (Multiverse): not touched
    fake_agent.put_file(paper.id, "plugins/Essentials/userdata/a.yml", "home")
    resp = reset(clients["owner"], paper, django_capture_on_commit_callbacks, folders=BRUH)
    assert resp.status_code == 202, resp.json()
    job = last_job(paper)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["layout"] == "bukkit" and job.result["deleted"] == BRUH
    assert job.result["kept"] == ["bruh/datapacks", "bruh_nether/paper-world.yml"]
    files = fake_agent.fs[str(paper.id)]["files"]
    assert sorted(p for p in files if p.split("/")[0] in BRUH) == ["bruh/datapacks/pack.zip", "bruh_nether/paper-world.yml"]
    assert "bruh2/level.dat" in files and "plugins/Essentials/userdata/a.yml" in files
    # every folder that is deleted was looked up in the snapshot first
    assert [args[2] for args, _kwargs in fake_agent.called("backup_files")] == BRUH
    assert "Carried over: bruh/datapacks, bruh_nether/paper-world.yml" in log_of(job)
    # what was carried over is not a world: a second reset has nothing to delete
    data = plan(clients["owner"], paper).json()
    assert [f["state"] for f in data["folders"]] == ["pending", "pending", "missing"]
    assert [b["code"] for b in data["blockers"]] == ["no_world"]


def test_keep_options_are_passed_to_the_agent(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    fake_agent.put_file(server.id, "world/datapacks/pack.zip", "dp")
    fake_agent.put_file(server.id, "world/serverconfig/mod-server.toml", "x = 1")
    reset(clients["owner"], server, capture, keep=["config"])
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.called("world_reset")[-1][1]["keep"] == ["config"]
    assert job.result["kept"] == ["world/serverconfig"]
    assert fake_agent.get_file(server.id, "world/datapacks/pack.zip") is None
    assert fake_agent.text(server.id, "world/serverconfig/mod-server.toml") == "x = 1"
    assert AuditEvent.objects.get(action="instance.world_reset").details["keep"] == ["config"]
    put_world(fake_agent, server)
    reset(clients["owner"], server, capture, keep=[])
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED and job.result["kept"] == []
    assert not [p for p in fake_agent.fs[str(server.id)]["files"] if p.startswith("world/")]


def test_safety_backup_ignores_the_policy_excludes(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """A server whose backup policy leaves the world out still gets it into the safety backup of a reset."""
    capture = django_capture_on_commit_callbacks
    RetentionPolicy.objects.create(instance=server, excludes=["world", "*.jar"])
    # other backups keep honouring the policy (and the fake agent honours the patterns)
    with capture(execute=True):
        assert clients["owner"].post(f"/api/instances/{server.id}/backups", {}, format="json").status_code == 202
    assert "world" in fake_agent.called("create_backup")[-1][1]["excludes"]
    manual = fake_agent.repos[str(server.id)][-1]["tree"]["files"]
    assert "world/level.dat" not in manual and "mods/a.jar" not in manual and "ops.json" in manual

    fake_agent.put_file(server.id, "cache/junk.bin", "junk")  # the built-in exclusions still apply
    assert plan(clients["owner"], server).json()["backup_exclusions"] == 2
    assert reset(clients["owner"], server, capture).status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    excludes = fake_agent.called("create_backup")[-1][1]["excludes"]
    assert "world" not in excludes and "*.jar" not in excludes and "cache" in excludes
    safety = fake_agent.repos[str(server.id)][-1]["tree"]["files"]
    assert safety["world/region/r.0.0.mca"] == b"old" and "mods/a.jar" in safety and "cache/junk.bin" not in safety


def test_backup_with_warnings_aborts_before_deleting(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """restic exit code 3 (some files unreadable) is a successful backup, but not a safety backup."""
    start_container(fake_agent, server)
    fake_agent.backup_warnings = ["error: open /data/world/region/r.0.0.mca: permission denied"]
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="random")
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "safety_backup_incomplete"
    assert job.error.startswith("The world was not changed.")
    assert "permission denied" in log_of(job)
    assert not fake_agent.called("world_reset") and not fake_agent.called("backup_files")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.text(server.id, "server.properties") == PROPS  # the seed was not written
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert "started again" in log_of(job)
    server.refresh_from_db()
    assert server.desired_state == "running" and server.active_job_id is None
    # the backup record stays, with its warnings
    assert Backup.objects.get(instance=server).warnings == fake_agent.backup_warnings


@pytest.mark.parametrize("lost", ["folder", "marker"])
def test_snapshot_without_the_world_aborts_before_deleting(clients, paper, fake_agent, django_capture_on_commit_callbacks, lost):
    """A snapshot that lacks a world folder (another mount, a restic quirk) is not a safety backup."""
    start_container(fake_agent, paper)

    def drop_from_snapshot(*_args, **_kwargs):
        tree = fake_agent.repos[str(paper.id)][-1]["tree"]
        gone = "bruh_nether/" if lost == "folder" else "bruh_nether/region/"
        for path in [p for p in tree["files"] if p.startswith(gone)]:
            del tree["files"][path]
        tree["dirs"] = {d for d in tree["dirs"] if not (d + "/").startswith(gone)}

    fake_agent.hooks["backup_files"] = drop_from_snapshot
    resp = reset(clients["manager"], paper, django_capture_on_commit_callbacks, folders=BRUH, stop_if_running=True)
    assert resp.status_code == 202
    job = last_job(paper)
    assert job.status == JobStatus.FAILED and job.error_code == "safety_backup_incomplete"
    assert job.error.startswith("The world was not changed.") and "'bruh_nether'" in job.error
    assert ("level.dat" in job.error) is (lost == "folder") and "region" in job.error
    assert not fake_agent.called("world_reset")
    for name in BRUH:
        assert fake_agent.text(paper.id, f"{name}/region/r.0.0.mca") == name
    assert fake_agent.containers[str(paper.id)]["running"] is True


def test_snapshot_that_cannot_be_listed_aborts_before_deleting(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failures["backup_files"] = AgentError("restic ls failed: repository is locked", "restic_failed", 500)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "restic_failed"
    assert job.error.startswith("The world was not changed.")
    assert not fake_agent.called("world_reset")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True


def test_backup_failure_aborts_before_touching_anything(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failed_operations["backup"] = ("restic: repository locked", "backup_failed")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="custom", seed="abc")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "backup_failed"
    assert job.error == "The world was not changed. restic: repository locked"
    assert not fake_agent.called("world_reset") and not fake_agent.called("write_text")
    assert fake_agent.text(server.id, "server.properties") == PROPS
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True
    server.refresh_from_db()
    assert server.active_job_id is None


def test_safety_backup_follows_the_panel_setting(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    SystemSetting.set("pre_restore_backup", False)
    assert plan(clients["owner"], server).json()["pre_restore_backup_default"] is False
    reset(clients["owner"], server, capture)
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED and job.result["safety_backup"] is None
    assert not fake_agent.called("create_backup") and not fake_agent.called("backup_files")
    assert "No safety backup was requested" in log_of(job)
    assert AuditEvent.objects.get(action="instance.world_reset").details["pre_restore_backup"] is False
    put_world(fake_agent, server)
    reset(clients["owner"], server, capture, pre_restore_backup=True)
    assert last_job(server).result["safety_backup"] is not None
    SystemSetting.set("pre_restore_backup", True)
    put_world(fake_agent, server)
    reset(clients["owner"], server, capture, pre_restore_backup=False)
    assert last_job(server).result["safety_backup"] is None


def test_level_name_changed_between_request_and_job(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """The job deletes what was confirmed or nothing: a different world folder means world_changed."""
    start_container(fake_agent, server)

    def rename_level():
        fake_agent.put_file(server.id, "server.properties", PROPS + "level-name=other\n")
        put_world(fake_agent, server, "other", marker="other")

    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks, between=rename_level, stop_if_running=True)
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "world_changed"
    assert job.error.startswith("The world was not changed.")
    assert named(fake_agent, "stop", "create_backup", "world_reset", "start", "write_text") == []
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.text(server.id, "other/region/r.0.0.mca") == "other"
    assert fake_agent.containers[str(server.id)]["running"] is True


def test_world_folders_changed_between_request_and_job(clients, paper, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.delete_files(paper.id, ["bruh_the_end"], recursive=True)
    capture = django_capture_on_commit_callbacks
    resp = reset(
        clients["manager"],
        paper,
        capture,
        between=lambda: put_world(fake_agent, paper, "bruh_the_end", marker="new end"),
        folders=["bruh", "bruh_nether"],
    )
    assert resp.status_code == 202
    job = last_job(paper)
    assert job.status == JobStatus.FAILED and job.error_code == "world_changed"
    assert not fake_agent.called("create_backup") and not fake_agent.called("world_reset")
    assert fake_agent.text(paper.id, "bruh/region/r.0.0.mca") == "bruh"


def test_job_rechecks_archived_blockers_and_the_seed_variable(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    cases = [
        (lambda: Instance.objects.filter(pk=server.pk).update(archived_at=timezone.now()), "archived", {}),
        (
            lambda: Instance.objects.filter(pk=server.pk).update(env={"EXTRA_ARGS": "--universe u"}),
            "world_location_overridden",
            {},
        ),
        (lambda: Instance.objects.filter(pk=server.pk).update(env={"SEED": "1"}), "seed_env_set", {"seed_mode": "random"}),
    ]
    for between, code, fields in cases:
        Instance.objects.filter(pk=server.pk).update(archived_at=None, env={})
        assert reset(clients["owner"], server, capture, between=between, **fields).status_code == 202
        job = last_job(server)
        assert job.status == JobStatus.FAILED and job.error_code == code, code
        assert job.error.startswith("The world was not changed.")
    assert not fake_agent.called("create_backup") and not fake_agent.called("world_reset")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.text(server.id, "server.properties") == PROPS


def test_seed_keep_leaves_server_properties_alone(clients, server, fake_agent, django_capture_on_commit_callbacks):
    reset(clients["owner"], server, django_capture_on_commit_callbacks)
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED and job.result["seed_mode"] == "keep" and job.result["seed_changed"] is False
    assert fake_agent.get_file(server.id, "server.properties") == PROPS.encode()
    assert not fake_agent.called("write_text")


def test_seed_random_clears_level_seed_and_keeps_comments(clients, server, fake_agent, django_capture_on_commit_callbacks):
    reset(clients["owner"], server, django_capture_on_commit_callbacks, seed_mode="random", seed="ignored")
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["seed_mode"] == "random" and job.result["seed_changed"] is True
    assert fake_agent.text(server.id, "server.properties") == PROPS.replace("level-seed=12345", "level-seed=")
    assert job.params["seed"] == ""  # a seed sent with another mode is not stored
    assert named(fake_agent, "write_text", "world_reset") == ["write_text", "world_reset"]  # written before the delete


def test_seed_custom_sets_level_seed_and_keeps_comments(clients, server, fake_agent, django_capture_on_commit_callbacks):
    resp = reset(clients["owner"], server, django_capture_on_commit_callbacks, seed_mode="custom", seed="  my seed: 1=2  ")
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["seed_mode"] == "custom" and job.result["seed_changed"] is True
    text = fake_agent.text(server.id, "server.properties")
    assert text == PROPS.replace("level-seed=12345", "level-seed=my seed: 1=2")
    from apps.instances import properties

    assert properties.parse(text)["level-seed"] == "my seed: 1=2"
    # the seed is in neither the audit trail nor the job as the API shows it
    assert "seed" not in AuditEvent.objects.get(action="instance.world_reset").details
    assert "my seed" not in str(resp.json()) and "my seed" not in log_of(job)


def test_seed_is_added_when_server_properties_has_none(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    fake_agent.put_file(server.id, "server.properties", "#comment\nmotd=hello\n")
    reset(clients["owner"], server, capture, seed_mode="random")  # no seed set already means random
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED and job.result["seed_changed"] is False
    assert fake_agent.text(server.id, "server.properties") == "#comment\nmotd=hello\n"
    put_world(fake_agent, server)
    reset(clients["owner"], server, capture, seed_mode="custom", seed="abc")
    assert last_job(server).result["seed_changed"] is True
    assert fake_agent.text(server.id, "server.properties") == "#comment\nmotd=hello\nlevel-seed=abc\n"


@pytest.mark.parametrize(
    ("failure", "unconfirmed"),
    [
        (AgentUnavailable("The node agent did not respond in time (PUT /files/text)"), True),
        (AgentError("<html>504 Gateway Time-out</html>", "agent_error", 504), True),
        (AgentError("'server.properties' is not a file", "not_a_file", 400), False),  # the agent refused: not written
    ],
)
def test_seed_write_without_an_answer_is_reported(
    clients, server, fake_agent, django_capture_on_commit_callbacks, failure, unconfirmed
):
    """The previous seed is not known when the write got no answer: the job says so instead of guessing."""
    start_container(fake_agent, server)
    fake_agent.failures["write_text"] = failure
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="custom", seed="abc")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == failure.error_code
    assert job.error.startswith("The world was not changed. ")
    assert ("may already hold the new seed" in log_of(job)) is unconfirmed
    assert "back to its previous value" not in log_of(job)
    assert not fake_agent.called("world_reset")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True  # the world is as it was: started again


def edit_motd(fake_agent, inst):
    """A hook: someone edits another key of server.properties while the agent is asked to reset."""

    def _hook(*_args, **_kwargs):
        fake_agent.put_file(
            inst.id, "server.properties", fake_agent.text(inst.id, "server.properties").replace("hello", "edited")
        )

    return _hook


def test_agent_refusal_restores_only_the_seed_and_restarts(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.hooks["world_reset"] = edit_motd(fake_agent, server)
    fake_agent.failures["world_reset"] = AgentError("Stop the server before resetting its world", "must_stop", 409)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="custom", seed="abc")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "must_stop"
    assert job.error == "The world was not changed. Stop the server before resetting its world"
    # level-seed is back, and the edit made meanwhile was not overwritten with the old file
    assert fake_agent.text(server.id, "server.properties") == PROPS.replace("hello", "edited")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert named(fake_agent, "stop", "world_reset", "start") == ["stop", "world_reset", "start"]
    assert "started again" in log_of(job) and "back to its previous value" in log_of(job)
    server.refresh_from_db()
    assert server.desired_state == "running" and server.active_job_id is None
    assert Backup.objects.filter(instance=server, kind=BackupKind.PRE_RESTORE).count() == 1


@pytest.mark.parametrize("code", sorted(world.WORLD_RESET_UNTOUCHED))
def test_clean_agent_failure_restores_only_the_seed_and_restarts(
    clients, server, fake_agent, django_capture_on_commit_callbacks, code
):
    """Every code of the whitelist means the agent left the world as it was."""
    start_container(fake_agent, server)
    fake_agent.hooks["world_reset"] = edit_motd(fake_agent, server)
    fake_agent.failed_operations["world_reset"] = ("The world could not be moved aside (I/O error). Nothing was changed.", code)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="random")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == code
    assert job.error == "The world was not changed. The world could not be moved aside (I/O error)."
    assert fake_agent.text(server.id, "server.properties") == PROPS.replace("hello", "edited")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True
    server.refresh_from_db()
    assert server.desired_state == "running"


def test_clean_failure_of_a_stopped_server_does_not_start_it(clients, server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.failed_operations["world_reset"] = ("The world could not be checked. Nothing was changed.", "world_reset_failed")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, seed_mode="random")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "world_reset_failed"
    assert fake_agent.text(server.id, "server.properties") == PROPS
    assert not fake_agent.called("start")


def test_start_failure_after_a_clean_failure_is_only_logged(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failed_operations["world_reset"] = ("The world could not be checked. Nothing was changed.", "world_reset_failed")
    fake_agent.failures["start"] = AgentError("Docker could not start the container", "start_failed", 500)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "world_reset_failed"  # the original error is kept
    assert "Could not start the server again: Docker could not start the container" in log_of(job)


def lose_operation(fake_agent):
    """A hook for world_reset: the agent accepts the request and forgets the operation (it restarted)."""

    def _hook(*_args, **_kwargs):
        fake_agent.failures["operation"] = AgentError("Operation not found (the agent may have restarted)", "agent_error", 404)

    return _hook


@pytest.mark.parametrize(
    ("how", "code"),
    [
        ("world_rollback_failed", "world_rollback_failed"),
        ("internal_error", "internal_error"),
        ("operation_failed", "operation_failed"),  # a code the panel does not know says nothing about the world
        ("operation_lost", "operation_lost"),
        ("unreachable", "agent_unavailable"),
        ("gateway", "agent_error"),
    ],
)
def test_unknown_outcome_leaves_the_server_stopped(clients, server, fake_agent, django_capture_on_commit_callbacks, how, code):
    """Anything but a clean failure: the world may be gone, so nothing is started and nothing is put back."""
    start_container(fake_agent, server)
    if how in ("world_rollback_failed", "internal_error", "operation_failed"):
        message = "Resetting the world failed and the world could not be put back completely. It is kept in the folder 'x'."
        fake_agent.failed_operations["world_reset"] = (message, how)
    elif how == "operation_lost":
        fake_agent.hooks["world_reset"] = lose_operation(fake_agent)
    elif how == "unreachable":  # the request may have reached the agent although no answer came back
        fake_agent.failures["world_reset"] = AgentUnavailable("The node agent did not respond in time")
    else:  # an answer that is not the agent's: a proxy in between gave up
        fake_agent.failures["world_reset"] = AgentError("<html>502 Bad Gateway</html>", "agent_error", 502)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="custom", seed="abc")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == code
    assert not job.error.startswith("The world was not changed")
    assert fake_agent.containers[str(server.id)]["running"] is False
    assert not fake_agent.called("start")
    server.refresh_from_db()
    assert server.desired_state == "stopped" and server.active_job_id is None
    log = log_of(job)
    assert "left stopped" in log and ".mcpanel-world-old-" in log and "safety backup taken by this job can be restored" in log
    # server.properties is left alone: the new seed stays
    assert fake_agent.text(server.id, "server.properties") == PROPS.replace("level-seed=12345", "level-seed=abc")
    assert named(fake_agent, "write_text") == ["write_text"]


def test_unknown_outcome_without_a_safety_backup(clients, server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.failed_operations["world_reset"] = ("could not put the world back", "world_rollback_failed")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, pre_restore_backup=False)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error == "could not put the world back"
    assert "left stopped" in log_of(job) and "can be restored" not in log_of(job)  # there is no backup to point at
    server.refresh_from_db()
    assert server.desired_state == "stopped"


def test_agent_error_answers_on_the_request_are_refusals(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """An error with the agent's own code (even a 5xx, e.g. Docker is down) proves the reset did not start."""
    start_container(fake_agent, server)
    fake_agent.failures["world_reset"] = AgentError("Cannot reach the Docker daemon", "docker_unavailable", 503)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "docker_unavailable"
    assert job.error.startswith("The world was not changed.")
    assert fake_agent.containers[str(server.id)]["running"] is True


def test_abandoned_during_the_backup(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """The job no longer owns the server: nothing is deleted and nothing is started."""
    start_container(fake_agent, server)
    fake_agent.hooks["create_backup"] = abandon(server)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="custom", seed="abc")
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")  # not overwritten
    assert fake_agent.called("create_backup") and not fake_agent.called("world_reset")
    assert not fake_agent.called("start") and not fake_agent.called("write_text")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.text(server.id, "server.properties") == PROPS
    assert fake_agent.containers[str(server.id)]["running"] is False
    assert "marked as failed before the world was touched" in log_of(job)
    server.refresh_from_db()
    assert server.active_job_id is None


def test_abandoned_during_a_failing_backup(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.hooks["create_backup"] = abandon(server)
    fake_agent.failed_operations["backup"] = ("restic: repository locked", "backup_failed")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert not fake_agent.called("world_reset") and not fake_agent.called("start")
    assert fake_agent.containers[str(server.id)]["running"] is False
    assert "the world was not touched" in log_of(job)


def test_abandoned_during_the_agent_call_with_a_clean_failure(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """Even a failure that left the world alone is not compensated by a job that lost the server."""
    start_container(fake_agent, server)
    fake_agent.hooks["world_reset"] = abandon(server)
    fake_agent.failed_operations["world_reset"] = ("The world could not be checked. Nothing was changed.", "world_reset_failed")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, seed_mode="custom", seed="abc")
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert not fake_agent.called("start")
    assert fake_agent.containers[str(server.id)]["running"] is False
    # no seed restore: server.properties keeps what the job wrote
    assert named(fake_agent, "write_text") == ["write_text"]
    assert fake_agent.text(server.id, "server.properties") == PROPS.replace("level-seed=12345", "level-seed=abc")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert "Nothing more was done" in log_of(job)
    server.refresh_from_db()
    assert server.desired_state == "running" and server.active_job_id is None  # nothing was changed for it either


def test_abandoned_during_a_successful_agent_call_does_not_start_the_server(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    start_container(fake_agent, server)
    fake_agent.hooks["world_reset"] = abandon(server)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert fake_agent.get_file(server.id, "world/level.dat") is None  # the agent did reset the world
    assert not fake_agent.called("start")
    assert fake_agent.containers[str(server.id)]["running"] is False
    assert "The world was reset" in log_of(job) and "left stopped" in log_of(job)


def abandon_when_running(inst):
    """Like ``abandon``, for agent methods the request itself calls before the job exists."""

    def _hook(*_args, **_kwargs):
        job = Job.objects.filter(instance=inst, kind="world_reset").order_by("-created_at").first()
        if job is not None and job.status == JobStatus.RUNNING:
            runner.finish(job, JobStatus.FAILED, error="Marked as failed by owner", error_code="abandoned")

    return _hook


def test_abandoned_before_the_stop_neither_stops_the_server_nor_takes_a_backup(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    """Marked as failed while the job was still checking the plan: it must not touch the server."""
    start_container(fake_agent, server)
    fake_agent.hooks["read_text"] = abandon_when_running(server)  # the job's first agent call
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    assert resp.status_code == 202, resp.json()
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert named(fake_agent, "stop", "create_backup", "world_reset", "write_text", "start") == []
    assert fake_agent.containers[str(server.id)]["running"] is True  # players were not disconnected
    assert Backup.objects.filter(instance=server).count() == 0
    assert "marked as failed before it did anything" in log_of(job)
    server.refresh_from_db()
    assert server.active_job_id is None


def test_abandoned_during_the_stop_takes_no_backup(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.hooks["stop"] = abandon(server)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert named(fake_agent, "stop", "create_backup", "world_reset", "start") == ["stop"]
    assert Backup.objects.filter(instance=server).count() == 0
    assert fake_agent.containers[str(server.id)]["running"] is False
    assert "before the safety backup was taken" in log_of(job) and "the server was left stopped" in log_of(job)
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"


def test_stop_whose_answer_is_lost_is_followed_by_a_start(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """The agent stopped the server but its answer never arrived: "the world was not changed" must
    not leave the server stopped (review 2026-11-10)."""
    start_container(fake_agent, server)

    def stopped_without_an_answer(instance_id, **_kwargs):
        fake_agent._set_exited(fake_agent.containers[str(instance_id)], 0)
        raise AgentUnavailable("The node agent did not respond in time (POST /stop)")

    fake_agent.hooks["stop"] = stopped_without_an_answer
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "agent_unavailable"
    assert job.error.startswith("The world was not changed.")
    assert named(fake_agent, "stop", "create_backup", "world_reset", "start") == ["stop", "start"]
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert "The world is unchanged; the server was started again" in log_of(job)
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    server.refresh_from_db()
    assert server.desired_state == "running"


def test_stop_that_the_agent_refused_leaves_the_server_running(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failures["stop"] = AgentError("Instance is busy with a 'backup' operation", "instance_busy", 409)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "instance_busy"
    assert job.error.startswith("The world was not changed.")
    assert not fake_agent.called("start") and not fake_agent.called("world_reset")
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert "The world is unchanged; the server was not stopped and is still running" in log_of(job)


def test_stop_without_an_answer_that_did_not_stop_the_server(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """No answer and the server runs: it is not started (it runs), and the log does not claim more than is known."""
    start_container(fake_agent, server)
    fake_agent.failures["stop"] = AgentUnavailable("The node agent did not respond in time (POST /stop)")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error.startswith("The world was not changed.")
    assert not fake_agent.called("start") and fake_agent.containers[str(server.id)]["running"] is True
    assert "The stop got no answer and the server is running at the moment" in log_of(job)


def test_abandoned_while_the_stop_fails_does_not_claim_the_server_is_stopped(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    start_container(fake_agent, server)
    fake_agent.hooks["stop"] = abandon(server)
    fake_agent.failures["stop"] = AgentUnavailable("The node agent did not respond in time (POST /stop)")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned")
    assert not fake_agent.called("start") and not fake_agent.called("create_backup")
    assert "the world was not touched, and the server may have been stopped: check it." in log_of(job)


def test_what_the_stop_did_is_in_the_job_log(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.stop_exit_code = 255  # the server runner gave up waiting and ended the server
    fake_agent.stop_info = {"warnings": ["save-all flush before the stop failed, the world may not be saved: timed out"]}
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True, pre_restore_backup=False)
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    text = log_of(job)
    assert "warning: save-all flush before the stop failed" in text
    assert "warning: the server did not shut down cleanly (exit code 255)" in text


def mark_for_resume(inst) -> None:
    """As ``manage.py host_shutdown`` leaves a server it stopped: meant to run, marked."""
    from datetime import timedelta

    Instance.objects.filter(pk=inst.pk).update(
        desired_state="running", resume_after_boot=True, resume_flagged_at=timezone.now() - timedelta(minutes=5)
    )
    inst.refresh_from_db()


@pytest.mark.parametrize(
    ("hook", "failed_operation", "line"),
    [
        ("read_text", None, "before it did anything"),
        ("create_backup", None, "before the world was touched"),
        (
            "world_reset",
            ("the world could not be put back; it is in .mcpanel-world-old-x", "world_rollback_failed"),
            "then the reset failed",
        ),
    ],
    ids=["before-anything", "during-the-backup", "rollback-failed"],
)
def test_abandoned_reset_settles_the_start_after_boot_mark(
    hook, failed_operation, line, clients, server, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    """The server waits to be started again after a host shutdown; a reset of it is marked as
    failed half-way. Whatever state that left, the panel must not start the server by itself."""
    from datetime import timedelta

    from apps.instances import scheduling
    from apps.instances.spec import build_spec, current_spec_hash

    fake_agent.apply(server.id, build_spec(server), start=False)
    Instance.objects.filter(pk=server.pk).update(container_spec_hash=current_spec_hash(server), installed_at=timezone.now())
    mark_for_resume(server)
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() - timedelta(minutes=1))
    fake_agent.hooks[hook] = abandon_when_running(server)
    if failed_operation:
        fake_agent.failed_operations["world_reset"] = failed_operation
    # this test is about the handler: the resume step's own check of failed jobs is tested elsewhere
    monkeypatch.setattr(scheduling, "_job_that_left_it_stopped", lambda *_a, **_k: None)
    resp = reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.json()
    job = last_job(server)
    assert (job.status, job.error_code) == (JobStatus.FAILED, "abandoned") and line in log_of(job)
    server.refresh_from_db()
    assert (server.resume_after_boot, server.resume_flagged_at) == (False, None)
    assert server.desired_state == "running"  # no decision about that was made
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers()
    assert result == {"started": None, "cleared": [], "waiting": 0}
    assert fake_agent.containers[str(server.id)]["running"] is False and not fake_agent.called("start")


def test_reset_with_an_unknown_outcome_settles_the_start_after_boot_mark(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    mark_for_resume(server)
    fake_agent.failed_operations["world_reset"] = ("the world could not be put back", "world_rollback_failed")
    reset(clients["manager"], server, django_capture_on_commit_callbacks, pre_restore_backup=False)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "world_rollback_failed"
    server.refresh_from_db()
    assert (server.resume_after_boot, server.resume_flagged_at, server.desired_state) == (False, None, "stopped")


def test_a_mark_written_after_the_job_began_is_not_dropped_by_it(clients, server, fake_agent, django_capture_on_commit_callbacks):
    """The job was marked as failed and lost the server; the server was then started by someone
    and stopped by host_shutdown again. That newer mark is not this job's to settle."""
    mark_for_resume(server)
    newer = timezone.now()

    def abandon_then_host_shutdown(*_args, **_kwargs):
        abandon(server)()
        Instance.objects.filter(pk=server.pk).update(resume_after_boot=True, resume_flagged_at=newer)

    fake_agent.hooks["create_backup"] = abandon_then_host_shutdown
    reset(clients["manager"], server, django_capture_on_commit_callbacks)
    assert last_job(server).error_code == "abandoned"
    server.refresh_from_db()
    assert (server.resume_after_boot, server.resume_flagged_at) == (True, newer)


def test_start_failure_after_a_reset_says_the_world_was_reset(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failures["start"] = AgentError("Docker could not start the container", "start_failed", 500)
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "start_failed"
    assert job.error == "The world was reset, but the server could not be started again: Docker could not start the container"
    assert fake_agent.get_file(server.id, "world/level.dat") is None
    assert "Deleted: world" in log_of(job)


def test_old_world_that_could_not_be_deleted_is_a_warning(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.world_reset_delete_error = "Device or resource busy"
    reset(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running=True)
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    leftover = job.result["leftover"]
    assert leftover.startswith(".mcpanel-world-old-") and len(job.result["warnings"]) == 1
    assert f"warning: {job.result['warnings'][0]}" in log_of(job)
    assert fake_agent.get_file(server.id, "world/level.dat") is None
    assert fake_agent.text(server.id, f"{leftover}/overworld/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert plan(clients["manager"], server).json()["leftovers"] == [leftover]


def test_world_reset_job_shows_restoring_status(server):
    from apps.instances import state

    assert state.JOB_STATUS["world_reset"] == "restoring"
    # without the entry a server stopped for the reset would show as failed (non-zero exit code, wanted running)
    container = {"exists": True, "status": "exited", "running": False, "exit_code": 143}
    server.desired_state = "running"
    assert state.compute(server, container, "world_reset") == ("restoring", "")


# --- the agent client and the simulated agent -----------------------------------------------------------
def test_agent_client_requests(node, monkeypatch):
    seen = []

    def fake_json(_client, method, path, **kwargs):
        seen.append((method, path, kwargs))
        return {"operation": {"id": "op-1"}, "folders": []}

    monkeypatch.setattr(AgentClient, "_json", fake_json)
    client = AgentClient(node)
    iid = uuid.uuid4()
    op = client.world_reset(iid, level_name="bruh", layout="bukkit", expect=["bruh", "bruh_nether"], keep=["datapacks"])
    assert op == {"id": "op-1"}
    method, path, kwargs = seen[-1]
    assert (method, path) == ("POST", f"/v1/instances/{iid}/world/reset")
    # the instance id is repeated in the body as a deliberate second key
    assert kwargs["json"] == {
        "confirm": str(iid),
        "level_name": "bruh",
        "layout": "bukkit",
        "expect": ["bruh", "bruh_nether"],
        "keep": ["datapacks"],
    }
    client.world_info(iid, "bruh", "bukkit")
    assert seen[-1][:2] == ("GET", f"/v1/instances/{iid}/world")
    assert seen[-1][2]["params"] == {"level_name": "bruh", "layout": "bukkit"}  # unchanged for world import/export
    client.world_info(iid, "bruh", "bukkit", sizes=False)
    assert seen[-1][2]["params"] == {"level_name": "bruh", "layout": "bukkit", "sizes": "false"}


def test_fake_agent_reset_refuses_like_the_agent(paper, fake_agent):
    """The simulation the job tests rely on: the agent's own checks (agent/mcagent/world.py reset)."""
    iid = paper.id
    args = {"level_name": "bruh", "layout": "bukkit", "keep": []}

    def code_of(call):
        with pytest.raises(AgentError) as exc:
            call()
        return exc.value.error_code, exc.value.agent_status

    # refused before an operation starts
    assert code_of(lambda: fake_agent.world_reset(iid, **{**args, "level_name": "plugins"}, expect=BRUH)) == (
        "invalid_level_name",
        400,
    )
    assert code_of(lambda: fake_agent.world_reset(iid, **{**args, "layout": "spigot"}, expect=BRUH))[1] == 422
    assert code_of(lambda: fake_agent.world_reset(iid, **args, expect=[]))[1] == 422
    assert code_of(lambda: fake_agent.world_reset(iid, **{**args, "keep": ["mods"]}, expect=BRUH))[1] == 422
    start_container(fake_agent, paper)
    assert code_of(lambda: fake_agent.world_reset(iid, **args, expect=BRUH)) == ("must_stop", 409)
    fake_agent.set_container_state(iid, status="restarting", running=False, restarting=True)
    assert code_of(lambda: fake_agent.world_reset(iid, **args, expect=BRUH)) == ("must_stop", 409)
    fake_agent.stop(iid)
    # failed operations: nothing is changed
    assert fake_agent.world_reset(iid, **args, expect=["bruh"])["error_code"] == "world_changed"
    fake_agent.put_file(iid, "bruh_the_end/notes.txt", "mine")
    fake_agent.delete_files(iid, ["bruh_the_end/level.dat", "bruh_the_end/region"], recursive=True)
    assert fake_agent.world_reset(iid, **args, expect=BRUH)["error_code"] == "not_a_world"  # one bad folder refuses all
    fake_agent.delete_files(iid, ["bruh_the_end"], recursive=True)
    fake_agent.put_file(iid, "bruh_the_end", "a file")
    assert fake_agent.world_reset(iid, **args, expect=BRUH)["error_code"] == "not_a_directory"
    for name in ("bruh", "bruh_nether"):
        assert fake_agent.text(iid, f"{name}/region/r.0.0.mca") == name
    # empty and pending folders are skipped, the rest is deleted
    fake_agent.delete_files(iid, ["bruh_the_end"])
    fake_agent.mkdir(iid, "bruh_the_end")
    op = fake_agent.world_reset(iid, **args, expect=["bruh", "bruh_nether"])
    assert op["status"] == "succeeded"
    assert op["result"] == {
        "level_name": "bruh",
        "layout": "bukkit",
        "deleted": ["bruh", "bruh_nether"],
        "kept": [],
        "skipped": ["bruh_the_end"],
        "leftover": None,
        "warnings": [],
    }
    assert fake_agent.world_reset(iid, **args, expect=["bruh"])["error_code"] == "world_changed"  # nothing left to delete


def test_fake_agent_backup_listing_and_excludes(server, fake_agent):
    iid = server.id
    fake_agent.put_file(iid, "world/cache/tile.bin", "x")
    fake_agent.put_file(iid, "logs/2026-11-08-1.log.gz", "gz")
    fake_agent.put_file(iid, "logs/latest.log", "log")
    fake_agent.put_file(iid, ".mcpanel-tmp-upload", "tmp")
    excludes = ["cache", "logs/*.log.gz", "/world", "ops.json"]
    snap_id = fake_agent.create_backup(iid, "b1", tags=[], excludes=excludes)["result"]["snapshot_id"]
    tree = fake_agent.repos[str(iid)][-1]["tree"]["files"]
    assert sorted(tree) == ["logs/latest.log", "mods/a.jar", "server.properties", "world/level.dat", "world/region/r.0.0.mca"]
    listing = fake_agent.backup_files(iid, snap_id, "world")
    assert listing["path"] == "world"
    assert [(e["name"], e["path"], e["type"]) for e in listing["entries"]] == [
        ("region", "world/region", "directory"),
        ("level.dat", "world/level.dat", "file"),
    ]
    assert fake_agent.backup_files(iid, snap_id[:8], "nowhere")["entries"] == []
    with pytest.raises(AgentError) as exc:
        fake_agent.backup_files(iid, "0" * 16, "world")
    assert exc.value.error_code == "snapshot_not_found"


def test_verify_world_in_backup(server, fake_agent):
    client = AgentClient(server.node)
    snap_id = fake_agent.create_backup(server.id, "b1", tags=[])["result"]["snapshot_id"]
    folder = {"path": "world", "markers": ["level.dat", "region"]}
    world.verify_world_in_backup(server, client, snap_id, [folder])
    for bad in (
        {"path": "world", "markers": ["level.dat", "session.lock"]},
        {"path": "world", "markers": []},
        {**folder, "path": "w2"},
    ):
        with pytest.raises(ApiError) as exc:
            world.verify_world_in_backup(server, client, snap_id, [folder, bad])
        assert exc.value.error_code == "safety_backup_incomplete" and exc.value.status_code == 409
    with pytest.raises(ApiError) as exc:  # a backup that produced no snapshot id
        world.verify_world_in_backup(server, client, "", [folder])
    assert exc.value.error_code == "safety_backup_incomplete"
