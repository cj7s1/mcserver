"""Container health check of the panel image (stdlib only).

``web`` mode (one role per container): requests ``/api/health`` (database + cache check) on the
local uvicorn listener, nothing else.

``all`` mode (one panel container, backend/container/supervise.py): healthy only when all of
this holds, and the reason names the part that is down:

* the supervisor's status file is fresh and every child (redis, web, worker, periodic, beat)
  is running;
* both Celery workers answered a ping within the last PING_MAX_AGE seconds (a worker whose
  process is alive but no longer consumes is caught here);
* ``/api/health`` answers ok;
* the last reconcile tick started less than TICK_MAX_AGE seconds ago (scheduler and periodic
  worker really do their work; measured from the start of the task).

There is no allowance for "just started": after a start the container stays "starting" until
both workers have answered and the first tick has run (about 20 s), so "healthy" always means
checked. Docker reports "unhealthy" only after three failed checks in a row, which is longer
than a restart of any single child takes.

The mode is ``all`` when the first argument says so (deploy/docker-compose.yml) or when the
supervisor's status file exists.

Django validates the Host header against ``MCPANEL_ALLOWED_HOSTS``, so the probe sends a host
name taken from that list instead of ``127.0.0.1``, which a production configuration may not
allow. Proxies from the environment are ignored so an egress proxy setting can never capture the
loopback probe.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

URL = "http://127.0.0.1:8000/api/health"
STATUS_PATH = os.path.join(os.environ.get("MCPANEL_RUN_DIR", "/run/mcpanel"), "supervisor.json")
STATUS_MAX_AGE = 30.0  # the supervisor rewrites its status every 5 s
PING_MAX_AGE = 120.0  # a worker must have answered a ping within this time
TICK_MAX_AGE = 180.0  # reconcile runs every 10 s; its no-overlap lock lasts at most 120 s


def probe_host() -> str:
    hosts = [h.strip() for h in os.environ.get("MCPANEL_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return "localhost"  # settings.py default: localhost,127.0.0.1
    if "localhost" in hosts or "*" in hosts:
        return "localhost"
    if "127.0.0.1" in hosts:
        return "127.0.0.1"
    # A leading dot (".example.com") also matches the bare domain.
    return hosts[0].lstrip(".") or "localhost"


def fetch_health() -> tuple[dict[str, Any] | None, str | None]:
    """The body of /api/health, or the reason why there is none."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(URL, headers={"Host": probe_host(), "Accept": "application/json"})
    try:
        with opener.open(request, timeout=8) as response:
            body = json.loads(response.read(65536) or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            checks = json.loads(exc.read(65536) or b"{}").get("checks")
        except (OSError, ValueError, AttributeError):
            checks = None
        return None, f"HTTP {exc.code}" + (f" {checks}" if checks else "")
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if not isinstance(body, dict):
        return None, "unexpected answer"
    return body, None


def read_status(path: str = STATUS_PATH) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, f"no status file ({path})"
    except (OSError, ValueError) as exc:
        return None, f"unreadable status file ({exc})"
    return (data, None) if isinstance(data, dict) else (None, "unusable status file")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def problems_all(
    status: dict[str, Any] | None,
    status_error: str | None,
    health: dict[str, Any] | None,
    health_error: str | None,
    now: float,
) -> list[str]:
    """Why the single panel container is not healthy (empty list: healthy)."""
    problems: list[str] = []
    children: dict[str, Any] = {}
    if status is None:
        problems.append(f"supervisor: {status_error or 'no status'}")
    else:
        written = _number(status.get("time"))
        if written is None or now - written > STATUS_MAX_AGE:
            age = "unknown" if written is None else f"{now - written:.0f} s"
            problems.append(f"supervisor: its status is {age} old (is it running, can it write {STATUS_PATH}?)")
        if status.get("phase") == "stopping":
            problems.append("supervisor: stopping")
        raw = status.get("children")
        children = raw if isinstance(raw, dict) else {}
        if not children:
            problems.append("supervisor: no children in its status")
        for name, child in children.items():
            state = child.get("state") if isinstance(child, dict) else None
            if state not in ("running", "idle"):  # (idle: the Discord bot while it is off)
                detail = ""
                if isinstance(child, dict) and child.get("last_exit") is not None:
                    detail = f" (last exit {child['last_exit']}, {child.get('restarts', 0)} restarts)"
                problems.append(f"{name}: {state or 'unknown'}{detail}")

    if status is not None:
        owners = status.get("worker_children") if isinstance(status.get("worker_children"), dict) else {}
        workers = status.get("workers") if isinstance(status.get("workers"), dict) else {}
        for node, answered in workers.items():
            owner = str(owners.get(node) or node)
            last = _number(answered)
            if last is not None and now - last <= PING_MAX_AGE:
                continue
            since = "has not answered a ping yet" if last is None else f"no answer to a ping for {now - last:.0f} s"
            problems.append(f"{owner}: {since} ({node})")

    if health is None:
        problems.append(f"web: /api/health failed ({health_error or 'no answer'})")
    else:
        checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
        for name, result in checks.items():
            if result != "ok":
                problems.append(f"{name}: {result}")
        if health.get("status") != "ok" and all(result == "ok" for result in checks.values()):
            problems.append(f"web: /api/health says {health.get('status')!r}")
        age = _number(health.get("tick_age_seconds"))
        if age is None or age > TICK_MAX_AGE:
            what = "no reconcile tick yet" if age is None else f"the last reconcile tick started {age:.0f} s ago"
            problems.append(f"beat/periodic: {what} (the scheduler or the workers are not doing their work)")
    return problems


def main(argv: list[str]) -> int:
    health, health_error = fetch_health()
    all_mode = (argv[:1] == ["all"]) or os.path.exists(STATUS_PATH)
    if all_mode:
        status, status_error = read_status()
        problems = problems_all(status, status_error, health, health_error, time.time())
    elif health is None:
        problems = [str(health_error)]
    elif health.get("status") != "ok":
        problems = [str(health)]
    else:
        problems = []
    if problems:
        print("unhealthy: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
