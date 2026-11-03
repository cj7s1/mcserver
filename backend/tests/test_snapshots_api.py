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
