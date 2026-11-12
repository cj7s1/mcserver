"""B2 of the feature plan of 2026-11-12: stop when empty (``Instance.idle_stop_minutes``, apps/instances/idle.py)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.instances import idle
from apps.instances.models import Instance
from apps.instances.spec import build_spec
from apps.jobs.models import Job, JobStatus
from apps.metrics.models import MetricSample
from apps.notifications.models import NotificationChannel

from .test_games_six import create

pytestmark = pytest.mark.django_db


def _running(make_instance: Any, fake_agent: Any, *, minutes: int = 30, ready_minutes_ago: int = 120, **fields: Any) -> Instance:
    inst = make_instance(**fields)
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    now = timezone.now()
    Instance.objects.filter(pk=inst.pk).update(
        status="running",
        desired_state="running",
        idle_stop_minutes=minutes,
        started_at=now - timedelta(minutes=ready_minutes_ago + 1),
        ready_at=now - timedelta(minutes=ready_minutes_ago),
        installed_at=now - timedelta(days=1),
    )
    inst.refresh_from_db()
    return inst


def _samples(inst: Instance, counts: list[int | None], *, every: int = 60, newest_age: int = 20) -> None:
    """``counts`` oldest first, one sample every ``every`` seconds, the newest ``newest_age`` seconds ago."""
    now = timezone.now()
    for i, count in enumerate(counts):
        age = newest_age + (len(counts) - 1 - i) * every
        MetricSample.objects.create(instance=inst, taken_at=now - timedelta(seconds=age), players_online=count, players_max=20)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    monkeypatch.setattr("apps.notifications.service.deliver.delay", lambda *a: out.append(a))
    NotificationChannel.objects.create(
        name="ops", kind="webhook", url="https://hooks.example/x", events=["instance.idle_stopped"]
    )
    return out


def test_an_empty_server_is_stopped_through_the_stop_job(
    make_instance: Any, fake_agent: Any, sent: list[Any], django_capture_on_commit_callbacks: Any
) -> None:
    inst = _running(make_instance, fake_agent, minutes=30)
    _samples(inst, [0] * 33)
    with django_capture_on_commit_callbacks(execute=True):
        assert idle.check_idle() == 1
    job = Job.objects.get(instance=inst, kind="stop")
    assert job.status == JobStatus.SUCCEEDED and "empty for 30 minutes" in job.title
    assert not fake_agent.containers[str(inst.id)]["running"]
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"
    event = AuditEvent.objects.get(action="instance.idle_stopped")
    assert event.details["reason"] == "stopped: empty for 30 minutes" and event.details["minutes"] == 30
    assert sent and sent[-1][1] == "instance.idle_stopped" and "30 minutes" in sent[-1][3]
    assert fake_agent.stop_requests[-1]["not_ready"] is False  # the ordinary stop: the world is saved


@pytest.mark.parametrize(
    "case",
    ["a player", "count unknown", "gap", "stale", "too short", "recently ready", "job running", "setting off", "stopped"],
)
def test_never_when_in_doubt(case: str, make_instance: Any, fake_agent: Any, sent: list[Any]) -> None:
    inst = _running(make_instance, fake_agent, minutes=30, ready_minutes_ago=10 if case == "recently ready" else 120)
    counts: list[int | None] = [0] * 33
    if case == "a player":
        counts[20] = 1
    if case == "count unknown":
        counts[10] = None
    if case == "gap":
        counts = [0] * 10 + [0] * 20  # with a 5-minute hole between them (below)
    if case == "too short":
        counts = [0] * 12
    if case == "gap":
        _samples(inst, counts[:20], newest_age=20)
        _samples(inst, counts[20:], newest_age=20 + 20 * 60 + 300)
    else:
        _samples(inst, counts, newest_age=400 if case == "stale" else 20)
    if case == "job running":
        job = Job.objects.create(kind="backup", title="b", instance=inst, status=JobStatus.RUNNING, exclusive=True)
        Instance.objects.filter(pk=inst.pk).update(active_job=job)
    if case == "setting off":
        Instance.objects.filter(pk=inst.pk).update(idle_stop_minutes=0)
    if case == "stopped":
        Instance.objects.filter(pk=inst.pk).update(desired_state="stopped")
    assert idle.check_idle() == 0
    assert not Job.objects.filter(instance=inst, kind="stop").exists() and sent == []


def test_one_bad_server_does_not_keep_the_others(make_instance: Any, fake_agent: Any, monkeypatch: Any) -> None:
    a = _running(make_instance, fake_agent, name="A")
    b = _running(make_instance, fake_agent, name="B")
    _samples(a, [0] * 33)
    _samples(b, [0] * 33)
    real = idle.due

    def due(instance: Any, now: Any) -> bool:
        if instance.pk == a.pk:
            raise RuntimeError("boom")
        return real(instance, now)

    monkeypatch.setattr(idle, "due", due)
    assert idle.check_idle() == 1
    assert Job.objects.filter(instance=b, kind="stop").exists()


def test_the_setting_is_offered_only_where_the_count_is_readable(
    clients: Any, make_instance: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    mc = make_instance()
    owner = clients["owner"]
    detail = owner.get(f"/api/instances/{mc.id}").json()
    assert detail["idle_stop_minutes"] == 0 and detail["idle_stop_unavailable"] is None
    assert owner.patch(f"/api/instances/{mc.id}", {"idle_stop_minutes": 30}, format="json").status_code == 200
    mc.refresh_from_db()
    assert mc.idle_stop_minutes == 30
    bad = owner.patch(f"/api/instances/{mc.id}", {"idle_stop_minutes": 2}, format="json")
    assert bad.status_code == 400 and "idle_stop_minutes" in bad.json()["error"]["details"]["fields"]
    vh = create(clients, django_capture_on_commit_callbacks, "valheim")
    detail = owner.get(f"/api/instances/{vh.id}").json()
    assert "Valheim has no way" in detail["idle_stop_unavailable"]
    refused = owner.patch(f"/api/instances/{vh.id}", {"idle_stop_minutes": 30}, format="json")
    assert refused.status_code == 400 and "Not available" in str(
        refused.json()["error"]["details"]["fields"]["idle_stop_minutes"]
    )
    assert owner.patch(f"/api/instances/{vh.id}", {"idle_stop_minutes": 0}, format="json").status_code == 200
    # the readable games
    for game in ("palworld", "project-zomboid", "hkmp", "cs2", "rust"):
        inst = create(clients, django_capture_on_commit_callbacks, game)
        assert idle.unavailable_reason(inst) is None, game


def test_the_tick_is_scheduled(settings: Any) -> None:
    entry = settings.CELERY_BEAT_SCHEDULE["idle-stop"]
    assert entry["task"] == "apps.instances.tasks.idle_stop_tick" and entry["options"]["expires"] < entry["schedule"]
