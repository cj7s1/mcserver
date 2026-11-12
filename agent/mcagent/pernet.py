"""One bridge network per server (A-1 of the security review of 2026-11-11, interface I-6).

On the shared instance network every game container reached every other one on every port. Now a spec
without a network (what the panel sends by default) gets its own bridge ``zz_mcpanel_i_<first 12 hex of the
instance id>`` with a /29 of ``AGENT_INSTANCE_POOL`` (default 10.213.0.0/16). The agent attaches its own
container to it (it reaches RCON and the games' APIs there); Docker isolates bridges from each other, so a
server no longer sees its neighbours.

* The subnet of a server is recorded in the agent's state folder (``server-networks.json``): the server
  keeps it when its network is created again. A subnet Docker refuses (it overlaps another network) is
  skipped and remembered.
* Unraid deletes user-defined networks at a Docker restart unless "Preserve user defined networks" is
  on, and compose recreates the agent's container at every upgrade: at the agent's start (:func:`startup`)
  and before every start of a server (``docker_ops.reattach_recreated_networks``) a missing server
  network is created again with its name and subnet, and the agent attaches itself to every server
  network.
* Nothing changes for a container that exists on the shared network: it keeps it until it is recreated.
* Fallback, never a dead server: switched off (``AGENT_PER_SERVER_NETWORKS=false``), an invalid pool, an
  agent that does not run in a container or cannot find its own container, an exhausted pool or a network
  Docker refuses: the server goes on the shared instance network as before; the reason is logged once and
  reported in ``GET /v1/info`` (:func:`status`).
* A server network is removed with the last container on it (``release``: retire, remove) and by the
  start-up sweep; a network without the agent's labels is never touched.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

from docker.errors import APIError, NotFound
from docker.types import IPAMConfig, IPAMPool

from .config import Settings
from .errors import AgentError

log = logging.getLogger(__name__)

LABEL_MANAGED = "io.mcpanel.managed"
LABEL_INSTANCE = "io.mcpanel.instance"
LABEL_NETWORK = "io.mcpanel.network"  # "server": a per-server network of this agent
# The agent's default route must stay on the networks compose gave it. Docker gives a container's default
# route to the network with the highest gateway priority, then to the one whose name sorts first. The agent
# joins every server network with the lowest priority (Engine API 1.48+), and for older daemons the prefix
# sorts after every compose network name ("<project>_<network>"; "z" is the last character a network name
# can start with).
NETWORK_PREFIX = "zz_mcpanel_i_"
_NAME_RE = re.compile(r"^zz_mcpanel_i_[0-9a-f]{12}$")
GW_PRIORITY = -1000  # compose networks have 0: a server network never carries the agent's default route
GW_PRIORITY_API = (1, 48)
SUBNET_PREFIX = 29
MIN_POOL_PREFIX = 28  # at least two server subnets
STATE_FILE = "server-networks.json"
MAX_CREATE_TRIES = 16  # subnets Docker refuses before the pool counts as exhausted

_lock = threading.RLock()  # allocation, the state file, network create/remove
_status: dict[str, Any] = {"reason": None, "fallbacks": 0}
_logged: set[str] = set()
_own: dict[str, str | None] = {}


class Unavailable(Exception):
    """A server network cannot be used for this server; it goes on the shared network."""


def _client() -> Any:
    from . import docker_ops

    return docker_ops.client()


def network_name(instance_id: UUID) -> str:
    return f"{NETWORK_PREFIX}{instance_id.hex[:12]}"


def is_server_network(name: str | None) -> bool:
    return bool(name) and _NAME_RE.match(str(name)) is not None


def pool(settings: Settings) -> ipaddress.IPv4Network:
    """The configured pool; ValueError with the reason when it cannot be used."""
    try:
        net = ipaddress.ip_network(str(settings.instance_pool).strip(), strict=True)
    except ValueError as exc:
        raise ValueError(f"AGENT_INSTANCE_POOL {settings.instance_pool!r} is not a network: {exc}") from None
    if not isinstance(net, ipaddress.IPv4Network) or not net.is_private:
        raise ValueError(f"AGENT_INSTANCE_POOL {net} is not a private IPv4 range")
    if net.prefixlen > MIN_POOL_PREFIX:
        raise ValueError(f"AGENT_INSTANCE_POOL {net} is too small (at least a /{MIN_POOL_PREFIX})")
    return net


def pool_or_none(settings: Settings) -> ipaddress.IPv4Network | None:
    try:
        return pool(settings)
    except ValueError:
        return None


# --- the agent's own container -------------------------------------------------------------------------
_MOUNT_ID_RE = re.compile(r"/containers/([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)")


def _candidate_ids(
    mountinfo: Path = Path("/proc/self/mountinfo"), cgroup: Path = Path("/proc/self/cgroup")
) -> list[str]:
    """Ids this process's container may have: its hostname (Docker's default is the short id), the
    container folder of the /etc/hostname, /etc/hosts and /etc/resolv.conf mounts, a 64-hex id in its
    cgroup path."""
    out: list[str] = []
    with contextlib.suppress(OSError):
        out.append(socket.gethostname())
    with contextlib.suppress(OSError):
        for line in mountinfo.read_text().splitlines():
            fields = line.split()
            if len(fields) > 4 and fields[4] in ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf"):
                if m := _MOUNT_ID_RE.search(line):
                    out.append(m.group(1))
    with contextlib.suppress(OSError):
        out.extend(re.findall(r"(?<![0-9a-f])([0-9a-f]{64})(?![0-9a-f])", cgroup.read_text()))
    return list(dict.fromkeys(c for c in out if c))


def _is_me(c: Any) -> bool:
    try:
        hostname = socket.gethostname()
    except OSError:
        return False
    attrs = getattr(c, "attrs", None) or {}
    state = attrs.get("State") or {}
    return (attrs.get("Config") or {}).get("Hostname") == hostname and bool(state.get("Running"))


def own_container_id(*, refresh: bool = False) -> str | None:
    """The id of the container this agent runs in; None outside a container or when it cannot be told.
    Every candidate is checked with Docker (its hostname is this process's and it runs); the compose
    labels (service ``agent``) are the last resort, used only when exactly one container fits."""
    if not refresh and "id" in _own:
        return _own["id"]
    found: str | None = None
    try:
        docker = _client()
        for cand in _candidate_ids():
            try:
                c = docker.containers.get(cand)
            except NotFound:
                continue
            if _is_me(c):
                found = str(c.id)
                break
        if found is None:
            matches = [
                c
                for c in docker.containers.list(filters={"label": "com.docker.compose.service=agent"})
                if _is_me(c)
            ]
            if len(matches) == 1:
                found = str(matches[0].id)
    except Exception as exc:  # Docker not reachable: ask again next time
        log.debug("could not look up the agent's own container: %s", exc)
        return None
    _own["id"] = found
    return found


# --- availability and status ---------------------------------------------------------------------------
def availability(settings: Settings) -> str | None:
    """None when server networks can be used; else why not."""
    if not settings.per_server_networks:
        return "switched off (AGENT_PER_SERVER_NETWORKS=false)"
    try:
        pool(settings)
    except ValueError as exc:
        return str(exc)
    if own_container_id() is None:
        return (
            "the agent does not run in a container, or could not find its own container, so it cannot "
            "join a server's network"
        )
    return None


def _fallback(instance_id: UUID | None, reason: str) -> None:
    with _lock:
        _status["reason"] = reason
        _status["fallbacks"] = int(_status.get("fallbacks") or 0) + 1
        first = reason not in _logged
        _logged.add(reason)
    if first:
        log.warning(
            "Servers go on the shared instance network (one network per server is not used%s): %s",
            f", e.g. for {instance_id}" if instance_id else "",
            reason,
        )


def status(settings: Settings) -> dict[str, Any]:
    """``GET /v1/info``: ``enabled`` (new servers get their own network) and ``reason`` (why not, or why
    a server last fell back to the shared network)."""
    reason = availability(settings)
    with _lock:
        last = _status.get("reason")
    return {
        "enabled": reason is None,
        "reason": reason or last,
        "prefix": NETWORK_PREFIX,  # every server network is named <prefix><first 12 hex of the id>
        "pool": str(settings.instance_pool),
        "subnet_prefix": SUBNET_PREFIX,
    }


# --- the state file ------------------------------------------------------------------------------------
def _state_path(settings: Settings) -> Path:
    return settings.state_dir / STATE_FILE


def _read(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(settings).read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s: %s", _state_path(settings), exc)
        data = {}
    subnets = data.get("subnets") if isinstance(data, dict) else None
    refused = data.get("refused") if isinstance(data, dict) else None
    return {
        "subnets": {k: v for k, v in (subnets or {}).items() if isinstance(k, str) and isinstance(v, str)},
        "refused": [v for v in (refused or []) if isinstance(v, str)],
    }


def _write(settings: Settings, state: dict[str, Any]) -> None:
    path = _state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, sort_keys=True)
    os.replace(tmp, path)


def recorded_subnet(settings: Settings, instance_id: UUID) -> str | None:
    with _lock:
        return _read(settings)["subnets"].get(str(instance_id))


def forget(settings: Settings, instance_id: UUID) -> None:
    """The server was deleted: its subnet is free again. Never raises."""
    with _lock:
        try:
            state = _read(settings)
            if state["subnets"].pop(str(instance_id), None) is not None:
                _write(settings, state)
        except OSError as exc:
            log.warning("Could not free the subnet of %s: %s", instance_id, exc)


# --- networks ------------------------------------------------------------------------------------------
def _existing_subnets(docker: Any) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out = []
    for net in docker.networks.list():
        for cfg in ((getattr(net, "attrs", None) or {}).get("IPAM") or {}).get("Config") or []:
            with contextlib.suppress(ValueError, TypeError):
                out.append(ipaddress.ip_network(cfg.get("Subnet"), strict=False))
    return out


def _free_subnet(settings: Settings, state: dict[str, Any], taken: list[Any]) -> ipaddress.IPv4Network:
    used = {ipaddress.ip_network(v, strict=False) for v in state["subnets"].values() if _valid(v)}
    refused = {ipaddress.ip_network(v, strict=False) for v in state["refused"] if _valid(v)}
    for subnet in pool(settings).subnets(new_prefix=SUBNET_PREFIX):
        if subnet in used or subnet in refused:
            continue
        if any(subnet.version == t.version and subnet.overlaps(t) for t in taken):
            continue
        return subnet
    raise Unavailable(f"the pool {settings.instance_pool} has no free /{SUBNET_PREFIX} left")


def _valid(value: str) -> bool:
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


def _get(name: str) -> Any | None:
    try:
        return _client().networks.get(name)
    except NotFound:
        return None


def _check_ours(net: Any, instance_id: UUID) -> None:
    labels = (getattr(net, "attrs", None) or {}).get("Labels") or {}
    if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_INSTANCE) != str(instance_id):
        raise AgentError(
            f"A network named {net.name} exists but was not made by MCPanel's agent for this server",
            "foreign_network",
            409,
        )


def _create(name: str, instance_id: UUID, subnet: ipaddress.IPv4Network) -> Any:
    return _client().networks.create(
        name,
        driver="bridge",
        ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))]),
        labels={LABEL_MANAGED: "true", LABEL_INSTANCE: str(instance_id), LABEL_NETWORK: "server"},
        check_duplicate=True,
    )


def _refused_overlap(exc: APIError) -> bool:
    text = str(getattr(exc, "explanation", None) or exc).lower()
    return "overlap" in text or "address space" in text or "pool" in text


def _api_version(api: Any) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in str(getattr(api, "api_version", "") or "").split("."))
    except ValueError:
        return ()


def _connect_low_priority(own: str, name: str) -> None:
    """Connect the agent's container to ``name`` with the lowest gateway priority (``GwPriority``, Engine
    API 1.48+; docker-py has no argument for it), so that the agent's default route stays where it was;
    a daemon without it gets a plain connect (the name order keeps the route then)."""
    api = _client().api
    if _api_version(api) >= GW_PRIORITY_API:
        res = api._post_json(
            api._url("/networks/{0}/connect", name),
            data={"Container": own, "EndpointConfig": {"GwPriority": GW_PRIORITY}},
        )
        api._raise_for_status(res)
        return
    api.connect_container_to_network(own, name)


def attach_self(name: str) -> None:
    """Connect the agent's own container to network ``name`` (nothing when it is attached already).
    Raises Unavailable when it cannot (the agent could not reach the server there)."""
    own = own_container_id()
    if own is None:
        raise Unavailable("the agent could not find its own container")
    try:
        _connect_low_priority(own, name)
    except APIError as exc:
        text = str(exc.explanation or exc).lower()
        if "already exists" in text or "already attached" in text or "is already connected" in text:
            return
        raise Unavailable(f"the agent could not join network {name}: {exc.explanation or exc}") from exc


def ensure(settings: Settings, instance_id: UUID) -> str:
    """The server's network, created when missing (with its recorded subnet, else a free one of the pool)
    and joined by the agent. The caller holds the server's lock. Unavailable when it cannot be used;
    AgentError 409 ``foreign_network`` when the name is taken by a network that is not the agent's."""
    name = network_name(instance_id)
    with _lock:
        net = _get(name)
        if net is not None:
            _check_ours(net, instance_id)
        else:
            state = _read(settings)
            recorded = state["subnets"].get(str(instance_id))
            subnet = ipaddress.ip_network(recorded, strict=False) if recorded and _valid(recorded) else None
            docker = _client()
            for _ in range(MAX_CREATE_TRIES):
                if subnet is None:
                    subnet = _free_subnet(settings, state, _existing_subnets(docker))
                try:
                    _create(name, instance_id, subnet)
                except APIError as exc:
                    if not _refused_overlap(exc):
                        raise Unavailable(f"Docker refused network {name}: {exc.explanation or exc}") from exc
                    log.warning("Docker refused subnet %s for %s (%s); trying another", subnet, name, exc)
                    state["refused"] = sorted({*state["refused"], str(subnet)})
                    subnet = None
                    continue
                if recorded != str(subnet):
                    log.info("Server %s gets network %s with subnet %s", instance_id, name, subnet)
                else:
                    log.warning("Network %s was missing; created it again with subnet %s", name, subnet)
                state["subnets"][str(instance_id)] = str(subnet)
                _write(settings, state)
                break
            else:
                _write(settings, state)
                raise Unavailable(f"Docker refused {MAX_CREATE_TRIES} subnets of the pool")
    try:
        attach_self(name)
    except Unavailable:
        if net is None:  # just made for nothing: the server goes on the shared network
            release(settings, instance_id)
        raise
    return name


def prepare(settings: Settings, instance_id: UUID) -> str | None:
    """For a new container of a spec without a network: the name of the server's own network, ready to
    use; None when the server goes on the shared instance network (the reason is logged and reported)."""
    reason = availability(settings)
    if reason is not None:
        _fallback(None, reason)
        return None
    try:
        return ensure(settings, instance_id)
    except Unavailable as exc:
        _fallback(instance_id, str(exc))
        return None


def _users(net: Any, own: str | None) -> list[str]:
    """Containers (any state) on network ``net`` other than the agent's own."""
    ids = [str(c.id) for c in _client().containers.list(all=True, filters={"network": net.name})]
    return [i for i in ids if not (own and (i == own or i.startswith(own) or own.startswith(i)))]


def release(settings: Settings, instance_id: UUID) -> bool:
    """Remove the server's network when no container but the agent's own is on it (any state); its
    subnet stays recorded. A network without the agent's labels is never touched. Never raises."""
    name = network_name(instance_id)
    with _lock:
        try:
            net = _get(name)
            if net is None:
                return False
            labels = (net.attrs or {}).get("Labels") or {}
            if labels.get(LABEL_MANAGED) != "true" or labels.get(LABEL_NETWORK) != "server":
                return False
            own = own_container_id()
            if _users(net, own):
                return False
            if own:
                with contextlib.suppress(APIError, NotFound):
                    _client().api.disconnect_container_from_network(own, name, force=True)
            net.remove()
            log.info("Removed network %s (no container of server %s left on it)", name, instance_id)
            return True
        except (APIError, NotFound) as exc:
            log.warning("Could not remove network %s: %s", name, exc)
            return False
        except Exception as exc:
            log.warning("Could not check network %s: %s", name, exc)
            return False


def server_networks() -> list[Any]:
    """Every per-server network of this agent that exists (labels checked)."""
    out = []
    for net in _client().networks.list(filters={"label": f"{LABEL_NETWORK}=server"}):
        labels = (net.attrs or {}).get("Labels") or {}
        if labels.get(LABEL_MANAGED) == "true" and is_server_network(net.name):
            out.append(net)
    return out


def startup(settings: Settings, lock_for: Any) -> dict[str, list[str]]:
    """At the agent's start (Docker answering): create the network again of every server that has a
    container on a missing server network, join every server network, and remove the ones no container
    uses any more. ``lock_for(instance_id)``: the server's lock (a context manager). Never raises."""
    from . import docker_ops

    done: dict[str, list[str]] = {"recreated": [], "joined": [], "removed": []}
    if not settings.per_server_networks and not _has_server_networks():
        return done
    own = own_container_id(refresh=True)
    try:
        containers = docker_ops.client().containers.list(all=True, filters={"label": f"{LABEL_MANAGED}=true"})
    except Exception as exc:
        log.warning("Server networks not checked at start (Docker): %s", exc)
        return done
    existing = {n.name for n in server_networks()}
    for c in containers:
        nets = ((c.attrs or {}).get("NetworkSettings") or {}).get("Networks") or {}
        iid = (c.labels or {}).get(LABEL_INSTANCE)
        for name in nets:
            if not is_server_network(name) or name in existing or not iid:
                continue
            try:
                instance_id = UUID(iid)
                if network_name(instance_id) != name:
                    continue
                with lock_for(instance_id):
                    ensure(settings, instance_id)
                done["recreated"].append(name)
                existing.add(name)
            except Exception as exc:
                log.warning(
                    "Could not create network %s again: %s (the server's start tries again)", name, exc
                )
    for net in server_networks():
        try:
            if not _users(net, own):
                labels = (net.attrs or {}).get("Labels") or {}
                iid = labels.get(LABEL_INSTANCE)
                if iid and release(settings, UUID(iid)):
                    done["removed"].append(net.name)
                continue
            if own is not None:
                attach_self(net.name)
                done["joined"].append(net.name)
        except Exception as exc:
            log.warning("Network %s: %s", net.name, exc)
    if any(done.values()):
        log.info(
            "Server networks at start: joined %d, created again %d, removed %d",
            len(done["joined"]),
            len(done["recreated"]),
            len(done["removed"]),
        )
    return done


def _has_server_networks() -> bool:
    try:
        return bool(server_networks())
    except Exception:
        return False
