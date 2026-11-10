"""Shared helpers for the integration tests (marker ``integration``: real agent + Docker).

The tests drive the panel's own code paths (job handlers through the job runner, the
content service, API views) against a real node agent through the real ``AgentClient``.
Everything they create is named ``it-…`` and removed by :class:`InstanceTracker`, also
when an assertion fails. Not collected by pytest (no ``test_`` prefix).
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

READY_TIMEOUT = float(os.environ.get("MCPANEL_IT_READY_TIMEOUT", "360"))
# Host address the game ports of the test servers are published on. The model's default is
# 0.0.0.0, which would put every test server on the LAN while the tests run.
BIND_IP = os.environ.get("MCPANEL_IT_BIND_IP") or "127.0.0.1"


class Timer:
    def __init__(self) -> None:
        self.start = time.monotonic()
        self.marks: list[tuple[str, float]] = []
        self._last = self.start

    def mark(self, label: str) -> None:
        now = time.monotonic()
        elapsed, self._last = now - self._last, now
        self.marks.append((label, elapsed))
        print(f"[it] {label}: {elapsed:.1f}s (total {now - self.start:.1f}s)", flush=True)

    def summary(self) -> None:
        print("[it] timings: " + ", ".join(f"{label} {secs:.1f}s" for label, secs in self.marks), flush=True)


def host_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))  # probing the host; nothing listens
        except OSError:
            return False
    return True


def make_node(name: str, url: str, token: str, *, fingerprint: str = "", ports: tuple[int, int] | None = None) -> Any:
    """Register a real agent as the default node (optionally TLS-pinned) and check it is usable."""
    from apps.core.models import SystemSetting
    from apps.nodes.client import AgentClient
    from apps.nodes.models import Node
    from apps.nodes.tls import normalize_fingerprint

    node = Node.objects.create(
        name=name,
        agent_url=url,
        agent_token=token,
        use_settings_token=False,
        is_default=True,
        tls_fingerprint=normalize_fingerprint(fingerprint) if fingerprint else "",
    )
    health = AgentClient(node).health()
    assert health["docker"] is True, f"agent at {url} reports Docker unavailable: {health}"
    if ports:
        SystemSetting.set("port_range_start", ports[0])
        SystemSetting.set("port_range_end", ports[1])
    return node


def pick_port(node: Any, ports: tuple[int, int]) -> int:
    from apps.instances import services

    used = services.ports_in_use(node)  # panel DB + every port the Docker host publishes
    for port in range(ports[0], ports[1] + 1):
        if port not in used and host_port_free(port):
            return port
    pytest.fail(f"no free port in {ports}")


def run_job(job_kind: str, instance: Any, **params: Any) -> Any:
    from apps.jobs.models import Job, JobStatus
    from apps.jobs.runner import enqueue

    # django_db(transaction=True): on_commit fires immediately and eager Celery runs the job inline
    job = enqueue(job_kind, title=f"it {job_kind}", instance=instance, params=params)
    job = Job.objects.get(pk=job.pk)
    assert job.status == JobStatus.SUCCEEDED, f"{job_kind} job {job.status}: [{job.error_code}] {job.error}\n" + "\n".join(
        job.log[-20:]
    )
    return job


def wait_status(instance: Any, wanted: str, timeout: float) -> Any:
    from apps.instances.reconcile import reconcile_instance
    from apps.nodes.client import AgentClient

    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while True:
        inst = reconcile_instance(instance.id)
        if not seen or seen[-1] != inst.status:
            seen.append(inst.status)
            print(f"[it]   status -> {inst.status} {inst.status_detail!r}", flush=True)
        if inst.status == wanted:
            return inst
        if inst.status == "failed" or time.monotonic() > deadline:
            try:
                tail = "\n".join(AgentClient(inst.node).logs(inst.id, tail=60))
            except Exception as exc:  # pragma: no cover - diagnostics only
                tail = f"<logs unavailable: {exc}>"
            pytest.fail(f"instance did not reach {wanted!r} (statuses: {seen}); container log tail:\n{tail}")
        time.sleep(3)


def rcon(instance: Any, command: str, *, attempts: int = 20) -> str:
    from apps.core.errors import ApiError
    from apps.nodes.client import AgentClient

    last: Exception | None = None
    for _ in range(attempts):
        try:
            out = AgentClient(instance.node).command(instance.id, command)
            print(f"[it]   rcon {command!r} -> {out.strip()!r}", flush=True)
            return out
        except ApiError as exc:  # RCON listener can lag slightly behind the status ping
            last = exc
            time.sleep(3)
    raise AssertionError(f"RCON command {command!r} kept failing: {last}")


def restic_available(node: Any) -> bool:
    from apps.nodes.client import AgentClient

    return bool(AgentClient(node).info().get("restic"))


def backup_repo_dir(node: Any, instance_id: uuid.UUID) -> Path:
    """Where the agent keeps the restic repository of a server, by the agent's own account.

    The backups directory is not always ``<data root>/backups`` (``AGENT_BACKUP_ROOT``), so the
    path is read from the agent instead of being built from the data root: a check of a path
    that never existed would pass without checking anything.
    """
    from apps.nodes.client import AgentClient

    return Path(AgentClient(node).info()["storage"]["backups"]["path"]) / "restic" / str(instance_id)


# The agent makes its data root root:root 0750 at every start (mcagent/maintenance.py), so a test
# run as a normal user cannot look into it. With MCPANEL_IT_AGENT_CONTAINER (the agent's container,
# which sees the data root at the same path, as the dev and CI setups mount it) the on-disk checks
# go through `docker exec <container>`; without it they are skipped with a message saying so.
AGENT_CONTAINER = os.environ.get("MCPANEL_IT_AGENT_CONTAINER", "")
DISK_SKIP = (
    "the agent's data root is root:root 0750 and this test runs as uid {uid}: set MCPANEL_IT_AGENT_CONTAINER "
    "to the agent's container (the checks then run there with docker exec), or run the tests as root"
)


def agent_exec(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "exec", AGENT_CONTAINER, *argv], capture_output=True, text=True, check=False)


class AgentDisk:
    """Checks of files under the agent's data root: directly when this process may read them,
    else in the agent's container (``MCPANEL_IT_AGENT_CONTAINER``), else the test is skipped."""

    @staticmethod
    def _direct(fn: Any, *args: Any) -> Any:
        try:
            return True, fn(*args)
        except PermissionError:
            if not AGENT_CONTAINER:
                pytest.skip(DISK_SKIP.format(uid=os.geteuid()))
            return False, None

    def exists(self, path: Path) -> bool:
        direct, value = self._direct(lambda p: p.exists(), path)
        return value if direct else agent_exec("test", "-e", str(path)).returncode == 0

    def is_dir(self, path: Path) -> bool:
        direct, value = self._direct(lambda p: p.is_dir(), path)
        return value if direct else agent_exec("test", "-d", str(path)).returncode == 0

    def stat(self, path: Path) -> tuple[int, int, int]:
        """(permission bits, uid, gid) of ``path`` (not following a link)."""

        def local(p: Path) -> tuple[int, int, int]:
            st = p.lstat()
            return st.st_mode & 0o7777, st.st_uid, st.st_gid

        direct, value = self._direct(local, path)
        if direct:
            return value
        proc = agent_exec("stat", "-c", "%a %u %g", str(path))
        assert proc.returncode == 0, proc.stderr
        mode, uid, gid = proc.stdout.split()
        return int(mode, 8), int(uid), int(gid)

    def owners(self, top: Path) -> list[tuple[str, int, int]]:
        """(path, uid, gid) of ``top`` and, for a directory, everything below it (links not followed)."""

        def local(t: Path) -> list[tuple[str, int, int]]:
            paths = [t, *(t.rglob("*") if t.is_dir() and not t.is_symlink() else [])]
            return [(str(p), p.lstat().st_uid, p.lstat().st_gid) for p in paths]

        direct, value = self._direct(local, top)
        if direct:
            return value
        proc = agent_exec("find", str(top), "-printf", "%p\t%U\t%G\n")
        assert proc.returncode == 0, proc.stderr
        return [(p, int(u), int(g)) for p, u, g in (line.split("\t") for line in proc.stdout.splitlines() if line)]

    def names_below(self, top: Path) -> list[str]:
        def local(t: Path) -> list[str]:
            return [p.name for p in t.rglob("*")]

        direct, value = self._direct(local, top)
        if direct:
            return value
        proc = agent_exec("find", str(top), "-mindepth", "1", "-printf", "%f\n")
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.splitlines()


disk = AgentDisk()


def runtime_owner(node: Any) -> tuple[int, int]:
    """The uid/gid the agent gives server files (the itzg runtime user)."""
    from apps.nodes.client import AgentClient

    policy = AgentClient(node).info()["policy"]
    return int(policy["runtime_uid"]), int(policy["runtime_gid"])


def wrong_owner(paths: list[Path], owner: tuple[int, int]) -> list[str]:
    """The given paths (directories recursively) that are not owned by ``owner`` (``disk``)."""
    return [f"{p} is {uid}:{gid}" for top in paths for p, uid, gid in disk.owners(top) if (uid, gid) != owner]


def container_names(instance_id: uuid.UUID) -> str | None:
    """Names of Docker containers for the instance (None when the docker CLI is unusable here)."""
    proc = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=mcpanel-{instance_id.hex[:12]}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


class InstanceTracker:
    """Creates ``it-…`` instances and removes their container, files and backups afterwards."""

    def __init__(self, data_root: str) -> None:
        self.data_root = data_root
        self.created: list[tuple[uuid.UUID, Any]] = []

    def path(self, *parts: str) -> Path | None:
        """A path under the agent's data root as seen from here (None if not configured)."""
        return Path(self.data_root, *parts) if self.data_root else None

    def instance_dir(self, instance: Any) -> Path | None:
        return self.path("instances", str(instance.id))

    def create(self, node: Any, kind: str, ports: tuple[int, int], **fields: Any) -> Any:
        from apps.instances.models import Instance
        from apps.instances.spec import image_for

        java = fields.pop("java_version", 21)  # Mojang: 1.21.x requires Java 21
        values: dict[str, Any] = {
            "name": f"it-{kind}-{uuid.uuid4().hex[:6]}",
            "node": node,
            "server_type": "VANILLA",
            "minecraft_version": "1.21.1",
            "java_version": java,
            "image": image_for(java),
            "memory_mb": 1024,
            "jvm_flags_preset": "none",
            "stop_timeout": 60,
            "host_ip": BIND_IP,
            "game_port": pick_port(node, ports),
        }
        values.update(fields)
        instance = Instance.objects.create(**values)
        self.created.append((instance.id, node))
        print(f"[it] instance {instance.name} ({instance.id}) on {node.name}, port {instance.game_port}", flush=True)
        return instance

    def cleanup(self) -> None:
        """Best effort, whatever state the test left: agent delete, then docker rm as a fallback."""
        from apps.nodes.client import AgentClient

        for instance_id, node in self.created:
            client = AgentClient(node)
            try:
                op = client.delete_data(instance_id)  # idempotent: container, files, restic repo
                client.wait_operation(op["id"], timeout=300)
            except Exception as exc:
                print(f"[it] cleanup via agent failed for {instance_id}: {exc}; falling back to docker rm", flush=True)
                subprocess.run(["docker", "rm", "-f", f"mcpanel-{instance_id.hex[:12]}"], capture_output=True, check=False)
