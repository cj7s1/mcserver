"""The claim guard: no published game server may run unclaimed (Satisfactory).

Whoever reaches an unclaimed Satisfactory server first owns it (``PasswordlessLogin`` as
InitialAdmin, then ``ClaimServer``). The panel claims a new server while its container publishes no
port, and the agent refuses to create or start a container with published ports while the claim is
missing (``SatisfactoryDriver.publish_guard``, called by apply and by ``docker_ops.start``). What
neither can intercept is a start by Docker itself (the ``on-failure`` restart policy, a daemon
restart) of a container whose claim was lost meanwhile (``saved/ServerSettings.7777.sav`` removed,
a backup from before the claim restored). So this loop watches every running container whose
driver guards publishing (``guards_publish``) and that has published ports:

* per container run (container id + ``State.StartedAt``), as soon as its API answers, it asks
  whether the server is unclaimed (``driver.claim_state``). Polled every POLL_SECONDS, so the
  window between the API coming up and the stop is small; nobody can claim through an API that
  does not answer, so "down" just means "look again";
* unclaimed: the container is stopped at once (``docker stop``, no save: nobody claimed it, and
  the game cannot save on a signal anyway) and the reason is recorded (``server_unclaimed``);
* the API answers unexpectedly (a different TLS certificate than earlier in the run, an error code
  the probe does not know): fail closed, stopped and recorded (``claim_unverified``);
* claimed: the run is verified, and checked again every RECHECK_SECONDS. A verified run clears a
  record an earlier run left.

Containers without published ports are left alone: that is how the panel claims a server.

Since the real-container gate of 2026-11-11 (an unclaimed server answered on its published port for
1.6 s after a hand ``docker start``) the container's own start wrapper refuses such a start before
the game runs (``claimmarker``: exit 78 unless the claim marker matches the settings file), and the
restart policy is "no". This loop stays as defence in depth, and also:

* reacts to Docker's ``start`` events at once (``watch_events``, besides the poll), and stops a
  published run that started without a matching claim marker before its API is even up (only
  for a run that just started: a running server's settings file may change legitimately, e.g. an
  admin in the game sets a client password);
* records the wrapper's refusal (a ``die`` event with exit code 78 of a published container) as a
  ``guard_stop``, so the panel closes the ports and verifies the claim again;
* removes the claim marker when the settings file of a running server changes outside an
  operation of the panel (``claimmarker.check_outside_change``).

The stop takes the per-server lock (``docker_ops.instance_lock``) when it is free within
LOCK_WAIT seconds, and goes ahead without it otherwise: an apply holds the lock for minutes
(pull, graceful stop of the old container), and an unclaimed published server must not wait for
it. A ``docker stop`` never needed the lock (``/stop`` and ``/kill`` do not take it either); the
stop is only sent when the container is still the same run.

The record (``<data root>/agent/guard-stops/<instance id>.json``, 0600 in a 0700 folder of the
agent's own state, outside every server folder, so nobody with file access to a server can write
it) is reported as ``guard_stop`` (``{"code", "message", "at"}``) by ``GET /instances/{id}/status``
and ``GET /instances`` while it describes the container's latest run (or the last run of a server
without a container); a later run that verifies as claimed, or the next apply, removes it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from . import claimmarker
from .config import Settings
from .errors import AgentError

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
RECHECK_SECONDS = 300.0
STOP_TIMEOUT = 10  # seconds docker stop gives the server (it cannot save on a signal anyway)
LOCK_WAIT = 1.0
FRESH_RUN_SECONDS = 30.0  # a run this young is checked against the claim marker
EVENT_WINDOW_SECONDS = 5  # one events request covers this many seconds (then the next one)
EVENT_RETRY_SECONDS = 5.0
RECORD_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC

MESSAGES = {
    "server_unclaimed": (
        "The agent stopped this server: it ran with published ports but was not claimed (its claim was "
        "lost, e.g. the server settings file was removed or a backup from before the claim was restored). "
        "Whoever reaches an unclaimed server first owns it. Run the server's setup (claim) again before "
        "its ports are opened."
    ),
    "claim_unverified": (
        "The agent stopped this server: it ran with published ports and its claim could not be verified "
        "(the API answered unexpectedly: {detail}). Start it again when the problem is solved; it is "
        "checked again as soon as its API answers."
    ),
    "marker_missing": (
        "The agent stopped this server: it was started with published ports although its claim was not "
        "verified since the server settings file last changed (no matching claim marker; it was started "
        "outside the panel). Start it from the panel: it verifies the claim on an unpublished container "
        "before the ports are opened."
    ),
    "start_refused": (
        "The server's start guard refused to start it (exit code {code}): it has published ports and its "
        "claim was not verified since the server settings file last changed (it was started outside the "
        "panel, e.g. by docker start). Start it from the panel: it verifies the claim on an unpublished "
        "container before the ports are opened."
    ),
}


# --- the record ------------------------------------------------------------------------------------
def directory(settings: Settings) -> Path:
    return settings.state_dir / "guard-stops"


def path(settings: Settings, instance_id: UUID) -> Path:
    return directory(settings) / f"{instance_id}.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def record(
    settings: Settings,
    instance_id: UUID,
    *,
    container_id: str,
    started_at: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """Store why the guard stopped the run ``(container_id, started_at)`` (replacing an older
    record), atomically. Raises OSError when it cannot be written."""
    data = {
        "version": RECORD_VERSION,
        "instance_id": str(instance_id),
        "container_id": str(container_id),
        "started_at": str(started_at),
        "code": code,
        "message": message[:2000],
        "at": _now(),
    }
    folder = directory(settings)
    folder.mkdir(parents=True, exist_ok=True)
    st = os.lstat(folder)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{folder} is not a directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(folder, 0o700)
    tmp = folder / f".{instance_id}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data, ensure_ascii=True).encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path(settings, instance_id))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return data


def load(settings: Settings, instance_id: UUID) -> dict[str, Any] | None:
    """The stored record, or None (none, or not one of ours). Never raises."""
    try:
        fd = os.open(path(settings, instance_id), os.O_RDONLY | _FILE_FLAGS)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECORD_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = json.loads(fh.read(MAX_RECORD_BYTES + 1))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if (
        not isinstance(data, dict)
        or data.get("version") != RECORD_VERSION
        or data.get("instance_id") != str(instance_id)
        or not isinstance(data.get("code"), str)
    ):
        return None
    return data


def clear(settings: Settings, instance_id: UUID) -> bool:
    """Remove the record. Returns whether there was one. Never raises."""
    try:
        os.unlink(path(settings, instance_id))
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not remove the guard-stop record of %s: %s", instance_id, exc)
        return False


def for_status(
    settings: Settings, instance_id: UUID, described: dict[str, Any] | None
) -> dict[str, Any] | None:
    """``guard_stop`` of a status answer: the record while it describes the container's latest run
    (``described``: ``docker_ops.describe`` of the container, None without one, then the record is
    about the last run). None otherwise."""
    data = load(settings, instance_id)
    if data is None:
        return None
    if described is not None and described.get("exists", True):
        if data.get("container_id") != described.get("id") or str(data.get("started_at") or "") != str(
            described.get("started_at") or ""
        ):
            return None  # another container, or a later run of it
    return {"code": data["code"], "message": str(data.get("message") or ""), "at": data.get("at")}


# --- the loop --------------------------------------------------------------------------------------
@contextlib.contextmanager
def _lock_if_free(instance_id: UUID) -> Iterator[bool]:
    from . import docker_ops

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(docker_ops.instance_lock(instance_id, wait=LOCK_WAIT))
            locked = True
        except AgentError:
            locked = False  # an operation holds it: the stop does not wait for it
        yield locked


def _run_key(c: Any) -> tuple[str, str]:
    state = (getattr(c, "attrs", None) or {}).get("State") or {}
    return str(c.id), str(state.get("StartedAt") or "")


class ClaimGuard:
    """One pass at a time (``check_all``); remembers the verified runs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._verified: dict[tuple[str, str], float] = {}
        self._marker_checked: set[tuple[str, str]] = set()
        # one check at a time: the poll and the events thread share the guard
        self._lock = threading.RLock()

    def check_all(self) -> None:
        from . import docker_ops

        containers = docker_ops.client().containers.list(
            filters={"label": f"{docker_ops.LABEL_MANAGED}=true", "status": "running"}, ignore_removed=True
        )
        seen: set[tuple[str, str]] = set()
        with self._lock:
            for c in containers:
                try:
                    key = self.check(c)
                except Exception:  # one server's trouble never ends the loop
                    log.exception("Claim check of container %s failed", getattr(c, "name", "?"))
                    continue
                if key is not None:
                    seen.add(key)
            for key in list(self._verified):
                if key not in seen:
                    del self._verified[key]
            self._marker_checked &= seen

    def on_start(self, container_id: str) -> None:
        """Docker's ``start`` event: check that run at once (not at the next poll)."""
        from . import docker_ops

        try:
            c = docker_ops.client().containers.get(container_id)
        except Exception as exc:  # gone again (the start wrapper refused it and it was removed)
            log.debug("container %s of a start event: %s", container_id, exc)
            return
        with self._lock:
            self.check(c, fresh=True)

    def on_die(self, container_id: str, exit_code: int | None) -> None:
        """Docker's ``die`` event: the start wrapper's refusal of a published container is recorded
        as a ``guard_stop`` (the panel then closes the ports and verifies the claim again)."""
        from . import docker_ops, drivers

        if exit_code != claimmarker.REFUSED_EXIT:
            return
        try:
            c = docker_ops.client().containers.get(container_id)
        except Exception as exc:
            log.debug("container %s of a die event: %s", container_id, exc)
            return
        labels = getattr(c, "labels", None) or {}
        try:
            instance_id = UUID(str(labels.get(docker_ops.LABEL_INSTANCE)))
        except ValueError:
            return
        if labels.get(docker_ops.LABEL_MANAGED) != "true":
            return
        if not drivers.for_container(c).guards_publish or not docker_ops.exposes_ports(c):
            return
        container_key = _run_key(c)
        log.warning("The start guard of %s refused a published start without a verified claim", c.name)
        try:
            record(
                self.settings,
                instance_id,
                container_id=container_key[0],
                started_at=container_key[1],
                code="server_unclaimed",
                message=MESSAGES["start_refused"].format(code=exit_code),
            )
        except OSError as exc:
            log.error("Could not record why %s did not start: %s", c.name, exc)

    def check(self, c: Any, *, fresh: bool = False) -> tuple[str, str] | None:
        """Check one running container; the run's key when it is one the guard watches. ``fresh``:
        the run just started (a start event), so it must have a matching claim marker."""
        from . import docker_ops, drivers

        labels = getattr(c, "labels", None) or {}
        try:
            instance_id = UUID(str(labels.get(docker_ops.LABEL_INSTANCE)))
        except ValueError:
            return None
        if labels.get(docker_ops.LABEL_MANAGED) != "true":
            return None
        driver = drivers.for_container(c)
        if not driver.guards_publish:
            return None
        state = (getattr(c, "attrs", None) or {}).get("State") or {}
        if not state.get("Running"):
            return None
        claim_file = getattr(driver, "claim_file", None)
        published = docker_ops.exposes_ports(c)
        key = _run_key(c)
        if published and claim_file and key not in self._marker_checked and (fresh or _young(key[1])):
            self._marker_checked.add(key)
            if not claimmarker.matches(self.settings, instance_id, claim_file):
                self.stop(c, instance_id, key, "server_unclaimed", MESSAGES["marker_missing"])
                return key
        if claim_file:
            # the game rewrote its settings file outside an operation of the panel (an admin in the
            # game changed a setting): the next published start needs the panel's verification again
            claimmarker.check_outside_change(self.settings, instance_id, claim_file)
        if not published:
            return None
        now = time.monotonic()
        verified = self._verified.get(key)
        if verified is not None and now - verified < RECHECK_SECONDS:
            return key
        try:
            result = driver.claim_state(self.settings, c)
        except AgentError as exc:
            log.warning("The claim of %s could not be verified: %s", c.name, exc)
            self.stop(
                c, instance_id, key, "claim_unverified", MESSAGES["claim_unverified"].format(detail=exc)
            )
            return key
        if result == "unclaimed":
            self.stop(c, instance_id, key, "server_unclaimed", MESSAGES["server_unclaimed"])
        elif result == "claimed":
            if verified is None and clear(self.settings, instance_id):
                log.info("Claim of %s verified; the guard-stop record of an earlier run was removed", c.name)
            self._verified[key] = now
        return key

    def stop(self, c: Any, instance_id: UUID, key: tuple[str, str], code: str, message: str) -> None:
        with _lock_if_free(instance_id) as locked:
            try:
                c.reload()
            except Exception as exc:  # removed meanwhile: nothing runs
                log.info("Container %s is gone before the claim guard stopped it: %s", c.name, exc)
                return
            state = (c.attrs or {}).get("State") or {}
            if _run_key(c) != key or not state.get("Running"):
                return  # another run by now: the next pass checks it
            log.warning(
                "Stopping %s (%s): %s%s", c.name, code, message, "" if locked else " (operation in progress)"
            )
            c.stop(timeout=STOP_TIMEOUT)
            self._verified.pop(key, None)
            try:
                record(
                    self.settings,
                    instance_id,
                    container_id=key[0],
                    started_at=key[1],
                    code=code,
                    message=message,
                )
            except OSError as exc:
                log.error("Could not record why %s was stopped: %s", c.name, exc)


def _young(started_at: str) -> bool:
    """Whether a run's ``State.StartedAt`` lies less than FRESH_RUN_SECONDS back."""
    import re

    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$", str(started_at or ""))
    if m is None or m.group(1).startswith("0001-"):
        return False
    fraction = (m.group(2) or ".0")[:7]
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        started = datetime.fromisoformat(f"{m.group(1)}{fraction}{zone}")
    except ValueError:
        return False
    return (datetime.now(UTC) - started).total_seconds() < FRESH_RUN_SECONDS


def handle_event(guard: ClaimGuard, event: dict[str, Any]) -> None:
    """One decoded Docker event of a managed container."""
    if event.get("Type") != "container":
        return
    action = str(event.get("Action") or event.get("status") or "")
    container_id = str(event.get("id") or (event.get("Actor") or {}).get("ID") or "")
    if not container_id:
        return
    if action == "start":
        guard.on_start(container_id)
    elif action == "die":
        attributes = (event.get("Actor") or {}).get("Attributes") or {}
        try:
            code: int | None = int(attributes.get("exitCode"))
        except (TypeError, ValueError):
            code = None
        guard.on_die(container_id, code)


def watch_events(
    settings: Settings,
    stop: threading.Event,
    guard: ClaimGuard,
    *,
    window: int = EVENT_WINDOW_SECONDS,
    now: Callable[[], float] = time.time,
) -> None:
    """Docker's ``start``/``die`` events of managed containers, in windows of ``window`` seconds
    (``since`` = the end of the previous window, so none is lost between two requests, and the thread
    notices ``stop`` within a window)."""
    from . import docker_ops

    since = int(now())
    failing = False
    while not stop.is_set():
        until = int(now()) + window
        try:
            events = docker_ops.client().events(
                since=since,
                until=until,
                decode=True,
                filters={
                    "type": "container",
                    "event": ["start", "die"],
                    "label": f"{docker_ops.LABEL_MANAGED}=true",
                },
            )
            for event in events:
                try:
                    handle_event(guard, event)
                except Exception:  # one event's trouble never ends the watch
                    log.exception("Claim guard: handling a Docker event failed")
                if stop.is_set():
                    break
            since = until
            failing = False
        except Exception as exc:  # Docker not reachable: the poll still runs; try again
            if not failing:
                log.warning("Claim guard: cannot read Docker's events (%s); retrying", exc)
            failing = True
            stop.wait(EVENT_RETRY_SECONDS)


def run(settings: Settings, stop: threading.Event, interval: float = POLL_SECONDS) -> None:
    """The loop (a thread of the agent, started with it, ended by ``stop``), and the events thread."""
    guard = ClaimGuard(settings)
    threading.Thread(
        target=watch_events, args=(settings, stop, guard), name="claim-guard-events", daemon=True
    ).start()
    failing = False
    while not stop.is_set():
        try:
            guard.check_all()
            failing = False
        except Exception as exc:  # Docker not reachable: try again
            if not failing:
                log.warning("Claim guard: cannot list the running containers (%s); retrying", exc)
            failing = True
        stop.wait(interval)
