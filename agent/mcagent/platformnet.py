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
LABEL_ONEOFF = "com.docker.compose.oneoff"
LABEL_CONFIG_HASH = "com.docker.compose.config-hash"
LABEL_NUMBER = "com.docker.compose.container-number"
LABEL_NETWORK_KEY = "com.docker.compose.network"
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


def is_member(labels: dict[str, Any], project: str) -> bool:
    """Whether a container is a long-running service container of the compose project ``project`` (F-3,
    F-18): what compose itself always sets on those (project, service, config hash, container number,
    ``oneoff=False``), and never a container the agent manages (Docker copies an image's labels into its
    containers, so a game image could carry compose labels). One-offs (``docker compose run``: upgrade.sh's
    migration checks) are never members."""
    if labels.get(LABEL_PROJECT) != project or labels.get("io.mcpanel.managed"):
        return False
    if str(labels.get(LABEL_ONEOFF, "")).lower() != "false":
        return False
    return bool(labels.get(LABEL_SERVICE) and labels.get(LABEL_CONFIG_HASH) and labels.get(LABEL_NUMBER))


def _project_networks(project: str) -> dict[str, str]:
    """The networks compose made for ``project``: {name: compose key (``internal``, ``instances``, ...)}."""
    out: dict[str, str] = {}
    for net in _client().networks.list(filters={"label": f"{LABEL_PROJECT}={project}"}):
        labels = (getattr(net, "attrs", None) or {}).get("Labels") or {}
        if labels.get(LABEL_PROJECT) == project:
            out[str(net.name)] = str(labels.get(LABEL_NETWORK_KEY) or "")
    return out


def _reconnect(
    cid: str, network: str, aliases: list[str], ip: str | None, mac: str | None, *, connected: bool
) -> str:
    """Put ``cid`` on ``network`` with ``aliases`` (after a disconnect when ``connected``), in steps (F-6):
    with its IP and MAC, with its MAC, with the aliases only, a plain connect. Returns the step that worked;
    raises the last error when none did (then the container is off that network: the caller says so loudly
    and tries again at the next pass, past the usual limit)."""
    api = _client().api
    if connected:
        api.disconnect_container_from_network(cid, network, force=True)
    steps = [
        ("same IP and MAC", {"ipv4_address": ip, "aliases": aliases, "mac_address": mac}),
        ("same MAC", {"aliases": aliases, "mac_address": mac}),
        ("aliases only", {"aliases": aliases}),
        ("plain connect", {}),
    ]
    last: Exception | None = None
    for what, kwargs in steps:
        kwargs = {k: v for k, v in kwargs.items() if v}
        try:
            api.connect_container_to_network(cid, network, **kwargs)
            return what
        except Exception as exc:
            text = str(getattr(exc, "explanation", None) or exc).lower()
            if "already exists" in text or "already attached" in text:
                return "already on it"
            last = exc
    assert last is not None
    raise last


def _restore_aliases(
    c: Any, cid: str, service: str, networks: list[str], attrs: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Reconnect ``c`` to each of ``networks`` with its compose aliases (service and container name), keeping
    its IP address and MAC address there when Docker lets it (:func:`_reconnect`). Docker has no call that
    changes an endpoint's aliases in place: one disconnect and connect per network, a cut of well under a
    second for a running container. Returns (what was done, whether it was left off a network)."""
    name = str(c.name).lstrip("/")
    aliases = [a for a in (service, name) if a]
    results = []
    stranded = False
    for network in networks:
        ep = _networks(attrs).get(network) or {}
        ip = ep.get("IPAddress") or None
        mac = ep.get("MacAddress") or None
        log.warning(
            "Container %s is on %s but does not answer to its service name %r there (an endpoint reconnected "
            "without its aliases, e.g. by Unraid's Docker start script); reconnecting it with aliases %s "
            "(IP %s, MAC %s kept where Docker lets it)",
            name,
            network,
            service,
            aliases,
            ip,
            mac,
        )
        try:
            step = _reconnect(cid, network, aliases, ip, mac, connected=True)
            now_names = _names(_networks(_client().containers.get(cid).attrs or {}).get(network) or {})
            results.append("restored" if service in now_names else f"reconnected ({step}) without its name")
        except Exception as exc:
            stranded = True
            log.error(
                "Container %s is OFF network %s: every way to connect it again failed (%s); trying again at the "
                "next pass",
                name,
                network,
                exc,
            )
            results.append(f"failed, off the network: {type(exc).__name__}")
    result = "restored" if all(r == "restored" for r in results) else "; ".join(results)
    _record(name, service, ", ".join(networks), "reconnect with aliases", result)
    return {"container": name, "result": result}, stranded


def repair_once(settings: Settings, *, now: float | None = None) -> list[dict[str, Any]]:
    """One pass (see the module docstring). Returns the repairs it tried. Never raises."""
    from . import pernet as _pernet

    done: list[dict[str, Any]] = []
    if not settings.repair_platform_networks:
        return done
    own = _pernet.own_container_id()
    if own is None:
        return done
    now = time.monotonic() if now is None else now
    docker = _client()
    try:
        me = docker.containers.get(own)
        my_labels = (me.attrs.get("Config") or {}).get("Labels") or {}
        project = my_labels.get(LABEL_PROJECT)
        if not project:
            return done
        project_nets = _project_networks(project)
        members = docker.containers.list(all=True, filters={"label": f"{LABEL_PROJECT}={project}"})
    except Exception as exc:
        log.debug("platform networks not checked (Docker): %s", exc)
        return done
    seen = _load_seen(settings)
    before = json.dumps(seen, sort_keys=True)
    present: set[str] = set()
    for c in members:
        try:
            c.reload()
            attrs = c.attrs or {}
            labels = (attrs.get("Config") or {}).get("Labels") or {}
            cid = str(c.id)
            is_me = cid == own or cid.startswith(own)
            if not (is_me or is_member(labels, project)):
                continue  # (never outside the project, never a one-off or a container the agent manages)
            present.add(cid)
            nets = _networks(attrs)
            # what it was on while fine, per container (a new container starts afresh), only ever growing
            # (F-5), and only networks of the project itself (F-18)
            on_project = sorted(n for n in nets if n in project_nets)
            seen[cid] = sorted(set(seen.get(cid) or []) | set(on_project))
            network = _intended(attrs)
            wanted = [
                n for n in dict.fromkeys([*seen[cid], *([network] if network else [])]) if n in project_nets
            ]
            if is_me:
                # the agent always belongs on the shared instance network and the project's internal network
                extra = [
                    settings.instance_network,
                    *(n for n, key in project_nets.items() if key == "internal"),
                ]
                wanted = list(dict.fromkeys([*wanted, *(n for n in extra if n in project_nets)]))
            state = attrs.get("State") or {}
            if not (is_me or state.get("Running") or state.get("Restarting")):
                continue
            service = labels.get(LABEL_SERVICE) or ""
            aliases = [a for a in (service, str(c.name).lstrip("/")) if a]
            missing = [n for n in wanted if n not in nets]
            unnamed = [n for n in on_project if service and service not in _names(nets.get(n) or {})]
            if not missing and not unnamed:
                continue
            stranded = bool(_tries.get(cid, {}).get("stranded"))
            if not stranded and not _may_try(cid, now):
                _say_given_up(cid, c.name, "still lacks its network or its name")
                continue
            if missing:
                result = _reconnect_member(c, cid, service, aliases, missing, nets, is_me=is_me)
                done.append(result)
                if stranded and not str(result["result"]).startswith("failed"):
                    with _lock:
                        _tries[cid]["stranded"] = False
            else:
                result, stranded_now = _restore_aliases(c, cid, service, unnamed, attrs)
                done.append(result)
                with _lock:
                    _tries.setdefault(cid, {"attempts": 0, "last": now, "given_up": False})["stranded"] = (
                        stranded_now
                    )
            if is_me and _on_self_repaired is not None:
                with contextlib.suppress(Exception):
                    _on_self_repaired()  # the source guard sees the agent's networks again at once (F-19)
        except Exception as exc:
            log.warning("Platform network check of %s failed: %s", getattr(c, "name", "?"), exc)
    for cid in [k for k in seen if k not in present]:
        del seen[cid]  # containers that are gone
    if json.dumps(seen, sort_keys=True) != before:
        _save_seen(settings, seen)
    return done


def _reconnect_member(
    c: Any,
    cid: str,
    service: str,
    aliases: list[str],
    missing: list[str],
    nets: dict[str, Any],
    *,
    is_me: bool,
) -> dict[str, Any]:
    """Give a member the networks it lacks. One that still has a network (the agent always) is connected live;
    one with none at all is stopped, connected and started (Docker runs it with ``lo`` only otherwise)."""
    name = str(c.name).lstrip("/")
    live = is_me or bool(nets)
    log.warning(
        "Container %s of this stack is not on %s (Docker dropped it after a start that failed on a published "
        "port, or it was disconnected); %s with aliases %s",
        name,
        ", ".join(missing),
        "connecting it" if live else "stopping it, connecting it, starting it",
        aliases,
    )
    results = []
    try:
        if not live:
            c.stop(timeout=STOP_TIMEOUT)
        for network in missing:
            try:
                results.append(_reconnect(cid, network, aliases, None, None, connected=False))
            except Exception as exc:
                log.warning("Could not connect %s to %s: %s", name, network, exc)
                results.append(f"failed: {type(exc).__name__}")
        if not live:
            c.start()
        c.reload()
        now_on = _networks(c.attrs or {})
        result = ("repaired" if not is_me else "connected") if all(n in now_on for n in missing) else "failed"
    except Exception as exc:
        log.warning("Repairing the network of %s failed: %s", name, exc)
        result = f"failed: {type(exc).__name__}"
        if not live:  # never leave it stopped because of the repair
            with contextlib.suppress(Exception):
                c.reload()
                if (c.attrs.get("State") or {}).get("Running") is False:
                    c.start()
    action = "connect" if live else "stop, connect, start"
    _record(name, service, ", ".join(missing), action, result)
    return {"container": name, "result": result}


# called after the agent reconnected itself (main: refresh the source guard's networks; F-19)
_on_self_repaired: Any = None


def on_self_repaired(fn: Any) -> None:
    global _on_self_repaired
    _on_self_repaired = fn


def repair_game_containers(settings: Settings, *, now: float | None = None) -> list[dict[str, Any]]:
    """A game container that Docker itself started (its restart policy) after a start of it had failed on a
    published port runs with no network (no players, no RCON); the agent's own starts repair before they
    start, Docker's do not (F-19). Such a running managed container is stopped and started again through the
    agent's start, which attaches it first; under the server's lock (skipped when busy) and the same limits.
    Never raises."""
    from . import docker_ops

    done: list[dict[str, Any]] = []
    if not settings.repair_platform_networks:
        return done
    now = time.monotonic() if now is None else now
    try:
        running = docker_ops.client().containers.list(filters={"label": f"{docker_ops.LABEL_MANAGED}=true"})
    except Exception as exc:
        log.debug("game containers not checked (Docker): %s", exc)
        return done
    for c in running:
        try:
            attrs = c.attrs or {}
            iid = ((attrs.get("Config") or {}).get("Labels") or {}).get(docker_ops.LABEL_INSTANCE)
            if not iid or _networks(attrs) or _intended(attrs) is None:
                continue
            if not (attrs.get("State") or {}).get("Running"):
                continue
            cid = str(c.id)
            if not _may_try(cid, now):
                _say_given_up(cid, c.name, "still runs without a network")
                continue
            from uuid import UUID

            log.warning(
                "Game container %s runs without its network %s (Docker started it after a failed start); "
                "stopping it and starting it again through the agent, which attaches it first",
                c.name,
                _intended(attrs),
            )
            try:
                with docker_ops.instance_lock(UUID(iid), wait=0.0):
                    docker_ops.graceful_stop(settings, c, None)
                    docker_ops.start(UUID(iid), settings)
                c.reload()
                result = "repaired" if _networks(c.attrs or {}) else "failed"
            except Exception as exc:
                result = f"failed: {type(exc).__name__}: {exc}"[:200]
                log.warning("Repairing the network of %s failed: %s", c.name, exc)
            _record(str(c.name), "", str(_intended(attrs)), "stop, attach, start", result)
            done.append({"container": str(c.name), "result": result})
        except Exception as exc:
            log.warning("Network check of %s failed: %s", getattr(c, "name", "?"), exc)
    return done


def run(settings: Settings, stop: threading.Event) -> None:
    """At the agent's start and every REPAIR_SECONDS: :func:`repair_once`, :func:`repair_game_containers`.
    Never raises."""
    while True:
        try:
            repair_once(settings)
        except Exception:
            log.exception("The platform network repair failed")
        try:
            repair_game_containers(settings)
        except Exception:
            log.exception("The game container network repair failed")
        if stop.wait(REPAIR_SECONDS):
            return
