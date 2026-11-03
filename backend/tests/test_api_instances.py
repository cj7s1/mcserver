"""Instance API: RBAC (scenario 8), creation validation, job locking, lifecycle via the fake agent."""

from __future__ import annotations

import uuid

import pytest

from apps.accounts.models import InstanceAccess, Role
from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupStatus
from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db


def err(resp) -> str:
    return resp.json()["error"]["code"]


@pytest.fixture
def pair(make_instance, fake_agent):
    a = make_instance(name="Alpha")
    b = make_instance(name="Bravo")
    for inst in (a, b):
        fake_agent.prepare(inst.id, {"server.properties": "motd=hello\nserver-port=25565\n"})
    return a, b


def run_power(client, inst, action, capture):
    with capture(execute=True):
        resp = client.post(f"/api/instances/{inst.id}/power", {"action": action}, format="json")
    return resp


# --- visibility --------------------------------------------------------------------------------
def test_list_visibility(clients, pair, users, roles):
    a, _b = pair
    names = lambda c: sorted(i["name"] for i in c.get("/api/instances/").json())  # noqa: E731
    assert names(clients["owner"]) == ["Alpha", "Bravo"]
    assert names(clients["viewer"]) == ["Alpha", "Bravo"]  # global viewer role
    assert names(clients["norole"]) == []
    InstanceAccess.objects.create(user=users["norole"], instance=a, role=roles["operator"])
    assert names(clients["norole"]) == ["Alpha"]
    assert clients["anon"].get("/api/instances/").status_code == 403


def test_invisible_instances_are_404(clients, pair, users, roles):
    a, b = pair
    InstanceAccess.objects.create(user=users["norole"], instance=a, role=roles["operator"])
    c = clients["norole"]
    assert c.get(f"/api/instances/{a.id}").status_code == 200
    for url in (
        f"/api/instances/{b.id}",
        f"/api/instances/{b.id}/logs",
        f"/api/instances/{b.id}/files?path=",
        f"/api/instances/{b.id}/backups",
        f"/api/instances/{uuid.uuid4()}",
    ):
        assert c.get(url).status_code == 404, url
    resp = c.post(f"/api/instances/{b.id}/power", {"action": "start"}, format="json")
    assert resp.status_code == 404
    assert not Job.objects.filter(instance=b).exists()


def test_serialized_permissions_reflect_grants(clients, pair, users, roles):
    a, _ = pair
    InstanceAccess.objects.create(user=users["norole"], instance=a, role=roles["viewer"])
    data = clients["norole"].get(f"/api/instances/{a.id}").json()
    assert data["permissions"] == ["instance.console", "instance.view"]
    assert "rcon_password" not in data


# --- viewer ----------------------------------------------------------------------------------------
def test_viewer_is_read_only(clients, pair, fake_agent, django_capture_on_commit_callbacks):
    a, _ = pair
    fake_agent.apply(a.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    v = clients["viewer"]
    assert v.get(f"/api/instances/{a.id}").status_code == 200
    assert v.get(f"/api/instances/{a.id}/logs").status_code == 200  # console permission
    with django_capture_on_commit_callbacks(execute=True):
        denied = [
            v.post(f"/api/instances/{a.id}/power", {"action": "stop"}, format="json"),
            v.post(f"/api/instances/{a.id}/power", {"action": "kill"}, format="json"),
            v.post(f"/api/instances/{a.id}/command", {"command": "op viewer"}, format="json"),
            v.put(f"/api/instances/{a.id}/files/text", {"path": "ops.json", "content": "[]"}, format="json"),
            v.post(f"/api/instances/{a.id}/files/delete", {"paths": ["server.properties"]}, format="json"),
            v.get(f"/api/instances/{a.id}/files?path="),
            v.patch(f"/api/instances/{a.id}", {"memory_mb": 1024}, format="json"),
            v.patch(f"/api/instances/{a.id}/properties", {"values": {"motd": "x"}}, format="json"),
            v.delete(f"/api/instances/{a.id}", {"confirm_name": "Alpha"}, format="json"),
            v.post(f"/api/instances/{a.id}/archive"),
            v.post(f"/api/instances/{a.id}/backups", {}, format="json"),
        ]
    assert [r.status_code for r in denied] == [403] * len(denied)
    assert all(err(r) == "permission_denied" for r in denied)
    assert fake_agent.containers[str(a.id)]["running"] is True
    assert fake_agent.commands == []
    assert fake_agent.text(a.id, "server.properties").startswith("motd=hello")
    assert not Job.objects.exists()


def test_viewer_cannot_restore_or_delete_backups(clients, pair, users):
    a, _ = pair
    backup = Backup.objects.create(instance=a, name="b", status=BackupStatus.SUCCEEDED, snapshot_id="ab" * 32)
    v = clients["viewer"]
    assert v.post(f"/api/instances/{a.id}/backups/{backup.id}/restore", {"confirm": True}, format="json").status_code == 403
    assert v.delete(f"/api/instances/{a.id}/backups/{backup.id}").status_code == 403
    # operators may create backups but not restore/delete them (destructive)
    op = clients["operator"]
    assert op.post(f"/api/instances/{a.id}/backups/{backup.id}/restore", {"confirm": True}, format="json").status_code == 403
    assert op.delete(f"/api/instances/{a.id}/backups/{backup.id}").status_code == 403
    assert op.patch(f"/api/instances/{a.id}/backups/{backup.id}", {"pinned": True}, format="json").status_code == 403
    assert not Job.objects.exists()


# --- operator ----------------------------------------------------------------------------------------
def test_operator_can_power_but_not_configure(clients, pair, fake_agent, django_capture_on_commit_callbacks):
    a, _ = pair
    op = clients["operator"]
    resp = run_power(op, a, "start", django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    a.refresh_from_db()
    assert a.desired_state == "running"
    assert a.status == "running"
    assert a.active_job_id is None
    assert fake_agent.containers[str(a.id)]["running"] is True
    assert a.container_spec_hash  # created from the current spec

    out = op.post(f"/api/instances/{a.id}/command", {"command": "/list"}, format="json")
    assert out.status_code == 200
    assert out.json() == {"command": "list", "output": "There are 0 of a max of 20 players online: "}
    assert AuditEvent.objects.filter(action="instance.command", actor__username="operator").exists()
    assert op.get(f"/api/instances/{a.id}/files?path=").status_code == 200

    assert run_power(op, a, "stop", django_capture_on_commit_callbacks).status_code == 202
    a.refresh_from_db()
    assert a.status == "stopped" and a.desired_state == "stopped"
    assert fake_agent.containers[str(a.id)]["running"] is False

    assert op.patch(f"/api/instances/{a.id}", {"memory_mb": 8192}, format="json").status_code == 403
    assert op.patch(f"/api/instances/{a.id}/properties", {"values": {"pvp": False}}, format="json").status_code == 403
    assert op.put(f"/api/instances/{a.id}/files/text", {"path": "x.txt", "content": "x"}, format="json").status_code == 403
    assert op.post(f"/api/instances/{a.id}/recreate").status_code == 403
    assert op.delete(f"/api/instances/{a.id}", {"confirm_name": "Alpha"}, format="json").status_code == 403
    a.refresh_from_db()
    assert a.memory_mb == 2048


def test_command_validation(clients, pair, fake_agent):
    a, _ = pair
    fake_agent.apply(a.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    op = clients["operator"]
    assert op.post(f"/api/instances/{a.id}/command", {"command": "say a\nop me"}, format="json").status_code == 400
    assert op.post(f"/api/instances/{a.id}/command", {"command": ""}, format="json").status_code == 400
    assert op.post(f"/api/instances/{a.id}/command", {"command": "x" * 1401}, format="json").status_code == 400
    assert fake_agent.commands == []


def test_command_on_stopped_server_reports_agent_error(clients, pair, fake_agent):
    a, _ = pair
    fake_agent.apply(a.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=False)
    resp = clients["operator"].post(f"/api/instances/{a.id}/command", {"command": "list"}, format="json")
    assert resp.status_code == 409
    assert err(resp) == "not_running"


def test_per_instance_grant_only_covers_that_instance(
    clients, pair, users, roles, fake_agent, django_capture_on_commit_callbacks
):
    a, b = pair
    InstanceAccess.objects.create(user=users["norole"], instance=a, role=roles["operator"])
    c = clients["norole"]
    assert run_power(c, a, "start", django_capture_on_commit_callbacks).status_code == 202
    assert fake_agent.containers[str(a.id)]["running"] is True
    assert run_power(c, b, "start", django_capture_on_commit_callbacks).status_code == 404
    assert str(b.id) not in fake_agent.containers
    # a grant does not confer global rights
    assert c.get("/api/users").status_code == 403
    resp = c.post(
        "/api/instances/",
        {"name": "x", "accept_eula": True, "server_type": "VANILLA", "minecraft_version": "1.20.4"},
        format="json",
    )
    assert resp.status_code == 403


def test_kill_is_immediate_and_audited(clients, pair, fake_agent):
    a, _ = pair
    fake_agent.apply(a.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    Instance.objects.filter(pk=a.pk).update(desired_state="running", installed_at="2026-01-01T00:00:00Z")
    resp = clients["operator"].post(f"/api/instances/{a.id}/power", {"action": "kill"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["status"]["exit_code"] == 137
    a.refresh_from_db()
    assert a.desired_state == "stopped"
    assert a.status == "stopped"
    assert AuditEvent.objects.filter(action="instance.kill").exists()


# --- job lock --------------------------------------------------------------------------------------------
def test_job_lock_returns_instance_busy(clients, pair, fake_agent, django_capture_on_commit_callbacks):
    a, _ = pair
    op = clients["operator"]
    # inside the test transaction on_commit never fires, so the first job stays queued
    first = op.post(f"/api/instances/{a.id}/power", {"action": "start"}, format="json")
    assert first.status_code == 202
    second = op.post(f"/api/instances/{a.id}/power", {"action": "stop"}, format="json")
    assert second.status_code == 409
    body = second.json()["error"]
    assert body["code"] == "instance_busy"
    assert body["details"]["job_id"] == first.json()["id"]
    backup = op.post(f"/api/instances/{a.id}/backups", {}, format="json")
    assert backup.status_code == 409 and err(backup) == "instance_busy"
    # other instances are unaffected
    assert op.post(f"/api/instances/{pair[1].id}/power", {"action": "start"}, format="json").status_code == 202
    # once the job finishes the lock is released
    from apps.jobs.runner import run_job

    run_job(first.json()["id"])
    a.refresh_from_db()
    assert a.active_job_id is None
    assert run_power(op, a, "stop", django_capture_on_commit_callbacks).status_code == 202


def test_failed_job_releases_lock_and_reports_error(clients, pair, fake_agent, django_capture_on_commit_callbacks):
    a, _ = pair
    fake_agent.failed_operations["apply"] = ("Docker refused to create the container: port is already allocated", "create_failed")
    resp = run_power(clients["operator"], a, "start", django_capture_on_commit_callbacks)
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.FAILED
    assert job.error_code == "create_failed"
    assert "already allocated" in job.error
    a.refresh_from_db()
    assert a.active_job_id is None
    detail = clients["operator"].get(f"/api/jobs/{job.id}").json()
    assert detail["status"] == "failed" and detail["error_code"] == "create_failed"


# --- creation --------------------------------------------------------------------------------------------
VALID = {"name": "Survival", "accept_eula": True, "server_type": "VANILLA", "minecraft_version": "1.20.4", "memory_mb": 1024}


def create(client, **overrides):
    body = {**VALID, **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    return client.post("/api/instances/", body, format="json")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"accept_eula": False}, "accept_eula"),
        ({"accept_eula": None}, "accept_eula"),
        ({"env": {"1BAD": "x"}}, "env"),
        ({"env": {"BAD-KEY": "x"}}, "env"),
        ({"env": {"EULA": "FALSE"}}, "env"),
        ({"env": {"memory": "64G"}}, "env"),
        ({"env": {"RCON_PASSWORD": "x"}}, "env"),
        ({"env": {"OK": "nul\x00byte"}}, "env"),
        ({"env": ["not", "a", "dict"]}, "env"),
        ({"jvm_args": "-Xmx8G"}, "jvm_args"),
        ({"jvm_args": "-XX:+UseG1GC -Xms1G"}, "jvm_args"),
        ({"jvm_args": "not-an-option"}, "jvm_args"),
        ({"memory_mb": 128}, "memory_mb"),
        ({"game_port": 80}, "game_port"),
        ({"java_version": 9}, "java_version"),
        ({"server_type": "CUSTOM"}, "server_type"),
        ({"server_type": None}, "server_type"),
        ({"minecraft_version": None}, "minecraft_version"),
        ({"server_properties": {"server-port": 1}}, "server_properties"),
        ({"server_properties": {"max-players": 0}}, "server_properties"),
        ({"host_ip": "not-an-ip"}, "host_ip"),
    ],
)
def test_create_validation(clients, node, fake_agent, mojang_java, overrides, field):
    resp = create(clients["owner"], **overrides)
    assert resp.status_code == 400, resp.content
    body = resp.json()["error"]
    assert body["code"] == "validation_error"
    assert field in body["details"]["fields"]
    assert not Instance.objects.exists()


def test_create_port_conflicts(clients, node, fake_agent, mojang_java, make_instance):
    make_instance(name="Existing", game_port=25600)
    resp = create(clients["owner"], game_port=25600)
    assert resp.status_code == 409
    assert err(resp) == "port_conflict"
    assert resp.json()["error"]["details"] == {"port": 25600}
    fake_agent.add_foreign_port(25601, container="someone-else")
    resp = create(clients["owner"], game_port=25601)
    assert resp.status_code == 409 and err(resp) == "port_conflict"
    assert Instance.objects.count() == 1


def test_create_permissions(clients, node, fake_agent, mojang_java):
    for who in ("viewer", "operator", "manager", "norole", "anon"):
        assert create(clients[who]).status_code == 403, who
    assert not Instance.objects.exists()


def test_create_and_install(clients, node, fake_agent, mojang_java, django_capture_on_commit_callbacks, settings):
    settings.MCPANEL_PORT_RANGE_START = 25565
    settings.MCPANEL_PORT_RANGE_END = 25600
    fake_agent.add_foreign_port(25565)
    with django_capture_on_commit_callbacks(execute=True):
        resp = create(
            clients["admin"],
            env={"MOTD": "ignored by props", "DIFFICULTY": "hard"},
            jvm_args="-XX:+UseG1GC",
            server_properties={"pvp": False, "difficulty": "hard"},
        )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["java"]["java_version"] == 17
    inst = Instance.objects.get(pk=body["instance"]["id"])
    assert inst.game_port == 25566  # 25565 is taken by another container
    assert inst.image == "itzg/minecraft-server:stable-java17"
    assert inst.java_version == 17
    assert inst.memory_mb == 1024
    job = Job.objects.get(pk=body["job"]["id"])
    assert job.kind == "install" and job.status == JobStatus.SUCCEEDED, job.error
    props = fake_agent.text(inst.id, "server.properties")
    assert "motd=Survival" in props and "pvp=false" in props and "difficulty=hard" in props
    container = fake_agent.containers[str(inst.id)]
    assert container["running"] is True
    env = container["spec"]["env"]
    assert env["EULA"] == "TRUE" and env["MEMORY"] == "1024M" and env["DIFFICULTY"] == "hard"
    assert container["spec"]["ports"][0] == {"container_port": 25565, "host_port": 25566, "protocol": "tcp", "host_ip": "0.0.0.0"}
    inst.refresh_from_db()
    assert inst.desired_state == "running"
    assert inst.status == "running"
    assert inst.installed_at is not None  # first healthy observation
    assert AuditEvent.objects.filter(action="instance.created").exists()


def test_create_without_start(clients, node, fake_agent, mojang_java, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = create(clients["owner"], start=False, game_port=25640)
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert fake_agent.containers[str(inst.id)]["running"] is False
    inst.refresh_from_db()
    assert inst.status == "created"
    assert inst.desired_state == "stopped"


def test_creator_without_global_view_gets_manager_grant(make_user, client_for, node, fake_agent, mojang_java):
    creator_role = Role.objects.create(key="creator", name="Creator", permissions=["instance.create"])
    creator = make_user("creator", role=creator_role)
    resp = create(client_for(creator))
    assert resp.status_code == 201
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    grant = InstanceAccess.objects.get(user=creator, instance=inst)
    assert grant.role.key == "server_manager"
    assert client_for(creator).get(f"/api/instances/{inst.id}").status_code == 200


# --- configuration ------------------------------------------------------------------------------------
def test_patch_validation_and_restart_flag(clients, pair, fake_agent, make_instance):
    a, b = pair
    m = clients["manager"]
    for body in (
        {"env": {"TYPE": "PAPER"}},
        {"jvm_args": "-Xmx2G"},
        {"memory_mb": 100},
        {"restart_policy": "always"},
        {"network": "bad name!"},
    ):
        assert m.patch(f"/api/instances/{a.id}", body, format="json").status_code == 400, body
    conflict = m.patch(f"/api/instances/{a.id}", {"game_port": b.game_port}, format="json")
    assert conflict.status_code == 409 and err(conflict) == "port_conflict"
    before = Instance.objects.get(pk=a.pk).config_revision
    ok = m.patch(f"/api/instances/{a.id}", {"memory_mb": 3072, "description": "hi"}, format="json")
    assert ok.status_code == 200
    a.refresh_from_db()
    assert a.memory_mb == 3072 and a.config_revision == before + 1
    # description-only edits do not require a container recreate
    m.patch(f"/api/instances/{a.id}", {"description": "again"}, format="json")
    a.refresh_from_db()
    assert a.config_revision == before + 1


def test_properties_endpoint_rejects_managed_keys(clients, pair, fake_agent):
    a, _ = pair
    m = clients["manager"]
    got = m.get(f"/api/instances/{a.id}/properties").json()
    assert got["values"]["motd"] == "hello" and "server-port" in got["managed_keys"]
    assert m.patch(f"/api/instances/{a.id}/properties", {"values": {"server-port": 1}}, format="json").status_code == 400
    raw = m.patch(f"/api/instances/{a.id}/properties", {"raw": "motd=hello\nserver-port=1\n"}, format="json")
    assert raw.status_code == 400
    ok = m.patch(f"/api/instances/{a.id}/properties", {"values": {"motd": "New\nLine", "pvp": False}}, format="json")
    assert ok.status_code == 200
    assert sorted(ok.json()["changed"]) == ["motd", "pvp"]
    text = fake_agent.text(a.id, "server.properties")
    assert "server-port=25565" in text and "motd=New\\nLine" in text and "pvp=false" in text


def test_files_round_trip_for_manager(clients, pair, fake_agent):
    a, _ = pair
    m = clients["manager"]
    assert m.post(f"/api/instances/{a.id}/files/mkdir", {"path": "config"}, format="json").status_code == 200
    assert (
        m.put(f"/api/instances/{a.id}/files/text", {"path": "config/a.toml", "content": "x = 1"}, format="json").status_code
        == 200
    )
    listing = m.get(f"/api/instances/{a.id}/files?path=config").json()
    assert [e["name"] for e in listing["entries"]] == ["a.toml"]
    assert m.get(f"/api/instances/{a.id}/files/text?path=config/a.toml").json()["content"] == "x = 1"
    escape = m.get(f"/api/instances/{a.id}/files/text?path=../../etc/passwd")
    assert escape.status_code == 400 and err(escape) == "invalid_path"
    assert m.post(f"/api/instances/{a.id}/files/delete", {"paths": ["config"]}, format="json").status_code == 409
    assert (
        m.post(f"/api/instances/{a.id}/files/delete", {"paths": ["config"], "recursive": True}, format="json").status_code == 200
    )
    assert fake_agent.get_file(a.id, "config/a.toml") is None


# --- delete / archive ------------------------------------------------------------------------------------
def test_delete_requires_confirmation_and_removes_everything(clients, pair, fake_agent, django_capture_on_commit_callbacks):
    a, _ = pair
    fake_agent.apply(a.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    owner = clients["owner"]
    wrong = owner.delete(f"/api/instances/{a.id}", {"confirm_name": "alpha"}, format="json")
    assert wrong.status_code == 400 and err(wrong) == "confirmation_required"
    with django_capture_on_commit_callbacks(execute=True):
        resp = owner.delete(f"/api/instances/{a.id}", {"confirm_name": "Alpha"}, format="json")
    assert resp.status_code == 202
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.instance_id is None  # job history survives the instance
    assert not Instance.objects.filter(pk=a.pk).exists()
    assert str(a.id) not in fake_agent.containers and str(a.id) not in fake_agent.fs


def test_archive_and_unarchive(clients, pair, fake_agent, django_capture_on_commit_callbacks):
    a, _ = pair
    fake_agent.apply(a.id, {"image": "itzg/minecraft-server:java17", "ports": []}, start=True)
    owner = clients["owner"]
    with django_capture_on_commit_callbacks(execute=True):
        assert owner.post(f"/api/instances/{a.id}/archive").status_code == 202
    a.refresh_from_db()
    assert a.archived_at is not None and a.status == "archived"
    assert str(a.id) not in fake_agent.containers  # container removed
    assert fake_agent.get_file(a.id, "server.properties") is not None  # data kept
    assert [i["name"] for i in owner.get("/api/instances/").json()] == ["Bravo"]
    assert [i["name"] for i in owner.get("/api/instances/?archived=true").json()] == ["Alpha"]
    start = owner.post(f"/api/instances/{a.id}/power", {"action": "start"}, format="json")
    assert start.status_code == 409 and err(start) == "archived"
    assert owner.post(f"/api/instances/{a.id}/unarchive").status_code == 200
    a.refresh_from_db()
    assert a.archived_at is None


# --- access grants --------------------------------------------------------------------------------------
def test_access_grants_cannot_exceed_grantor(make_user, client_for, pair, users, roles):
    a, _ = pair
    delegate = Role.objects.create(
        key="delegate", name="Delegate", permissions=["instance.view", "instance.access", "instance.control"]
    )
    lead = make_user("lead")
    InstanceAccess.objects.create(user=lead, instance=a, role=delegate)
    c = client_for(lead)
    viewer_grant = c.put(f"/api/instances/{a.id}/access", {"user": users["norole"].id, "role": roles["viewer"].id}, format="json")
    assert viewer_grant.status_code == 403  # viewer includes instance.console which lead lacks
    watcher = Role.objects.create(key="watcher", name="Watcher", permissions=["instance.view", "instance.control"])
    ok = c.put(f"/api/instances/{a.id}/access", {"user": users["norole"].id, "role": watcher.id}, format="json")
    assert ok.status_code == 200
    # cannot upgrade itself to server manager on this instance
    escalate = c.put(f"/api/instances/{a.id}/access", {"user": lead.id, "role": roles["server_manager"].id}, format="json")
    assert escalate.status_code == 403
    assert InstanceAccess.objects.get(user=lead, instance=a).role == delegate
    # the owner can grant anything
    owner_grant = client_for(users["owner"]).put(
        f"/api/instances/{a.id}/access", {"user": lead.id, "role": roles["server_manager"].id}, format="json"
    )
    assert owner_grant.status_code == 200, owner_grant.content


@pytest.mark.parametrize(
    "body",
    [
        {"user": "abc", "role": "x"},
        {"user": True, "role": 1},
        {"user": -1, "role": 1},
        {"user": 1.5, "role": 1},
        {"user": 10**30, "role": 1},
        ["a"],
        {},
    ],
)
def test_access_grant_rejects_malformed_input(clients, pair, body):
    a, _ = pair
    resp = clients["owner"].put(f"/api/instances/{a.id}/access", body, format="json")
    assert resp.status_code == 400, resp.content
    assert not InstanceAccess.objects.exists()


@pytest.mark.parametrize("query", ["", "?user=abc", "?user=-1", "?user=1e3"])
def test_access_revoke_rejects_malformed_user(clients, pair, query):
    a, _ = pair
    assert clients["owner"].delete(f"/api/instances/{a.id}/access{query}").status_code == 400


def test_access_grants_cannot_override_or_revoke_stronger_grants(make_user, client_for, pair, users, roles):
    a, _ = pair
    delegate = Role.objects.create(
        key="delegate2", name="Delegate", permissions=["instance.view", "instance.access", "instance.control"]
    )
    lead = make_user("lead2")
    InstanceAccess.objects.create(user=lead, instance=a, role=delegate)
    InstanceAccess.objects.create(user=users["norole"], instance=a, role=roles["server_manager"])
    c = client_for(lead)
    watcher = Role.objects.create(key="watcher2", name="Watcher", permissions=["instance.view"])
    downgrade = c.put(f"/api/instances/{a.id}/access", {"user": users["norole"].id, "role": watcher.id}, format="json")
    assert downgrade.status_code == 403
    assert c.delete(f"/api/instances/{a.id}/access?user={users['norole'].id}").status_code == 403
    assert InstanceAccess.objects.get(user=users["norole"], instance=a).role == roles["server_manager"]
    # the owner can
    assert client_for(users["owner"]).delete(f"/api/instances/{a.id}/access?user={users['norole'].id}").status_code == 204
    assert not InstanceAccess.objects.filter(user=users["norole"], instance=a).exists()
