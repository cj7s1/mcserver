"""Application side of the single panel container: queued jobs are sent again when the job worker
is ready, the reconcile tick reports its age, /api/health names the processes, tick log lines can
be silenced, and the deployment files describe the three-container layout.

The supervisor itself is tested in test_supervise.py, the entrypoint in test_security_audit_http.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import weakref
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.jobs import tasks as job_tasks
from apps.jobs.models import Job, JobStatus
from apps.notifications import service as notify_service
from mcpanel import celery as celery_mod

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------------------
# queued jobs are sent again when the job worker becomes ready
# --------------------------------------------------------------------------------------
@pytest.fixture
def dispatched(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr("apps.jobs.tasks.run_job.delay", lambda job_id: sent.append(job_id))
    return sent


def _job(status: str = JobStatus.QUEUED, age: timedelta = timedelta(0), **extra) -> Job:
    job = Job.objects.create(kind="backup", title="t", status=status, exclusive=False, **extra)
    if age:
        Job.objects.filter(pk=job.pk).update(created_at=timezone.now() - age)
    return job


@pytest.mark.django_db
def test_queued_jobs_are_sent_again_oldest_first(dispatched):
    second = _job(age=timedelta(minutes=1))
    first = _job(age=timedelta(minutes=5))
    _job(JobStatus.RUNNING, started_at=timezone.now(), heartbeat_at=timezone.now())
    _job(JobStatus.SUCCEEDED)
    _job(JobStatus.FAILED)
    _job(JobStatus.CANCELLED)
    assert job_tasks.resend_queued_jobs() == 2
    assert dispatched == [str(first.id), str(second.id)]
    # nothing about the jobs changed: the message is all that was lost
    assert set(Job.objects.filter(status=JobStatus.QUEUED).values_list("pk", flat=True)) == {first.id, second.id}


@pytest.mark.django_db
def test_jobs_queued_for_hours_are_left_to_the_watchdog(dispatched):
    """After a long outage a stale job must not start out of the blue: the watchdog fails it as never started."""
    _job(age=job_tasks.STALE_QUEUED + timedelta(minutes=1))
    fresh = _job(age=job_tasks.STALE_QUEUED - timedelta(minutes=1))
    assert job_tasks.resend_queued_jobs() == 1 and dispatched == [str(fresh.id)]


@pytest.mark.django_db
def test_no_queued_jobs_sends_nothing(dispatched):
    assert job_tasks.resend_queued_jobs() == 0 and dispatched == []


def _consumer(*queues: str) -> SimpleNamespace:
    return SimpleNamespace(task_consumer=SimpleNamespace(queues=[SimpleNamespace(name=name) for name in queues]))


@pytest.mark.django_db
def test_only_a_worker_that_runs_jobs_sends_them_again(dispatched):
    job = _job()
    celery_mod._resend_queued_jobs(sender=_consumer("periodic"))  # the periodic worker never runs jobs
    assert dispatched == []
    celery_mod._resend_queued_jobs(sender=_consumer("celery", "periodic"))
    assert dispatched == [str(job.id)]
    celery_mod._resend_queued_jobs(sender=object())  # queues unknown: better twice than never
    assert dispatched == [str(job.id)] * 2


def test_the_hook_is_connected_to_worker_ready_and_can_never_stop_a_worker(monkeypatch, caplog):
    from celery import signals

    receivers = [ref() if isinstance(ref, weakref.ReferenceType) else ref for _key, ref in signals.worker_ready.receivers]
    assert celery_mod._resend_queued_jobs in receivers

    def broken() -> int:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(job_tasks, "resend_queued_jobs", broken)
    with caplog.at_level(logging.WARNING, logger="mcpanel.celery"):
        celery_mod._resend_queued_jobs(sender=_consumer("celery"))  # must not raise
    assert "Could not send the queued jobs again" in caplog.text and "database is gone" in caplog.text


@pytest.mark.django_db
def test_a_message_sent_twice_runs_the_job_once(monkeypatch):
    """The re-send may duplicate a message that was not lost after all; run_job only claims QUEUED jobs."""
    from apps.jobs import runner

    runs: list[str] = []
    monkeypatch.setitem(runner.HANDLERS, "zz_resend_probe", lambda job, ctx: runs.append(str(job.id)) or {})
    job = Job.objects.create(kind="zz_resend_probe", title="t", exclusive=False)
    job_tasks.resend_queued_jobs()  # eager Celery: runs it right here
    job_tasks.resend_queued_jobs()
    runner.run_job(str(job.id))
    job.refresh_from_db()
    assert runs == [str(job.id)] and job.status == JobStatus.SUCCEEDED


# --------------------------------------------------------------------------------------
# reconcile tick age (health check of the single panel container)
# --------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_reconcile_tick_records_when_it_started(monkeypatch):
    from apps.instances import tasks as instance_tasks

    assert celery_mod.tick_age() is None  # no tick yet (the cache is cleared for every test)
    seen: list[float | None] = []
    monkeypatch.setattr(instance_tasks, "reconcile_node", lambda node: seen.append(celery_mod.tick_age()) or {})
    from apps.nodes.models import Node

    Node.objects.create(name="tick-node", agent_url="http://agent.invalid:8700", is_default=True)
    instance_tasks.reconcile_all()
    # recorded at the start of the run (a run that hangs must look old), and readable afterwards
    assert seen and seen[0] is not None and seen[0] < 5
    age = celery_mod.tick_age()
    assert age is not None and 0 <= age < 5
    cache.set(celery_mod.TICK_KEY, time.time() - 400, 60)
    assert 399 < celery_mod.tick_age() < 405


@pytest.mark.django_db
def test_other_tasks_and_skipped_runs_do_not_count_as_a_tick():
    from apps.instances import tasks as instance_tasks
    from apps.jobs.tasks import watchdog

    watchdog()
    assert celery_mod.tick_age() is None
    # the previous reconcile run is still going: this one is skipped and must not look like progress
    cache.add(f"celery:single-flight:{celery_mod.TICK_TASK}", "someone-else", 60)
    assert instance_tasks.reconcile_all() is None
    assert celery_mod.tick_age() is None
    assert celery_mod.TICK_TASK in celery_mod.SINGLE_FLIGHT and celery_mod.TICK_TASK == instance_tasks.reconcile_all.name


def test_tick_age_survives_a_broken_cache(monkeypatch):
    def boom(*_args, **_kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(cache, "get", boom)
    monkeypatch.setattr(cache, "set", boom)
    assert celery_mod.tick_age() is None
    celery_mod._mark_tick()  # must not raise: the tick itself still has to run


# --------------------------------------------------------------------------------------
# /api/health
# --------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_health_reports_the_tick_age_and_keeps_its_meaning(monkeypatch):
    monkeypatch.delenv("MCPANEL_SUPERVISOR_STATUS", raising=False)
    body = Client().get("/api/health").json()
    assert body == {"status": "ok", "checks": {"database": "ok", "cache": "ok"}, "tick_age_seconds": None}
    cache.set(celery_mod.TICK_KEY, time.time() - 42, 60)
    response = Client().get("/api/health")
    assert response.status_code == 200 and 41.5 < response.json()["tick_age_seconds"] < 44
    assert "components" not in response.json()  # one role per container: Docker shows the roles


@pytest.mark.django_db
def test_health_names_the_processes_of_the_single_container(monkeypatch, tmp_path):
    status = tmp_path / "supervisor.json"
    children = {name: {"state": "running", "pid": 1} for name in ("redis", "web", "worker", "periodic")}
    children["beat"] = {"state": "backoff", "pid": None}
    status.write_text(json.dumps({"time": time.time(), "phase": "running", "children": children}))
    monkeypatch.setenv("MCPANEL_SUPERVISOR_STATUS", str(status))
    response = Client().get("/api/health")
    # a child that is down does not make the web process answer 503: it is alive and says who is not
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert response.json()["components"] == {
        "redis": "running",
        "web": "running",
        "worker": "running",
        "periodic": "running",
        "beat": "backoff",
    }
    assert "pid" not in json.dumps(response.json())
    status.write_text(json.dumps({"time": time.time() - 300, "phase": "running", "children": children}))
    assert Client().get("/api/health").json()["components"] == {"supervisor": "stale"}
    status.write_text("not json")
    assert Client().get("/api/health").json()["components"] == {"supervisor": "unknown"}
    monkeypatch.setenv("MCPANEL_SUPERVISOR_STATUS", str(tmp_path / "missing.json"))
    assert Client().get("/api/health").json()["components"] == {"supervisor": "unknown"}


# --------------------------------------------------------------------------------------
# tick log lines
# --------------------------------------------------------------------------------------
def _record(level: int, name: str | None) -> logging.LogRecord:
    record = logging.LogRecord("celery.app.trace", level, __file__, 1, "Task %(name)s succeeded", None, None)
    if name is not None:
        record.data = {"name": name, "id": "x"}
    return record


def test_quiet_ticks_drops_only_the_info_lines_of_the_beat_ticks():
    quiet = celery_mod._QuietTicks()
    for tick in celery_mod.SINGLE_FLIGHT:
        assert quiet.filter(_record(logging.INFO, tick)) is False
        assert quiet.filter(_record(logging.WARNING, tick)) is True  # failures and retries stay
        assert quiet.filter(_record(logging.ERROR, tick)) is True
    for kept in ("apps.jobs.runner.run_job", "apps.notifications.deliver", "apps.modpacks.tasks.check_update"):
        assert quiet.filter(_record(logging.INFO, kept)) is True
    assert quiet.filter(_record(logging.INFO, None)) is True  # lines that are not about a task


def test_quiet_ticks_is_only_installed_on_request():
    """web / worker / beat as separate containers log exactly as before; the supervisor asks for it."""
    for name in ("celery.worker.strategy", "celery.app.trace"):
        assert not any(isinstance(f, celery_mod._QuietTicks) for f in logging.getLogger(name).filters)
    source = (REPO / "backend" / "container" / "supervise.py").read_text()
    assert 'child_env["MCPANEL_QUIET_TICKS"] = "1"' in source


# --------------------------------------------------------------------------------------
# webhook guard and the Redis socket
# --------------------------------------------------------------------------------------
def test_internal_service_names_match_the_compose_file():
    compose = yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())
    assert set(notify_service.INTERNAL_SERVICE_NAMES) == set(compose["services"])
    assert {"worker", "worker-periodic", "beat"}.isdisjoint(notify_service.BLOCKED_HOSTNAMES)


@pytest.mark.django_db
def test_a_redis_socket_url_adds_no_endpoint_and_breaks_nothing(monkeypatch):
    monkeypatch.setattr(notify_service, "_internal_cache", None)
    monkeypatch.setattr(notify_service, "_getaddrinfo", lambda *a, **k: [])
    with override_settings(
        REDIS_URL="unix:///run/mcpanel/redis.sock?db=0",
        CELERY_BROKER_URL="redis+socket:///run/mcpanel/redis.sock",
        MCPANEL_AGENT_URL="http://10.9.8.7:8700",
    ):
        hosts, endpoints = notify_service._internal_targets()
    assert hosts == frozenset()
    ports = {port for _ip, port in endpoints}
    assert 8700 in ports and 6379 not in ports  # the agent (and the database) are pinned, Redis has no address
    assert notify_service._url_endpoint("unix:///run/mcpanel/redis.sock?db=0", 6379) is None
    assert notify_service._url_endpoint("redis+socket:///run/mcpanel/redis.sock", 6379) is None


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:6379/", "http://0.0.0.0:6379/", "http://[::1]:6379/", "http://localhost:6379/"]
)
def test_webhooks_to_the_panel_containers_own_loopback_are_refused(url):
    with pytest.raises(ValueError, match="not allowed"):
        notify_service.validate_url("webhook", url)


# --------------------------------------------------------------------------------------
# deployment files
# --------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())


def test_the_stack_is_three_containers(compose):
    services = compose["services"]
    default = {name for name, service in services.items() if not service.get("profiles")}
    assert default == {"postgres", "agent", "panel"}
    assert {"worker", "worker-periodic", "beat"}.isdisjoint(services)
    # Redis as its own container exists only behind a profile (the way back from Redis in the panel container)
    assert services["redis"]["profiles"] == ["external-redis"]
    assert "redis" not in services["panel"].get("depends_on", {})


def test_the_panel_service_runs_everything_and_gets_two_minutes_to_stop(compose):
    panel = compose["services"]["panel"]
    assert panel["command"] == ["all"]
    assert panel["stop_grace_period"] == "2m"
    assert panel["init"] is True and panel["cap_drop"] == ["ALL"] and panel["security_opt"] == ["no-new-privileges:true"]
    assert panel["healthcheck"]["test"] == ["CMD", "python", "/usr/local/bin/mcpanel-healthcheck.py", "all"]
    assert any(str(mount).startswith("/run/mcpanel:") and "mode=0700" in str(mount) for mount in panel["tmpfs"])
    assert panel["logging"]["options"] == {"max-size": "20m", "max-file": "5"}
    assert "/var/run/docker.sock" not in json.dumps(panel)  # still no Docker socket in the panel
    assert "volumes" not in panel
    # the supervisor's deadline is below the grace period Docker gives the container
    source = (REPO / "backend" / "container" / "supervise.py").read_text()
    assert float(re.search(r"^STOP_DEADLINE = ([0-9.]+)", source, re.M).group(1)) < 120


def test_redis_is_a_socket_inside_the_panel_container_by_default(compose):
    env = compose["x-panel-env"]
    assert env["MCPANEL_REDIS_URL"] == "${MCPANEL_REDIS_URL:-unix:///run/mcpanel/redis.sock?db=0}"
    # The broker URL is not built from MCPANEL_REDIS_URL here: a unix:// URL written into .env
    # would reach Celery as it is ("No such transport: unix"). Empty means: the panel derives it.
    assert env["MCPANEL_CELERY_BROKER_URL"] == "${MCPANEL_CELERY_BROKER_URL:-}"
    assert compose["services"]["panel"]["environment"] == env
    example = (REPO / "deploy" / ".env.example").read_text()
    assert "\n#COMPOSE_PROFILES=external-redis\n" in example and "\n#MCPANEL_REDIS_URL=redis://redis:6379/0\n" in example


@pytest.mark.parametrize(
    ("redis_url", "broker_url", "expected"),
    [
        # what the Compose file passes by default, and the same line written into .env by hand
        ("unix:///run/mcpanel/redis.sock?db=0", "", "redis+socket:///run/mcpanel/redis.sock"),
        ("unix:///run/mcpanel/redis.sock", "", "redis+socket:///run/mcpanel/redis.sock"),
        ("unix:///srv/redis.sock?db=3", "", "redis+socket:///srv/redis.sock?virtual_host=3"),
        ("unix://:s3cret@/srv/redis.sock?db=1", "", "redis+socket://:s3cret@/srv/redis.sock?virtual_host=1"),
        # the external-redis profile: one variable in .env
        ("redis://redis:6379/0", "", "redis://redis:6379/0"),
        # a broker URL of its own wins, and a unix:// one is put right, too
        ("redis://redis:6379/0", "rediss://broker.example:6380/2", "rediss://broker.example:6380/2"),
        ("redis://redis:6379/0", "unix:///run/other.sock?db=2", "redis+socket:///run/other.sock?virtual_host=2"),
    ],
)
def test_the_broker_url_is_derived_from_the_redis_url_in_a_form_celery_can_use(redis_url, broker_url, expected):
    """The settings as a fresh process reads them from the environment the Compose file passes."""
    env = {key: value for key, value in os.environ.items() if key not in ("MCPANEL_REDIS_URL", "MCPANEL_CELERY_BROKER_URL")}
    env.update(MCPANEL_REDIS_URL=redis_url, MCPANEL_CELERY_BROKER_URL=broker_url, DJANGO_SETTINGS_MODULE="mcpanel.settings")
    code = (
        "from kombu import Connection\n"
        "import mcpanel.settings as s\n"
        "connection = Connection(s.CELERY_BROKER_URL)\n"
        "connection.transport  # raises for a transport kombu does not have\n"
        "print(s.CELERY_BROKER_URL, connection.transport_cls, s.REDIS_URL)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=REPO / "backend", capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == [expected, expected.split(":")[0].split("+")[0], redis_url]


def test_the_agent_image_is_pinned_to_the_same_debian_release():
    """It installs btrfs-progs and zfsutils-linux from Debian: a base image that moves to the next
    release by itself would change them under a rebuild (`upgrade.sh run` pulls base images)."""
    dockerfile = (REPO / "agent" / "Dockerfile").read_text()
    assert re.findall(r"^FROM (python:\S+)", dockerfile, re.M) == ["python:3.13-slim-trixie", "python:3.13-slim-trixie"]


def test_locally_built_images_are_never_pulled(compose):
    """`docker compose pull` must skip them: a foreign image of that name would get every secret (panel)
    or the Docker socket (agent). Image-only services with a local name made the documented pull fail."""
    for name, service in compose["services"].items():
        if str(service.get("image", "")).startswith("mcpanel/"):
            assert "build" in service and service.get("pull_policy") == "never", name
        else:
            assert "build" not in service, name


def test_base_images_are_pinned_to_a_debian_release_and_redis_to_its_line():
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    python_bases = re.findall(r"^FROM (python:\S+)", dockerfile, re.M)
    assert python_bases == ["python:3.13-slim-trixie", "python:3.13-slim-trixie"]
    assert re.search(r"^FROM redis:7\.4 AS redis$", dockerfile, re.M)
    assert "COPY --from=redis /usr/local/bin/redis-server" in dockerfile
    assert "redis-server --version | grep -Eq 'v=7\\.4\\.[0-9]+ '" in dockerfile  # the build asserts the version
    assert "install -d -o 10001 -g 10001 -m 0700 /run/mcpanel" in dockerfile
    assert "apt-get install" not in dockerfile  # in particular not Debian's Redis 8


def test_docs_describe_the_three_container_layout():
    for path in ("DEPLOYMENT.md", "docs/UNRAID.md", "docs/QUICKSTART-UNRAID.md", "README.md", "ARCHITECTURE.md"):
        text = (REPO / path).read_text()
        # the old services are only named where the way from the old layout is described
        assert "| `worker-periodic`" not in text and "| `beat`" not in text, path
        assert "worker-periodic" not in text or path == "DEPLOYMENT.md", path
        assert "upgrade.sh" in text or path in ("README.md", "ARCHITECTURE.md"), path
    deployment = (REPO / "DEPLOYMENT.md").read_text()
    assert "Coming from the 7-container layout" in deployment
    for needle in ("upgrade.sh status", "upgrade.sh dry-run", "upgrade.sh run", "upgrade.sh rollback", "external-redis"):
        assert needle in deployment, needle


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_upgrade_script_is_runnable_and_needs_only_bash_docker_and_git():
    """The real run (old layout -> run -> rollback -> run, with containers) is .github/scripts/upgrade-rehearsal.sh."""
    script = REPO / "deploy" / "scripts" / "upgrade.sh"
    assert os.access(script, os.X_OK)
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True, timeout=30).returncode == 0
    out = subprocess.run(["bash", str(script), "help"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "{status|dry-run|run|rollback}" in out.stdout
    for args in ([], ["frobnicate"]):
        assert subprocess.run(["bash", str(script), *args], capture_output=True, timeout=30).returncode == 64
    assert subprocess.run(["bash", str(script), "run", "--timeout", "soon"], capture_output=True, timeout=30).returncode == 1
    text = script.read_text()
    # `rollback` replaces this very file (git checkout) while it runs: bash must have read all of it before
    assert text.rstrip().endswith('main "$@"; exit $?')
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for tool in ("jq", "yq", "python3", "awk", "curl", "flock", "sudo"):  # (python only inside the panel container)
        assert not re.search(rf"(^|[\s|;(]){tool}\s", code), tool
    for needle in ("up -d --remove-orphans", "build --pull", "pull --ignore-buildable", "--exclude-table-data=django_session"):
        assert needle in code, needle
    # its files are private and already ignored by git (deploy/*.env, deploy/*.sql)
    assert 'STATE_FILE="$DEPLOY_DIR/upgrade-state.env"' in text and "umask 077" in text
    ignore = (REPO / ".gitignore").read_text()
    assert "deploy/*.env" in ignore and "deploy/*.sql" in ignore
