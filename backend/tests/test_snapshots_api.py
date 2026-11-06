import pytest

from apps.jobs import runner
from apps.jobs.models import Job, JobStatus


@pytest.fixture
def server(make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance(name="Snap")
    fake_agent.prepare(inst.id, {"world/level.dat": "v1"})
    with django_capture_on_commit_callbacks(execute=True):
        runner.enqueue("install", title="install", instance=inst, params={"start": True})
    return inst


def _post(c, capture, url, body=None):
    with capture(execute=True):
        return c.post(url, body or {}, format="json")


def test_snapshot_create_rollback_delete(server, fake_agent, client_for, users, django_capture_on_commit_callbacks):
    c = client_for(users["owner"])
    cap = django_capture_on_commit_callbacks
    assert c.get(f"/api/instances/{server.id}/snapshots").json()["capability"]["available"] is True
    r = _post(c, cap, f"/api/instances/{server.id}/snapshots", {"label": "before change"})
    assert Job.objects.get(pk=r.json()["id"]).status == JobStatus.SUCCEEDED
    name = c.get(f"/api/instances/{server.id}/snapshots").json()["snapshots"][0]["name"]
    fake_agent.put_file(server.id, "world/level.dat", "v2-broken")
    r = _post(c, cap, f"/api/instances/{server.id}/snapshots/{name}")
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirmation_required"
    r = _post(c, cap, f"/api/instances/{server.id}/snapshots/{name}", {"confirm": True})
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.text(server.id, "world/level.dat") == "v1"
    assert job.result["restarted"] is True and fake_agent.containers[str(server.id)]["running"]
    kinds = [s["kind"] for s in c.get(f"/api/instances/{server.id}/snapshots").json()["snapshots"]]
    assert "pre_restore" in kinds  # automatic safety snapshot before rollback
    with cap(execute=True):
        r = c.delete(f"/api/instances/{server.id}/snapshots/{name}")
    assert Job.objects.get(pk=r.json()["id"]).status == JobStatus.SUCCEEDED


def test_snapshot_permissions_and_name_validation(server, client_for, users):
    viewer = client_for(users["viewer"])
    assert viewer.post(f"/api/instances/{server.id}/snapshots", {}, format="json").status_code == 403
    owner = client_for(users["owner"])
    r = owner.post(f"/api/instances/{server.id}/snapshots/not-a-snapshot", {"confirm": True}, format="json")
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_snapshot"


def test_zfs_provider_is_passed_through(server, fake_agent, client_for, users, django_capture_on_commit_callbacks):
    """The panel is provider-agnostic: a ZFS node works through the same endpoints and jobs."""
    fake_agent.snapshot_provider = "zfs"
    c = client_for(users["owner"])
    cap = django_capture_on_commit_callbacks
    capability = c.get(f"/api/instances/{server.id}/snapshots").json()["capability"]
    assert capability["provider"] == "zfs" and capability["instance_ready"] is True
    r = _post(c, cap, f"/api/instances/{server.id}/snapshots/convert")
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("ZFS dataset" in line for line in job.log) and not any("btrfs" in line for line in job.log)
    r = _post(c, cap, f"/api/instances/{server.id}/snapshots", {"label": "z"})
    assert Job.objects.get(pk=r.json()["id"]).status == JobStatus.SUCCEEDED
    name = c.get(f"/api/instances/{server.id}/snapshots").json()["snapshots"][0]["name"]
    # ZFS refuses to delete a snapshot that history datasets depend on; the reason reaches the user
    fake_agent.snapshot_delete_error = fake_agent._err(
        f"Snapshot {name} cannot be deleted yet: the server state from before a rollback is based on it",
        "snapshot_has_dependents",
        409,
    )
    with cap(execute=True):
        r = c.delete(f"/api/instances/{server.id}/snapshots/{name}")
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.FAILED and "before a rollback" in job.error
    assert job.error_code == "snapshot_has_dependents"


def _run(capture, job_kind, server, **params):
    with capture(execute=True):
        job = runner.enqueue(job_kind, title=job_kind, instance=server, params=params)
    job.refresh_from_db()
    return job


def test_scheduled_retention_keeps_snapshots_with_dependents(server, fake_agent, django_capture_on_commit_callbacks):
    """ZFS refuses to delete a snapshot that history datasets depend on: retention skips it."""
    cap = django_capture_on_commit_callbacks
    label = "scheduled hourly"  # retention is per schedule: the scheduler labels its snapshots
    for _ in range(4):
        job = _run(cap, "snapshot_create", server, label=label, kind="scheduled", keep=4)
        assert job.status == JobStatus.SUCCEEDED
    names = [s["name"] for s in fake_agent.snapshots(server.id)["snapshots"]]  # newest first
    assert len(names) == 4
    fake_agent.snapshot_dependents = {names[2]}
    job = _run(cap, "snapshot_create", server, label=label, kind="scheduled", keep=1)
    assert job.status == JobStatus.SUCCEEDED, job.error
    left = [s["name"] for s in fake_agent.snapshots(server.id)["snapshots"]]
    assert names[2] in left and len(left) == 2  # the new one + the one with dependents
    assert any("kept snapshot" in line and names[2] in line for line in job.log)
    # other delete errors are logged too (retention is best effort: the new snapshot exists)
    fake_agent.snapshot_delete_error = fake_agent._err("btrfs failed: boom", "btrfs_failed", 500)
    job = _run(cap, "snapshot_create", server, label=label, kind="scheduled", keep=1)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("could not delete snapshot" in line and "boom" in line for line in job.log)
    assert len(fake_agent.snapshots(server.id)["snapshots"]) == 3


def test_delete_server_logs_agent_warnings(server, fake_agent, django_capture_on_commit_callbacks):
    fake_agent.delete_data_warnings = ["Could not delete snapshot 20261101T000000Z: btrfs failed: busy"]
    job = _run(django_capture_on_commit_callbacks, "delete", server)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("Could not delete snapshot" in line for line in job.log)
