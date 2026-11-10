"""Docker container lifecycle for Minecraft instances.

Only containers carrying the ``io.mcpanel.managed`` label are ever touched.
All containers are created from an :class:`InstanceSpec`, which deliberately
cannot express privileged mode, extra capabilities, devices or extra mounts.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
from docker.errors import APIError, ImageNotFound, NotFound

# _create_container_args is docker-py's own (private) conversion from containers.create()
# arguments to the low-level API call; see create_container() for why it is used directly.
from docker.models.containers import Container, _create_container_args

from .config import Settings, get_settings
from .rcon import RconError, RconUnavailable, run_command_sync
from .schemas import InstanceSpec

log = logging.getLogger(__name__)

LABEL_MANAGED = "io.mcpanel.managed"
LABEL_INSTANCE = "io.mcpanel.instance"
LABEL_NAME = "io.mcpanel.name"
LABEL_SPEC_HASH = "io.mcpanel.spec-hash"
LABEL_STOP_TIMEOUT = "io.mcpanel.stop-timeout"
GAME_PORT = 25565
RCON_PORT = 25575

# Graceful stop (see graceful_stop). Seconds.
RCON_PROBE_TIMEOUT = 3.0  # connect + login: "does RCON answer quickly?"
RCON_STOP_INTERVAL = 2.0  # between tries to deliver `stop` while `docker stop` is in progress
RCON_STOP_COMMAND_TIMEOUT = 5.0  # waiting for the server's answer to `stop`


class AgentError(Exception):
    def __init__(self, message: str, code: str = "agent_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


_client_lock = threading.Lock()
_client: docker.DockerClient | None = None


def client() -> docker.DockerClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = docker.from_env(timeout=120)
        return _client


def container_name(instance_id: UUID) -> str:
    return f"mcpanel-{instance_id.hex[:12]}"


def instance_dir(settings: Settings, instance_id: UUID) -> Path:
    return settings.instances_dir / str(instance_id)


def host_instance_dir(settings: Settings, instance_id: UUID) -> Path:
    return settings.host_instances_dir / str(instance_id)


# --- images ----------------------------------------------------------------------
def split_image(image: str) -> tuple[str, str]:
    if "@" in image:
        raise AgentError("Image digests are not supported; use a tag", "invalid_image")
    repo, _, tag = image.rpartition(":")
    if not repo or "/" in tag:
        repo, tag = image, "latest"
    return repo, tag


def validate_image(settings: Settings, image: str) -> str:
    repo, tag = split_image(image)
    if repo not in settings.allowed_images:
        raise AgentError(
            f"Image repository '{repo}' is not in the agent allow-list", "image_not_allowed", 403
        )
    from .schemas import IMAGE_TAG_RE

    if not IMAGE_TAG_RE.match(tag):
        raise AgentError("Invalid image tag", "invalid_image")
    return f"{repo}:{tag}"


def image_present(image: str) -> bool:
    try:
        client().images.get(image)
        return True
    except ImageNotFound:
        return False


def pull_image(image: str, progress: Any = None) -> None:
    repo, tag = split_image(image)
    api = client().api
    layers: dict[str, tuple[int, int]] = {}
    for event in api.pull(repo, tag=tag, stream=True, decode=True):
        if "error" in event:
            raise AgentError(f"Image pull failed: {event['error']}", "pull_failed", 502)
        detail = event.get("progressDetail") or {}
        if event.get("id") and detail.get("total"):
            layers[event["id"]] = (detail.get("current", 0), detail["total"])
        if progress and layers:
            cur = sum(c for c, _ in layers.values())
            tot = sum(t for _, t in layers.values()) or 1
            progress(min(cur / tot, 1.0), f"Pulling {image}: {event.get('status', '')}")


# --- networks ----------------------------------------------------------------------
def ensure_network(settings: Settings) -> None:
    name = settings.instance_network
    try:
        client().networks.get(name)
    except NotFound:
        log.info("Creating instance network %s", name)
        client().networks.create(name, driver="bridge", labels={LABEL_MANAGED: "true"})


# --- containers ----------------------------------------------------------------------
def get_container(instance_id: UUID) -> Container | None:
    try:
        c = client().containers.get(container_name(instance_id))
    except NotFound:
        return None
    if c.labels.get(LABEL_MANAGED) != "true" or c.labels.get(LABEL_INSTANCE) != str(instance_id):
        raise AgentError(
            "A container with the expected name exists but is not managed by MCPanel",
            "foreign_container",
            409,
        )
    return c


def require_container(instance_id: UUID) -> Container:
    c = get_container(instance_id)
    if c is None:
        raise AgentError("The instance has no container; apply its configuration first", "no_container", 404)
    return c


def label_safe_name(name: str) -> str:
    """The server name as stored in the ``io.mcpanel.name`` label: without ``=``.

    Unraid decides which containers are its own with a plain text search for
    ``net.unraid.docker.managed=`` over the names and all label text of ``docker ps``
    (rc.docker), label values included. Containers that match are stopped by Unraid itself
    at array stop, with Unraid's timeout, and then count as manually stopped. A value
    without ``=`` can never produce that text, whatever the server is called.
    """
    return name[:100].replace("=", "-")


def build_create_kwargs(
    settings: Settings, instance_id: UUID, spec: InstanceSpec, spec_hash: str
) -> dict[str, Any]:
    image = validate_image(settings, spec.image)
    network = spec.network or settings.instance_network
    if network not in settings.effective_networks():
        raise AgentError(f"Network '{network}' is not permitted by agent policy", "network_not_allowed", 403)
    if spec.memory_limit_mb > settings.max_memory_mb:
        raise AgentError(
            f"Memory limit exceeds agent maximum of {settings.max_memory_mb} MB", "memory_limit", 400
        )

    env: dict[str, str] = dict(settings.runtime_extra_env)
    env.update(spec.env)
    env.update(
        {
            "EULA": spec.env.get("EULA", "TRUE"),
            "UID": str(settings.runtime_uid),
            "GID": str(settings.runtime_gid),
            "ENABLE_RCON": "true",
            "RCON_PASSWORD": spec.rcon_password,
            "RCON_PORT": str(RCON_PORT),
            "SERVER_PORT": str(GAME_PORT),
        }
    )

    volumes: dict[str, dict[str, str]] = {
        str(host_instance_dir(settings, instance_id)): {"bind": "/data", "mode": "rw"},
    }
    if settings.runtime_ca_bundle:
        volumes[str(settings.runtime_ca_bundle)] = {"bind": "/mcpanel/certs/ca-bundle.crt", "mode": "ro"}
    if settings.runtime_java_truststore:
        volumes[str(settings.runtime_java_truststore)] = {"bind": "/mcpanel/certs/cacerts", "mode": "ro"}

    ports: dict[str, Any] = {}
    if network != "host":
        for p in spec.ports:
            if not (settings.port_min <= p.host_port <= settings.port_max):
                raise AgentError(
                    f"Host port {p.host_port} is outside the permitted range {settings.port_min}-{settings.port_max}",
                    "port_not_allowed",
                    403,
                )
            key = f"{p.container_port}/{p.protocol}"
            ports.setdefault(key, []).append((p.host_ip, p.host_port))

    kwargs: dict[str, Any] = {
        "image": image,
        "name": container_name(instance_id),
        "hostname": f"mc-{instance_id.hex[:8]}",
        "environment": env,
        "labels": {
            LABEL_MANAGED: "true",
            LABEL_INSTANCE: str(instance_id),
            LABEL_NAME: label_safe_name(spec.name),
            LABEL_SPEC_HASH: spec_hash,
            LABEL_STOP_TIMEOUT: str(spec.stop_timeout),
        },
        "volumes": volumes,
        "ports": ports,
        "network": network,
        "mem_limit": f"{spec.memory_limit_mb}m",
        "memswap_limit": f"{spec.memory_limit_mb}m",
        "pids_limit": settings.pids_limit,
        "restart_policy": {
            "Name": spec.restart_policy,
            **({"MaximumRetryCount": 3} if spec.restart_policy == "on-failure" else {}),
        },
        "stop_signal": "SIGTERM",
        # Docker's own stop timeout (Config.StopTimeout): what the server gets when Docker stops
        # it without the panel (host shutdown, daemon restart, `docker restart`); without it that
        # is Docker's default of 10 s. Capped by AGENT_SHUTDOWN_STOP_TIMEOUT because Unraid kills
        # the Docker daemon 30 s after asking it to stop, and the daemon waits
        # max(StopTimeout of its containers) + 5 s for them. Stops from the panel pass the full
        # spec.stop_timeout themselves (kept in the stop-timeout label as the agent's default).
        # Not an argument of docker-py's containers.create(): create_container() takes it out.
        "stop_timeout": min(spec.stop_timeout, settings.shutdown_stop_timeout),
        "stdin_open": True,
        "tty": False,
        "detach": True,
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        # The itzg entrypoint starts as root to fix ownership, then drops to UID/GID.
        "cap_add": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL"],
        "log_config": {
            "Type": "json-file",
            "Config": {"max-size": settings.log_max_size, "max-file": str(settings.log_max_files)},
        },
    }
    hc = spec.healthcheck
    kwargs["healthcheck"] = {
        "test": ["CMD", "mc-health"],
        "interval": hc.interval_s * 1_000_000_000,
        "timeout": hc.timeout_s * 1_000_000_000,
        "start_period": hc.start_period_s * 1_000_000_000,
        "retries": hc.retries,
    }
    if spec.cpu_limit:
        kwargs["nano_cpus"] = int(spec.cpu_limit * 1e9)
    return kwargs


def create_container(settings: Settings, instance_id: UUID, spec: InstanceSpec, spec_hash: str) -> Container:
    kwargs = build_create_kwargs(settings, instance_id, spec, spec_hash)
    if kwargs["network"] == settings.instance_network:
        ensure_network(settings)
    # docker-py's high-level containers.create() cannot set Config.StopTimeout: it rejects a
    # stop_timeout keyword with a TypeError (docker 7.x), only the low-level create_container()
    # has it. So this does exactly what containers.create() does internally (same argument
    # conversion, same API call, same lookup of the result) plus stop_timeout.
    # tests/test_docker_ops.py compares the two requests on the wire.
    stop_timeout = kwargs.pop("stop_timeout")
    docker_client = client()
    try:
        low_level = _create_container_args(
            {**kwargs, "command": None, "version": docker_client.api.api_version}
        )
        created = docker_client.api.create_container(**low_level, stop_timeout=stop_timeout)
        return docker_client.containers.get(created["Id"])
    except APIError as exc:
        raise AgentError(
            f"Docker refused to create the container: {exc.explanation or exc}", "create_failed", 502
        ) from exc


def remove_container(instance_id: UUID, *, timeout: int = 60, settings: Settings | None = None) -> bool:
    c = get_container(instance_id)
    if c is None:
        return False
    graceful_stop(settings or get_settings(), c, timeout)  # its warnings are in the agent log
    c.remove(v=False)
    return True


_BUILTIN_NETWORKS = frozenset({"host", "none", "bridge"})


def reattach_recreated_networks(c: Container) -> list[str]:
    """Point a stopped container at the current network of every name it is attached to.

    Docker lets a network be removed while only *stopped* containers use it (for example
    ``docker compose down`` of the MCPanel stack while every server is stopped, or
    ``docker network prune``). A container that has run before still holds the old
    network ID, so once a network with the same name exists again ``start`` fails with
    "network <id> not found". Disconnecting and reconnecting by name repairs that; nothing
    else about the container changes. Returns the names of the repaired networks.
    """
    c.reload()
    if c.status not in ("created", "exited", "dead"):
        return []
    networks = (c.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    repaired: list[str] = []
    for name, endpoint in networks.items():
        old_id = (endpoint or {}).get("NetworkID") or ""
        if not old_id or name in _BUILTIN_NETWORKS:
            continue  # never started (resolved by name at start) or a built-in network
        try:
            current = client().networks.get(name)
        except NotFound:
            continue  # nothing to reattach to; start() reports the missing network
        if current.name != name or current.id == old_id:
            continue
        log.warning("Network %s was recreated; reconnecting container %s to it", name, c.name)
        try:
            client().api.disconnect_container_from_network(c.id, name, force=True)
            client().api.connect_container_to_network(c.id, name)
        except APIError as exc:
            raise AgentError(
                f"Could not reconnect the container to network '{name}': {exc.explanation or exc}",
                "start_failed",
                502,
            ) from exc
        repaired.append(name)
    return repaired


def start(instance_id: UUID) -> None:
    c = require_container(instance_id)
    reattach_recreated_networks(c)
    try:
        c.start()
    except APIError as exc:
        text = str(exc.explanation or exc)
        code = (
            "port_conflict"
            if ("port is already allocated" in text or "address already in use" in text)
            else "start_failed"
        )
        raise AgentError(
            f"Docker could not start the container: {text}", code, 409 if code == "port_conflict" else 502
        ) from exc


def default_stop_timeout(c: Container) -> int:
    try:
        return int(c.labels.get(LABEL_STOP_TIMEOUT, "60"))
    except ValueError:
        return 60


def flush_timeout(stop_timeout: int) -> float:
    """How long ``save-all flush`` may take before a stop: half the stop timeout, 10 to 60 s."""
    return float(min(60, max(10, stop_timeout / 2)))


@dataclass
class StopResult:
    """What a graceful stop did besides ``docker stop``; the panel logs it."""

    flushed: bool = False  # the server answered `save-all flush` before the stop
    stop_sent_via_rcon: bool = False  # the agent delivered `stop` itself (RCON was not up at first)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "flushed": self.flushed,
            "stop_sent_via_rcon": self.stop_sent_via_rcon,
            "warnings": list(self.warnings),
        }


def _flush_before_stop(target: tuple[str, int, str], bound: float, result: StopResult) -> bool:
    """``save-all flush`` if RCON answers quickly. Returns whether RCON answered (accepted the login).

    Never raises: whatever goes wrong here is a warning and the stop goes on.
    """
    host, port, password = target
    try:
        run_command_sync(
            host, port, password, "save-all flush", timeout=bound, connect_timeout=RCON_PROBE_TIMEOUT
        )
    except RconUnavailable as exc:
        result.warnings.append(
            "The world was not flushed before the stop: RCON is not available "
            f"(the server is probably still starting): {exc}"
        )
        return False
    except RconError as exc:
        result.warnings.append(f"save-all flush before the stop failed, the world may not be saved: {exc}")
        return True
    except Exception as exc:
        log.exception("Unexpected error while flushing the world before a stop")
        result.warnings.append(f"The world was not flushed before the stop: {exc}")
        return False  # unknown; the courier below then makes sure `stop` arrives
    result.flushed = True
    return True


class _RconStopCourier:
    """Delivers ``stop`` through the agent's own RCON client while ``docker stop`` is in progress.

    For a server that had not opened RCON yet when the stop began: the itzg runner
    (mc-server-runner) tries ``rcon-cli stop`` once when it gets SIGTERM, and its console
    fallback cannot work in these containers (their stdin is a read-only pipe). The stop
    would be lost: the server finishes starting and is killed STOP_DURATION later without
    saving. So a helper thread keeps trying every RCON_STOP_INTERVAL seconds and sends
    ``stop`` once, as soon as the server accepts an RCON login.

    The first try comes one interval after the thread was started, which is right before
    ``docker stop`` is sent: Docker must have registered the stop before the server exits, or
    the restart policy would start it again. The thread never touches the caller's Container
    object, and it sends nothing after :meth:`finish` returned (a restart may start the server
    again right after it).
    """

    def __init__(self, container_id: str, name: str, target: tuple[str, int, str]) -> None:
        self._container_id = container_id
        self._target = target
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._sent = False
        self._warning: str | None = None
        self._thread = threading.Thread(target=self._run, name=f"rcon-stop-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _container_running(self) -> bool:
        try:
            state = client().api.inspect_container(self._container_id).get("State") or {}
        except NotFound:
            return False
        except Exception:
            # Docker did not say. `docker stop` returning ends the loop in any case, and the
            # RCON login only fits this server, so trying is the safer side.
            return True
        return bool(state.get("Running"))

    def _run(self) -> None:
        host, port, password = self._target
        try:
            while not self._done.wait(RCON_STOP_INTERVAL):
                if not self._container_running() or self._done.is_set():
                    return
                try:
                    run_command_sync(
                        host,
                        port,
                        password,
                        "stop",
                        timeout=RCON_STOP_COMMAND_TIMEOUT,
                        connect_timeout=RCON_PROBE_TIMEOUT,
                    )
                except RconUnavailable:
                    continue  # RCON is not open yet
                except RconError as exc:
                    # Logged in and sent. A server that is shutting down often drops the
                    # connection before it answers, so this counts as delivered.
                    with self._lock:
                        self._sent = True
                        self._warning = f"stop was sent through RCON but the server did not confirm it: {exc}"
                    return
                with self._lock:
                    self._sent = True
                return
        except Exception:
            log.exception("RCON stop helper failed")

    def finish(self, result: StopResult) -> None:
        """Called when ``docker stop`` has returned (or failed): end the loop and collect the outcome."""
        self._done.set()
        # A try that is in flight is bounded by the two RCON timeouts (+1 s backstop); wait for
        # it, so that nothing is sent after this returns.
        self._thread.join(RCON_PROBE_TIMEOUT + RCON_STOP_COMMAND_TIMEOUT + 3.0)
        with self._lock:
            result.stop_sent_via_rcon = self._sent
            if self._warning:
                result.warnings.append(self._warning)


def graceful_stop(settings: Settings, c: Container, timeout: int | None) -> StopResult:
    """Stop a container the way the panel means it: save first, then stop, and make sure it arrives.

    1. Running and RCON answers quickly: ``save-all flush``, waiting up to flush_timeout() for
       the answer. Some modpacks abort their own shutdown save, so the stop must not be the
       only save. A failure is a warning, never an error.
    2. ``docker stop`` with the requested timeout. Always, also when the agent delivers the
       stop command itself: it is what tells Docker the stop is intended, so the restart policy
       does not start the server again.
    3. If RCON did not answer in step 1, :class:`_RconStopCourier` delivers ``stop`` as soon as
       it does, until the container has exited or ``docker stop`` has returned.

    A container that is not running is left alone. Docker errors of ``docker stop`` propagate.
    """
    result = StopResult()
    c.reload()
    if c.status not in ("running", "restarting", "paused"):
        return result
    timeout = timeout or default_stop_timeout(c)
    courier: _RconStopCourier | None = None
    if c.status == "running":  # restarting / paused: no server that could answer
        try:
            target = _rcon_target(settings, c)
        except AgentError as exc:
            target = None
            result.warnings.append(f"The world was not flushed before the stop: {exc}")
        if target is not None and not _flush_before_stop(target, flush_timeout(timeout), result):
            courier = _RconStopCourier(c.id, c.name, target)
    if courier is not None:
        courier.start()
    try:
        c.stop(timeout=timeout)
    finally:
        if courier is not None:
            courier.finish(result)
    for warning in result.warnings:
        log.warning("Stopping %s: %s", c.name, warning)
    return result


def stop(instance_id: UUID, timeout: int | None, settings: Settings | None = None) -> StopResult:
    c = require_container(instance_id)
    return graceful_stop(settings or get_settings(), c, timeout)


def kill(instance_id: UUID) -> None:
    c = require_container(instance_id)
    c.reload()
    if c.status in ("running", "restarting", "paused"):
        c.kill()


def _parse_ts(value: str | None) -> str | None:
    if not value or value.startswith("0001-"):
        return None
    return value


def describe(c: Container) -> dict[str, Any]:
    attrs = c.attrs
    state = attrs.get("State", {})
    health = state.get("Health") or {}
    net_settings = attrs.get("NetworkSettings", {}) or {}
    networks = net_settings.get("Networks") or {}
    ip = None
    for net in networks.values():
        if net.get("IPAddress"):
            ip = net["IPAddress"]
            break
    ports = []
    for key, bindings in (net_settings.get("Ports") or {}).items():
        cport, _, proto = key.partition("/")
        for b in bindings or []:
            ports.append(
                {
                    "container_port": int(cport),
                    "protocol": proto,
                    "host_ip": b.get("HostIp"),
                    "host_port": int(b.get("HostPort")),
                }
            )
    health_log = health.get("Log") or []
    return {
        "exists": True,
        "id": c.id,
        "name": c.name,
        "image": attrs.get("Config", {}).get("Image"),
        "status": state.get("Status"),
        "running": bool(state.get("Running")),
        "restarting": bool(state.get("Restarting")),
        "exit_code": state.get("ExitCode"),
        "oom_killed": bool(state.get("OOMKilled")),
        "error": state.get("Error") or None,
        "started_at": _parse_ts(state.get("StartedAt")),
        "finished_at": _parse_ts(state.get("FinishedAt")),
        "health": health.get("Status"),
        "health_output": (health_log[-1].get("Output", "")[-500:] if health_log else None),
        "restart_count": attrs.get("RestartCount", 0),
        "spec_hash": c.labels.get(LABEL_SPEC_HASH),
        "ip_address": ip,
        "ports": ports,
        "created_at": attrs.get("Created"),
    }


def status(instance_id: UUID) -> dict[str, Any]:
    c = get_container(instance_id)
    if c is None:
        return {"exists": False}
    c.reload()
    return describe(c)


def list_managed() -> list[dict[str, Any]]:
    out = []
    for c in client().containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"}):
        info = describe(c)
        info["instance_id"] = c.labels.get(LABEL_INSTANCE)
        out.append(info)
    return out


def published_ports() -> list[dict[str, Any]]:
    """Host ports published by *any* container on this Docker host (for conflict checks)."""
    used = []
    for c in client().containers.list(all=False):
        for key, bindings in (c.attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
            _cport, _, proto = key.partition("/")
            for b in bindings or []:
                used.append(
                    {
                        "host_ip": b.get("HostIp"),
                        "host_port": int(b.get("HostPort")),
                        "protocol": proto,
                        "container": c.name,
                        "managed": c.labels.get(LABEL_MANAGED) == "true",
                        "instance_id": c.labels.get(LABEL_INSTANCE),
                    }
                )
    return used


def stats(instance_id: UUID) -> dict[str, Any]:
    c = require_container(instance_id)
    c.reload()
    if c.status != "running":
        return {"running": False}
    s = c.stats(stream=False)
    cpu = s.get("cpu_stats", {})
    pre = s.get("precpu_stats", {})
    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get(
        "total_usage", 0
    )
    sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
    online = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1
    cpu_percent = (cpu_delta / sys_delta) * online * 100.0 if sys_delta > 0 and cpu_delta >= 0 else 0.0
    mem = s.get("memory_stats", {})
    mem_stats = mem.get("stats", {})
    cache = mem_stats.get("inactive_file", mem_stats.get("total_inactive_file", mem_stats.get("cache", 0)))
    mem_usage = max(mem.get("usage", 0) - cache, 0)
    rx = tx = 0
    for net in (s.get("networks") or {}).values():
        rx += net.get("rx_bytes", 0)
        tx += net.get("tx_bytes", 0)
    blk_read = blk_write = 0
    for entry in s.get("blkio_stats", {}).get("io_service_bytes_recursive") or []:
        if entry.get("op", "").lower() == "read":
            blk_read += entry.get("value", 0)
        elif entry.get("op", "").lower() == "write":
            blk_write += entry.get("value", 0)
    return {
        "running": True,
        "timestamp": datetime.now().astimezone().isoformat(),
        "cpu_percent": round(cpu_percent, 2),
        "cpu_count": online,
        "memory_bytes": mem_usage,
        "memory_limit_bytes": mem.get("limit", 0),
        "network_rx_bytes": rx,
        "network_tx_bytes": tx,
        "block_read_bytes": blk_read,
        "block_write_bytes": blk_write,
        "pids": s.get("pids_stats", {}).get("current"),
    }


def logs(instance_id: UUID, tail: int = 500, since: int | None = None) -> str:
    c = require_container(instance_id)
    kwargs: dict[str, Any] = {"tail": tail, "timestamps": False, "stdout": True, "stderr": True}
    if since:
        kwargs["since"] = since
    return c.logs(**kwargs).decode("utf-8", errors="replace")


def rcon_endpoint(settings: Settings, instance_id: UUID) -> tuple[str, int, str]:
    """Return (host, port, password) for the instance's RCON listener.

    The password is read from the container's own configuration so it never has to
    travel in agent API requests.
    """
    c = require_container(instance_id)
    c.reload()
    if c.status != "running":
        raise AgentError("The server is not running", "not_running", 409)
    return _rcon_target(settings, c)


def _rcon_target(settings: Settings, c: Container) -> tuple[str, int, str]:
    """(host, port, password) from the attributes of a running container that was just reloaded."""
    password = ""
    for item in c.attrs.get("Config", {}).get("Env") or []:
        if item.startswith("RCON_PASSWORD="):
            password = item.split("=", 1)[1]
    if not password:
        raise AgentError("Container has no RCON password configured", "rcon_unavailable", 409)
    info = describe(c)
    if settings.rcon_mode == "published":
        for p in info["ports"]:
            if p["container_port"] == RCON_PORT:
                return "127.0.0.1", p["host_port"], password
        raise AgentError("RCON port is not published", "rcon_unavailable", 409)
    if c.attrs.get("HostConfig", {}).get("NetworkMode") == "host":
        return "127.0.0.1", RCON_PORT, password
    if not info["ip_address"]:
        raise AgentError("Container has no IP address", "rcon_unavailable", 409)
    return info["ip_address"], RCON_PORT, password


def docker_info() -> dict[str, Any]:
    info = client().info()
    version = client().version()
    return {
        "server_version": version.get("Version"),
        "api_version": version.get("ApiVersion"),
        "os": info.get("OperatingSystem"),
        "kernel": info.get("KernelVersion"),
        "architecture": info.get("Architecture"),
        "cpus": info.get("NCPU"),
        "memory_bytes": info.get("MemTotal"),
        "storage_driver": info.get("Driver"),
        "cgroup_version": info.get("CgroupVersion"),
        "containers_running": info.get("ContainersRunning"),
        "name": info.get("Name"),
    }
