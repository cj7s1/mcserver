"""No container for a stopped server: the retire step of the reconciler and everything that has
to keep working for a server without a container (against the fake agent).

The rule (``reconcile.retire_blocker``): the container of a server is retired when it exists and
is not running, has been like that for the settle time, and the server is meant to be stopped,
not archived and not held by a job. The agent saves the record of the last run before it removes
the container; the tests of that half are in agent/tests/test_lastrun.py.
"""

from __future__ import annotations

import gzip
import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupStatus
from apps.instances import reconcile, scheduling, services
from apps.instances.diagnostics import diagnose
from apps.instances.models import Instance, Schedule, Status
from apps.instances.reconcile import AUTO_STOP_PREFIX, maybe_retire, reconcile_instance, reconcile_node, retire_blocker
from apps.instances.spec import build_spec, current_spec_hash, restart_required
from apps.instances.tasks import reconcile_all
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient, AgentError

pytestmark = pytest.mark.django_db

RETIRED = "instance.container_retired"
RETIRE_FAILED = "instance.container_retire_failed"
LEVEL_DAT = gzip.compress(b"\x0a\x00\x00\x00")


# --- helpers ------------------------------------------------------------------------------------
def run(kind, instance, capture, **params) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=instance, params=params)
    job.refresh_from_db()
    return job


@pytest.fixture
def server(make_instance, fake_agent):
    """A server that is installed and running, the way the Start job leaves it."""

    def _make(name: str = "Survival", **fields):
        fields.setdefault("desired_state", "running")
        inst = make_instance(name=name, **fields)
        fake_agent.prepare(inst.id, {"server.properties": "motd=hello\n"})
        fake_agent.put_file(inst.id, "world/level.dat", LEVEL_DAT)
        fake_agent.put_file(inst.id, "world/region/r.0.0.mca", "old")
        fake_agent.apply(inst.id, build_spec(inst), start=True)
        Instance.objects.filter(pk=inst.pk).update(
            container_spec_hash=current_spec_hash(inst), installed_at=timezone.now(), applied_revision=1, status="running"
        )
        inst.refresh_from_db()
        return inst

    return _make


def sweep(fake_agent) -> None:
    """A reconcile pass some time later: every container has been in its state for a while."""
    fake_agent.age_containers()
    reconcile_all()


def has_container(fake_agent, inst) -> bool:
    return str(inst.id) in fake_agent.containers


def stopped_and_retired(fake_agent, inst, capture, lines=("[12:00:00] [Server thread/INFO]: Stopping server",)):
    """Stop through the Stop job, then let the reconciler retire the container."""
    fake_agent.log_lines[str(inst.id)] = list(lines)
    assert run("stop", inst, capture).status == JobStatus.SUCCEEDED
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    inst.refresh_from_db()
    return inst


# --- the rule -----------------------------------------------------------------------------------
def test_stop_keeps_the_container_until_the_settle_time_then_it_is_retired(
    server, fake_agent, django_capture_on_commit_callbacks
):
    inst = server()
    fake_agent.stop_exit_code = 0
    fake_agent.log_lines[str(inst.id)] = [
        "[12:00:00] [Server thread/INFO]: Saving worlds",
        "[12:00:01] ThreadedAnvilChunkStorage: All dimensions are saved",
    ]
    container_id = fake_agent.containers[str(inst.id)]["id"]
    assert run("stop", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    # the job's own reconcile and a pass right after it leave the container alone: it stopped a moment ago
    reconcile_all()
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail, inst.container_state) == (Status.STOPPED, "", "exited")

    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    record = fake_agent.last_runs[str(inst.id)]
    assert record["container_id"] == container_id and record["exit_code"] == 0
    assert record["log"]["lines"][-1].endswith("All dimensions are saved")
    inst.refresh_from_db()
    # the status reads like that of a stopped server, not "no container"
    assert (inst.status, inst.status_detail, inst.container_state) == (Status.STOPPED, "", "")
    assert inst.last_exit_code == 0 and inst.oom_killed is False
    event = AuditEvent.objects.get(action=RETIRED)
    assert event.details == {"recorded": True, "exit_code": 0, "console_lines": 2} and event.instance_id == inst.id
    # later passes find nothing to do and the values stay
    reconcile_all()
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail, inst.last_exit_code) == (Status.STOPPED, "", 0)
    assert AuditEvent.objects.filter(action=RETIRED).count() == 1


def test_retired_exit_code_and_out_of_memory_flag_survive(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    fake_agent.stop_exit_code = 137
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.set_container_state(inst.id, oom_killed=True)
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    for _ in range(2):  # from the retire answer, then from the list of the next pass
        inst.refresh_from_db()
        assert inst.last_exit_code == 137 and inst.oom_killed is True
        assert (inst.status, inst.status_detail) == (Status.FAILED, "Killed: out of memory. Increase the memory allocation.")
        reconcile_all()
    reconcile_instance(inst.id)  # and from the status of a single server
    inst.refresh_from_db()
    assert inst.last_exit_code == 137 and inst.oom_killed is True and inst.status == Status.FAILED


def test_a_server_that_died_while_meant_to_run_keeps_its_container(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    fake_agent.set_container_state(inst.id, status="exited", running=False, exit_code=1, finished_at=fake_agent._now())
    sweep(fake_agent)
    sweep(fake_agent)
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    inst.refresh_from_db()
    assert inst.status == Status.FAILED and "exit code 1" in inst.status_detail
    assert retire_blocker(inst, fake_agent.status(inst.id)) == "meant_to_run"

    # Stop dismisses it: the server is then meant to be stopped, and the next pass retires the container
    assert run("stop", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    inst.refresh_from_db()
    assert inst.status == Status.STOPPED and inst.last_exit_code == 1


@pytest.mark.parametrize(
    "state",
    [
        {"status": "running", "running": True},
        {"status": "restarting", "running": False, "restarting": True},
        {"status": "paused", "running": True},
        {"status": "removing", "running": False},
    ],
)
def test_only_a_container_that_is_not_running_is_retired(server, fake_agent, state):
    inst = server(desired_state="stopped")  # runs although the panel recorded "stopped"
    fake_agent.set_container_state(inst.id, **state)
    sweep(fake_agent)
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    assert retire_blocker(inst, fake_agent.status(inst.id)) == "running"


def test_a_server_held_by_a_job_keeps_its_container(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    job = Job.objects.create(kind="restore", title="Restore", instance=inst, status=JobStatus.RUNNING)
    Instance.objects.filter(pk=inst.pk).update(active_job=job)
    sweep(fake_agent)
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    # also a job that is only queued, and one that does not hold the lock (a countdown)
    Instance.objects.filter(pk=inst.pk).update(active_job=None)
    Job.objects.filter(pk=job.pk).update(status=JobStatus.QUEUED, exclusive=False)
    sweep(fake_agent)
    assert has_container(fake_agent, inst)
    assert retire_blocker(inst, fake_agent.status(inst.id)) == "job_active"
    Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED)
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)


def test_the_rule_reads_the_server_again_before_it_decides(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.age_containers()
    stale = Instance.objects.select_related("node").get(pk=inst.pk)  # loaded before a Start was queued
    container = fake_agent.status(inst.id)
    Instance.objects.filter(pk=inst.pk).update(desired_state="running")
    assert retire_blocker(stale, container) == "meant_to_run"
    assert maybe_retire(stale, container) is False and has_container(fake_agent, inst)


def test_an_archived_server_is_left_alone(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    Instance.objects.filter(pk=inst.pk).update(archived_at=timezone.now())
    inst.refresh_from_db()
    fake_agent.age_containers()
    assert retire_blocker(inst, fake_agent.status(inst.id)) == "archived"
    reconcile_all()
    assert not fake_agent.called("retire")


def test_setting_off_keeps_the_containers_of_stopped_servers(server, fake_agent, settings, django_capture_on_commit_callbacks):
    settings.MCPANEL_RETIRE_STOPPED_CONTAINERS = False
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    sweep(fake_agent)
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    inst.refresh_from_db()
    assert (inst.status, inst.container_state) == (Status.STOPPED, "exited")
    # start and stop work as they always did: the same container is started again
    container_id = fake_agent.containers[str(inst.id)]["id"]
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    assert fake_agent.containers[str(inst.id)]["id"] == container_id and not fake_agent.called("apply")[1:]


def test_an_older_agent_keeps_todays_behaviour(server, fake_agent, clients, django_capture_on_commit_callbacks):
    fake_agent.features = []  # GET /v1/info names no "retire"
    inst = server()
    fake_agent.log_lines[str(inst.id)] = ["line one"]
    run("stop", inst, django_capture_on_commit_callbacks)
    sweep(fake_agent)
    sweep(fake_agent)
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    assert len(fake_agent.called("info")) == 1  # asked once, then remembered
    assert clients["owner"].get(f"/api/instances/{inst.id}/logs").json() == {"lines": ["line one"]}
    container_id = fake_agent.containers[str(inst.id)]["id"]
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    assert fake_agent.containers[str(inst.id)]["id"] == container_id


def test_agent_support_is_asked_once_per_node_and_noticed_after_an_update(
    server, fake_agent, django_capture_on_commit_callbacks, monkeypatch
):
    from django.core.cache import cache

    fake_agent.features = []
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    sweep(fake_agent)
    assert has_container(fake_agent, inst)
    fake_agent.features = ["retire"]  # the agent was updated
    sweep(fake_agent)
    assert has_container(fake_agent, inst)  # "no" is remembered for a minute
    cache.delete(f"agent-features:{inst.node.pk}:{AgentClient(inst.node).base}")
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)


def test_an_unreachable_agent_is_not_remembered_as_unsupported(server, fake_agent):
    inst = server()
    client = AgentClient(inst.node)
    fake_agent.failures["info"] = AgentError("boom", "docker_unavailable", 503)
    assert client.supports("retire") is False
    del fake_agent.failures["info"]
    assert client.supports("retire") is True


def test_a_server_created_without_starting_stays_not_installed(
    make_instance, fake_agent, clients, django_capture_on_commit_callbacks
):
    inst = make_instance(name="Fresh")
    assert run("install", inst, django_capture_on_commit_callbacks, start=False).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    assert inst.status == Status.CREATED and has_container(fake_agent, inst) and inst.applied_revision == 1
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    assert str(inst.id) not in fake_agent.last_runs  # it never ran: nothing to record
    assert AuditEvent.objects.get(action=RETIRED).details["recorded"] is False
    reconcile_all()
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail) == (Status.CREATED, "Not installed yet")
    body = clients["owner"].get(f"/api/instances/{inst.id}").json()
    assert body["status"] == "created" and body["installed_at"] is None
    # the first start installs it
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    assert inst.status == Status.RUNNING and inst.installed_at is not None


def test_a_container_that_never_ran_does_not_replace_the_record_of_the_last_run(
    server, fake_agent, mojang_java, django_capture_on_commit_callbacks
):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks, lines=["the last real run"])
    mojang_java("1.20.6", 21)
    # an update of the stopped server creates a container (nothing starts it), which is retired again
    job = run(
        "software_update",
        inst,
        django_capture_on_commit_callbacks,
        minecraft_version="1.20.6",
        loader_version="",
        java_version=21,
        backup=False,
    )
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["status"] == "created"
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    assert fake_agent.last_runs[str(inst.id)]["log"]["lines"] == ["the last real run"]
    inst.refresh_from_db()
    assert inst.status == Status.STOPPED and inst.minecraft_version == "1.20.6"


def test_a_record_that_cannot_be_written_keeps_the_container_and_is_reported(
    server, fake_agent, django_capture_on_commit_callbacks
):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.failures["retire"] = AgentError(
        "The last-run record of the server could not be saved (OSError: No space left on device), so its container was kept",
        "last_run_failed",
        500,
    )
    sweep(fake_agent)
    sweep(fake_agent)
    assert has_container(fake_agent, inst)
    events = AuditEvent.objects.filter(action=RETIRE_FAILED)
    assert events.count() == 1  # once an hour per server, not on every pass
    assert events[0].success is False and events[0].details["code"] == "last_run_failed"
    assert "No space left" in events[0].details["error"]
    inst.refresh_from_db()
    assert (inst.status, inst.container_state) == (Status.STOPPED, "exited")
    del fake_agent.failures["retire"]
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)


@pytest.mark.parametrize("code", ["container_running", "not_settled", "instance_busy", "container_busy"])
def test_not_now_answers_of_the_agent_are_not_failures(server, fake_agent, code, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.failures["retire"] = AgentError("later", code, 409)
    sweep(fake_agent)
    assert has_container(fake_agent, inst) and not AuditEvent.objects.filter(action=RETIRE_FAILED).exists()


def test_a_retire_problem_does_not_stop_the_pass_for_other_servers(
    server, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    first, second = server("A first"), server("B second")
    for inst in (first, second):
        run("stop", inst, django_capture_on_commit_callbacks)
    real = fake_agent.retire

    def flaky(instance_id, **kwargs):
        if str(instance_id) == str(first.id):
            raise RuntimeError("unexpected")
        return real(instance_id, **kwargs)

    monkeypatch.setattr(AgentClient, "retire", lambda _self, instance_id, **kwargs: flaky(instance_id, **kwargs))
    sweep(fake_agent)
    assert has_container(fake_agent, first) and not has_container(fake_agent, second)


def test_the_agent_is_told_the_settle_time(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    sweep(fake_agent)
    assert fake_agent.called("retire") == [((inst.id,), {"settle_seconds": reconcile.RETIRE_SETTLE_SECONDS})]


# --- crash loops, kill, cut-off jobs ------------------------------------------------------------
def test_crash_loop_stop_is_retired_and_still_diagnosed(make_instance, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = make_instance(name="Looping", desired_state="running")
    fake_agent.prepare(inst.id, {})
    fake_agent.start_healthy = False
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    fake_agent.log_lines[str(inst.id)] = [
        "[init] Failed to auto-install CurseForge modpack",
        "mods have disallowed project distribution",
    ]
    fake_agent.set_container_state(inst.id, restart_count=1)
    reconcile_instance(inst.id)  # baseline of the not-ready streak
    fake_agent.set_container_state(inst.id, restart_count=7)
    reconcile_instance(inst.id)  # 6 crashes: the panel stops it
    inst.refresh_from_db()
    assert inst.status == Status.FAILED and inst.status_detail.startswith(AUTO_STOP_PREFIX) and inst.desired_state == "stopped"
    detail = inst.status_detail
    assert has_container(fake_agent, inst)

    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    assert fake_agent.last_runs[str(inst.id)]["restart_count"] == 7
    for _ in range(2):
        reconcile_all()
        inst.refresh_from_db()
        assert (inst.status, inst.status_detail) == (Status.FAILED, detail)  # the explanation stays until the next start
    found = diagnose(inst)
    assert [i["code"] for i in found["issues"]] == ["crash_loop", "curseforge_manual_downloads"]
    assert found["restart_count"] == 7 and found["lines_scanned"] == 3  # two lines and the end-of-run marker
    body = clients["owner"].get(f"/api/instances/{inst.id}/diagnostics").json()
    assert body["issues"][0]["code"] == "crash_loop" and "7 restarts" in body["issues"][0]["title"]

    # the next start runs in a new container; the record no longer says "keeps crashing"
    fake_agent.start_healthy = True
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    assert inst.status == Status.RUNNING
    assert "crash_loop" not in [i["code"] for i in diagnose(inst)["issues"]]


def test_kill_only_kills_and_the_container_is_retired_later(server, fake_agent, clients):
    inst = server()
    resp = clients["operator"].post(f"/api/instances/{inst.id}/power", {"action": "kill"}, format="json")
    assert resp.status_code == 200 and resp.json()["status"]["exit_code"] == 137  # the answer still has it
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    inst.refresh_from_db()
    assert inst.desired_state == "stopped" and inst.last_exit_code == 137
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    inst.refresh_from_db()
    assert inst.status == Status.STOPPED and inst.last_exit_code == 137


def test_kill_of_a_server_without_a_container_is_a_no_op(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    resp = clients["operator"].post(f"/api/instances/{inst.id}/power", {"action": "kill"}, format="json")
    assert resp.status_code == 200 and resp.json()["status"]["exists"] is False
    assert resp.json()["status"]["last_run"]["exit_code"] == 0


def test_kill_during_stop_leaves_the_removal_to_the_reconciler(
    server, fake_agent, clients, users, django_capture_on_commit_callbacks
):
    inst = server()
    killed = []

    def kill_meanwhile(*_args, **_kwargs):  # the user presses Kill while the Stop job waits for the server
        if not killed:
            killed.append(services.power(Instance.objects.get(pk=inst.pk), "kill", users["operator"]))

    fake_agent.hooks["stop"] = kill_meanwhile
    job = run("stop", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert killed and killed[0]["exit_code"] == 137
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")  # neither removed it
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    assert fake_agent.last_runs[str(inst.id)]["exit_code"] == 137


def test_kill_during_restart_does_not_take_the_container_away(server, fake_agent, users, django_capture_on_commit_callbacks):
    inst = server()
    container_id = fake_agent.containers[str(inst.id)]["id"]

    def kill_meanwhile(*_args, **_kwargs):
        services.power(Instance.objects.get(pk=inst.pk), "kill", users["operator"])
        fake_agent.age_containers()
        reconcile_all()  # a pass in the middle of the restart: the job holds the server

    fake_agent.hooks["restart"] = kill_meanwhile
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    c = fake_agent.containers[str(inst.id)]
    assert c["id"] == container_id and c["running"] is True and not fake_agent.called("retire")


def test_a_stop_job_that_was_cut_off_is_finished_by_the_reconciler(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    # the worker died after the agent had stopped the server: desired state says stopped, the job was failed as interrupted
    Instance.objects.filter(pk=inst.pk).update(desired_state="stopped")
    fake_agent.stop(inst.id)
    Job.objects.create(kind="stop", title="Stop", instance=inst, status=JobStatus.FAILED, error_code="interrupted")
    sweep(fake_agent)
    assert not has_container(fake_agent, inst)
    inst.refresh_from_db()
    assert inst.status == Status.STOPPED


# --- the host shutdown hook: the server is meant to run, so it keeps its container -----------------
def test_host_shutdown_stop_keeps_the_container_and_resume_starts_it(
    server, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    inst = server()
    other = stopped_and_retired(fake_agent, server("Stopped by hand"), django_capture_on_commit_callbacks)
    container_id = fake_agent.containers[str(inst.id)]["id"]
    out = io.StringIO()
    call_command("host_shutdown", "--timeout", "5", stdout=out)
    assert "stopped   Survival" in out.getvalue()
    inst.refresh_from_db()
    assert inst.desired_state == "running" and inst.resume_after_boot is True
    assert fake_agent.containers[str(inst.id)]["running"] is False

    for _ in range(3):  # passes of the panel that starts after the boot
        sweep(fake_agent)
    assert fake_agent.containers[str(inst.id)]["id"] == container_id and not fake_agent.called("retire")[1:]
    inst.refresh_from_db()
    assert inst.status == Status.STOPPED and "started again automatically" in inst.status_detail
    assert retire_blocker(inst, fake_agent.status(inst.id)) == "meant_to_run"

    # the resume step of the panel that starts after Docker came back: the same container starts again
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() + timedelta(seconds=1))
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers(timezone.now() + timedelta(seconds=2))
    assert result["started"] == str(inst.id)
    c = fake_agent.containers[str(inst.id)]
    assert c["id"] == container_id and c["running"] is True
    # the server someone stopped by hand has no container and is not started
    assert not has_container(fake_agent, other)


def test_resume_starts_a_marked_server_whose_container_is_gone(
    server, fake_agent, monkeypatch, django_capture_on_commit_callbacks
):
    """Not produced by the retire rule (the server is meant to run), but the resume step must cope:
    the container was removed by hand, or with the Docker data."""
    inst = server()
    call_command("host_shutdown", "--timeout", "5", stdout=io.StringIO())
    fake_agent.containers.pop(str(inst.id))
    monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() + timedelta(seconds=1))
    with django_capture_on_commit_callbacks(execute=True):
        result = scheduling.resume_flagged_servers(timezone.now() + timedelta(seconds=2))
    assert result["started"] == str(inst.id) and fake_agent.containers[str(inst.id)]["running"] is True


# --- what a server without a container still answers ------------------------------------------------
def test_console_history_after_stop_and_after_the_next_start(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = server()
    old = [f"old line {i}" for i in range(5)]
    inst = stopped_and_retired(fake_agent, inst, django_capture_on_commit_callbacks, lines=old)
    marker = "[mcpanel] ---- end of the run that stopped (exited with code 0) ----"
    url = f"/api/instances/{inst.id}/logs"
    assert clients["owner"].get(url, {"tail": 2000}).json()["lines"] == [*old, marker]
    assert clients["owner"].get(url, {"tail": 3}).json()["lines"] == ["old line 3", "old line 4", marker]
    assert clients["norole"].get(url).status_code in (403, 404)  # behind the console permission, as before

    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    fake_agent.log_lines[str(inst.id)] = ["new line 0", "new line 1"]
    assert clients["owner"].get(url, {"tail": 2000}).json()["lines"] == [*old, marker, "new line 0", "new line 1"]
    assert clients["owner"].get(url, {"tail": 4}).json()["lines"] == ["old line 4", marker, "new line 0", "new line 1"]
    assert clients["owner"].get(url, {"tail": 2}).json()["lines"] == ["new line 0", "new line 1"]

    # the next retire replaces the record: the run before the last is gone from the console
    stopped_and_retired(fake_agent, inst, django_capture_on_commit_callbacks, lines=["new line 0", "new line 1"])
    assert clients["owner"].get(url, {"tail": 2000}).json()["lines"] == ["new line 0", "new line 1", marker]


def test_console_history_of_a_server_that_never_ran_is_empty(make_instance, fake_agent, clients):
    inst = make_instance(name="Never ran")
    assert clients["owner"].get(f"/api/instances/{inst.id}/logs").json() == {"lines": []}
    assert diagnose(inst) == {"issues": [], "restart_count": 0, "lines_scanned": 0}


def test_stats_and_commands_answer_not_running(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    stats = clients["owner"].get(f"/api/instances/{inst.id}/stats", {"disk": "true"})
    assert stats.status_code == 200 and stats.json()["running"] is False and "disk" in stats.json()
    resp = clients["owner"].post(f"/api/instances/{inst.id}/command", {"command": "list"}, format="json")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "not_running"
    assert resp.json()["error"]["message"] == "The server is not running"


def test_scheduled_command_on_a_retired_server_reads_not_running(server, fake_agent, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    schedule = Schedule.objects.create(
        instance=inst, name="say", action="command", cron="* * * * *", payload={"command": "say hi"}, only_when_running=False
    )
    Schedule.objects.filter(pk=schedule.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
    with django_capture_on_commit_callbacks(execute=True):
        scheduling.run_due_schedules(timezone.now())
    schedule.refresh_from_db()
    assert "not running" in schedule.last_result and "no container" not in schedule.last_result.lower()


def test_schedules_start_and_restart_a_retired_server(server, fake_agent, django_capture_on_commit_callbacks):
    for action in ("start", "restart"):
        inst = stopped_and_retired(fake_agent, server(f"Scheduled {action}"), django_capture_on_commit_callbacks)
        schedule = Schedule.objects.create(instance=inst, name=action, action=action, cron="* * * * *", only_when_running=False)
        Schedule.objects.filter(pk=schedule.pk).update(next_run_at=timezone.now() - timedelta(minutes=1))
        with django_capture_on_commit_callbacks(execute=True):
            scheduling.run_due_schedules(timezone.now())
        job = Job.objects.filter(instance=inst, kind=action).latest("created_at")
        assert job.status == JobStatus.SUCCEEDED, job.error
        assert fake_agent.containers[str(inst.id)]["running"] is True


def test_ports_of_a_retired_server_stay_reserved(server, fake_agent, make_instance, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    used = services.ports_in_use(inst.node)
    assert used[inst.game_port] == "server 'Survival'"
    with pytest.raises(Exception) as exc:
        services.check_port_available(inst.node, inst.game_port)
    assert getattr(exc.value, "error_code", "") == "port_conflict"
    assert services.allocate_port(inst.node) != inst.game_port


# --- starting again ---------------------------------------------------------------------------------
def test_start_creates_a_new_container_and_keeps_the_data(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    first = fake_agent.containers[str(inst.id)]["id"]
    inst = stopped_and_retired(fake_agent, inst, django_capture_on_commit_callbacks)
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    c = fake_agent.containers[str(inst.id)]
    assert c["id"] != first and c["running"] is True
    assert "Creating the server's container" in "\n".join(job.log)
    assert fake_agent.text(inst.id, "world/region/r.0.0.mca") == "old"
    assert fake_agent.text(inst.id, "server.properties") == "motd=hello\n"
    inst.refresh_from_db()
    assert inst.status == Status.RUNNING and inst.desired_state == "running" and restart_required(inst) is False
    # settings changed while it was stopped apply at that start, without a "restart required"
    inst = stopped_and_retired(fake_agent, inst, django_capture_on_commit_callbacks)
    Instance.objects.filter(pk=inst.pk).update(memory_mb=3072)
    assert run("start", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    assert fake_agent.containers[str(inst.id)]["spec"]["env"]["MEMORY"] == "3072M"


def test_start_copes_with_a_container_removed_between_status_and_start(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.age_containers()

    def retire_now(instance_id, *_a, **_k):  # the retire that was already on its way arrives first
        fake_agent.hooks.pop("start")
        fake_agent.retire(instance_id, settle_seconds=0)

    fake_agent.hooks["start"] = retire_now
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"] is True and len(fake_agent.called("start")) == 1
    assert str(inst.id) in fake_agent.last_runs


def test_other_start_errors_are_not_swallowed(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.failures["start"] = AgentError(
        "Docker could not start the container: port is already allocated", "port_conflict", 409
    )
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED and job.error_code == "port_conflict"


def test_restart_of_a_retired_server_creates_the_container(server, fake_agent, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"] is True


def test_restart_copes_with_a_container_removed_between_status_and_restart(
    server, fake_agent, django_capture_on_commit_callbacks
):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.age_containers()

    def retire_now(instance_id, *_a, **_k):
        fake_agent.hooks.pop("restart")
        fake_agent.retire(instance_id, settle_seconds=0)

    fake_agent.hooks["restart"] = retire_now
    job = run("restart", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"] is True


def test_stop_copes_with_a_container_removed_between_status_and_stop(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    fake_agent.set_container_state(inst.id, status="exited", running=False, exit_code=1, finished_at=fake_agent._now())
    fake_agent.age_containers()

    def gone(instance_id, *_a, **_k):
        fake_agent.containers.pop(str(instance_id))

    fake_agent.hooks["stop"] = gone
    job = run("stop", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"


def test_a_changed_runtime_image_is_noted_in_the_job_log_and_the_audit_log(
    server, fake_agent, django_capture_on_commit_callbacks
):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    before = fake_agent.last_runs[str(inst.id)]["image_id"]
    # same tag, same build: nothing to say
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert "is not the build" not in "\n".join(job.log)
    inst = stopped_and_retired(fake_agent, inst, django_capture_on_commit_callbacks)
    # the tag now points to another build (the image was pruned and downloaded again)
    fake_agent.image_ids[inst.image] = "sha256:" + "ab" * 32
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED
    assert "is not the build this server last ran on" in "\n".join(job.log)
    event = AuditEvent.objects.get(action="instance.runtime_image_changed")
    assert event.details == {"image": inst.image, "previous_image_id": before, "image_id": "sha256:" + "ab" * 32}


# --- jobs that stop a server, work and start it again: each start creates a missing container -------
def _remove_container_during(fake_agent, method: str, inst) -> None:
    """The container disappears while the job works on the stopped server's files."""

    def gone(*_a, **_k):
        fake_agent.hooks.pop(method)
        fake_agent.containers.pop(str(inst.id), None)

    fake_agent.hooks[method] = gone


def _backed_up(inst, fake_agent, capture) -> Backup:
    job = run("backup", inst, capture, name="b1")
    assert job.status == JobStatus.SUCCEEDED, job.error
    return Backup.objects.get(pk=job.result["backup_id"])


def test_restore_starts_again_without_a_container(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    backup = _backed_up(inst, fake_agent, django_capture_on_commit_callbacks)
    _remove_container_during(fake_agent, "restore_backup", inst)
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is True and fake_agent.containers[str(inst.id)]["running"] is True


def test_restore_path_starts_again_without_a_container(server, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    inst = server()
    backup = _backed_up(inst, fake_agent, django_capture_on_commit_callbacks)
    monkeypatch.setattr(
        AgentClient,
        "backup_restore_path",
        lambda _self, instance_id, snapshot_id, path: (
            fake_agent.containers.pop(str(instance_id), None),
            fake_agent._op("restore", str(instance_id), {"restored": path}),
        )[1],
    )
    job = run("restore_path", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), path="world")
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is True and fake_agent.containers[str(inst.id)]["running"] is True


def test_snapshot_rollback_starts_again_without_a_container(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    snap = run("snapshot_create", inst, django_capture_on_commit_callbacks, label="before")
    assert snap.status == JobStatus.SUCCEEDED, snap.error
    _remove_container_during(fake_agent, "restore_snapshot", inst)
    job = run("snapshot_restore", inst, django_capture_on_commit_callbacks, name=snap.result["name"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is True and fake_agent.containers[str(inst.id)]["running"] is True


def test_snapshot_convert_starts_again_without_a_container(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    _remove_container_during(fake_agent, "convert_snapshots", inst)
    job = run("snapshot_convert", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"] is True


def _replace_world(client, inst, capture, **fields):
    import zipfile

    from django.core.files.uploadedfile import SimpleUploadedFile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MyWorld/level.dat", LEVEL_DAT)
        zf.writestr("MyWorld/region/r.0.0.mca", "new")
    body = {"confirm": "true", "stop_if_running": "true", "pre_restore_backup": "false", **fields}
    body["file"] = SimpleUploadedFile("world.zip", buf.getvalue(), content_type="application/zip")
    with capture(execute=True):
        return client.post(f"/api/instances/{inst.id}/world/replace", body, format="multipart")


def test_world_replace_starts_again_without_a_container(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = server()
    _remove_container_during(fake_agent, "world_replace", inst)
    assert _replace_world(clients["manager"], inst, django_capture_on_commit_callbacks).status_code == 202
    job = Job.objects.filter(instance=inst, kind="world_replace").latest("created_at")
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.containers[str(inst.id)]["running"] is True
    assert fake_agent.text(inst.id, "world/region/r.0.0.mca") == "new"


def test_world_replace_failure_starts_again_without_a_container(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = server()
    fake_agent.failed_operations["world_replace"] = ("The archive holds no world", "no_world_in_archive")
    _remove_container_during(fake_agent, "world_replace", inst)
    _replace_world(clients["manager"], inst, django_capture_on_commit_callbacks)
    job = Job.objects.filter(instance=inst, kind="world_replace").latest("created_at")
    assert job.status == JobStatus.FAILED and job.error_code == "no_world_in_archive"
    assert "the server was started again" in "\n".join(job.log)
    assert fake_agent.containers[str(inst.id)]["running"] is True
    assert fake_agent.text(inst.id, "world/region/r.0.0.mca") == "old"


def _reset_world(client, inst, capture, **fields):
    body = {"confirm_name": inst.name, "folders": ["world"], "stop_if_running": True, "pre_restore_backup": False, **fields}
    with capture(execute=True):
        return client.post(f"/api/instances/{inst.id}/world/reset", body, format="json")


def test_world_reset_starts_again_without_a_container(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = server()
    _remove_container_during(fake_agent, "world_reset", inst)
    resp = _reset_world(clients["manager"], inst, django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.json()
    job = Job.objects.filter(instance=inst, kind="world_reset").latest("created_at")
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is True and fake_agent.containers[str(inst.id)]["running"] is True
    assert fake_agent.get_file(inst.id, "world/level.dat") is None


def test_world_reset_failure_starts_again_without_a_container(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = server()
    fake_agent.failed_operations["world_reset"] = ("The world folders changed", "world_changed")
    _remove_container_during(fake_agent, "world_reset", inst)
    _reset_world(clients["manager"], inst, django_capture_on_commit_callbacks)
    job = Job.objects.filter(instance=inst, kind="world_reset").latest("created_at")
    assert job.status == JobStatus.FAILED and job.error.startswith("The world was not changed.")
    assert "the server was started again" in "\n".join(job.log)
    assert fake_agent.containers[str(inst.id)]["running"] is True
    assert fake_agent.get_file(inst.id, "world/level.dat") is not None


def test_kill_during_a_restore_does_not_break_the_start_afterwards(server, fake_agent, users, django_capture_on_commit_callbacks):
    inst = server()
    backup = _backed_up(inst, fake_agent, django_capture_on_commit_callbacks)

    def kill_and_sweep(*_a, **_k):  # Kill while the files are being restored, then a reconcile pass
        fake_agent.hooks.pop("restore_backup")
        services.power(Instance.objects.get(pk=inst.pk), "kill", users["operator"])
        fake_agent.age_containers()
        reconcile_all()

    fake_agent.hooks["restore_backup"] = kill_and_sweep
    job = run("restore", inst, django_capture_on_commit_callbacks, backup_id=str(backup.id), pre_restore_backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert not fake_agent.called("retire")  # the job held the server
    assert fake_agent.containers[str(inst.id)]["running"] is True


# --- the other things a stopped server is used for ---------------------------------------------------
def test_backup_restore_and_rollback_of_a_retired_server(server, fake_agent, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    inst = stopped_and_retired(fake_agent, server(), capture)
    backup = _backed_up(inst, fake_agent, capture)
    assert backup.status == BackupStatus.SUCCEEDED and backup.consistent is True
    fake_agent.put_file(inst.id, "world/region/r.0.0.mca", "changed")
    job = run("restore", inst, capture, backup_id=str(backup.id), pre_restore_backup=True)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["restarted"] is False and not has_container(fake_agent, inst)
    assert fake_agent.text(inst.id, "world/region/r.0.0.mca") == "old"
    snap = run("snapshot_create", inst, capture, label="s")
    assert snap.status == JobStatus.SUCCEEDED, snap.error
    job = run("snapshot_restore", inst, capture, name=snap.result["name"])
    assert job.status == JobStatus.SUCCEEDED and job.result["restarted"] is False
    assert run("snapshot_convert", inst, capture).status == JobStatus.SUCCEEDED
    assert not has_container(fake_agent, inst)


def test_world_replace_and_reset_of_a_retired_server(server, fake_agent, clients, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    inst = stopped_and_retired(fake_agent, server(), capture)
    assert _replace_world(clients["manager"], inst, capture).status_code == 202
    job = Job.objects.filter(instance=inst, kind="world_replace").latest("created_at")
    assert job.status == JobStatus.SUCCEEDED and job.result["restarted"] is False
    assert fake_agent.text(inst.id, "world/region/r.0.0.mca") == "new"
    assert _reset_world(clients["manager"], inst, capture).status_code == 202
    job = Job.objects.filter(instance=inst, kind="world_reset").latest("created_at")
    assert job.status == JobStatus.SUCCEEDED and job.result["restarted"] is False
    assert not has_container(fake_agent, inst)


def test_clone_of_a_retired_server(server, fake_agent, make_instance, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    source = stopped_and_retired(fake_agent, server(), capture)
    target = make_instance(name="Copy")
    job = run("clone", source, capture, target_id=str(target.id))
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert fake_agent.text(target.id, "world/region/r.0.0.mca") == "old"
    # the clone's container (created, never started) is retired too; the clone can be started
    sweep(fake_agent)
    assert not has_container(fake_agent, target) and str(target.id) not in fake_agent.last_runs
    target.refresh_from_db()
    assert target.status == Status.STOPPED
    assert run("start", target, capture).status == JobStatus.SUCCEEDED
    assert fake_agent.containers[str(target.id)]["running"] is True


def test_archive_and_unarchive_of_a_retired_server(server, fake_agent, clients, django_capture_on_commit_callbacks):
    capture = django_capture_on_commit_callbacks
    inst = stopped_and_retired(fake_agent, server(), capture, lines=["before the archive"])
    assert run("archive", inst, capture).status == JobStatus.SUCCEEDED
    inst.refresh_from_db()
    assert inst.status == Status.ARCHIVED
    sweep(fake_agent)
    with capture(execute=True):
        resp = clients["owner"].post(f"/api/instances/{inst.id}/unarchive", format="json")
    assert resp.status_code in (200, 202), resp.content
    inst.refresh_from_db()
    assert inst.archived_at is None
    reconcile_instance(inst.id)
    inst.refresh_from_db()
    assert (inst.status, inst.status_detail) == (Status.STOPPED, "")
    assert clients["owner"].get(f"/api/instances/{inst.id}/logs").json()["lines"][0] == "before the archive"
    assert run("start", inst, capture).status == JobStatus.SUCCEEDED


def test_archiving_a_running_server_keeps_its_last_run(server, fake_agent, clients, django_capture_on_commit_callbacks):
    inst = server()
    fake_agent.log_lines[str(inst.id)] = ["ran until the archive"]
    assert run("archive", inst, django_capture_on_commit_callbacks).status == JobStatus.SUCCEEDED
    assert not has_container(fake_agent, inst)
    assert clients["owner"].get(f"/api/instances/{inst.id}/logs").json()["lines"][0] == "ran until the archive"


def test_delete_removes_the_record(server, fake_agent, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks)
    assert str(inst.id) in fake_agent.last_runs
    job = run("delete", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert str(inst.id) not in fake_agent.last_runs and not Instance.objects.filter(pk=inst.pk).exists()


def test_recreate_of_a_retired_server(server, fake_agent, django_capture_on_commit_callbacks):
    inst = stopped_and_retired(fake_agent, server(), django_capture_on_commit_callbacks, lines=["kept"])
    job = run("recreate", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED and job.result["restarted"] is False
    sweep(fake_agent)
    assert not has_container(fake_agent, inst) and fake_agent.last_runs[str(inst.id)]["log"]["lines"] == ["kept"]


# --- the roll-out command ----------------------------------------------------------------------------
def _command(*args) -> str:
    out = io.StringIO()
    call_command("retire_stopped_containers", *args, stdout=out)
    return out.getvalue()


def test_command_dry_run_lists_and_changes_nothing(server, fake_agent, settings, django_capture_on_commit_callbacks):
    settings.MCPANEL_RETIRE_STOPPED_CONTAINERS = False  # the staged roll-out: look first
    capture = django_capture_on_commit_callbacks
    stopped, running, crashed = server("Stopped"), server("Running"), server("Crashed")
    run("stop", stopped, capture)
    fake_agent.set_container_state(crashed.id, status="exited", running=False, exit_code=1, finished_at=fake_agent._now())
    fake_agent.age_containers(seconds=3 * 86400)
    out = _command("--dry-run")
    assert "MCPANEL_RETIRE_STOPPED_CONTAINERS is off" in out
    assert "would remove  Stopped: exited 3 days ago, exit code 0" in out
    assert "keep          Running: its container is running" in out
    assert "keep          Crashed: the server is meant to run but is not running (exited 3 days ago, exit code 1)" in out
    assert "1 container(s) would be removed, 2 kept, 0 server(s) without a container." in out
    assert not fake_agent.called("retire") and all(has_container(fake_agent, i) for i in (stopped, running, crashed))


def test_command_removes_what_the_dry_run_listed(server, fake_agent, settings, django_capture_on_commit_callbacks):
    settings.MCPANEL_RETIRE_STOPPED_CONTAINERS = False  # the command is a manual sweep: it works either way
    capture = django_capture_on_commit_callbacks
    stopped, running = server("Stopped"), server("Running")
    fake_agent.log_lines[str(stopped.id)] = ["a", "b", "c"]
    run("stop", stopped, capture)
    fake_agent.age_containers()
    out = _command()
    assert "removed       Stopped: exited 1 hour ago, exit code 0; last run saved (3 console lines)" in out
    assert "1 container(s) removed, 1 kept, 0 server(s) without a container." in out
    assert not has_container(fake_agent, stopped) and has_container(fake_agent, running)
    stopped.refresh_from_db()
    assert (stopped.status, stopped.container_state) == (Status.STOPPED, "")
    assert "0 container(s) removed, 1 kept, 1 server(s) without a container." in _command()


def test_command_with_an_older_agent_and_an_unknown_node(server, fake_agent, django_capture_on_commit_callbacks):
    from django.core.management.base import CommandError

    fake_agent.features = []
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    fake_agent.age_containers()
    out = _command()
    assert "its agent is older than this panel" in out and "would remove  Survival" in out
    assert has_container(fake_agent, inst) and not fake_agent.called("retire")
    with pytest.raises(CommandError, match="no node named"):
        _command("--node", "nope")
    fake_agent.failures["list_containers"] = AgentError("down", "docker_unavailable", 503)
    with pytest.raises(CommandError, match="Could not ask the agent"):
        _command()


def test_reconcile_node_counts_a_retire_as_a_change(server, fake_agent, django_capture_on_commit_callbacks):
    inst = server()
    run("stop", inst, django_capture_on_commit_callbacks)
    reconcile_all()
    fake_agent.age_containers()
    assert reconcile_node(inst.node)["changed"] == 1
    assert reconcile_node(inst.node)["changed"] == 0


# --- the client: what goes over the wire, and agents from before the feature --------------------------
def test_client_requests_and_answers_of_older_agents(node, monkeypatch):
    import uuid

    calls: list[tuple] = []
    answers: dict[tuple[str, str], object] = {}

    def fake_json(_self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return answers[(method, path)]

    monkeypatch.setattr(AgentClient, "_json", fake_json)
    client = AgentClient(node)
    iid = uuid.uuid4()

    answers[("GET", "/v1/instances")] = {"containers": [{"instance_id": "x"}]}  # an agent without records
    assert client.list_state() == {"containers": [{"instance_id": "x"}], "last_runs": {}}
    assert client.list_containers() == [{"instance_id": "x"}]
    answers[("GET", "/v1/instances")] = {"containers": [], "last_runs": {"a": {"exit_code": 1}}}
    assert client.list_state()["last_runs"] == {"a": {"exit_code": 1}}

    answers[("POST", f"/v1/instances/{iid}/retire")] = {"retired": True, "recorded": True}
    assert client.retire(iid, settle_seconds=10)["retired"] is True
    assert calls[-1] == ("POST", f"/v1/instances/{iid}/retire", {"json": {"settle_seconds": 10}, "timeout": 120})
    answers[("DELETE", f"/v1/instances/{iid}/last-run")] = {"deleted": True}
    assert client.forget_last_run(iid) is True

    for info in ({"version": "0.1.0"}, {"features": "retire"}, {"features": None}):  # no list of features: none
        answers[("GET", "/v1/info")] = info
        assert client.features(refresh=True) == frozenset() and client.supports("retire") is False
    answers[("GET", "/v1/info")] = {"features": ["retire", "something-newer"]}
    assert client.features(refresh=True) == {"retire", "something-newer"}
    asked = len(calls)
    assert client.supports("retire") is True and len(calls) == asked  # remembered
