"""Network repair of the platform's own containers (boot-network repair, 2026-11-12).

When a container's start fails while Docker programs one of its published ports (a port conflict, an
address that is not there yet at boot), Docker drops the container's network attachment from its
configuration: ``NetworkSettings.Networks`` is empty while ``HostConfig.NetworkMode`` still names the
network, and every later start succeeds with no network but ``lo``. That is how the owner's panel ran in a
restart loop for hours after a reboot, unable to resolve its database. The agent is the only component
with the Docker socket, so it repairs its own compose project:

* at its start and every REPAIR_SECONDS it looks at the containers of its own compose project (label
  ``com.docker.compose.project`` equal to its own container's; nothing when it cannot find itself or has no
  such label) that run or restart: one with no network while ``HostConfig.NetworkMode`` names a
  user-defined network is stopped, connected to that network again with the aliases compose gives (its
  service name and its container name), and started;
* the agent's own container is connected again without a restart: to the network of its ``NetworkMode``
  and to the shared instance network;
* bounded and careful: never a container outside the project, never one that has a network, one attempt per
  container per RETRY_SECONDS, at most MAX_ATTEMPTS failed or undone repairs per container (then it stops
  and says so: a deliberate ``docker network disconnect`` is not fought forever), every action logged at
  warning level, the last repairs in ``GET /v1/info`` (:func:`status`), off with
  ``AGENT_REPAIR_PLATFORM_NETWORKS=false``.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import threading
import time
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
REPAIR_SECONDS = 60.0
RETRY_SECONDS = 300.0
MAX_ATTEMPTS = 3
STOP_TIMEOUT = 20
# the networks each service of the stack was seen on while it had them, kept in the state folder: Docker
# keeps only the first one in HostConfig.NetworkMode when it drops them (the agent is on two)
STATE_FILE = "platform-networks.json"

_lock = threading.Lock()
_recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=20)
# container id -> {"attempts": n, "last": monotonic time of the last attempt, "given_up": bool}
_tries: dict[str, dict[str, Any]] = {}


def _client() -> Any:
    from . import docker_ops

    return docker_ops.client()


def _intended(attrs: dict[str, Any]) -> str | None:
    mode = str((attrs.get("HostConfig") or {}).get("NetworkMode") or "")
    if not mode or mode in ("default", "host", "none", "bridge") or mode.startswith("container:"):
        return None
    return mode


def _networks(attrs: dict[str, Any]) -> dict[str, Any]:
    return (attrs.get("NetworkSettings") or {}).get("Networks") or {}


def _record(container: str, service: str, network: str, action: str, result: str) -> None:
    with _lock:
        _recent.appendleft(
            {
                "container": container,
                "service": service,
                "network": network,
                "action": action,
                "result": result,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )


def status(settings: Settings) -> dict[str, Any]:
    """``GET /v1/info``: whether the repair is on, the agent's compose project, the last repairs."""
    with _lock:
        recent = list(_recent)
        given_up = sorted(cid[:12] for cid, t in _tries.items() if t.get("given_up"))
    return {
        "enabled": bool(settings.repair_platform_networks),
        "project": _project(),
        "recent": recent,
        "given_up": given_up,
    }


def _project() -> str | None:
    from . import pernet

    own = pernet.own_container_id()
    if own is None:
        return None
    try:
        labels = _client().containers.get(own).attrs.get("Config", {}).get("Labels") or {}
    except Exception:
        return None
    return labels.get(LABEL_PROJECT) or None


def _may_try(cid: str, now: float) -> bool:
    with _lock:
        t = _tries.setdefault(cid, {"attempts": 0, "last": None, "given_up": False})
        if t["given_up"]:
            return False
        if t["last"] is not None and now - t["last"] < RETRY_SECONDS:
            return False
        if t["attempts"] >= MAX_ATTEMPTS:
            t["given_up"] = True
            return False
        t["attempts"] += 1
        t["last"] = now
        return True


def _gave_up_now(cid: str) -> bool:
    with _lock:
        t = _tries.get(cid) or {}
        return bool(t.get("given_up"))


def _load_seen(settings: Settings) -> dict[str, list[str]]:
    try:
        data = json.loads((settings.state_dir / STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(n) for n in v if isinstance(n, str)] for k, v in data.items() if isinstance(v, list)}


def _save_seen(settings: Settings, seen: dict[str, list[str]]) -> None:
    path = settings.state_dir / STATE_FILE
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(seen, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("could not keep the stack's networks in %s: %s", path, exc)


def _connect(cid: str, network: str, aliases: list[str]) -> None:
    _client().api.connect_container_to_network(cid, network, aliases=aliases or None)


def _say_given_up(cid: str, name: str, what: str) -> None:
    if _gave_up_now(cid) and not _tries[cid].get("said"):
        _tries[cid]["said"] = True
        log.warning(
            "Container %s %s after %d repairs (or they were undone, e.g. deliberately); not repairing it any more",
            name,
            what,
            MAX_ATTEMPTS,
        )


def _names(endpoint: dict[str, Any]) -> set[str]:
    """The names an endpoint answers to on its network: its aliases and (Docker 25+) its DNS names."""
    return {str(n) for n in [*(endpoint.get("Aliases") or []), *(endpoint.get("DNSNames") or [])] if n}


def _project_of(network: str, cache: dict[str, str | None]) -> str | None:
    if network not in cache:
        try:
            labels = (_client().networks.get(network).attrs or {}).get("Labels") or {}
            cache[network] = labels.get(LABEL_PROJECT) or None
        except Exception:
            cache[network] = None
    return cache[network]


def _restore_aliases(
    c: Any, cid: str, service: str, networks: list[str], attrs: dict[str, Any]
) -> dict[str, Any]:
    """Reconnect ``c`` to each of ``networks`` with its compose aliases (its service name and container
    name), keeping its IP address and MAC address there. Docker has no call that changes an endpoint's
    aliases in place, so this is one disconnect and connect per network: a cut of well under a second for
    a running container, done once (the attempt limits of the module apply)."""
    name = str(c.name).lstrip("/")
    aliases = [a for a in (service, name) if a]
    api = _client().api
    results = []
    for network in networks:
        ep = _networks(attrs).get(network) or {}
        ip = ep.get("IPAddress") or None
        mac = ep.get("MacAddress") or None
        log.warning(
            "Container %s is on %s but does not answer to its service name %r there (an endpoint reconnected "
            "without its aliases, e.g. by Unraid's Docker start script); reconnecting it with aliases %s "
            "(IP %s, MAC %s kept)",
            name,
            network,
            service,
            aliases,
            ip,
            mac,
        )
        try:
            api.disconnect_container_from_network(cid, network, force=True)
            try:
                api.connect_container_to_network(
                    cid, network, ipv4_address=ip, aliases=aliases, mac_address=mac
                )
            except Exception as exc:
                # a fixed address is refused on a network without a user-configured subnet: Docker picks one
                if "user specified ip address" not in str(exc).lower():
                    raise
                api.connect_container_to_network(cid, network, aliases=aliases, mac_address=mac)
            now_names = _names(_networks(_client().containers.get(cid).attrs or {}).get(network) or {})
            results.append("restored" if service in now_names else "failed")
        except Exception as exc:
            log.warning("Restoring the aliases of %s on %s failed: %s", name, network, exc)
            results.append(f"failed: {type(exc).__name__}")
    result = "restored" if all(r == "restored" for r in results) else "; ".join(results)
    _record(name, service, ", ".join(networks), "reconnect with aliases", result)
    return {"container": name, "result": result}


def repair_once(settings: Settings, *, now: float | None = None) -> list[dict[str, Any]]:
    """One pass (see the module docstring). Returns the repairs it tried. Never raises."""
    from . import pernet

    done: list[dict[str, Any]] = []
    if not settings.repair_platform_networks:
        return done
    own = pernet.own_container_id()
    if own is None:
        return done
    now = time.monotonic() if now is None else now
    docker = _client()
    try:
        me = docker.containers.get(own)
        project = ((me.attrs.get("Config") or {}).get("Labels") or {}).get(LABEL_PROJECT)
        if not project:
            return done
        members = docker.containers.list(all=True, filters={"label": f"{LABEL_PROJECT}={project}"})
    except Exception as exc:
        log.debug("platform networks not checked (Docker): %s", exc)
        return done
    from . import pernet as _pernet

    seen = _load_seen(settings)
    before = json.dumps(seen, sort_keys=True)
    net_projects: dict[str, str | None] = {}
    for c in members:
        try:
            c.reload()
            attrs = c.attrs or {}
            labels = (attrs.get("Config") or {}).get("Labels") or {}
            if labels.get(LABEL_PROJECT) != project:
                continue  # (never outside the agent's own project)
            key = f"{project}/{labels.get(LABEL_SERVICE) or c.name}"
            stack = [n for n in _networks(attrs) if not _pernet.is_server_network(n)]
            if stack:
                seen[key] = sorted(stack)  # what it is on while it is fine: the networks to give back
            network = _intended(attrs)
            state = attrs.get("State") or {}
            is_me = str(c.id) == own or str(c.id).startswith(own)
            service = labels.get(LABEL_SERVICE) or ""
            # on its networks, but not answering to its service name there (Unraid's start script reconnects
            # endpoints with a MAC address and no aliases): the aliases come back
            unnamed = [
                n
                for n in stack
                if service
                and _project_of(n, net_projects) == project
                and service not in _names(_networks(attrs).get(n) or {})
            ]
            if is_me and any(n not in _networks(attrs) for n in (network, settings.instance_network) if n):
                unnamed = []  # missing networks first (below); its names come with them
            if unnamed and (is_me or state.get("Running") or state.get("Restarting")):
                cid = str(c.id)
                if _may_try(cid, now):
                    done.append(_restore_aliases(c, cid, service, unnamed, attrs))
                else:
                    _say_given_up(cid, c.name, "still answers to no service name")
                continue
            wanted = list(dict.fromkeys([*(seen.get(key) or []), *([network] if network else [])]))
            if is_me:
                # the agent: its stack networks, whatever server networks it is on meanwhile
                wanted = list(dict.fromkeys([*wanted, settings.instance_network]))
                missing = [n for n in wanted if n not in _networks(attrs)]
                if not missing:
                    continue
            elif _networks(attrs):
                continue  # (its count stays: a repair undone again and again ends in giving up)
            if network is None or not (is_me or state.get("Running") or state.get("Restarting")):
                continue
            cid = str(c.id)
            if not _may_try(cid, now):
                _say_given_up(cid, c.name, "still has no network")
                continue
            aliases = [a for a in (service, str(c.name).lstrip("/")) if a]
            if is_me:
                targets = missing
                log.warning(
                    "The agent's own container is not on %s (its configuration lost it); connecting it",
                    ", ".join(targets),
                )
                for target in targets:
                    try:
                        _connect(cid, target, aliases)
                    except Exception as exc:
                        log.warning("Could not connect the agent to %s: %s", target, exc)
                now_on = _networks(docker.containers.get(cid).attrs or {})
                result = "connected" if all(t in now_on for t in targets) else "failed"
                _record(c.name, service, ", ".join(targets), "connect", result)
                done.append({"container": c.name, "result": result})
                continue
            log.warning(
                "Container %s of this stack runs without a network (Docker dropped %s after a start that failed "
                "on a published port); stopping it, connecting it to %s with aliases %s, starting it",
                c.name,
                network,
                ", ".join(wanted or [network]),
                aliases,
            )
            try:
                c.stop(timeout=STOP_TIMEOUT)
                for target in wanted or [network]:
                    _connect(cid, target, aliases)
                c.start()
                c.reload()
                result = "repaired" if _networks(c.attrs or {}) else "failed"
            except Exception as exc:
                log.warning("Repairing the network of %s failed: %s", c.name, exc)
                result = f"failed: {type(exc).__name__}"
                # never leave it stopped because of the repair: start it again as it was
                try:
                    c.reload()
                    if (c.attrs.get("State") or {}).get("Running") is False:
                        c.start()
                except Exception as exc2:
                    log.warning("Could not start %s again: %s", c.name, exc2)
            _record(c.name, service, ", ".join(wanted or [network]), "stop, connect, start", result)
            done.append({"container": c.name, "result": result})
        except Exception as exc:
            log.warning("Platform network check of %s failed: %s", getattr(c, "name", "?"), exc)
    if json.dumps(seen, sort_keys=True) != before:
        _save_seen(settings, seen)
    return done


def run(settings: Settings, stop: threading.Event) -> None:
    """At the agent's start and every REPAIR_SECONDS: :func:`repair_once`. Never raises."""
    while True:
        try:
            repair_once(settings)
        except Exception:
            log.exception("The platform network repair failed")
        if stop.wait(REPAIR_SECONDS):
            return
