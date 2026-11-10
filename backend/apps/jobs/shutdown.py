"""The job worker is being stopped: long waits inside jobs end at once.

Celery's warm shutdown waits for the running jobs. A job that waits for an agent operation
(``AgentClient.wait_operation``, up to hours), for a countdown or for the lock of a server
(``JobContext.sleep_until``) would hold the worker for minutes, longer than Docker gives it when
the host stops. So when the worker's main process gets its stop signal (``worker_shutting_down``,
mcpanel/celery.py) it leaves a flag file named after its process id; the pool processes that run
the jobs check for their parent's flag in ``JobContext.check_cancelled`` and in the guard of
``wait_operation``, which then raise ``runner.WorkerStopping``. The job is failed as interrupted
right away; the agent goes on with an operation it has started.

Not every stop signal is a stop: the panel container's supervisor also sends one to make the
worker start again after a restart of Redis, and then the worker finishes its job without a time
limit (backend/container/supervise.py). Under the supervisor a flag therefore only counts while
the supervisor's status file says ``"phase": "stopping"``.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading

FLAG_PREFIX = "mcpanel-worker-stopping-"
_requested = threading.Event()  # set in the process that got the signal (solo/thread pools run jobs there)
_confirmed = False  # once a stop is confirmed it stays one


def _flag_dir() -> str:
    return os.environ.get("MCPANEL_WORKER_FLAG_DIR") or tempfile.gettempdir()


def flag_path(pid: int) -> str:
    return os.path.join(_flag_dir(), f"{FLAG_PREFIX}{pid}")


def request_stop() -> None:
    """Called in the worker's main process when it begins to shut down. Never raises."""
    _requested.set()
    with contextlib.suppress(OSError):
        fd = os.open(flag_path(os.getpid()), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.close(fd)


def clear() -> None:
    """Called when a worker's main process starts: a flag left by an earlier process with the
    same id (process ids repeat in a container) must not stop this one's jobs."""
    global _confirmed
    _requested.clear()
    _confirmed = False
    with contextlib.suppress(OSError):
        os.unlink(flag_path(os.getpid()))


def _supervisor_stopping() -> bool:
    path = os.environ.get("MCPANEL_SUPERVISOR_STATUS")
    if not path:
        return True  # one role per container: a stop signal is a stop
    try:
        with open(path) as fh:
            return json.load(fh).get("phase") == "stopping"
    except (OSError, ValueError, AttributeError):
        return True


def stop_requested() -> bool:
    """True when the worker running this job is being stopped (see the module docstring)."""
    global _confirmed
    if _confirmed:
        return True
    flagged = _requested.is_set() or os.path.exists(flag_path(os.getppid()))
    if flagged and _supervisor_stopping():
        _confirmed = True
    return _confirmed
