import pytest
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus
from apps.jobs.runner import run_job
from apps.nodes.models import Node

pytestmark = pytest.mark.django_db


@pytest.fixture
def instance():
    node = Node.objects.create(name="n-rec", agent_url="http://agent.invalid:8700", is_default=True)
    return Instance.objects.create(name="R", node=node, game_port=25612, image="itzg/minecraft-server:stable-java21")


def test_unknown_job_kind_fails_and_releases_lock(instance, monkeypatch):
    monkeypatch.setattr("apps.instances.reconcile.reconcile_instance", lambda *_a, **_k: None)
    job = Job.objects.create(kind="does_not_exist", title="x", instance=instance, exclusive=True)
    Instance.objects.filter(pk=instance.pk).update(active_job=job)
    run_job(str(job.id))
    job.refresh_from_db()
    instance.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.error_code == "unknown_job_kind"
    assert instance.active_job_id is None


def test_abandon_stuck_job(instance):
    owner = User.objects.create_user("own", password="x" * 12, is_owner=True)
    viewer = User.objects.create_user("view", password="x" * 12)
    job = Job.objects.create(kind="backup", title="stuck", instance=instance, status=JobStatus.RUNNING, exclusive=True)
    Instance.objects.filter(pk=instance.pk).update(active_job=job)
    c = APIClient()
    c.force_login(viewer)
    assert c.post(f"/api/jobs/{job.id}/abandon").status_code in (403, 404)
    c.force_login(owner)
    r = c.post(f"/api/jobs/{job.id}/abandon")
    assert r.status_code == 200 and r.json()["error_code"] == "abandoned"
    instance.refresh_from_db()
    assert instance.active_job_id is None
    assert c.post(f"/api/jobs/{job.id}/abandon").status_code == 409
