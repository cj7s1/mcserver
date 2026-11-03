"""Docker container lifecycle for Minecraft instances.

Only containers carrying the ``io.mcpanel.managed`` label are ever touched.
All containers are created from an :class:`InstanceSpec`, which deliberately
cannot express privileged mode, extra capabilities, devices or extra mounts.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container

from .config import Settings
from .schemas import InstanceSpec

log = logging.getLogger(__name__)

LABEL_MANAGED = "io.mcpanel.managed"
LABEL_INSTANCE = "io.mcpanel.instance"
LABEL_NAME = "io.mcpanel.name"
LABEL_SPEC_HASH = "io.mcpanel.spec-hash"
LABEL_STOP_TIMEOUT = "io.mcpanel.stop-timeout"
GAME_PORT = 25565
RCON_PORT = 25575


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
            LABEL_NAME: spec.name[:100],
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
    try:
        return client().containers.create(**kwargs)
    except APIError as exc:
        raise AgentError(
            f"Docker refused to create the container: {exc.explanation or exc}", "create_failed", 502
        ) from exc


def remove_container(instance_id: UUID, *, timeout: int = 60) -> bool:
    c = get_container(instance_id)
    if c is None:
        return False
    c.reload()
    if c.status in ("running", "restarting", "paused"):
        c.stop(timeout=timeout)
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


def stop(instance_id: UUID, timeout: int | None) -> None:
    c = require_container(instance_id)
    c.reload()
    if c.status in ("running", "restarting", "paused"):
        c.stop(timeout=timeout or default_stop_timeout(c))


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
