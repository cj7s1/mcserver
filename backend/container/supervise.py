#!/usr/bin/env python3
"""Process supervisor of the mcpanel/panel image in ``all`` mode (standard library only).

One container, one supervisor, up to five children: Redis (unless MCPANEL_REDIS_URL points to a
server elsewhere), the web server, the job worker, the periodic worker and the scheduler. It runs
as the image's unprivileged user under the container's init and needs no capability: it only
starts and signals its own children.

Behaviour (pinned by backend/tests/test_supervise.py and the container tests in CI):

* It never exits because a child failed. A child that exits is started again after a capped
  backoff (1, 2, 4 ... 30 s); until then the status file names it and the container is unhealthy.
* Start order: Redis first, the others only once Redis answers PING. If Redis dies it is started
  again and, once it answers, the others are restarted so that queue consumers and live-event
  subscriptions are rebuilt and queued jobs are sent again (mcpanel/celery.py). The job worker
  is not killed for that: it gets SIGTERM (Celery's warm shutdown), finishes the job it is
  running, however long that takes, and is started again when it has exited by itself.
* Redis never starts with a dump file in its directory: what could be one is removed first
  (the web process can write there, and cache values are pickles).
* Every output line of a child is passed on with the child's name in front; a child that never
  stops writing is read in bounded pieces, so it cannot keep the supervisor from its other work.
* The status file is JSON in a private 0700 directory. A failed write is logged, never raised.
* Stop (SIGTERM / SIGINT): web server, scheduler and job worker first, then the periodic worker
  (it still delivers the notifications of jobs that just finished), then Redis. Every child has
  its own grace period and everything is gone within STOP_DEADLINE seconds, which is below the
  container's stop_grace_period of 2 minutes.
* Both Celery workers are pinged (``celery inspect ping``) every PING_INTERVAL seconds; the time
  of each answer is in the status file for the health check (backend/container/healthcheck.py).

Commands: ``run`` (default), ``guard`` (refuse to start next to the worker / beat containers of
the previous layout), ``step NAME COMMAND...`` (run one command to its end with NAME in front of
its output: the migrations before the children start), ``status`` (print the status file; exit 1
unless everything runs).
"""

from __future__ import annotations

import ipaddress
import json
import os
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

RUN_DIR = "/run/mcpanel"
STATUS_FILE = "supervisor.json"

BACKOFF_FIRST = 1.0  # seconds before a child that exited is started again
BACKOFF_MAX = 30.0  # cap of the doubling delay
STABLE_AFTER = 60.0  # a child that ran this long starts again at BACKOFF_FIRST
READY_TIMEOUT = 30.0  # a child with a readiness check that is not ready by then is started again
STOP_DEADLINE = 116.0  # everything is gone this long after the stop signal
STAGE_RESERVE = 3.0  # seconds of the deadline kept for every later stop stage
HEARTBEAT = 5.0  # the status file is rewritten at least this often
PING_INTERVAL = 45.0  # seconds between two worker pings (the health check wants one within 120 s)
PING_RETRY = 15.0  # ... after a ping that a worker did not answer
PING_TIMEOUT = 30.0  # a ping command still running after this is killed
PING_WARMUP = 8.0  # a worker is not pinged before it ran this long
READ_CHUNK = 65536  # bytes per read of a child's output
READ_CHUNKS_PER_ROUND = 16  # reads per child and round: a child that floods its output is read 1 MiB at a time
READ_CHUNKS_AT_EXIT = 64  # ... and what an exited child left in its pipe (at most the pipe's size) is read whole
# What Redis could load when it starts, or leaves behind when someone told it to save (file names
# of Redis 7 with default names; "temp-*.rdb" is a save in progress).
REDIS_DATA_FILES = ("dump.rdb", "appendonly.aof", "appendonlydir")
EX_CONFIG = 78

WAITING, STARTING, RUNNING, BACKOFF, STOPPING, STOPPED = "waiting", "starting", "running", "backoff", "stopping", "stopped"


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: tuple[str, ...]
    grace: float  # seconds between SIGTERM and SIGKILL when it is stopped
    stage: int = 0  # stop order: stage 0 first; a stage is stopped when the earlier ones are gone
    after: tuple[str, ...] = ()  # started only while these children are ready; restarted when they come back
    env: Mapping[str, str] | None = None  # None: the supervisor's environment
    ready: Callable[[], bool] | None = None  # None: ready as soon as it runs
    # Called before every start; what it returns is logged. If it raises, the child is not
    # started and tried again later.
    before_start: Callable[[], str | None] | None = None
    # A restart the supervisor decides on (Redis came back) sends SIGTERM and then waits for the
    # child to exit by itself, without a kill after ``grace``: the job worker finishes its job.
    finish_before_restart: bool = False


@dataclass(frozen=True)
class Probe:
    """A short command that is run again and again: ``celery inspect ping`` of both workers."""

    argv: tuple[str, ...]
    nodes: Mapping[str, str]  # Celery node name -> child that runs it
    interval: float = PING_INTERVAL
    retry: float = PING_RETRY
    timeout: float = PING_TIMEOUT


class Child:
    def __init__(self, spec: ChildSpec, now: float) -> None:
        self.spec = spec
        self.state = WAITING
        self.since = now  # wall clock of the last state change
        self.pid: int | None = None
        self.started = 0.0  # monotonic
        self.next_start = 0.0
        self.kill_at: float | None = None
        self.killed = False
        self.restart_wanted = False
        self.starts = 0
        self.failures = 0
        self.last_exit: int | None = None


def describe_exit(code: int | None) -> str:
    if code is None:
        return "never started"
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exit code {code}"


class Lines:
    """Splits a child's output into lines and writes each with the child's name in front."""

    LIMIT = 16384  # a longer line is passed on in pieces

    def __init__(self, name: str, write: Callable[[bytes], None]) -> None:
        self.prefix = f"[{name}] ".encode()
        self.write = write
        self.rest = b""

    def feed(self, data: bytes) -> None:
        *lines, self.rest = (self.rest + data).split(b"\n")
        for line in lines:
            self.write(self.prefix + line.rstrip(b"\r") + b"\n")
        if len(self.rest) > self.LIMIT:
            self.flush()

    def flush(self) -> None:
        if self.rest:
            self.write(self.prefix + self.rest + b"\n")
            self.rest = b""


def write_stdout(data: bytes) -> None:
    """Write to the container log. A closed or full log must never stop the supervisor."""
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(1, view) :]
    except OSError:
        pass


class Processes:
    """The operating system as the supervisor uses it (the tests replace it by a fake)."""

    def __init__(self, write: Callable[[bytes], None] = write_stdout) -> None:
        self.write = write
        self.selector = selectors.DefaultSelector()
        self.popen: dict[int, subprocess.Popen[bytes]] = {}
        self.fds: dict[int, int] = {}  # pid -> read end of its output pipe
        self.sinks: dict[int, Any] = {}  # fd -> Lines or bytearray
        # Signals wake the loop at once instead of at the next timeout.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self.selector.register(self._wake_r, selectors.EVENT_READ, None)

    def wake_on_signals(self) -> None:
        signal.set_wakeup_fd(self._wake_w.fileno(), warn_on_full_buffer=False)
        signal.signal(signal.SIGCHLD, lambda *_: None)

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def spawn(self, name: str, argv: tuple[str, ...], env: Mapping[str, str], capture: bool = False) -> int:
        """Start a command in its own session. Its output is passed on line by line, or kept (capture)."""
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv comes from this file, never from input
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=write_fd,
                stderr=subprocess.DEVNULL if capture else write_fd,
                env=dict(env),
                start_new_session=True,
            )
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)
        os.set_blocking(read_fd, False)
        self.popen[proc.pid] = proc
        self.fds[proc.pid] = read_fd
        self.sinks[read_fd] = bytearray() if capture else Lines(name, self.write)
        self.selector.register(read_fd, selectors.EVENT_READ, read_fd)
        return proc.pid

    def signal(self, pid: int, sig: int, group: bool = False) -> None:
        try:
            if group:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _read(self, fd: int, chunks: int = READ_CHUNKS_PER_ROUND) -> bool:
        """Pass on what the pipe holds now, at most ``chunks`` reads of it. False at end of file.

        The limit is what keeps a child that writes without a pause from holding the loop: what
        is left stays in the pipe (the child waits in its write) and is read in the next round.
        """
        sink = self.sinks.get(fd)
        if sink is None:
            return False
        for _ in range(chunks):
            try:
                data = os.read(fd, READ_CHUNK)
            except BlockingIOError:
                return True
            except OSError:
                return False
            if not data:
                return False
            if isinstance(sink, bytearray):
                if len(sink) < 1 << 20:
                    sink += data
            else:
                sink.feed(data)
        return True

    def _close(self, fd: int) -> Any:
        sink = self.sinks.pop(fd, None)
        if sink is None:
            return None
        if isinstance(sink, Lines):
            sink.flush()
        try:
            self.selector.unregister(fd)
        except (KeyError, ValueError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        return sink

    def pump(self, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for output or a signal and pass the output on."""
        for key, _events in self.selector.select(max(0.0, timeout)):
            if key.data is None:
                try:
                    self._wake_r.recv(4096)
                except OSError:
                    pass
            elif not self._read(key.data):
                # End of file: stop watching it. reap() closes it when the child is gone; closing
                # it here would lose captured output and let the number be reused too early.
                try:
                    self.selector.unregister(key.data)
                except (KeyError, ValueError):
                    pass

    def reap(self) -> list[tuple[int, int, bytes]]:
        """Exited children as (pid, exit code, captured output).

        What is left of a child's process group is killed before the child is reaped (its pid
        cannot be reused before that), so a worker killed with SIGKILL never leaves pool
        processes behind. As PID 1 (no init) this also reaps adopted orphans.
        """
        done = []
        while True:
            try:
                info = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                break
            if info is None:
                break
            pid = info.si_pid
            if pid in self.popen:
                self.signal(pid, signal.SIGKILL, group=True)
            try:
                _, status = os.waitpid(pid, 0)
            except ChildProcessError:
                continue
            proc = self.popen.pop(pid, None)
            if proc is None:
                continue
            code = os.waitstatus_to_exitcode(status)
            proc.returncode = code
            output = b""
            fd = self.fds.pop(pid, None)
            if fd is not None:
                self._read(fd, READ_CHUNKS_AT_EXIT)
                sink = self._close(fd)
                if isinstance(sink, bytearray):
                    output = bytes(sink)
            done.append((pid, code, output))
        return done


def parse_pongs(output: bytes) -> set[str]:
    """Names of the workers that answered, from the output of ``celery inspect ping --json``."""
    for line in reversed(output.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return {str(node) for node, reply in data.items() if isinstance(reply, dict) and reply.get("ok") == "pong"}
    return set()


class Supervisor:
    def __init__(
        self,
        specs: Iterable[ChildSpec],
        procs: Any,
        *,
        status_path: str | None = None,
        probe: Probe | None = None,
        environ: Mapping[str, str] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.procs = procs
        self.children = {spec.name: Child(spec, procs.time()) for spec in specs}
        self.last_stage = max((c.spec.stage for c in self.children.values()), default=0)
        self.status_path = status_path
        self.probe = probe
        self.environ = dict(os.environ if environ is None else environ)
        self.log = log or self._log
        self.phase = "starting"
        self.started = procs.time()
        self.stop_requested = False
        self.stop_started: float | None = None
        self.done = False
        self.pongs: dict[str, float] = {}  # worker node -> wall clock of its last answer
        self.ping_note = "not pinged yet"
        self._probe_pid: int | None = None
        self._probe_started = 0.0
        self._probe_due = 0.0
        self._dirty = True
        self._last_status = float("-inf")
        self._status_error: str | None = None
        self._last_error = ("", float("-inf"))

    # --- logging / status ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        write_stdout(f"[supervisor] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n".encode("utf-8", "replace"))

    def _set(self, child: Child, state: str) -> None:
        if child.state != state:
            child.state = state
            child.since = self.procs.time()
            self._dirty = True

    def snapshot(self) -> dict[str, Any]:
        return {
            "time": self.procs.time(),
            "pid": os.getpid(),
            "phase": self.phase,
            "started": self.started,
            "children": {
                name: {
                    "state": c.state,
                    "pid": c.pid,
                    "since": c.since,
                    "restarts": max(0, c.starts - 1),
                    "last_exit": c.last_exit,
                }
                for name, c in self.children.items()
            },
            "workers": {node: self.pongs.get(node) for node in (self.probe.nodes if self.probe else {})},
            "worker_children": dict(self.probe.nodes) if self.probe else {},
            "ping": self.ping_note,
        }

    def write_status(self) -> None:
        """Rewrite the status file. Never raises: a full disk must not end supervision."""
        self._dirty = False
        self._last_status = self.procs.monotonic()
        if not self.status_path:
            return
        try:
            data = json.dumps(self.snapshot()).encode()
            tmp = f"{self.status_path}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, self.status_path)
            if self._status_error is not None:
                self.log("the status file can be written again")
            self._status_error = None
        except Exception as exc:
            if str(exc) != self._status_error:
                self.log(f"cannot write the status file ({exc}); the health check reports unhealthy until this is fixed")
            self._status_error = str(exc)

    # --- one round ---------------------------------------------------------------------------
    def step(self) -> None:
        now = self.procs.monotonic()
        for pid, code, output in self.procs.reap():
            self._exited(pid, code, output, now)
        if self.stop_requested and self.stop_started is None:
            self.stop_started = now
            self.phase = "stopping"
            self._dirty = True
            self.log("stop requested: " + ", ".join(self._stop_plan()))
        if self.stop_started is None:
            self._supervise(now)
            self._ping(now)
        else:
            self._stop(now)
        if self._dirty or now - self._last_status >= HEARTBEAT:
            self.write_status()

    def timeout(self) -> float:
        """Seconds the main loop may sleep before the next round."""
        return 0.25 if self.stop_started is not None or any(c.state == STARTING for c in self.children.values()) else 0.5

    def run(self) -> int:
        self.log("starting: " + ", ".join(self.children))
        while not self.done:
            try:
                self.procs.pump(self.timeout())
                self.step()
            except Exception:  # a bug here must not leave the children without a supervisor
                text = traceback.format_exc()
                now = self.procs.monotonic()
                if text != self._last_error[0] or now - self._last_error[1] > 30:
                    self.log("internal error (supervision goes on):\n" + text)
                    self._last_error = (text, now)
                time.sleep(0.5)
        self.write_status()
        self.log("all children are gone; exiting")
        return 0

    # --- running -------------------------------------------------------------------------------
    def _backoff(self, child: Child, now: float, ran: float) -> float:
        child.failures = 1 if ran >= STABLE_AFTER else child.failures + 1
        delay = min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** min(child.failures - 1, 16))
        child.next_start = now + delay
        self._set(child, BACKOFF)
        return delay

    def _exited(self, pid: int, code: int, output: bytes, now: float) -> None:
        if pid == self._probe_pid:
            self._probe_done(code, output, now)
            return
        child = next((c for c in self.children.values() if c.pid == pid), None)
        if child is None:
            return
        name, ran = child.spec.name, now - child.started
        child.pid, child.last_exit, child.kill_at, child.killed = None, code, None, False
        self._dirty = True
        if self.stop_started is not None:
            self._set(child, STOPPED)
            self.log(f"{name} stopped ({describe_exit(code)})")
        elif child.restart_wanted:
            child.restart_wanted = False
            child.next_start = now
            self._set(child, BACKOFF)
            self.log(f"{name} stopped for its restart ({describe_exit(code)})")
        else:
            delay = self._backoff(child, now, ran)
            self.log(f"{name} exited ({describe_exit(code)}) after {ran:.0f} s; starting it again in {delay:.0f} s")

    def _ready(self, name: str) -> bool:
        child = self.children.get(name)
        return child is None or child.state == RUNNING

    def _start(self, child: Child, now: float) -> None:
        name = child.spec.name
        env = dict(self.environ if child.spec.env is None else child.spec.env)
        if child.spec.before_start is not None:
            try:
                note = child.spec.before_start()
            except Exception as exc:
                delay = self._backoff(child, now, 0.0)
                self.log(f"{name} is not started: its preparation failed ({exc}); trying again in {delay:.0f} s")
                return
            if note:
                self.log(f"before the start of {name}: {note}")
        try:
            child.pid = self.procs.spawn(name, child.spec.argv, env)
        except OSError as exc:
            delay = self._backoff(child, now, 0.0)
            self.log(f"{name} could not be started ({exc}); trying again in {delay:.0f} s")
            return
        child.started = now
        child.starts += 1
        self._set(child, STARTING if child.spec.ready else RUNNING)
        self.log(f"started {name} (pid {child.pid})")

    def _restart(self, child: Child, now: float, why: str) -> None:
        child.restart_wanted = True
        if child.spec.finish_before_restart:
            # No kill deadline: a backup or restore that runs for hours is not cut off because
            # Redis was started again. The worker takes no new job until it is back.
            self.log(f"restarting {child.spec.name}: {why} (it finishes what it is doing first, without a time limit)")
            child.kill_at, child.killed = None, False
        else:
            self.log(f"restarting {child.spec.name}: {why}")
            child.kill_at, child.killed = now + child.spec.grace, False
        self._set(child, STOPPING)
        self.procs.signal(child.pid, signal.SIGTERM)

    def _kill(self, child: Child, why: str) -> None:
        self.log(f"{child.spec.name} {why}: killing its process group")
        child.killed = True
        self.procs.signal(child.pid, signal.SIGKILL, group=True)

    def _supervise(self, now: float) -> None:
        for child in self.children.values():
            name = child.spec.name
            if child.state in (WAITING, BACKOFF):
                if now >= child.next_start and all(self._ready(dep) for dep in child.spec.after):
                    self._start(child, now)
            elif child.state == STARTING and not child.killed:
                try:
                    ready = bool(child.spec.ready()) if child.spec.ready else True
                except Exception:
                    ready = False
                if ready:
                    self._set(child, RUNNING)
                    self.log(f"{name} is ready")
                    for other in self.children.values():
                        if name in other.spec.after and other.state in (RUNNING, STARTING) and other.pid is not None:
                            self._restart(other, now, f"{name} was started again")
                elif now - child.started > READY_TIMEOUT:
                    self._kill(child, f"is not ready after {READY_TIMEOUT:.0f} s")
            elif child.state == STOPPING and not child.killed and child.kill_at is not None and now >= child.kill_at:
                self._kill(child, f"did not stop within {child.spec.grace:.0f} s")
        if self.phase == "starting" and all(c.state == RUNNING for c in self.children.values()):
            self.phase = "running"
            self._dirty = True
            self.log("all children are running")

    # --- worker ping -----------------------------------------------------------------------------
    def _ping(self, now: float) -> None:
        probe = self.probe
        if probe is None:
            return
        if self._probe_pid is not None:
            if now - self._probe_started > probe.timeout:
                self.procs.signal(self._probe_pid, signal.SIGKILL, group=True)
            return
        workers = [self.children[name] for name in set(probe.nodes.values()) if name in self.children]
        if now < self._probe_due or any(c.state != RUNNING or now - c.started < PING_WARMUP for c in workers):
            return
        try:
            self._probe_pid = self.procs.spawn("ping", probe.argv, self.environ, capture=True)
            self._probe_started = now
        except OSError as exc:
            self._probe_due = now + probe.retry
            self._note(f"the ping command could not be started ({exc})")

    def _note(self, note: str) -> None:
        if note != self.ping_note:
            if note != "ok" or self.ping_note != "not pinged yet":
                self.log(f"worker ping: {note}")
            self.ping_note = note
            self._dirty = True

    def _probe_done(self, code: int, output: bytes, now: float) -> None:
        probe = self.probe
        self._probe_pid = None
        if probe is None:
            return
        answered = parse_pongs(output)
        for node in probe.nodes:
            if node in answered:
                self.pongs[node] = self.procs.time()
        missing = [node for node in probe.nodes if node not in answered]
        self._probe_due = now + (probe.retry if missing else probe.interval)
        self._dirty = True
        if missing:
            self._note(f"no answer from {', '.join(missing)} ({describe_exit(code)})")
        else:
            self._note("ok")

    # --- stopping ----------------------------------------------------------------------------------
    def _stop_plan(self) -> list[str]:
        stages: dict[int, list[str]] = {}
        for child in self.children.values():
            stages.setdefault(child.spec.stage, []).append(f"{child.spec.name} ({child.spec.grace:.0f} s)")
        return [" + ".join(stages[stage]) for stage in sorted(stages)] + [f"deadline {STOP_DEADLINE:.0f} s"]

    def _stop(self, now: float) -> None:
        assert self.stop_started is not None
        if self._probe_pid is not None:
            self.procs.signal(self._probe_pid, signal.SIGKILL, group=True)
        live = [c for c in self.children.values() if c.pid is not None]
        for child in self.children.values():
            if child.pid is None:
                self._set(child, STOPPED)
        if not live:
            self.done = True
            return
        elapsed = now - self.stop_started
        if elapsed >= STOP_DEADLINE + 3:
            self.log("giving up on: " + ", ".join(c.spec.name for c in live))
            self.done = True
            return
        stage = min(c.spec.stage for c in live)
        for child in live:
            child.restart_wanted = False
            limit = self.stop_started + STOP_DEADLINE - STAGE_RESERVE * (self.last_stage - child.spec.stage)
            if child.spec.stage == stage and child.state != STOPPING:
                child.kill_at, child.killed = min(now + child.spec.grace, limit), False
                self._set(child, STOPPING)
                self.procs.signal(child.pid, signal.SIGTERM)
            elif child.state == STOPPING and child.kill_at is None:
                # was finishing its job for a restart (no deadline): the stop gives it its grace
                # period from now. No second SIGTERM: Celery would take that as "stop at once".
                child.kill_at = min(now + child.spec.grace, limit)
            elif child.state == STOPPING and child.kill_at is not None and child.kill_at > limit:
                child.kill_at = limit  # was being restarted when the stop signal came
            if child.killed:
                continue
            if child.state == STOPPING and child.kill_at is not None and now >= child.kill_at:
                self._kill(child, f"did not stop within its grace period ({child.spec.grace:.0f} s)")
            elif elapsed >= STOP_DEADLINE:
                self._kill(child, f"is still there at the deadline ({STOP_DEADLINE:.0f} s)")


# --- start-up guard ------------------------------------------------------------------------------
OLD_SERVICES = ("worker", "worker-periodic", "beat")


def connected_networks(route_file: str = "/proc/net/route") -> list[ipaddress.IPv4Network]:
    """IPv4 networks this container is directly attached to (its Compose networks)."""
    networks = []
    with open(route_file) as fh:
        for line in fh.readlines()[1:]:
            fields = line.split()
            if len(fields) < 8 or fields[0] == "lo" or fields[2] != "00000000":
                continue
            address = socket.inet_ntoa(struct.pack("<L", int(fields[1], 16)))
            mask = socket.inet_ntoa(struct.pack("<L", int(fields[7], 16)))
            network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
            if 0 < network.prefixlen < 32 and isinstance(network, ipaddress.IPv4Network):
                networks.append(network)
    return networks


def resolve_names(names: Iterable[str], timeout: float = 5.0) -> dict[str, list[str]]:
    """IPv4 addresses of the names that resolve within ``timeout`` seconds (all looked up at once)."""
    found: dict[str, list[str]] = {}

    def lookup(name: str) -> None:
        try:
            # the trailing dot keeps the resolver from trying the host's search domains
            infos = socket.getaddrinfo(f"{name}.", None, socket.AF_INET, socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return
        found[name] = sorted({str(info[4][0]) for info in infos})

    threads = [threading.Thread(target=lookup, args=(name,), daemon=True) for name in names]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return dict(found)


def old_layout_containers(resolved: Mapping[str, Iterable[str]], networks: Iterable[ipaddress.IPv4Network]) -> dict[str, str]:
    """Service names of the previous layout that answer from one of this container's own networks."""
    networks = list(networks)
    found = {}
    for name, addresses in resolved.items():
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if any(ip in network for network in networks):
                found[name] = address
                break
    return found


def guard(environ: Mapping[str, str]) -> int:
    """Refuse to start while worker / worker-periodic / beat containers of the old layout run.

    They would run old code next to the processes of this container: two schedulers and two
    sets of workers. Happens when the stack is upgraded with a plain ``docker compose up -d``.
    """
    if environ.get("MCPANEL_SKIP_LAYOUT_GUARD", "").strip().lower() in ("1", "true", "yes", "on"):
        return 0
    try:
        found = old_layout_containers(resolve_names(OLD_SERVICES), connected_networks())
    except Exception as exc:  # the guard is a second line of defence: never block a start on its own failure
        print(f"[guard] could not check for containers of the previous layout ({exc}); starting anyway", file=sys.stderr)
        return 0
    if not found:
        return 0
    names = ", ".join(f"{name} ({address})" for name, address in sorted(found.items()))
    message = (
        f"containers of the previous layout are still running: {names}.\n"
        "This container now runs the web server, both workers and the scheduler itself. Next to the\n"
        "old ones every schedule would run twice, with old code. Remove the old containers with\n"
        "    cd <mcpanel source>/deploy && ./scripts/upgrade.sh run\n"
        "or  cd <mcpanel source>/deploy && docker compose up -d --remove-orphans\n"
        "Refusing to start (MCPANEL_SKIP_LAYOUT_GUARD=true skips this check)."
    )
    print("\n".join(f"[guard] {line}" for line in message.splitlines()), file=sys.stderr)
    return EX_CONFIG


# --- the panel's children ------------------------------------------------------------------------
def embedded_redis_socket(environ: Mapping[str, str]) -> str | None:
    """Socket path of the Redis this container runs itself, None when Redis is somewhere else."""
    url = environ.get("MCPANEL_REDIS_URL", "")
    if not url.startswith("unix://"):
        return None
    return urlsplit(url).path or None


def redis_answers(path: str, timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(b"PING\r\n")
            return sock.recv(16).startswith(b"+PONG")
    except OSError:
        return False


def remove_redis_data(directory: str) -> list[str]:
    """Remove what Redis would load at its start from ``directory``; returns the names removed.

    Redis is started with saving off, so a dump file there was not written by this container's
    own Redis on its way down: someone told Redis to save, or wrote the file. Redis would load
    it, and the values of the cache are pickles. A link is removed, never followed. Raises
    OSError when something cannot be removed: Redis is not started next to it.
    """
    removed = []
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return removed
    for name in names:
        if name not in REDIS_DATA_FILES and not (name.startswith("temp-") and name.endswith(".rdb")):
            continue
        path = os.path.join(directory, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
        removed.append(name)
    return removed


def panel_plan(environ: Mapping[str, str], run_dir: str) -> tuple[list[ChildSpec], Probe]:
    entrypoint = "mcpanel-entrypoint"
    host = socket.gethostname()
    specs: list[ChildSpec] = []
    redis_socket = embedded_redis_socket(environ)
    after: tuple[str, ...] = ()
    if redis_socket:
        after = ("redis",)
        redis_dir = os.path.dirname(redis_socket) or run_dir

        def clean_redis_dir() -> str | None:
            removed = remove_redis_data(redis_dir)
            return f"removed {', '.join(sorted(removed))} from {redis_dir} (Redis would have loaded it)" if removed else None

        specs.append(
            ChildSpec(
                "redis",
                # No TCP listener at all, only a socket in a directory of this user. Nothing is
                # written to disk and nothing is loaded from it (jobs and state are in PostgreSQL,
                # queued jobs are sent again): no save points, no append-only file, and the dump
                # file it would load at its start (dbfilename in dir) is removed before every start.
                (
                    "redis-server",
                    *("--port", "0", "--unixsocket", redis_socket, "--unixsocketperm", "700"),
                    *("--save", "", "--appendonly", "no", "--dir", redis_dir, "--dbfilename", "dump.rdb"),
                ),
                grace=5,
                stage=2,
                # Redis gets none of the panel's secrets
                env={key: environ[key] for key in ("PATH", "TZ", "LANG") if environ.get(key)},
                ready=lambda: redis_answers(redis_socket),
                before_start=clean_redis_dir,
            )
        )
    specs += [
        # the same command as `web`, without the migration step (entrypoint.sh)
        ChildSpec("web", (entrypoint, "serve"), grace=15, stage=0, after=after),
        # after a restart of Redis it finishes the job it is running before it is restarted
        ChildSpec(
            "worker", (entrypoint, "worker", "-n", "celery@%h"), grace=110, stage=0, after=after, finish_before_restart=True
        ),
        # always 2 slots, whatever MCPANEL_WORKER_CONCURRENCY says; WARNING: no line per tick
        ChildSpec(
            "periodic",
            (entrypoint, "worker", "-Q", "periodic", "-c", "2", "-n", "periodic@%h", "-l", "warning"),
            grace=30,
            stage=1,
            after=after,
        ),
        # in-memory schedule: no pickle file that the web process could write
        ChildSpec("beat", (entrypoint, "beat", "-S", "celery.beat:Scheduler", "-l", "warning"), grace=10, stage=0, after=after),
    ]
    nodes = {f"celery@{host}": "worker", f"periodic@{host}": "periodic"}
    ping = ("celery", "-A", "mcpanel", "inspect", "ping", "-d", ",".join(nodes), "-t", "10", "--json")
    return specs, Probe(ping, nodes)


def private_directory(path: str) -> str | None:
    """Make sure ``path`` is a directory only this user can enter. None with a reason logged otherwise."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError(f"{path} is not a directory owned by UID {os.geteuid()}")
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
        return path
    except OSError as exc:
        write_stdout(f"[supervisor] no private run directory ({exc}); there will be no status file\n".encode())
        return None


def run(environ: Mapping[str, str]) -> int:
    run_dir = environ.get("MCPANEL_RUN_DIR", RUN_DIR)
    status_path = os.path.join(run_dir, STATUS_FILE) if private_directory(run_dir) else None
    specs, probe = panel_plan(environ, run_dir)
    child_env = dict(environ)
    child_env["MCPANEL_SUPERVISOR_STATUS"] = status_path or ""
    child_env["MCPANEL_QUIET_TICKS"] = "1"
    procs = Processes()
    supervisor = Supervisor(specs, procs, status_path=status_path, probe=probe, environ=child_env)

    def stop(_signum: int, _frame: Any) -> None:
        supervisor.stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    procs.wake_on_signals()
    return supervisor.run()


def step(name: str, argv: list[str]) -> int:
    """Run one command to its end, every line of its output with ``name`` in front; its exit code."""
    procs = Processes()
    try:
        pid = procs.spawn(name, tuple(argv), os.environ)
    except OSError as exc:
        write_stdout(f"[{name}] cannot run {argv[0]}: {exc}\n".encode())
        return 127
    while True:
        procs.pump(0.5)
        for done, code, _output in procs.reap():
            if done == pid:
                return code if code >= 0 else 128 - code


# --- status command ------------------------------------------------------------------------------
def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def status(environ: Mapping[str, str]) -> int:
    path = os.path.join(environ.get("MCPANEL_RUN_DIR", RUN_DIR), STATUS_FILE)
    try:
        with open(path) as fh:
            data = json.load(fh)
        now = time.time()
        age = now - float(data["time"])
        print(f"phase {data['phase']}, supervisor pid {data['pid']}, status written {_ago(age)} ago")
        ok = data["phase"] == "running" and age < 30
        for name, child in data["children"].items():
            ok = ok and child["state"] == RUNNING
            line = f"  {name:<9} {child['state']:<9} for {_ago(now - float(child['since']))}"
            line += f", pid {child['pid']}" if child.get("pid") else f", {describe_exit(child.get('last_exit'))}"
            print(f"{line}, restarts {child['restarts']}")
        for node, answered in (data.get("workers") or {}).items():
            print(f"  ping {node}: " + (f"answered {_ago(now - float(answered))} ago" if answered else "no answer yet"))
            ok = ok and bool(answered) and now - float(answered) <= 120
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"no usable status file at {path}: {exc}", file=sys.stderr)
        return 1
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    if command == "run":
        return run(os.environ)
    if command == "guard":
        return guard(os.environ)
    if command == "status":
        return status(os.environ)
    if command == "step" and len(argv) >= 3:
        return step(argv[1], argv[2:])
    print("usage: mcpanel-supervise.py [run | guard | status | step NAME COMMAND...]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
