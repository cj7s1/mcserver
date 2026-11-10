"""The graceful stop behind /stop, /restart and remove_container (docker_ops.graceful_stop).

What it must do: flush the world first when RCON answers, always run ``docker stop``, and
deliver ``stop`` through the agent's own RCON client when the server had not opened RCON yet
(the itzg runner tries once and then loses the stop). Docker is a fake container; RCON is
either a scripted fake of ``run_command_sync`` or, for the bounds, a real TCP peer that hangs.
The last test runs the same stop against a real Docker daemon (``MCPANEL_TEST_DOCKER=1``).
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import docker.errors
import pytest

from mcagent import docker_ops, rcon
from mcagent.config import Settings
from mcagent.docker_ops import AgentError, StopResult

IID = uuid.UUID("0123abcd-0000-4000-8000-00000000beef")
PASSWORD = "s3cret-rcon-password"
IP = "172.30.0.5"
FLUSH = "save-all flush"


# --- fakes ----------------------------------------------------------------------------------------
class FakeContainer:
    """A managed container whose ``stop`` behaves like ``docker stop``: it returns when the
    server process has ended, or after ``kill_after`` seconds (Docker's SIGKILL)."""

    def __init__(self, log: list, status: str = "running") -> None:
        self.id = "c0ffee"
        self.name = docker_ops.container_name(IID)
        self.labels = {
            docker_ops.LABEL_MANAGED: "true",
            docker_ops.LABEL_INSTANCE: str(IID),
            docker_ops.LABEL_STOP_TIMEOUT: "90",
        }
        self.log = log
        self.state = status
        self.env = ["EULA=TRUE", f"RCON_PASSWORD={PASSWORD}"]
        self.ports: dict = {"25565/tcp": [{"HostIp": "0.0.0.0", "HostPort": "25570"}], "25575/tcp": None}
        self.kill_after = 5.0
        self.killed = False
        self.exit_code = 0  # of the last run; 137 after Docker's SIGKILL, like the real thing
        self.reload_error: Exception | None = None
        self.removed = False
        self.stop_error: Exception | None = None
        self.stop_issued = threading.Event()
        self.stop_may_return = threading.Event()

    @property
    def status(self) -> str:
        return self.state

    @property
    def attrs(self) -> dict:
        return {
            "Config": {"Image": "itzg/minecraft-server:java21", "Env": list(self.env)},
            "HostConfig": {"NetworkMode": "mcpanel_instances"},
            "State": {
                "Status": self.state,
                "Running": self.state in ("running", "paused", "restarting"),
                "Restarting": self.state == "restarting",
                "ExitCode": self.exit_code,
            },
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": IP}}, "Ports": self.ports},
        }

    def reload(self) -> None:
        if self.reload_error is not None and self.stop_issued.is_set():
            raise self.reload_error

    def server_exits(self) -> None:
        """The Minecraft process ended by itself (it got ``stop``)."""
        self.state = "exited"
        self.stop_may_return.set()

    def stop(self, timeout=None) -> None:
        self.log.append(("docker stop", timeout))
        self.stop_issued.set()
        if self.stop_error is not None:
            raise self.stop_error
        if self.state == "running" and not self.stop_may_return.wait(self.kill_after):
            self.killed = True
            self.exit_code = 137
        self.state = "exited"

    def start(self) -> None:
        self.log.append("docker start")
        self.state = "running"
        self.exit_code = 0  # Docker resets it for the new run

    def kill(self) -> None:
        self.log.append("docker kill")
        self.state = "exited"

    def remove(self, v=False) -> None:
        self.log.append("docker rm")
        self.removed = True


class FakeDocker:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.containers = self
        self.api = self

    def get(self, name):
        if self.container.removed or name != self.container.name:
            raise docker.errors.NotFound("no such container")
        return self.container

    def inspect_container(self, container_id):
        if self.container.removed:
            raise docker.errors.NotFound("no such container")
        assert container_id == self.container.id
        return self.container.attrs


class FakeRcon:
    """Stands in for ``run_command_sync``. Closed (login refused) until ``up`` is set."""

    def __init__(self, container: FakeContainer, log: list) -> None:
        self.container = container
        self.log = log
        self.up = threading.Event()
        self.calls: list[SimpleNamespace] = []
        self.fail: dict[str, BaseException] = {}
        self.open_after_refusals_during_stop: int | None = None
        self.shutdown_takes = 0.0  # how long the server keeps running (and answering) after `stop`
        self._refused_during_stop = 0

    def __call__(self, host, port, password, command, *, timeout, connect_timeout):
        assert (host, port, password) == (IP, 25575, PASSWORD)
        self.calls.append(SimpleNamespace(command=command, timeout=timeout, connect_timeout=connect_timeout))
        if not self.up.is_set():
            self.log.append(("rcon refused", command))
            if self.container.stop_issued.is_set():
                self._refused_during_stop += 1
                if self._refused_during_stop == self.open_after_refusals_during_stop:
                    self.up.set()
            raise rcon.RconUnavailable(
                f"Cannot connect to RCON at {host}:{port}: [Errno 111] Connect call failed"
            )
        self.log.append(("rcon", command))
        if command in self.fail:
            raise self.fail[command]
        if command == "stop":
            if self.shutdown_takes:
                threading.Timer(self.shutdown_takes, self.container.server_exits).start()
            else:
                self.container.server_exits()
        return ""

    def sent(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "rcon"]


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    return Settings(data_root=tmp_path / "data", token="t")


@pytest.fixture
def server(monkeypatch):
    """A running server with fake Docker and fake RCON; ``log`` is the order of what happened."""
    log: list = []
    container = FakeContainer(log)
    fake_rcon = FakeRcon(container, log)
    monkeypatch.setattr(docker_ops, "client", lambda: FakeDocker(container))
    monkeypatch.setattr(docker_ops, "run_command_sync", fake_rcon)
    monkeypatch.setattr(docker_ops, "RCON_STOP_INTERVAL", 0.02)
    yield SimpleNamespace(container=container, rcon=fake_rcon, log=log)
    container.stop_may_return.set()
    assert _courier_threads() == []  # no helper thread outlives its stop


def _courier_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("rcon-stop-")]


def _docker_stops(log: list) -> list:
    return [entry for entry in log if entry[0] == "docker stop"]


# --- flush before the stop -----------------------------------------------------------------------
def test_flushes_then_stops_when_rcon_answers(server, settings):
    server.rcon.up.set()
    server.container.stop_may_return.set()  # the runner's own rcon-cli stop works: Docker returns
    result = docker_ops.stop(IID, 90, settings)
    assert server.log == [("rcon", FLUSH), ("docker stop", 90)]
    assert result.as_dict() == {"flushed": True, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}
    [flush] = server.rcon.calls
    assert flush.timeout == 45 and flush.connect_timeout == docker_ops.RCON_PROBE_TIMEOUT
    assert not server.container.killed


@pytest.mark.parametrize(
    ("stop_timeout", "bound"), [(1, 10), (5, 10), (20, 10), (30, 15), (90, 45), (120, 60), (900, 60)]
)
def test_flush_bound_is_half_the_stop_timeout_between_10_and_60_seconds(
    server, settings, stop_timeout, bound
):
    assert docker_ops.flush_timeout(stop_timeout) == bound
    server.rcon.up.set()
    server.container.stop_may_return.set()
    docker_ops.stop(IID, stop_timeout, settings)
    assert server.rcon.calls[0].timeout == bound
    assert _docker_stops(server.log) == [("docker stop", stop_timeout)]


def test_stop_without_a_timeout_uses_the_label(server, settings):
    server.rcon.up.set()
    server.container.stop_may_return.set()
    docker_ops.stop(IID, None, settings)
    assert server.log == [("rcon", FLUSH), ("docker stop", 90)]
    assert server.rcon.calls[0].timeout == 45


@pytest.mark.parametrize(
    "error",
    [
        rcon.RconError("Timed out waiting for RCON response"),
        rcon.RconError("RCON connection closed"),
    ],
)
def test_flush_failure_is_a_warning_and_the_stop_goes_on(server, settings, error):
    server.rcon.up.set()
    server.rcon.fail[FLUSH] = error
    server.container.stop_may_return.set()
    result = docker_ops.stop(IID, 60, settings)
    assert server.log == [("rcon", FLUSH), ("docker stop", 60)]
    assert result.flushed is False and result.stop_sent_via_rcon is False
    [warning] = result.warnings
    assert "save-all flush" in warning and str(error) in warning
    assert PASSWORD not in warning
    # RCON answered, so the runner's own `stop` works: the agent sends none
    assert server.rcon.sent() == [FLUSH]


def test_unexpected_flush_error_does_not_block_the_stop(server, settings):
    server.rcon.up.set()
    server.rcon.fail[FLUSH] = RuntimeError("event loop is broken")
    result = docker_ops.stop(IID, 60, settings)
    assert _docker_stops(server.log) == [("docker stop", 60)]
    assert result.flushed is False and len(result.warnings) == 1
    # not known whether RCON works: the helper delivered `stop` to be sure
    assert result.stop_sent_via_rcon is True and server.rcon.sent() == [FLUSH, "stop"]


def test_no_rcon_password_is_a_warning_and_the_stop_goes_on(server, settings):
    server.container.env = ["EULA=TRUE"]
    server.container.stop_may_return.set()
    result = docker_ops.stop(IID, 60, settings)
    assert server.log == [("docker stop", 60)]
    assert result.flushed is False and result.stop_sent_via_rcon is False
    assert result.warnings == [
        "The world was not flushed before the stop: Container has no RCON password configured"
    ]


def test_unexpected_error_while_reading_the_rcon_address_does_not_block_the_stop(server, settings):
    """Docker reports a published port without a host port: describe() raises ValueError, which is
    no AgentError. The stop must still reach Docker (review 2026-11-10, exp_stop_exc.py)."""
    server.container.ports = {"25565/tcp": [{"HostIp": "0.0.0.0", "HostPort": ""}]}
    server.container.stop_may_return.set()
    result = docker_ops.stop(IID, 30, settings)
    assert server.log == [("docker stop", 30)] and server.container.state == "exited"
    assert result.flushed is False and result.stop_sent_via_rcon is False
    [warning] = result.warnings
    assert warning.startswith("The world was not flushed before the stop: ValueError")
    assert PASSWORD not in warning
    assert server.rcon.calls == []  # no address, so nothing was tried


@pytest.mark.parametrize("flush_error", [None, rcon.RconError("Timed out waiting for RCON response")])
def test_no_helper_when_rcon_answered_the_flush(server, settings, flush_error):
    """RCON accepted the login for the flush, so the runner's own `rcon-cli stop` works: the agent
    must not send a second `stop` while `docker stop` is waiting for the server to end."""
    server.rcon.up.set()
    if flush_error is not None:
        server.rcon.fail[FLUSH] = flush_error
    server.container.kill_after = 0.3  # `docker stop` blocks for 15 retry intervals
    started: list[str] = []
    real_start = docker_ops._RconStopCourier.start
    docker_ops._RconStopCourier.start = lambda self: (started.append("courier"), real_start(self))[1]
    try:
        result = docker_ops.stop(IID, 90, settings)
    finally:
        docker_ops._RconStopCourier.start = real_start
    assert started == []
    assert server.rcon.sent() == [FLUSH] and len(server.rcon.calls) == 1  # no `stop`, no further login
    assert result.stop_sent_via_rcon is False
    assert server.log == [("rcon", FLUSH), ("docker stop", 90)]


# --- stop while the server is still starting ------------------------------------------------------
def test_stop_is_delivered_through_rcon_when_rcon_opens_after_docker_stop_began(server, settings):
    """The 2026-11-08 case: SIGTERM arrives before RCON is open, the runner's one `rcon-cli stop`
    fails and the stop is lost. The agent must send `stop` itself once RCON answers."""
    server.rcon.open_after_refusals_during_stop = 3
    result = docker_ops.stop(IID, 90, settings)
    assert server.log == [
        ("rcon refused", FLUSH),  # the probe: RCON is not up, nothing to flush
        ("docker stop", 90),
        ("rcon refused", "stop"),
        ("rcon refused", "stop"),
        ("rcon refused", "stop"),
        ("rcon", "stop"),  # exactly once
    ]
    assert result.flushed is False and result.stop_sent_via_rcon is True
    assert len(result.warnings) == 1 and "RCON is not available" in result.warnings[0]
    assert "not flushed" in result.warnings[0] and PASSWORD not in result.warnings[0]
    assert not server.container.killed  # ended by `stop`, long before Docker's timeout
    stop_try = server.rcon.calls[-1]
    assert stop_try.timeout == docker_ops.RCON_STOP_COMMAND_TIMEOUT
    assert stop_try.connect_timeout == docker_ops.RCON_PROBE_TIMEOUT


def test_stop_is_sent_once_while_the_server_takes_its_time_to_shut_down(server, settings):
    server.rcon.open_after_refusals_during_stop = 1
    server.rcon.shutdown_takes = 0.3  # 15 retry intervals in which RCON still answers
    result = docker_ops.stop(IID, 90, settings)
    assert server.rcon.sent() == ["stop"]
    assert result.stop_sent_via_rcon is True and not server.container.killed


def test_rcon_stop_is_never_sent_before_docker_stop(server, settings, monkeypatch):
    """Docker must know about the stop first, or the restart policy starts the server again."""

    def opens_right_after_the_probe(*args, **kwargs):
        try:
            return server.rcon(*args, **kwargs)
        finally:
            server.rcon.up.set()

    monkeypatch.setattr(docker_ops, "run_command_sync", opens_right_after_the_probe)
    result = docker_ops.stop(IID, 90, settings)
    assert server.log == [("rcon refused", FLUSH), ("docker stop", 90), ("rcon", "stop")]
    assert result.stop_sent_via_rcon is True


def test_docker_stop_runs_and_returns_when_rcon_never_answers(server, settings):
    server.container.kill_after = 0.3  # Docker's SIGKILL at the timeout, scaled down
    result = docker_ops.stop(IID, 90, settings)
    assert _docker_stops(server.log) == [("docker stop", 90)]
    assert server.container.killed
    assert result.flushed is False and result.stop_sent_via_rcon is False
    assert server.rcon.sent() == []
    tries = len(server.rcon.calls)
    assert tries >= 3  # the probe and the retries during those 0.3 s
    # docker stop has returned: nothing more is tried, also when RCON opens now
    server.rcon.up.set()
    time.sleep(0.2)
    assert len(server.rcon.calls) == tries and server.rcon.sent() == []


def test_retries_end_when_the_container_has_exited(server, settings):
    done = threading.Event()
    results: list[StopResult] = []

    def run() -> None:
        results.append(docker_ops.stop(IID, 90, settings))
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert server.container.stop_issued.wait(5)
    deadline = time.monotonic() + 5
    while len(server.rcon.calls) < 3:  # the helper is retrying
        assert time.monotonic() < deadline
        time.sleep(0.01)
    server.container.state = "exited"  # the process ended, `docker stop` has not returned yet
    time.sleep(0.1)
    tries = len(server.rcon.calls)
    server.rcon.up.set()
    time.sleep(0.2)
    assert len(server.rcon.calls) == tries  # no try against a container that is gone
    assert not done.is_set()
    server.container.stop_may_return.set()
    assert done.wait(5)
    assert results[0].stop_sent_via_rcon is False and server.rcon.sent() == []


def test_helper_sends_nothing_once_docker_stop_has_returned_during_its_container_check(
    server, settings, monkeypatch
):
    """`docker stop` returns while the helper is asking Docker whether the container still runs.
    A restart starts the server again right after: the answer "running" then belongs to the new
    run, and a `stop` sent now would shut down the server that was just started."""
    container = server.container
    server.rcon.up.set()  # RCON would take the command at once
    checking, answer = threading.Event(), threading.Event()

    class SlowDocker(FakeDocker):
        def inspect_container(self, container_id):
            checking.set()
            assert answer.wait(5)
            return {"State": {"Running": True}}  # the restarted server

    monkeypatch.setattr(docker_ops, "client", lambda: SlowDocker(container))
    courier = docker_ops._RconStopCourier(container.id, container.name, (IP, 25575, PASSWORD))
    result = StopResult()
    courier.start()
    assert checking.wait(5)  # the helper is inside its check
    finishing = threading.Thread(target=courier.finish, args=(result,), daemon=True)
    finishing.start()  # what graceful_stop does when `docker stop` has returned
    deadline = time.monotonic() + 5
    while not courier._done.is_set():
        assert time.monotonic() < deadline
        time.sleep(0.005)
    answer.set()
    finishing.join(5)
    assert not finishing.is_alive()
    assert server.rcon.calls == [] and server.rcon.sent() == []
    assert result.stop_sent_via_rcon is False and result.warnings == []


def test_stop_counts_as_sent_when_the_server_drops_the_connection_while_stopping(server, settings):
    server.rcon.open_after_refusals_during_stop = 1
    server.rcon.fail["stop"] = rcon.RconError("RCON connection closed")
    server.container.kill_after = 0.3
    result = docker_ops.stop(IID, 90, settings)
    assert server.rcon.sent() == ["stop"]  # once, no second try
    assert result.stop_sent_via_rcon is True
    assert any("did not confirm" in w and "RCON connection closed" in w for w in result.warnings)


def test_docker_stop_error_propagates_and_ends_the_helper(server, settings):
    server.container.stop_error = docker.errors.APIError("500 Server Error", explanation="cannot stop")
    with pytest.raises(docker.errors.APIError):
        docker_ops.stop(IID, 90, settings)
    assert _docker_stops(server.log) == [("docker stop", 90)]
    assert _courier_threads() == []


# --- containers that are not running -----------------------------------------------------------
@pytest.mark.parametrize("status", ["exited", "created", "dead"])
def test_a_stopped_container_is_left_alone(server, settings, status):
    server.container.state = status
    server.rcon.up.set()
    result = docker_ops.stop(IID, 90, settings)
    assert server.log == [] and server.rcon.calls == []
    # nothing was stopped, so there is no exit code of a stopped run either
    assert result.as_dict() == {
        "flushed": False,
        "stop_sent_via_rcon": False,
        "warnings": [],
        "exit_code": None,
    }


@pytest.mark.parametrize("status", ["restarting", "paused"])
def test_restarting_and_paused_containers_get_docker_stop_without_rcon(server, settings, status):
    server.container.state = status
    server.rcon.up.set()
    result = docker_ops.stop(IID, 90, settings)
    assert server.log == [("docker stop", 90)] and server.rcon.calls == []
    assert result.as_dict() == {"flushed": False, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}


def test_stop_needs_a_container(monkeypatch, settings):
    class Empty:
        class containers:
            @staticmethod
            def get(name):
                raise docker.errors.NotFound("no such container")

    monkeypatch.setattr(docker_ops, "client", lambda: Empty())
    with pytest.raises(AgentError) as exc:
        docker_ops.stop(IID, 90, settings)
    assert exc.value.code == "no_container"


def test_kill_is_unchanged(server):
    server.rcon.up.set()
    docker_ops.kill(IID)
    assert server.log == ["docker kill"] and server.rcon.calls == []


# --- remove_container ------------------------------------------------------------------------------
def test_remove_container_stops_gracefully_first(server, settings):
    server.rcon.up.set()
    server.container.stop_may_return.set()
    assert docker_ops.remove_container(IID, timeout=120, settings=settings) is True
    assert server.log == [("rcon", FLUSH), ("docker stop", 120), "docker rm"]
    assert server.rcon.calls[0].timeout == 60


def test_remove_container_delivers_stop_to_a_starting_server(server, settings):
    server.rcon.open_after_refusals_during_stop = 2
    assert docker_ops.remove_container(IID, timeout=90, settings=settings) is True
    assert server.rcon.sent() == ["stop"] and not server.container.killed
    assert server.log[1] == ("docker stop", 90) and server.log[-1] == "docker rm"


def test_remove_container_of_a_stopped_container_skips_the_stop(server, settings):
    server.container.state = "exited"
    assert docker_ops.remove_container(IID, timeout=90, settings=settings) is True
    assert server.log == ["docker rm"] and server.rcon.calls == []


def test_remove_container_settings_are_optional(server, monkeypatch, settings):
    """Callers that only pass a timeout keep working: the agent's own settings are used."""
    monkeypatch.setattr(docker_ops, "get_settings", lambda: settings)
    server.rcon.up.set()
    server.container.stop_may_return.set()
    assert docker_ops.remove_container(IID, timeout=30) is True
    assert server.log == [("rcon", FLUSH), ("docker stop", 30), "docker rm"]
    assert server.rcon.calls[0].timeout == 15


# --- HTTP routes -------------------------------------------------------------------------------------
def test_stop_route_reports_what_the_stop_did(api, auth, server):
    server.rcon.up.set()
    server.container.stop_may_return.set()
    r = api.post(f"/v1/instances/{IID}/stop", json={"timeout": 90}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert server.log == [("rcon", FLUSH), ("docker stop", 90)]
    assert body["stop"] == {"flushed": True, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}
    # the answer is still the container status
    assert body["exists"] is True and body["running"] is False and body["status"] == "exited"
    assert body["name"] == "mcpanel-0123abcd0000"


@pytest.mark.parametrize("body", [None, {}])
def test_stop_route_without_a_timeout(api, auth, server, body):
    server.rcon.open_after_refusals_during_stop = 2
    r = api.post(f"/v1/instances/{IID}/stop", json=body, headers=auth)
    assert r.status_code == 200
    stop = r.json()["stop"]
    assert stop["flushed"] is False and stop["stop_sent_via_rcon"] is True
    assert len(stop["warnings"]) == 1 and PASSWORD not in stop["warnings"][0]
    assert _docker_stops(server.log) == [("docker stop", 90)]


def test_restart_route_uses_the_graceful_stop_then_starts(api, auth, server):
    server.rcon.up.set()
    server.container.stop_may_return.set()
    r = api.post(f"/v1/instances/{IID}/restart", json={"timeout": 40}, headers=auth)
    assert r.status_code == 200
    assert server.log == [("rcon", FLUSH), ("docker stop", 40), "docker start"]
    body = r.json()
    assert body["stop"] == {"flushed": True, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}
    assert body["running"] is True and body["status"] == "running"


def test_restart_route_reports_the_exit_code_of_the_run_it_stopped(api, auth, server):
    """After a restart the container status describes the new run (exit code 0 again): a server
    that had to be killed at the timeout is only visible in ``stop.exit_code``."""
    server.rcon.up.set()
    server.container.kill_after = 0.1  # never exits by itself: Docker's SIGKILL, scaled down
    r = api.post(f"/v1/instances/{IID}/restart", json={"timeout": 40}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert server.container.killed and server.log[-1] == "docker start"
    assert body["running"] is True and body["exit_code"] == 0  # the new run
    assert body["stop"]["exit_code"] == 137  # the run that was stopped


def test_stop_route_reports_a_kill_at_the_timeout(api, auth, server):
    server.rcon.up.set()
    server.container.kill_after = 0.1
    r = api.post(f"/v1/instances/{IID}/stop", json={"timeout": 40}, headers=auth)
    assert r.status_code == 200
    assert r.json()["stop"]["exit_code"] == 137 and r.json()["exit_code"] == 137


def test_exit_code_is_left_out_when_docker_does_not_say(server, settings):
    """The stop has happened; an inspect that fails afterwards must not turn it into an error."""
    server.rcon.up.set()
    server.container.stop_may_return.set()
    server.container.reload_error = docker.errors.APIError("500 Server Error", explanation="daemon busy")
    real_reload = server.container.reload
    reloads: list[bool] = []

    def reload() -> None:
        reloads.append(server.container.stop_issued.is_set())
        real_reload()

    server.container.reload = reload
    result = docker_ops.stop(IID, 90, settings)
    assert reloads == [False, True]  # before the stop (the state) and after it (the exit code)
    assert _docker_stops(server.log) == [("docker stop", 90)]
    assert result.exit_code is None and result.flushed is True


def test_restart_route_delivers_stop_to_a_starting_server(api, auth, server):
    server.rcon.open_after_refusals_during_stop = 2
    r = api.post(f"/v1/instances/{IID}/restart", json={"timeout": 90}, headers=auth)
    assert r.status_code == 200
    assert server.log[-2:] == [("rcon", "stop"), "docker start"]
    assert r.json()["stop"]["stop_sent_via_rcon"] is True
    assert not server.container.killed


def test_remove_route_uses_the_graceful_stop(api, auth, server):
    server.rcon.up.set()
    server.container.stop_may_return.set()
    r = api.delete(f"/v1/instances/{IID}/container", params={"timeout": 50}, headers=auth)
    assert r.status_code == 200 and r.json() == {"removed": True}
    assert server.log == [("rcon", FLUSH), ("docker stop", 50), "docker rm"]


def test_kill_route_sends_no_rcon(api, auth, server):
    server.rcon.up.set()
    r = api.post(f"/v1/instances/{IID}/kill", headers=auth)
    assert r.status_code == 200 and "stop" not in r.json()
    assert server.log == ["docker kill"] and server.rcon.calls == []


# --- bounds, with a real socket that hangs --------------------------------------------------------
def _packet(req_id: int, ptype: int, payload: bytes) -> bytes:
    body = struct.pack("<ii", req_id, ptype) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class RconPeer:
    """A TCP peer on 127.0.0.1 that speaks as much RCON as it is told to.

    ``login``: "ok", "deny" (wrong password) or "hang" (accepts the connection, never answers).
    ``command``: "ok", "hang" or "close" (drops the connection without an answer).
    """

    def __init__(self, login: str = "ok", command: str = "ok", port: int = 0) -> None:
        self.login = login
        self.command = command
        self.logins = 0
        self.commands: list[str] = []
        self.on_command = lambda _command: None
        self._closed = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", port))
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn: socket.socket) -> tuple[int, int, bytes]:
        def exactly(n: int) -> bytes:
            data = b""
            while len(data) < n:
                chunk = conn.recv(n - len(data))
                if not chunk:
                    raise EOFError
                data += chunk
            return data

        (length,) = struct.unpack("<i", exactly(4))
        data = exactly(length)
        req_id, ptype = struct.unpack("<ii", data[:8])
        return req_id, ptype, data[8:-2]

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            try:
                req_id, _ptype, _password = self._read(conn)
                self.logins += 1
                if self.login == "hang":
                    self._closed.wait()
                    return
                conn.sendall(_packet(-1 if self.login == "deny" else req_id, 2, b""))
                while True:
                    req_id, _ptype, payload = self._read(conn)
                    self.commands.append(payload.decode())
                    self.on_command(payload.decode())
                    if self.command == "hang":
                        self._closed.wait()
                        return
                    if self.command == "close":
                        return
                    conn.sendall(_packet(req_id, 0, b"done"))
            except (OSError, EOFError):
                pass

    def close(self) -> None:
        self._closed.set()
        self._sock.close()


@pytest.fixture
def peer():
    peers: list[RconPeer] = []

    def make(**kwargs) -> RconPeer:
        peers.append(RconPeer(**kwargs))
        return peers[-1]

    yield make
    for p in peers:
        p.close()


def _timed(fn):
    started = time.monotonic()
    try:
        return fn(), time.monotonic() - started
    except Exception as exc:
        return exc, time.monotonic() - started


def test_run_command_sync_returns_the_answer(peer):
    p = peer()
    assert rcon.run_command_sync("127.0.0.1", p.port, "pw", "list", timeout=5, connect_timeout=2) == "done"
    assert p.commands == ["list"]


def test_run_command_sync_login_that_hangs_is_unavailable_within_the_connect_timeout(peer):
    p = peer(login="hang")
    outcome, took = _timed(
        lambda: rcon.run_command_sync("127.0.0.1", p.port, "pw", "stop", timeout=30, connect_timeout=0.3)
    )
    assert isinstance(outcome, rcon.RconUnavailable)
    assert 0.25 <= took < 2.0
    assert p.commands == []  # nothing was sent


def test_run_command_sync_refused_and_denied_logins_are_unavailable(peer):
    closed = socket.create_server(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    outcome, took = _timed(
        lambda: rcon.run_command_sync("127.0.0.1", port, "pw", "stop", timeout=30, connect_timeout=3)
    )
    assert isinstance(outcome, rcon.RconUnavailable) and took < 2.0
    p = peer(login="deny")
    outcome, _took = _timed(
        lambda: rcon.run_command_sync("127.0.0.1", p.port, "pw", "stop", timeout=30, connect_timeout=3)
    )
    assert isinstance(outcome, rcon.RconUnavailable) and p.commands == []


@pytest.mark.parametrize("command", ["hang", "close"])
def test_run_command_sync_command_that_gets_no_answer_is_bounded(peer, command):
    p = peer(command=command)
    outcome, took = _timed(
        lambda: rcon.run_command_sync("127.0.0.1", p.port, "pw", FLUSH, timeout=0.4, connect_timeout=2)
    )
    assert isinstance(outcome, rcon.RconError) and not isinstance(outcome, rcon.RconUnavailable)
    assert took < 2.5
    assert p.commands == [FLUSH]


@pytest.fixture
def socket_server(monkeypatch, settings, peer):
    """A running fake container whose RCON is a real TCP peer (published-port mode), with the
    RCON timeouts scaled down. ``make(login=..., command=...)`` returns (container, peer, log)."""
    settings.rcon_mode = "published"
    monkeypatch.setattr(docker_ops, "RCON_PROBE_TIMEOUT", 0.3)
    monkeypatch.setattr(docker_ops, "RCON_STOP_COMMAND_TIMEOUT", 0.3)
    monkeypatch.setattr(docker_ops, "RCON_STOP_INTERVAL", 0.05)
    monkeypatch.setattr(docker_ops, "flush_timeout", lambda _stop_timeout: 0.4)
    containers: list[FakeContainer] = []

    def make(**kwargs):
        p = peer(**kwargs)
        log: list = []
        container = FakeContainer(log)
        container.ports["25575/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": str(p.port)}]
        monkeypatch.setattr(docker_ops, "client", lambda: FakeDocker(container))
        containers.append(container)
        return container, p, log

    yield make
    for container in containers:
        container.stop_may_return.set()
    assert _courier_threads() == []


def test_rcon_that_accepts_and_never_answers_cannot_hold_the_stop(socket_server, settings):
    container, p, log = socket_server(login="hang")
    container.kill_after = 0.6  # Docker's own timeout ends the stop
    result, took = _timed(lambda: docker_ops.stop(IID, 90, settings))
    assert log == [("docker stop", 90)]
    assert result.flushed is False and result.stop_sent_via_rcon is False
    assert "RCON is not available" in result.warnings[0] and "did not answer within" in result.warnings[0]
    # probe (0.3) + docker stop (0.6) + at most one try in flight (0.3)
    assert took < 3.0
    assert p.logins >= 2 and p.commands == []


def test_flush_that_hangs_is_cut_off_at_its_bound(socket_server, settings):
    container, p, log = socket_server(command="hang")
    container.stop_may_return.set()
    result, took = _timed(lambda: docker_ops.stop(IID, 90, settings))
    assert log == [("docker stop", 90)]
    assert result.flushed is False and result.stop_sent_via_rcon is False
    assert "save-all flush" in result.warnings[0] and "Timed out" in result.warnings[0]
    assert 0.35 <= took < 2.5  # the 0.4 s flush bound, not the peer's forever
    assert p.commands == [FLUSH]  # RCON answered the login: no `stop` from the agent


def test_stop_command_that_is_never_answered_cannot_hold_the_stop(socket_server, settings):
    container, p, log = socket_server(login="hang", command="hang")
    container.kill_after = 5.0
    p.on_command = lambda command: container.server_exits() if command == "stop" else None

    def open_rcon() -> None:  # RCON starts to answer logins shortly after docker stop began
        container.stop_issued.wait(5)
        time.sleep(0.1)
        p.login = "ok"

    threading.Thread(target=open_rcon, daemon=True).start()
    result, took = _timed(lambda: docker_ops.stop(IID, 90, settings))
    assert log == [("docker stop", 90)]
    assert p.commands == ["stop"] and not container.killed
    assert result.stop_sent_via_rcon is True
    assert any("did not confirm" in w for w in result.warnings)
    assert took < 4.0


def test_stop_route_with_a_real_socket_from_the_request_thread_pool(api, auth, agent_settings, socket_server):
    """The route runs the stop in a worker thread; the real RCON client must work there."""
    agent_settings.rcon_mode = "published"  # the settings the api fixture serves
    container, p, log = socket_server()
    container.stop_may_return.set()
    r = api.post(f"/v1/instances/{IID}/stop", json={"timeout": 90}, headers=auth)
    assert r.status_code == 200
    assert r.json()["stop"] == {"flushed": True, "stop_sent_via_rcon": False, "warnings": [], "exit_code": 0}
    assert p.commands == [FLUSH] and log == [("docker stop", 90)]


def test_restart_route_with_a_real_socket_delivers_stop_to_a_starting_server(
    api, auth, agent_settings, socket_server
):
    agent_settings.rcon_mode = "published"
    container, p, log = socket_server(login="hang")
    p.on_command = lambda command: container.server_exits() if command == "stop" else None

    def open_rcon() -> None:
        container.stop_issued.wait(5)
        time.sleep(0.1)
        p.login = "ok"

    threading.Thread(target=open_rcon, daemon=True).start()
    r = api.post(f"/v1/instances/{IID}/restart", json={"timeout": 90}, headers=auth)
    assert r.status_code == 200
    stop = r.json()["stop"]
    assert stop["flushed"] is False and stop["stop_sent_via_rcon"] is True
    assert p.commands == ["stop"] and not container.killed
    assert log == [("docker stop", 90), "docker start"]


# --- real Docker ---------------------------------------------------------------------------------
@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("MCPANEL_TEST_DOCKER") != "1", reason="set MCPANEL_TEST_DOCKER=1")
def test_real_docker_graceful_stop(tmp_path, settings, monkeypatch):
    """The stop against a real daemon, with tiny containers (mcpanel-audit-*, removed afterwards)
    that ignore SIGTERM like a server whose stop got lost, and exit when ``/data/stop`` appears.
    RCON is a peer of this test on 127.0.0.1:25575 (host network mode).

    1. RCON opens only after ``docker stop`` began: the agent delivers ``stop``, the container
       exits by itself long before the timeout and the restart policy does not start it again.
    2. RCON answers from the start: flush, then ``docker stop``; nobody sends ``stop`` here (the
       itzg runner would), so Docker kills the container at the requested timeout.
    """
    import docker

    image = os.environ.get("MCPANEL_TEST_TINY_IMAGE", "busybox:1.37")
    try:
        client = docker.from_env(timeout=60)
        client.ping()
        client.images.get(image)
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"Docker with {image} is not available: {exc}")
    try:
        socket.create_server(("127.0.0.1", docker_ops.RCON_PORT)).close()
    except OSError as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"127.0.0.1:{docker_ops.RCON_PORT} is in use: {exc}")
    monkeypatch.setattr(docker_ops, "client", lambda: client)
    monkeypatch.setattr(docker_ops, "container_name", lambda iid: f"mcpanel-audit-{iid.hex[:12]}")
    script = "trap '' TERM; while [ ! -e /data/stop ]; do sleep 0.2; done"
    created = []
    peers: list[RconPeer] = []

    def run_server() -> tuple[uuid.UUID, object, Path]:
        iid = uuid.uuid4()
        data = tmp_path / iid.hex
        data.mkdir()
        data.chmod(0o755)
        container = client.containers.create(
            image,
            ["sh", "-c", script],
            name=docker_ops.container_name(iid),
            labels={docker_ops.LABEL_MANAGED: "true", docker_ops.LABEL_INSTANCE: str(iid)},
            environment={"RCON_PASSWORD": PASSWORD},
            network_mode="host",
            volumes={str(data): {"bind": "/data", "mode": "ro"}},
            restart_policy={"Name": "unless-stopped"},
            mem_limit="64m",
        )
        created.append(container)
        container.start()
        return iid, container, data

    def state(container) -> dict:
        return client.api.inspect_container(container.id)["State"]

    try:
        # 1. the server is still starting: RCON opens one second after the stop began
        iid, container, data = run_server()

        def open_rcon_later() -> None:
            time.sleep(1.0)
            peers.append(RconPeer(port=docker_ops.RCON_PORT))
            peers[-1].on_command = lambda command: (data / "stop").touch() if command == "stop" else None

        threading.Thread(target=open_rcon_later, daemon=True).start()
        result, took = _timed(lambda: docker_ops.stop(iid, 30, settings))
        assert isinstance(result, StopResult), result
        assert result.stop_sent_via_rcon is True and result.flushed is False
        assert peers[0].commands == ["stop"]
        assert took < 15, took  # not the 30 s after which Docker would have killed it
        time.sleep(1.5)  # a restart by the policy would have happened by now
        after = state(container)
        assert after["Status"] == "exited" and after["ExitCode"] == 0, after
        assert client.api.inspect_container(container.id)["RestartCount"] == 0
        container.remove(force=True)
        created.remove(container)

        # 2. RCON answers: flush first, then docker stop with the requested timeout
        peers[0].commands.clear()
        peers[0].on_command = lambda _command: None
        iid, container, data = run_server()
        result, took = _timed(lambda: docker_ops.stop(iid, 3, settings))
        assert isinstance(result, StopResult), result
        assert result.as_dict() == {"flushed": True, "stop_sent_via_rcon": False, "warnings": []}
        assert peers[0].commands == [FLUSH]
        after = state(container)
        assert after["Status"] == "exited" and after["ExitCode"] == 137, after  # killed at 3 s
        assert 2.5 < took < 15, took
        assert docker_ops.remove_container(iid, timeout=3, settings=settings) is True
        created.remove(container)
        assert docker_ops.get_container(iid) is None
    finally:
        for p in peers:
            p.close()
        for container in created:
            container.remove(force=True)
        client.close()
