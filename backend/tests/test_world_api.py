"""World import/export: GET /world, the zip export link, POST /world/replace and the world_replace job."""

from __future__ import annotations

import gzip
import io
import uuid
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import InstanceAccess, Role
from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupKind
from apps.core.models import SystemSetting
from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db

LEVEL_DAT = gzip.compress(b"\x0a\x00\x00\x00")


def err(resp) -> str:
    return resp.json()["error"]["code"]


def zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


NEW_WORLD = {"MyWorld/level.dat": LEVEL_DAT, "MyWorld/region/r.0.0.mca": "new"}


def put_world(fake_agent, inst, name="world", marker="old"):
    fake_agent.put_file(inst.id, f"{name}/level.dat", LEVEL_DAT)
    fake_agent.put_file(inst.id, f"{name}/region/r.0.0.mca", marker)


@pytest.fixture
def server(make_instance, fake_agent):
    inst = make_instance(name="Survival")
    fake_agent.prepare(inst.id, {"server.properties": "motd=hello\n"})
    put_world(fake_agent, inst)
    return inst


def start_container(fake_agent, inst):
    from apps.instances.spec import build_spec

    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running", desired_state="running")
    inst.refresh_from_db()


def replace(client, inst, capture, data=None, entries=NEW_WORLD, filename="world.zip", **fields):
    body = {"confirm": "true", **fields}
    if data is not None:
        with capture(execute=True):
            return client.post(f"/api/instances/{inst.id}/world/replace", {**body, **data}, format="json")
    body["file"] = SimpleUploadedFile(filename, zip_bytes(entries), content_type="application/zip")
    with capture(execute=True):
        return client.post(f"/api/instances/{inst.id}/world/replace", body, format="multipart")


def last_job(inst) -> Job:
    return Job.objects.filter(instance=inst, kind="world_replace").latest("created_at")


def temp_uploads(fake_agent, inst) -> list[str]:
    return [p for p in fake_agent.fs[str(inst.id)]["files"] if p.startswith(".mcpanel-world-upload-")]


# --- GET /world --------------------------------------------------------------------------------
def test_world_info_and_export_link(clients, server, fake_agent):
    data = clients["manager"].get(f"/api/instances/{server.id}/world").json()
    assert data["level_name"] == "world" and data["level_name_source"] == "default"
    assert data["layout"] == "vanilla" and data["exists"] is True
    assert data["total_bytes"] == len(LEVEL_DAT) + 3
    assert data["folders"][0]["path"] == "world" and data["folders"][0]["has_level_dat"] is True
    assert data["download_url"].startswith(f"/api/instances/{server.id}/files/archive?path=world&")
    assert data["can_replace"] is True and data["running"] is False
    assert data["pre_restore_backup_default"] is True
    # operators may read files (and download the world) but not replace it
    op = clients["operator"].get(f"/api/instances/{server.id}/world").json()
    assert op["can_replace"] is False
    assert clients["viewer"].get(f"/api/instances/{server.id}/world").status_code == 403
    assert clients["norole"].get(f"/api/instances/{server.id}/world").status_code == 404


def test_world_info_level_name_sources(clients, server, fake_agent):
    fake_agent.put_file(server.id, "server.properties", "level-name=survival\n")
    put_world(fake_agent, server, "survival")
    data = clients["owner"].get(f"/api/instances/{server.id}/world").json()
    assert (data["level_name"], data["level_name_source"]) == ("survival", "server.properties")
    Instance.objects.filter(pk=server.pk).update(env={"LEVEL": "hardcore", "WORLD": "https://example.invalid/w.zip"})
    data = clients["owner"].get(f"/api/instances/{server.id}/world").json()
    assert (data["level_name"], data["level_name_source"]) == ("hardcore", "env")
    assert data["exists"] is False and data["download_url"] is None
    assert any("WORLD" in w for w in data["warnings"])


@pytest.mark.parametrize("level", ["plugins", "../escape", ".hidden", "a\\\\b"])
def test_world_info_reports_unsupported_level_names(clients, server, fake_agent, level):
    fake_agent.put_file(server.id, "server.properties", f"level-name={level}\n")
    data = clients["owner"].get(f"/api/instances/{server.id}/world").json()
    assert data["error"]["code"] == "invalid_level_name"
    assert data["download_url"] is None


def test_bukkit_layout_lists_and_exports_dimension_folders(clients, make_instance, fake_agent, client_for, users):
    inst = make_instance(name="Paper", server_type="PAPER")
    fake_agent.prepare(inst.id)
    put_world(fake_agent, inst)
    put_world(fake_agent, inst, "world_nether", "nether")
    data = clients["owner"].get(f"/api/instances/{inst.id}/world").json()
    assert data["layout"] == "bukkit"
    assert [f["path"] for f in data["folders"]] == ["world", "world_nether", "world_the_end"]
    assert [f["exists"] for f in data["folders"]] == [True, True, False]
    url = data["download_url"]
    assert "path=world&path=world_nether&" in url and "world_the_end" not in url
    resp = clients["owner"].get(url)
    assert resp.status_code == 200
    assert 'filename="Paper-world.zip"' in resp["Content-Disposition"]
    names = set(zipfile.ZipFile(io.BytesIO(b"".join(resp.streaming_content))).namelist())
    assert names == {"world/level.dat", "world/region/r.0.0.mca", "world_nether/level.dat", "world_nether/region/r.0.0.mca"}
    event = AuditEvent.objects.filter(action="files.download_archive").latest("created_at")
    assert event.details["paths"] == ["world", "world_nether"]


def test_bukkit_yml_marks_a_custom_server_as_bukkit_layout(clients, server, fake_agent):
    fake_agent.put_file(server.id, "bukkit.yml", "settings: {}\n")
    assert clients["owner"].get(f"/api/instances/{server.id}/world").json()["layout"] == "bukkit"


def test_export_flushes_a_running_server_first(clients, server, fake_agent):
    start_container(fake_agent, server)
    url = clients["owner"].get(f"/api/instances/{server.id}/world").json()["download_url"]
    resp = clients["owner"].get(url)
    assert resp.status_code == 200
    assert (str(server.id), "save-all flush") in fake_agent.commands
    assert AuditEvent.objects.filter(action="files.download_archive").latest("created_at").details["flushed"] is True


def test_export_flush_needs_command_or_backup_permission(clients, server, fake_agent, make_user, client_for):
    """save-all flush is a console command: a user who may only read files downloads what is on disk."""
    start_container(fake_agent, server)
    reader = make_user("u-reader")
    role = Role.objects.create(key="reader", name="Reader", permissions=["instance.view", "instance.files.read"])
    InstanceAccess.objects.create(user=reader, instance=server, role=role)
    c = client_for(reader)
    data = c.get(f"/api/instances/{server.id}/world").json()
    assert data["download_flushes"] is False and "flush=" not in data["download_url"]
    # even when the parameter is added by hand
    resp = c.get(data["download_url"] + "&flush=true")
    assert resp.status_code == 200
    assert (str(server.id), "save-all flush") not in fake_agent.commands
    assert "flushed" not in AuditEvent.objects.filter(action="files.download_archive").latest("created_at").details
    # operators (instance.command / instance.backup) get the flush
    op = clients["operator"].get(f"/api/instances/{server.id}/world").json()
    assert op["download_flushes"] is True and "flush=true" in op["download_url"]


# --- POST /world/replace: permissions and validation ---------------------------------------------
def test_replace_permissions(clients, server, fake_agent, make_user, client_for, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    for name in ("operator", "viewer"):
        assert replace(clients[name], server, capture).status_code == 403, name
    assert replace(clients["norole"], server, capture).status_code == 404
    restore_only = Role.objects.create(key="restorer", name="Restorer", permissions=["instance.view", "instance.restore"])
    files_only = Role.objects.create(key="files", name="Files", permissions=["instance.view", "instance.files.write"])
    for role in (restore_only, files_only):
        user = make_user(f"u-{role.key}")
        InstanceAccess.objects.create(user=user, instance=server, role=role)
        resp = replace(client_for(user), server, capture)
        assert resp.status_code == 403, role.key
    assert not Job.objects.filter(kind="world_replace").exists()
    assert not temp_uploads(fake_agent, server)
    both = Role.objects.create(
        key="worlds", name="Worlds", permissions=["instance.view", "instance.restore", "instance.files.write"]
    )
    user = make_user("u-both")
    InstanceAccess.objects.create(user=user, instance=server, role=both)
    assert replace(client_for(user), server, capture).status_code == 202


def test_replace_requires_confirmation_and_an_archive(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    c = clients["manager"]
    resp = replace(c, server, capture, confirm="false")
    assert resp.status_code == 400 and err(resp) == "confirmation_required"
    resp = replace(c, server, capture, data={})
    assert resp.status_code == 400 and "file" in resp.json()["error"]["details"]["fields"]
    resp = replace(c, server, capture, filename="world.rar")
    assert resp.status_code == 400 and "file" in resp.json()["error"]["details"]["fields"]
    resp = replace(c, server, capture, data={"archive": "missing.zip"})
    assert resp.status_code == 404
    assert not Job.objects.filter(kind="world_replace").exists()
    assert not temp_uploads(fake_agent, server)


def test_invalid_level_name_is_refused_before_uploading(clients, server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(server.id, "server.properties", "level-name=plugins\n")
    resp = replace(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 400 and err(resp) == "invalid_level_name"
    assert not fake_agent.called("upload")


def test_busy_server_is_refused_before_uploading(clients, server, fake_agent, django_capture_on_commit_callbacks):
    first = clients["manager"].post(f"/api/instances/{server.id}/backups", {}, format="json")  # stays queued
    assert first.status_code == 202
    resp = replace(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "instance_busy"
    assert not fake_agent.called("upload")


def test_running_server_needs_consent_to_stop(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    resp = replace(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 409 and err(resp) == "must_stop"
    assert not fake_agent.called("upload")


# --- the job ----------------------------------------------------------------------------------------
def test_replace_stopped_server(clients, server, fake_agent, django_capture_on_commit_callbacks):
    resp = replace(clients["manager"], server, django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.json()
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "new"
    assert fake_agent.text(server.id, "server.properties") == "motd=hello\n"
    assert job.result["installed"] == ["world"] and job.result["replaced"] == ["world"]
    assert job.result["restarted"] is False
    # the safety backup holds the old world but not the uploaded archive
    safety = Backup.objects.get(pk=job.result["safety_backup"])
    assert safety.kind == BackupKind.PRE_RESTORE and "replacing the world" in safety.name
    snap = fake_agent.repos[str(server.id)][-1]["tree"]
    assert snap["files"]["world/region/r.0.0.mca"] == b"old"
    (_args, kwargs) = fake_agent.called("create_backup")[-1]
    assert any(e.startswith(".mcpanel-world-upload-") and e.endswith(".zip") for e in kwargs["excludes"])
    assert not temp_uploads(fake_agent, server)
    event = AuditEvent.objects.get(action="instance.world_replace")
    assert event.details["archive"] == "world.zip" and event.details["level_name"] == "world"
    assert event.details["pre_restore_backup"] is True
    server.refresh_from_db()
    assert server.active_job_id is None


def test_replace_running_server_stops_and_starts_it(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    resp = replace(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running="true")
    assert resp.status_code == 202
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is True
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert fake_agent.called("stop") and fake_agent.called("start")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "new"


def test_level_name_and_bukkit_dimensions(clients, make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance(name="Paper", server_type="PAPER")
    fake_agent.prepare(inst.id, {"server.properties": "level-name=survival\n"})
    put_world(fake_agent, inst, "survival")
    put_world(fake_agent, inst, "survival_nether", "old-nether")
    put_world(fake_agent, inst, "survival_the_end", "old-end")
    entries = {
        "server/world/level.dat": LEVEL_DAT,
        "server/world/region/r.0.0.mca": "new",
        "server/world_nether/level.dat": LEVEL_DAT,
        "server/world_nether/DIM-1/region/r.0.0.mca": "new-nether",
        "server/plugins/x.jar": "jar",
    }
    replace(clients["owner"], inst, django_capture_on_commit_callbacks, entries=entries)
    job = last_job(inst)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["level_name"] == "survival" and job.result["layout"] == "bukkit"
    assert fake_agent.text(inst.id, "survival/region/r.0.0.mca") == "new"
    assert fake_agent.text(inst.id, "survival_nether/DIM-1/region/r.0.0.mca") == "new-nether"
    assert fake_agent.get_file(inst.id, "survival_nether/region/r.0.0.mca") is None
    # the old end belonged to the old world: it is gone, Paper creates a new one
    assert fake_agent.get_file(inst.id, "survival_the_end/region/r.0.0.mca") is None
    assert sorted(job.result["replaced"]) == ["survival", "survival_nether", "survival_the_end"]
    assert fake_agent.get_file(inst.id, "plugins/x.jar") is None


def test_env_level_wins_over_server_properties(clients, server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.put_file(server.id, "server.properties", "level-name=survival\n")
    Instance.objects.filter(pk=server.pk).update(env={"LEVEL": "hardcore"})
    replace(clients["owner"], server, django_capture_on_commit_callbacks)
    assert last_job(server).status == JobStatus.SUCCEEDED
    assert fake_agent.text(server.id, "hardcore/region/r.0.0.mca") == "new"
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"


def test_vanilla_server_merges_bukkit_dimensions(clients, server, fake_agent, django_capture_on_commit_callbacks):
    entries = {"world/level.dat": LEVEL_DAT, "world_nether/DIM-1/region/a.mca": "n", "world_the_end/DIM1/region/a.mca": "e"}
    replace(clients["owner"], server, django_capture_on_commit_callbacks, entries=entries)
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert sorted(job.result["converted"]) == ["end", "nether"]
    assert fake_agent.text(server.id, "world/DIM-1/region/a.mca") == "n"
    assert "Bukkit-style" in "\n".join(job.log)


def test_failure_keeps_the_world_and_restarts_the_server(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    replace(
        clients["manager"],
        server,
        django_capture_on_commit_callbacks,
        entries={"readme.txt": "no world in here"},
        stop_if_running="true",
    )
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "no_level_dat"
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert "started again" in "\n".join(job.log)
    assert not temp_uploads(fake_agent, server)
    server.refresh_from_db()
    assert server.active_job_id is None
    # the safety backup was taken before the replace was attempted, so it exists
    assert Backup.objects.filter(instance=server, kind=BackupKind.PRE_RESTORE).count() == 1


def test_unknown_world_state_leaves_the_server_stopped(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failed_operations["world_replace"] = ("could not put the previous world back", "world_rollback_failed")
    replace(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running="true")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "world_rollback_failed"
    assert fake_agent.containers[str(server.id)]["running"] is False
    server.refresh_from_db()
    assert server.desired_state == "stopped"
    assert "left stopped" in "\n".join(job.log)
    assert not temp_uploads(fake_agent, server)


def test_backup_failure_aborts_before_touching_the_world(clients, server, fake_agent, django_capture_on_commit_callbacks):
    start_container(fake_agent, server)
    fake_agent.failed_operations["backup"] = ("restic: repository locked", "backup_failed")
    replace(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running="true")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "backup_failed"
    assert not fake_agent.called("world_replace")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.containers[str(server.id)]["running"] is True
    assert not temp_uploads(fake_agent, server)


def test_safety_backup_follows_the_panel_setting(clients, server, fake_agent, django_capture_on_commit_callbacks):
    SystemSetting.set("pre_restore_backup", False)
    replace(clients["owner"], server, django_capture_on_commit_callbacks)
    job = last_job(server)
    assert job.status == JobStatus.SUCCEEDED and job.result["safety_backup"] is None
    assert not fake_agent.called("create_backup")
    replace(clients["owner"], server, django_capture_on_commit_callbacks, pre_restore_backup="true")
    assert last_job(server).result["safety_backup"] is not None


def test_replace_from_archive_in_server_folder(clients, server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    fake_agent.put_file(server.id, "uploads/w.zip", zip_bytes(NEW_WORLD))
    resp = replace(clients["owner"], server, capture, data={"archive": "uploads/w.zip", "confirm": True})
    assert resp.status_code == 202
    assert last_job(server).status == JobStatus.SUCCEEDED
    assert fake_agent.get_file(server.id, "uploads/w.zip") is not None  # kept unless asked
    (_args, kwargs) = fake_agent.called("create_backup")[-1]
    assert not any("w.zip" in e for e in kwargs["excludes"])

    broken = zip_bytes({"notes.txt": "x"})
    fake_agent.put_file(server.id, "uploads/broken.zip", broken)
    replace(clients["owner"], server, capture, data={"archive": "uploads/broken.zip", "delete_archive": True})
    assert last_job(server).status == JobStatus.FAILED
    assert fake_agent.get_file(server.id, "uploads/broken.zip") == broken  # only removed after success

    replace(clients["owner"], server, capture, data={"archive": "uploads/w.zip", "delete_archive": True})
    assert last_job(server).status == JobStatus.SUCCEEDED
    assert fake_agent.get_file(server.id, "uploads/w.zip") is None


@pytest.mark.parametrize(
    ("archive", "server_type"),
    [("world/w.zip", "VANILLA"), ("world/sub/w.zip", "VANILLA"), ("world_nether/w.zip", "PAPER")],
)
def test_archive_inside_the_world_is_refused_before_stopping(
    clients, make_instance, fake_agent, django_capture_on_commit_callbacks, archive, server_type
):
    inst = make_instance(name="Inside", server_type=server_type)
    fake_agent.prepare(inst.id)
    put_world(fake_agent, inst)
    put_world(fake_agent, inst, "world_nether", "nether")
    fake_agent.put_file(inst.id, archive, zip_bytes(NEW_WORLD))
    start_container(fake_agent, inst)
    resp = replace(clients["owner"], inst, django_capture_on_commit_callbacks, data={"archive": archive, "stop_if_running": True})
    assert resp.status_code == 400 and err(resp) == "archive_in_world"
    assert not Job.objects.filter(kind="world_replace").exists()
    assert fake_agent.containers[str(inst.id)]["running"] is True
    assert fake_agent.get_file(inst.id, archive) is not None


def test_lost_connection_while_submitting_leaves_the_server_stopped(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    """The agent may have started the swap although the request timed out: do not start the server."""
    from apps.nodes.client import AgentUnavailable

    start_container(fake_agent, server)
    fake_agent.failures["world_replace"] = AgentUnavailable("The node agent did not respond in time")
    replace(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running="true")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "agent_unavailable"
    assert fake_agent.containers[str(server.id)]["running"] is False
    server.refresh_from_db()
    assert server.desired_state == "stopped"
    assert "left stopped" in "\n".join(job.log)


def test_start_failure_after_a_replace_says_the_world_was_replaced(
    clients, server, fake_agent, django_capture_on_commit_callbacks
):
    from apps.nodes.client import AgentError

    start_container(fake_agent, server)
    fake_agent.failures["start"] = AgentError("Docker could not start the container", "start_failed", 500)
    replace(clients["manager"], server, django_capture_on_commit_callbacks, stop_if_running="true")
    job = last_job(server)
    assert job.status == JobStatus.FAILED and job.error_code == "start_failed"
    assert job.error.startswith("The world was replaced, but the server could not be started again")
    assert fake_agent.text(server.id, "world/region/r.0.0.mca") == "new"
    assert not temp_uploads(fake_agent, server)


def test_world_replace_job_shows_restoring_status(server):
    from apps.instances.state import JOB_STATUS

    assert JOB_STATUS["world_replace"] == "restoring"


def test_upload_limit_applies_to_world_uploads(clients, server, settings):
    settings.MCPANEL_MAX_UPLOAD_BYTES = 1024
    resp = clients["owner"].post(
        f"/api/instances/{server.id}/world/replace",
        {"confirm": "true", "file": SimpleUploadedFile("w.zip", b"x" * 4096)},
        format="multipart",
    )
    assert resp.status_code == 413 and err(resp) == "file_too_large"


def test_unknown_instance_is_404(clients):
    assert clients["owner"].get(f"/api/instances/{uuid.uuid4()}/world").status_code == 404
