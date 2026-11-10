"""No container for a stopped server, against a REAL node agent + Docker daemon.

Skipped unless ``MCPANEL_IT_AGENT_URL`` is set, e.g.::

    MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration -s tests/test_integration_retire.py

Shares ``MCPANEL_IT_AGENT_TOKEN`` / ``MCPANEL_IT_AGENT_DATA_ROOT`` / ``MCPANEL_IT_READY_TIMEOUT`` /
``MCPANEL_IT_BIND_IP`` with ``test_integration_agent.py``. Optional: ``MCPANEL_IT_AGENT_CONTAINER``,
the name of the agent's own container: the last-run record is readable by root only, so its file
mode and content are checked through ``docker exec`` there (skipped, and said so, when unset).

One real vanilla server goes through the whole life of the feature, driven by the panel's own job
handlers, reconciler and management commands:

1. install + first start; a block is placed through RCON;
2. the Stop job; the reconciler retires the container after the settle time;
3. the last-run record (exit code 0, the save lines at the end of the console), the status, the
   console history, the console stream and kill / stats / command without a container;
4. Start: a new container, the world is intact, the previous run is in front of the new log;
5. ``manage.py host_shutdown`` (desired state stays "running"): the container is kept, and the
   resume step starts the same container again;
6. ``kill -9`` of the Java process while the server is meant to run: the container stays, the
   status says how it died; Stop dismisses it. With MCPANEL_RETIRE_STOPPED_CONTAINERS off (the
   staged roll-out) the exited container stays, ``manage.py retire_stopped_containers --dry-run``
   lists it and the command without ``--dry-run`` removes it, keeping how it died;
7. Delete removes the record.

The server uses the restart policy "no", so a killed server stays down instead of being started
again by Docker. Everything created is named ``it-…`` and removed even when an assertion fails.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from .integration_support import (
    READY_TIMEOUT,
    InstanceTracker,
    Timer,
    container_names,
    disk,
    make_node,
    rcon,
    run_job,
    wait_status,
)

AGENT_URL = os.environ.get("MCPANEL_IT_AGENT_URL", "")
AGENT_TOKEN = os.environ.get("MCPANEL_IT_AGENT_TOKEN", "dev-agent-token")
DATA_ROOT = os.environ.get("MCPANEL_IT_AGENT_DATA_ROOT", "/srv/mcpanel-dev")
AGENT_CONTAINER = os.environ.get("MCPANEL_IT_AGENT_CONTAINER", "")
PORTS = (25600, 25650)
BLOCK = (0, 100, 0)
MARKER = "[mcpanel] ---- end of the run that stopped "

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not AGENT_URL, reason="set MCPANEL_IT_AGENT_URL to run against a real agent"),
    pytest.mark.django_db(transaction=True),
]


@pytest.fixture
def it_node(transactional_db: Any) -> Any:
    return make_node("integration-agent", AGENT_URL, AGENT_TOKEN, ports=PORTS)


@pytest.fixture
def tracker(transactional_db: Any) -> Any:
    t = InstanceTracker(DATA_ROOT)
    yield t
    t.cleanup()


# --- helpers -------------------------------------------------------------------------------------
def _block_is(instance: Any, block: str, *, attempts: int = 10) -> bool:
    x, y, z = BLOCK
    out = ""
    for _ in range(attempts):
        out = rcon(instance, f"execute if block {x} {y} {z} minecraft:{block}")
        if "Test passed" in out:
            return True
        if "Test failed" in out:
            return False
        time.sleep(3)  # e.g. "That position is not loaded" right after start
    raise AssertionError(f"unexpected answer to block test: {out!r}")


def _docker_state(instance_id: Any) -> dict[str, Any] | None:
    """State of the server's container straight from Docker (None: there is no container)."""
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State}}|{{.Id}}", f"mcpanel-{instance_id.hex[:12]}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        assert "No such" in proc.stderr or "no such" in proc.stderr, proc.stderr
        return None
    state, _, container_id = proc.stdout.strip().rpartition("|")
    return {**json.loads(state), "Id": container_id}


def _reconcile_until_gone(node: Any, instance: Any, timeout: float = 90) -> float:
    """Run reconcile passes like the panel's scheduler does until the container is gone; seconds it took."""
    from apps.instances.reconcile import reconcile_node

    began = time.monotonic()
    while time.monotonic() - began < timeout:
        reconcile_node(node)
        if _docker_state(instance.id) is None:
            return time.monotonic() - began
        time.sleep(2)
    pytest.fail(f"the container of {instance.name} was not retired within {timeout:.0f} s: {_docker_state(instance.id)}")


def _reconcile_for(node: Any, seconds: float) -> None:
    """Reconcile passes for longer than the settle time, for containers that must stay."""
    from apps.instances.reconcile import reconcile_node

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        reconcile_node(node)
        time.sleep(3)
    reconcile_node(node)


def _agent_exec(*argv: str) -> str:
    proc = subprocess.run(["docker", "exec", AGENT_CONTAINER, *argv], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _stream_open(client: Any, instance_id: Any, tail: int) -> tuple[list[str], list[str]]:
    """What the console stream sends when it opens: (states, log lines) of the first two seconds."""
    from websockets.sync.client import connect

    states: list[str] = []
    lines: list[str] = []
    url = client.ws_url(f"/v1/instances/{instance_id}/logs/stream?tail={tail}")
    with connect(url, additional_headers=client.headers, open_timeout=10) as ws:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            except TimeoutError:
                break
            if msg.get("type") == "state":
                states.append(msg["state"])
            elif msg.get("type") == "logs":
                lines.extend(msg["lines"])
    return states, lines


def test_stopped_server_has_no_container(it_node, tracker, monkeypatch, settings):
    from django.core.management import call_command
    from django.utils import timezone

    from apps.audit.models import AuditEvent
    from apps.instances import scheduling
    from apps.instances.diagnostics import diagnose
    from apps.instances.models import Instance
    from apps.instances.reconcile import RETIRE_SETTLE_SECONDS, reconcile_instance, retire_blocker
    from apps.jobs.models import Job
    from apps.nodes.client import AgentClient, AgentError

    timer = Timer()
    client = AgentClient(it_node)
    assert client.supports("retire"), "this agent does not offer the retire operation (GET /v1/info, features)"
    instance = tracker.create(it_node, "retire", PORTS, restart_policy="no")
    iid = instance.id
    record_path = Path(DATA_ROOT, "agent", "last-run", f"{iid}.json") if DATA_ROOT else None
    try:
        # --- 1. install + first start -----------------------------------------------------------------
        run_job(
            "install",
            instance,
            start=True,
            server_properties={
                "motd": "MCPanel retire test",
                "level-type": "minecraft:flat",
                "generate-structures": "false",
                "view-distance": "4",
                "simulation-distance": "4",
                "spawn-protection": "0",
            },
        )
        inst = wait_status(instance, "running", READY_TIMEOUT)
        first = _docker_state(iid)
        assert first and first["Running"] is True
        timer.mark("install + first start until healthy")
        rcon(inst, "forceload add 0 0")
        x, y, z = BLOCK
        assert "Changed the block" in rcon(inst, f"setblock {x} {y} {z} minecraft:gold_block")
        assert _block_is(inst, "gold_block")

        # --- 2. Stop job, then the reconciler retires the container ------------------------------------
        job = run_job("stop", inst)
        print("[it]   stop job log: " + " | ".join(job.log[-4:]), flush=True)
        stopped = _docker_state(iid)
        # the stop job removes nothing: right after it the exited container is still there
        assert stopped and stopped["Running"] is False and stopped["ExitCode"] == 0 and stopped["Id"] == first["Id"]
        inst = reconcile_instance(iid)
        assert (inst.status, inst.container_state, inst.desired_state) == ("stopped", "exited", "stopped")
        took = _reconcile_until_gone(it_node, inst)
        print(f"[it]   container retired {took:.1f} s after the stop job (settle time {RETIRE_SETTLE_SECONDS:.0f} s)", flush=True)
        assert took >= RETIRE_SETTLE_SECONDS - 3, "the container was removed before the settle time"
        assert container_names(iid) in ("", None)
        timer.mark("stop job + retire by the reconciler")

        # --- 3. the last-run record and what a server without a container answers ------------------------
        status = client.status(iid)
        last_run = status.get("last_run") or {}
        print(
            f"[it]   status without a container: {json.dumps({k: v for k, v in status.items() if k != 'last_run'})}", flush=True
        )
        print(f"[it]   last run: {json.dumps(last_run)}", flush=True)
        assert status["exists"] is False
        assert last_run["exit_code"] == 0 and last_run["oom_killed"] is False and last_run["restart_count"] == 0
        assert last_run["container_id"] == first["Id"] and last_run["image_id"].startswith("sha256:")
        assert last_run["image"] == inst.image and last_run["status"] == "exited"
        assert last_run["started_at"] and last_run["finished_at"] and last_run["started_at"] < last_run["finished_at"]
        assert 20 < last_run["log_lines"] <= 2000
        inst = Instance.objects.get(pk=iid)
        assert (inst.status, inst.status_detail, inst.container_state) == ("stopped", "", "")
        assert inst.last_exit_code == 0 and inst.oom_killed is False
        retired = AuditEvent.objects.filter(action="instance.container_retired", instance_id=iid).latest("created_at")
        assert retired.details["recorded"] is True and retired.details["exit_code"] == 0

        history = client.logs(iid, tail=2000)
        tail = "\n".join(history[-40:])
        print("[it]   end of the saved console:\n      " + "\n      ".join(history[-8:]), flush=True)
        assert history[-1].startswith(MARKER) and "exited with code 0" in history[-1]
        assert "Stopping server" in tail and "Saving worlds" in tail and "All dimensions are saved" in tail
        assert any("Starting minecraft server" in line for line in history)  # the run from its start
        assert len(client.logs(iid, tail=5)) == 5
        found = diagnose(inst)
        assert found["restart_count"] == 0 and found["lines_scanned"] == len(client.logs(iid, tail=1500))
        assert "crash_loop" not in [i["code"] for i in found["issues"]]

        states, lines = _stream_open(client, iid, tail=30)
        print(f"[it]   console stream without a container: states {states}, {len(lines)} lines", flush=True)
        assert states == ["missing"] and len(lines) == 30 and lines[-1].startswith(MARKER)
        assert lines == history[-30:]

        assert client.kill(iid)["exists"] is False  # a no-op, not an error
        assert client.stats(iid) == {"running": False}
        with pytest.raises(AgentError) as not_running:
            client.command(iid, "list")
        assert not_running.value.error_code == "not_running" and not_running.value.agent_status == 409
        listed = client.list_state()
        assert str(iid) not in {c.get("instance_id") for c in listed["containers"]}
        assert listed["last_runs"][str(iid)]["exit_code"] == 0

        if record_path is not None:
            folder = record_path.parent
            mode, uid, _gid = disk.stat(folder)
            assert mode == 0o700 and uid == 0, oct(mode)
            if os.geteuid() != 0:
                with pytest.raises(PermissionError):  # root only: not even its name is visible to others
                    os.stat(record_path)
            assert tracker.instance_dir(inst) not in record_path.parents
            leaked = [name for name in disk.names_below(tracker.instance_dir(inst)) if "last-run" in name]
            assert leaked == []  # nothing of it inside the server folder (and so in no backup)
        if AGENT_CONTAINER and record_path is not None:
            mode = _agent_exec("stat", "-c", "%a %u %g", str(record_path)).strip()
            saved = json.loads(_agent_exec("cat", str(record_path)))
            print(
                f"[it]   record file {record_path}: mode/uid/gid {mode}, {len(saved['log']['lines'])} console lines", flush=True
            )
            assert mode == "600 0 0"
            assert saved["exit_code"] == 0 and saved["instance_id"] == str(iid) and saved["log"]["lines"] == history[:-1]
            assert "RCON_PASSWORD" not in json.dumps(saved) and inst.rcon_password not in json.dumps(saved)
        else:
            print("[it]   record file not inspected (set MCPANEL_IT_AGENT_CONTAINER and MCPANEL_IT_AGENT_DATA_ROOT)", flush=True)
        timer.mark("last-run record, status, console history and stream without a container")

        # --- 4. Start: a new container, the same world ----------------------------------------------------
        job = run_job("start", inst)
        assert "Creating the server's container" in "\n".join(job.log)
        inst = wait_status(inst, "running", READY_TIMEOUT)
        second = _docker_state(iid)
        assert second and second["Running"] is True and second["Id"] != first["Id"]
        assert _block_is(inst, "gold_block"), "the world was not the same after the start in a new container"
        history = client.logs(iid, tail=2000)
        marker_at = next(i for i, line in enumerate(history) if line.startswith(MARKER))
        before, after = "\n".join(history[:marker_at]), "\n".join(history[marker_at + 1 :])
        assert "All dimensions are saved" in before and "Starting minecraft server" in after  # previous run, then this one
        assert "last_run" not in client.status(iid)  # a container exists: its own state is the answer
        # the console of the running server opens the same way: the end of the previous run, then this one
        states, lines = _stream_open(client, iid, tail=2000)
        at = next(i for i, line in enumerate(lines) if line.startswith(MARKER))
        print(f"[it]   console stream of the new container: states {states}, {at} lines of the previous run in front", flush=True)
        assert states[:1] == ["running"] and lines[: at + 1] == history[: marker_at + 1]
        assert any("Starting minecraft server" in line for line in lines[at + 1 :])
        timer.mark("start in a new container until healthy; world intact; previous run in the console")

        # --- 5. host shutdown: the server is meant to run, so it keeps its container ------------------------
        out = io.StringIO()
        call_command("host_shutdown", "--timeout", "45", "--node", it_node.name, stdout=out)
        print("[it]   host_shutdown: " + " | ".join(out.getvalue().strip().splitlines()), flush=True)
        inst = Instance.objects.get(pk=iid)
        assert inst.desired_state == "running" and inst.resume_after_boot is True
        _reconcile_for(it_node, RETIRE_SETTLE_SECONDS + 8)
        kept = _docker_state(iid)
        assert kept and kept["Id"] == second["Id"] and kept["Running"] is False and kept["ExitCode"] == 0
        inst = Instance.objects.get(pk=iid)
        assert inst.status == "stopped" and "started again automatically" in inst.status_detail
        assert retire_blocker(inst, client.status(iid)) == "meant_to_run"
        # the panel that starts after the boot resumes it: the same container runs again
        monkeypatch.setattr(scheduling, "runtime_started_at", lambda: timezone.now() + timedelta(seconds=1))
        result = scheduling.resume_flagged_servers(timezone.now() + timedelta(seconds=2))
        assert result["started"] == str(iid), result
        inst = wait_status(inst, "running", READY_TIMEOUT)
        resumed = _docker_state(iid)
        assert resumed and resumed["Id"] == second["Id"] and resumed["Running"] is True
        assert _block_is(inst, "gold_block")
        timer.mark("host_shutdown keeps the container; resume starts it again")

        # --- 6. kill -9 of the Java process while the server is meant to run ---------------------------------
        name = f"mcpanel-{iid.hex[:12]}"
        # /proc/<pid>/comm is readable by everyone (exe is not, for root without CAP_SYS_PTRACE)
        killer = 'n=0; for p in /proc/[0-9]*; do if [ "$(cat $p/comm 2>/dev/null)" = java ]; then kill -9 ${p#/proc/} && n=$((n+1)); fi; done; echo $n'
        kill = subprocess.run(["docker", "exec", name, "sh", "-c", killer], capture_output=True, text=True, check=False)
        assert kill.returncode == 0 and kill.stdout.strip() == "1", (kill.stdout, kill.stderr)
        deadline = time.monotonic() + 60
        while (_docker_state(iid) or {}).get("Running") and time.monotonic() < deadline:
            time.sleep(1)
        crashed = _docker_state(iid)
        assert crashed and crashed["Running"] is False and crashed["ExitCode"] != 0, crashed
        _reconcile_for(it_node, RETIRE_SETTLE_SECONDS + 8)
        evidence = _docker_state(iid)
        assert evidence and evidence["Id"] == second["Id"], "the container of a crashed server was removed"
        inst = Instance.objects.get(pk=iid)
        print(
            f"[it]   after kill -9: Docker exit code {crashed['ExitCode']}; status {inst.status!r} {inst.status_detail!r}",
            flush=True,
        )
        assert inst.desired_state == "running" and inst.status == "failed"
        assert f"exit code {crashed['ExitCode']}" in inst.status_detail and inst.last_exit_code == crashed["ExitCode"]
        assert retire_blocker(inst, client.status(iid)) == "meant_to_run"
        assert not Job.objects.filter(instance_id=iid, status__in=("queued", "running")).exists()
        timer.mark("kill -9: container kept as evidence, status failed")
        # Stop dismisses it. With the switch off (the staged roll-out: look first) the reconciler
        # leaves the exited container alone, like the ones older releases left behind ...
        settings.MCPANEL_RETIRE_STOPPED_CONTAINERS = False
        run_job("stop", inst)
        _reconcile_for(it_node, 4)
        left = _docker_state(iid)
        assert left and left["Id"] == second["Id"] and left["Running"] is False
        # ... the roll-out command lists it without touching it, and then removes it
        out = io.StringIO()
        call_command("retire_stopped_containers", "--dry-run", stdout=out)
        print("[it]   retire_stopped_containers --dry-run: " + " | ".join(out.getvalue().strip().splitlines()), flush=True)
        assert f"would remove  {inst.name}: exited" in out.getvalue() and f"exit code {crashed['ExitCode']}" in out.getvalue()
        assert "1 container(s) would be removed, 0 kept" in out.getvalue()
        assert (_docker_state(iid) or {}).get("Id") == second["Id"]
        out = io.StringIO()
        call_command("retire_stopped_containers", stdout=out)
        print("[it]   retire_stopped_containers: " + " | ".join(out.getvalue().strip().splitlines()), flush=True)
        assert f"removed       {inst.name}: exited" in out.getvalue() and "last run saved (" in out.getvalue()
        assert "1 container(s) removed, 0 kept" in out.getvalue()
        assert _docker_state(iid) is None
        settings.MCPANEL_RETIRE_STOPPED_CONTAINERS = True
        last_run = client.status(iid)["last_run"]
        inst = Instance.objects.get(pk=iid)
        assert last_run["exit_code"] == crashed["ExitCode"] and last_run["container_id"] == second["Id"]
        assert (inst.status, inst.container_state, inst.last_exit_code) == ("stopped", "", crashed["ExitCode"])
        assert any("Starting minecraft server" in line for line in client.logs(iid, tail=2000))
        timer.mark("stop dismisses the crashed server; the roll-out command lists and removes its container")

        # --- 7. delete removes the record ---------------------------------------------------------------------
        run_job("delete", inst)
        assert not Instance.objects.filter(pk=iid).exists()
        assert client.status(iid) == {"exists": False, "operation": None}
        assert str(iid) not in client.list_state()["last_runs"]
        with pytest.raises(AgentError) as gone:
            client.logs(iid)
        assert gone.value.error_code == "no_container"
        if AGENT_CONTAINER and record_path is not None:
            left = _agent_exec("sh", "-c", f"ls -A {record_path.parent} | grep -c {iid} || true").strip()
            assert left == "0", left
        if DATA_ROOT:
            assert not disk.exists(tracker.path("instances", str(iid)))
        assert container_names(iid) in ("", None)
        timer.mark("delete removes the record")
    finally:
        timer.summary()
