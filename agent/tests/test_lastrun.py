"""The last-run record and the retire operation: no container for a stopped server.

``lastrun`` (the record, bounded and cleaned), ``docker_ops.retire`` (save the record, then remove
the container, under the per-server lock) and what the status, logs, log-stream, kill, stats and
command endpoints answer for a server without a container. Docker is a fake.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from datetime import UTC, datetime, timedelta

import docker.errors
import pytest

from mcagent import docker_ops, lastrun, main
from mcagent.docker_ops import AgentError
from mcagent.operations import registry

IID = uuid.UUID("0123abcd-0000-4000-8000-00000000beef")
OTHER = uuid.UUID("0123abcd-0000-4000-8000-00000000cafe")
PASSWORD = "s3cret-rcon-password"


def _ts(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"


class FakeContainer:
    """A managed container as docker-py shows it; ``log`` records what was done to it."""

    def __init__(
        self, calls: list, *, status: str = "exited", container_id: str = "c0ffee", output: bytes = b""
    ) -> None:
        self.id = container_id
        self.name = docker_ops.container_name(IID)
        self.labels = {
            docker_ops.LABEL_MANAGED: "true",
            docker_ops.LABEL_INSTANCE: str(IID),
            docker_ops.LABEL_SPEC_HASH: "abc123",
        }
        self.calls = calls
        self.state = status
        self.output = output
        self.exit_code = 0
        self.oom_killed = False
        self.restart_count = 0
        self.started_at = _ts(3600)
        self.finished_at = _ts(60)
        self.created_at = _ts(7200)
        self.health_output = "Connection refused"
        self.removed = False
        self.remove_error: Exception | None = None
        self.logs_error: Exception | None = None

    @property
    def status(self) -> str:
        return self.state

    @property
    def attrs(self) -> dict:
        return {
            "Image": "sha256:" + "ab" * 32,
            "Created": self.created_at,
            "RestartCount": self.restart_count,
            "Config": {"Image": "itzg/minecraft-server:stable-java21", "Env": [f"RCON_PASSWORD={PASSWORD}"]},
            "HostConfig": {"NetworkMode": "mcpanel_instances"},
            "State": {
                "Status": self.state,
                "Running": self.state in ("running", "paused"),
                "Restarting": self.state == "restarting",
                "ExitCode": self.exit_code,
                "OOMKilled": self.oom_killed,
                "Error": "",
                "StartedAt": self.started_at,
                "FinishedAt": self.finished_at,
                "Health": {"Status": "unhealthy", "Log": [{"Output": self.health_output}]},
            },
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "172.30.0.5"}}, "Ports": {}},
        }

    def reload(self) -> None:
        if self.removed:
            raise docker.errors.NotFound("no such container")

    def logs(self, **kwargs):
        self.calls.append(
            ("logs", {k: v for k, v in kwargs.items() if k in ("tail", "follow", "since", "stream")})
        )
        if self.logs_error is not None:
            raise self.logs_error
        data = self.output
        tail = kwargs.get("tail")
        if isinstance(tail, int):
            lines = data.splitlines(keepends=True)
            data = b"".join(lines[-tail:]) if tail else b""
        if kwargs.get("stream"):
            return _Stream([data[i : i + 7] for i in range(0, len(data), 7)])  # frames that split lines
        return data

    def remove(self, v=False) -> None:
        self.calls.append("docker rm")
        if self.remove_error is not None:
            raise self.remove_error
        self.removed = True

    def kill(self) -> None:
        self.calls.append("docker kill")
        self.state = "exited"

    def start(self) -> None:
        self.calls.append("docker start")
        self.state = "running"

    def stop(self, timeout=None) -> None:
        self.calls.append("docker stop")
        self.state = "exited"

    def stats(self, stream=False) -> dict:
        return {}


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._it = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self._it)

    def close(self) -> None:
        self.closed = True


class FakeDocker:
    def __init__(self) -> None:
        self.container: FakeContainer | None = None
        self.containers = self
        self.api = self

    def get(self, name):
        if self.container is None or self.container.removed or name != self.container.name:
            raise docker.errors.NotFound("no such container")
        return self.container

    def inspect_container(self, container_id):
        return self.get(docker_ops.container_name(IID)).attrs


@pytest.fixture
def world(monkeypatch, agent_settings):
    """Fake Docker with one exited container that printed three lines; ``calls`` is what happened."""
    calls: list = []
    fake = FakeDocker()
    fake.container = FakeContainer(
        calls, output=b'[init] starting\nDone (3.2s)! For help, type "help"\nSaving worlds\n'
    )
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "get_settings", lambda: agent_settings)
    monkeypatch.setattr(docker_ops, "run_command_sync", lambda *a, **k: "")
    fake.calls = calls
    return fake


def record_file(settings):
    return settings.state_dir / "last-run" / f"{IID}.json"


# --- cleaning untrusted console output ---------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain text\twith a tab", "plain text\twith a tab"),
        ("nul\x00 bell\x07 backspace\x08 del\x7f c1\x9b", "nul bell backspace del c1"),
        (
            "\x1b[32mgreen\x1b[0m and \x1b[1;31mbold red\x1b[m",
            "\x1b[32mgreen\x1b[0m and \x1b[1;31mbold red\x1b[m",
        ),
        ("cursor\x1b[2J\x1b[10;10H moved\x1b[?25l", "cursor moved"),
        ("title\x1b]0;evil\x07 link\x1b]8;;http://x\x1b\\text", "title linktext"),
        ("unterminated \x1b]0;never ends", "unterminated "),
        ("incomplete \x1b[12", "incomplete "),
        ("lone escape \x1b", "lone escape "),
        ("\x1b[38;5;196mindexed\x1b[0m \x1b[<1;2m \x1b[1 q", "\x1b[38;5;196mindexed\x1b[0m  "),
        ("§aMinecraft codes stay§r", "§aMinecraft codes stay§r"),
        ("", ""),
    ],
)
def test_clean_text_keeps_colours_and_drops_every_other_control(raw, clean):
    assert lastrun.clean_text(raw) == clean


def test_collect_tail_keeps_the_last_lines_cleaned():
    chunks = [b"one\r\ntw", b"o\n\x1b[31mthree\x1b[0m\x00\n", b"four without newline"]
    lines, truncated = lastrun.collect_tail(chunks, max_lines=3)
    assert lines == ["two", "\x1b[31mthree\x1b[0m", "four without newline"] and truncated is False


def test_collect_tail_never_fails_on_content():
    junk = bytes(range(256)) * 4 + b"\n\xff\xfe\xfd\n" + b"\xf0\x9f\x98" + b"\n" + "ünïcödé ✓\n".encode()
    lines, _ = lastrun.collect_tail([junk[i : i + 5] for i in range(0, len(junk), 5)])
    assert lines[-1] == "ünïcödé ✓"
    json.dumps(lines, ensure_ascii=True).encode("ascii")  # always representable
    assert all("\x00" not in line and "\x1b" not in line.replace("\x1b[", "") for line in lines)


def test_collect_tail_bounds_every_line_and_the_total():
    endless = [b"A" * 65536 for _ in range(64)]  # 4 MiB without a newline
    lines, truncated = lastrun.collect_tail([b"first\n", *endless, b"\nlast\n"], max_line_bytes=4096)
    assert [lines[0], lines[2]] == ["first", "last"] and truncated is True
    assert lines[1].endswith(lastrun.TRUNCATED_SUFFIX)
    assert len(lines[1].encode()) == 4096 + len(lastrun.TRUNCATED_SUFFIX)

    many = [b"x" * 100 + b"\n" for _ in range(500)]
    lines, truncated = lastrun.collect_tail(many, max_bytes=1000)
    assert len(lines) == 10 and truncated is True  # the oldest lines went first
    lines, truncated = lastrun.collect_tail([b"%d\n" % i for i in range(5000)])
    assert (
        len(lines) == lastrun.MAX_LINES and lines[0] == "3000" and lines[-1] == "4999" and truncated is False
    )


# --- the record on disk ---------------------------------------------------------------------------
def _record(lines=("a", "b"), **described) -> dict:
    info = {
        "id": "c0ffee",
        "name": "mcpanel-0123abcd0000",
        "image": "itzg/minecraft-server:stable-java21",
        "image_id": "sha256:" + "ab" * 32,
        "status": "exited",
        "exit_code": 0,
        "oom_killed": False,
        "error": None,
        "started_at": "2026-11-10T03:00:00.123456789Z",
        "finished_at": "2026-11-10T03:12:45.987654321Z",
        "created_at": "2026-11-10T02:59:00Z",
        "health": "unhealthy",
        "health_output": "x",
        "restart_count": 2,
        "spec_hash": "abc",
    }
    info.update(described)
    return lastrun.build(IID, info, list(lines), False)


def test_record_is_stored_in_the_agents_state_folder_readable_by_its_owner_only(agent_settings):
    os.umask(0o022)
    path = lastrun.write(agent_settings, IID, _record())
    assert path == agent_settings.data_root / "agent" / "last-run" / f"{IID}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # outside every server folder (and so outside backups, clones and the file manager)
    assert agent_settings.instances_dir not in path.parents
    assert path.read_bytes().isascii()
    assert [p.name for p in path.parent.iterdir()] == [f"{IID}.json"]  # no temporary file left


def test_record_round_trip_and_summary(agent_settings):
    record = _record(
        lines=["one", "two", "§cthree"], exit_code=137, oom_killed=True, health_output="bad\x00output\x1b[2J"
    )
    lastrun.write(agent_settings, IID, record)
    loaded = lastrun.load(agent_settings, IID)
    assert loaded == record and loaded["health_output"] == "badoutput"
    summary = lastrun.summary(agent_settings, IID)
    assert "log" not in summary and summary["log_lines"] == 3 and summary["log_truncated"] is False
    assert summary["exit_code"] == 137 and summary["oom_killed"] is True and summary["restart_count"] == 2
    assert summary["image_id"] == "sha256:" + "ab" * 32 and summary["container_id"] == "c0ffee"
    assert summary["started_at"].startswith("2026-11-10T03:00:00") and summary["finished_at"].startswith(
        "2026-11-10T03:12:45"
    )
    assert lastrun.summaries(agent_settings) == {str(IID): summary}
    assert lastrun.console_lines(loaded) == [
        "one",
        "two",
        "§cthree",
        "[mcpanel] ---- end of the run that stopped 2026-11-10 03:12:45 UTC (exited with code 137, out of memory) ----",
    ]


def test_the_largest_possible_record_is_still_read_back(agent_settings):
    """The size limits hold together: the fullest tail the collector can produce, with the text
    that grows most as ASCII-only JSON, is written and read back (not ignored as "too large")."""
    worst = (
        "\x1b[m" + "\u00e9" * 60 + "\U0001f600" * 20 + '"\\'
    ) * 80  # colour codes, 2- and 4-byte characters
    chunks = [(worst + "\n").encode()] * (lastrun.MAX_LINES + 50)
    lines, truncated = lastrun.collect_tail(chunks)
    assert truncated is True and sum(len(line.encode()) for line in lines) <= lastrun.MAX_LOG_BYTES
    # a character cut in half at the limit becomes one replacement character (3 bytes for at least 1)
    assert all(
        len(line.encode()) <= lastrun.MAX_LINE_BYTES + 2 + len(lastrun.TRUNCATED_SUFFIX) for line in lines
    )
    path = lastrun.write(
        agent_settings, IID, lastrun.build(IID, {"started_at": "x", "id": "c"}, lines, truncated)
    )
    assert path.stat().st_size < lastrun.MAX_RECORD_BYTES / 2
    loaded = lastrun.load(agent_settings, IID)
    assert loaded["log"]["lines"] == lines and loaded["log"]["truncated"] is True
    assert lastrun.summary(agent_settings, IID)["log_truncated"] is True


def test_a_new_record_replaces_the_old_one_and_the_summary_follows(agent_settings):
    lastrun.write(agent_settings, IID, _record(exit_code=1))
    assert lastrun.summary(agent_settings, IID)["exit_code"] == 1
    lastrun.write(agent_settings, IID, _record(exit_code=0, lines=["only"]))
    assert lastrun.summary(agent_settings, IID)["exit_code"] == 0
    assert lastrun.load(agent_settings, IID)["log"]["lines"] == ["only"]


def test_missing_and_unusable_records_read_as_none(agent_settings):
    assert lastrun.load(agent_settings, IID) is None and lastrun.summary(agent_settings, IID) is None
    assert lastrun.summaries(agent_settings) == {} and lastrun.delete(agent_settings, IID) is False
    folder = agent_settings.state_dir / "last-run"
    folder.mkdir()
    file = folder / f"{IID}.json"
    for content in (
        b"not json",
        b"[]",
        b'{"version": 99}',
        json.dumps({"version": 1, "instance_id": str(OTHER)}).encode(),
    ):
        file.write_bytes(content)
        assert lastrun.load(agent_settings, IID) is None and lastrun.summary(agent_settings, IID) is None
    file.unlink()
    target = folder / "elsewhere"
    target.write_text(json.dumps(_record()))
    file.symlink_to(target)  # never followed
    assert lastrun.load(agent_settings, IID) is None
    file.unlink()
    with open(file, "wb") as fh:
        fh.truncate(lastrun.MAX_RECORD_BYTES + 1)  # nothing this agent writes is that large
    assert lastrun.load(agent_settings, IID) is None
    (folder / "notes.txt").write_text("x")
    assert lastrun.summaries(agent_settings) == {}


def test_record_with_odd_fields_is_still_usable(agent_settings):
    folder = agent_settings.state_dir / "last-run"
    folder.mkdir()
    (folder / f"{IID}.json").write_text(
        json.dumps({"version": 1, "instance_id": str(IID), "log": "nope", "exit_code": 3})
    )
    assert lastrun.load(agent_settings, IID)["log"] == {"lines": [], "truncated": False}
    assert lastrun.summary(agent_settings, IID)["log_lines"] == 0
    assert lastrun.console_lines(lastrun.load(agent_settings, IID)) == []


def test_write_failure_is_reported_and_leaves_nothing_behind(agent_settings, monkeypatch):
    lastrun.write(agent_settings, IID, _record(lines=["kept"]))
    folder = agent_settings.state_dir / "last-run"

    def disk_full(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lastrun.os, "write", disk_full)
    with pytest.raises(lastrun.LastRunError, match="No space left"):
        lastrun.write(agent_settings, IID, _record(lines=["new"]))
    monkeypatch.undo()
    assert lastrun.load(agent_settings, IID)["log"]["lines"] == ["kept"]  # the previous record is untouched
    assert [p.name for p in folder.iterdir()] == [f"{IID}.json"]  # and no partial file is left


def test_write_refuses_a_state_folder_that_is_not_a_directory(agent_settings):
    (agent_settings.state_dir / "last-run").write_text("in the way")
    with pytest.raises(lastrun.LastRunError):
        lastrun.write(agent_settings, IID, _record())
    assert lastrun.load(agent_settings, IID) is None and lastrun.summaries(agent_settings) == {}


def test_delete_removes_the_record_and_leftovers_of_an_interrupted_write(agent_settings):
    lastrun.write(agent_settings, IID, _record())
    lastrun.write(agent_settings, OTHER, lastrun.build(OTHER, {"started_at": "x"}, [], False))
    folder = agent_settings.state_dir / "last-run"
    (folder / f".{IID}.deadbeef.tmp").write_text("partial")
    assert lastrun.delete(agent_settings, IID) is True
    assert sorted(p.name for p in folder.iterdir()) == [f"{OTHER}.json"]
    assert lastrun.summary(agent_settings, IID) is None and lastrun.delete(agent_settings, IID) is False


# --- retire ---------------------------------------------------------------------------------------
def test_retire_saves_the_record_then_removes_the_container(world, agent_settings):
    c = world.container
    c.exit_code, c.restart_count = 143, 4
    result = docker_ops.retire(agent_settings, IID, settle_seconds=10)
    assert result == {"retired": True, "reason": "", "recorded": True}
    assert world.calls == [
        ("logs", {"tail": lastrun.MAX_LINES, "follow": False, "stream": True}),
        "docker rm",
    ]
    record = json.loads(record_file(agent_settings).read_text())
    assert record["exit_code"] == 143 and record["restart_count"] == 4 and record["oom_killed"] is False
    assert record["container_id"] == "c0ffee" and record["image_id"] == "sha256:" + "ab" * 32
    assert record["image"] == "itzg/minecraft-server:stable-java21" and record["status"] == "exited"
    assert record["health"] == "unhealthy" and record["health_output"] == "Connection refused"
    assert record["started_at"] == c.started_at and record["finished_at"] == c.finished_at
    assert record["log"] == {
        "lines": ["[init] starting", 'Done (3.2s)! For help, type "help"', "Saving worlds"],
        "truncated": False,
    }
    assert PASSWORD not in record_file(agent_settings).read_text()  # the container's environment is not kept
    assert docker_ops.status(IID) == {"exists": False, "last_run": lastrun.summary(agent_settings, IID)}


@pytest.mark.parametrize("state", ["running", "restarting", "paused"])
def test_retire_refuses_a_container_that_runs(world, agent_settings, state):
    world.container.state = state
    with pytest.raises(AgentError) as exc:
        docker_ops.retire(agent_settings, IID, settle_seconds=0)
    assert (exc.value.code, exc.value.status) == ("container_running", 409)
    assert world.calls == [] and not record_file(agent_settings).exists()


def test_retire_refuses_a_container_in_another_state(world, agent_settings):
    world.container.state = "removing"
    with pytest.raises(AgentError) as exc:
        docker_ops.retire(agent_settings, IID, settle_seconds=0)
    assert exc.value.code == "container_busy" and world.calls == []


def test_retire_waits_for_the_settle_time_by_the_docker_hosts_clock(world, agent_settings):
    world.container.finished_at = _ts(4)
    with pytest.raises(AgentError) as exc:
        docker_ops.retire(agent_settings, IID, settle_seconds=10)
    assert (exc.value.code, exc.value.status) == ("not_settled", 409)
    assert world.calls == [] and not record_file(agent_settings).exists()
    assert docker_ops.retire(agent_settings, IID, settle_seconds=3)["retired"] is True


def test_stopped_seconds_reads_docker_timestamps():
    now = datetime(2026, 11, 10, 3, 13, 0, tzinfo=UTC)
    ran = {"started_at": "2026-11-10T03:00:00Z", "created_at": "2026-11-10T02:00:00Z"}
    assert docker_ops.stopped_seconds(
        {**ran, "finished_at": "2026-11-10T03:12:45.123456789Z"}, now
    ) == pytest.approx(14.876544)
    assert docker_ops.stopped_seconds({**ran, "finished_at": "2026-11-10T05:12:45+02:00"}, now) == 15
    # never started: since it was created; a finish time without a start is not used
    assert (
        docker_ops.stopped_seconds(
            {"created_at": "2026-11-10T03:12:00Z", "finished_at": "2026-11-10T03:12:59Z"}, now
        )
        == 60
    )
    # no usable time: unknown (the retire then goes ahead; the panel's own check has passed)
    for odd in (None, "", "garbage", "0001-01-01T00:00:00Z", "2026-13-45T99:99:99Z"):
        assert (
            docker_ops.stopped_seconds({"started_at": "x", "finished_at": odd, "created_at": odd}, now)
            is None
        )


def test_retire_of_a_container_that_never_ran_keeps_the_older_record(world, agent_settings):
    lastrun.write(agent_settings, IID, _record(lines=["the last real run"], id="an-older-container"))
    c = world.container
    c.state, c.started_at, c.finished_at = "created", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"
    c.created_at = _ts(4)
    with pytest.raises(AgentError) as exc:  # the settle time counts from its creation
        docker_ops.retire(agent_settings, IID, settle_seconds=10)
    assert exc.value.code == "not_settled"
    c.created_at = _ts(30)
    assert docker_ops.retire(agent_settings, IID, settle_seconds=10) == {
        "retired": True,
        "reason": "",
        "recorded": False,
    }
    assert world.calls == ["docker rm"]  # no log read, no record written
    assert lastrun.load(agent_settings, IID)["log"]["lines"] == ["the last real run"]


def test_retire_keeps_the_container_when_the_record_cannot_be_written(world, agent_settings, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise lastrun.LastRunError("OSError: [Errno 28] No space left on device")

    monkeypatch.setattr(lastrun, "write", refuse)
    with pytest.raises(AgentError) as exc:
        docker_ops.retire(agent_settings, IID, settle_seconds=0)
    assert (exc.value.code, exc.value.status) == ("last_run_failed", 500)
    assert "No space left" in str(exc.value) and "container was kept" in str(exc.value)
    assert "docker rm" not in world.calls and world.container.removed is False


def test_retire_keeps_the_container_when_its_log_cannot_be_read(world, agent_settings):
    world.container.logs_error = docker.errors.APIError(
        "500 Server Error", explanation="log driver does not support reading"
    )
    with pytest.raises(AgentError) as exc:
        docker_ops.retire(agent_settings, IID, settle_seconds=0)
    assert exc.value.code == "last_run_failed" and "docker rm" not in world.calls
    assert not record_file(agent_settings).exists()


def test_retire_without_a_container_is_success(world, agent_settings):
    world.container = None
    assert docker_ops.retire(agent_settings, IID, settle_seconds=10) == {
        "retired": False,
        "reason": "no_container",
        "recorded": False,
    }


@pytest.mark.parametrize(
    "error",
    [
        docker.errors.NotFound("No such container"),
        docker.errors.APIError(
            "409 Conflict",
            response=type("R", (), {"status_code": 409})(),
            explanation="removal of container c0ffee is already in progress",
        ),
    ],
)
def test_retire_counts_a_container_removed_by_someone_else_as_done(world, agent_settings, error):
    world.container.remove_error = error
    assert docker_ops.retire(agent_settings, IID, settle_seconds=0)["retired"] is True
    assert record_file(agent_settings).exists()


def test_retire_of_a_container_started_from_outside_meanwhile_is_not_now(world, agent_settings):
    world.container.remove_error = docker.errors.APIError(
        "409 Conflict",
        response=type("R", (), {"status_code": 409})(),
        explanation="cannot remove container: container is running: stop the container before removing",
    )
    with pytest.raises(AgentError) as exc:
        docker_ops.retire(agent_settings, IID, settle_seconds=0)
    assert (exc.value.code, exc.value.status) == ("container_running", 409)


def test_retire_reports_other_docker_errors_and_the_record_does_not_double_the_log(world, agent_settings):
    world.container.remove_error = docker.errors.APIError(
        "500 Server Error", explanation="driver failed to remove root filesystem"
    )
    with pytest.raises(docker.errors.APIError):
        docker_ops.retire(agent_settings, IID, settle_seconds=0)
    # the record describes the container that is still there: its lines are not shown twice
    assert docker_ops.console_history(IID, 100, None, agent_settings) == [
        "[init] starting",
        'Done (3.2s)! For help, type "help"',
        "Saving worlds",
    ]


# --- what a server without a container answers ---------------------------------------------------------
def test_status_without_container_and_without_record(world, agent_settings):
    world.container = None
    assert docker_ops.status(IID) == {"exists": False}


def test_kill_stats_and_commands_answer_not_running_without_a_container(world, agent_settings):
    world.container = None
    assert docker_ops.kill(IID) is None
    assert docker_ops.stats(IID) == {"running": False}
    with pytest.raises(AgentError) as exc:
        docker_ops.rcon_endpoint(agent_settings, IID)
    assert (exc.value.code, exc.value.status, str(exc.value)) == (
        "not_running",
        409,
        "The server is not running",
    )
    for call in (lambda: docker_ops.start(IID), lambda: docker_ops.stop(IID, 30, agent_settings)):
        with pytest.raises(AgentError) as exc:  # starting needs an apply; a stop job checks first
            call()
        assert (exc.value.code, exc.value.status) == ("no_container", 404)


def test_a_container_that_vanishes_between_two_docker_calls_reads_as_no_container(world, agent_settings):
    """A retire can remove the container between "get" and "inspect" of another request."""
    lastrun.write(agent_settings, IID, _record(lines=["saved"]))

    def vanished() -> None:
        raise docker.errors.NotFound("No such container")

    world.container.reload = vanished
    world.container.logs = lambda **_kwargs: vanished()
    assert docker_ops.status(IID)["exists"] is False and docker_ops.status(IID)["last_run"]["log_lines"] == 1
    assert docker_ops.kill(IID) is None
    assert docker_ops.stats(IID) == {"running": False}
    with pytest.raises(AgentError) as exc:
        docker_ops.rcon_endpoint(agent_settings, IID)
    assert exc.value.code == "not_running"
    assert docker_ops.console_history(IID, 10, None, agent_settings)[0] == "saved"
    assert docker_ops.retire(agent_settings, IID, 0) == {
        "retired": False,
        "reason": "no_container",
        "recorded": False,
    }
    assert world.calls == []


def test_routes_for_a_server_without_a_container(api, auth, world, agent_settings):
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    status = api.get(f"/v1/instances/{IID}/status", headers=auth).json()
    assert status["exists"] is False and status["operation"] is None and status["last_run"]["log_lines"] == 3
    kill = api.post(f"/v1/instances/{IID}/kill", headers=auth)
    assert kill.status_code == 200 and kill.json()["exists"] is False
    assert api.get(f"/v1/instances/{IID}/stats", headers=auth).json() == {"running": False}
    command = api.post(f"/v1/instances/{IID}/command", json={"command": "list"}, headers=auth)
    assert command.status_code == 409 and command.json()["error"]["code"] == "not_running"
    start = api.post(f"/v1/instances/{IID}/start", headers=auth)
    assert start.status_code == 404 and start.json()["error"]["code"] == "no_container"
    logs = api.get(f"/v1/instances/{IID}/logs", params={"tail": 2}, headers=auth).json()["lines"]
    assert logs[0] == "Saving worlds" and logs[1].startswith("[mcpanel] ---- end of the run that stopped ")
    # a server that never ran: no container and no record
    assert api.get(f"/v1/instances/{OTHER}/logs", headers=auth).json()["error"]["code"] == "no_container"
    assert api.get(f"/v1/instances/{OTHER}/status", headers=auth).json() == {
        "exists": False,
        "operation": None,
    }


def test_list_carries_the_last_run_records(api, auth, world, agent_settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "list_managed", lambda: [])
    assert api.get("/v1/instances", headers=auth).json() == {"containers": [], "last_runs": {}}
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    body = api.get("/v1/instances", headers=auth).json()
    assert body["containers"] == [] and list(body["last_runs"]) == [str(IID)]
    assert body["last_runs"][str(IID)]["exit_code"] == 0 and "log" not in body["last_runs"][str(IID)]


def test_info_advertises_the_retire_feature(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    monkeypatch.setattr(main.storage, "capabilities", lambda _settings: {})
    monkeypatch.setattr(main, "restic_available", lambda _settings: None)
    assert api.get("/v1/info", headers=auth).json()["features"] == ["retire"]


# --- console history --------------------------------------------------------------------------------
MARK = "[mcpanel] ---- end of the run that stopped "


def test_console_history_puts_the_previous_run_in_front_of_the_new_log(world, agent_settings):
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    world.container = FakeContainer(
        world.calls, status="running", container_id="new-container", output=b"new 1\nnew 2\n"
    )
    lines = docker_ops.console_history(IID, 500, None, agent_settings)
    assert lines[:3] == ["[init] starting", 'Done (3.2s)! For help, type "help"', "Saving worlds"]
    assert lines[3].startswith(MARK) and lines[4:] == ["new 1", "new 2"]
    assert [
        ln for ln in docker_ops.console_history(IID, 4, None, agent_settings) if not ln.startswith(MARK)
    ] == ["Saving worlds", "new 1", "new 2"]
    assert docker_ops.console_history(IID, 2, None, agent_settings) == ["new 1", "new 2"]
    assert docker_ops.console_history(IID, 1, None, agent_settings) == ["new 2"]
    # ``since`` reads the current container only
    assert docker_ops.console_history(IID, 500, 1700000000, agent_settings) == ["new 1", "new 2"]
    # the next retire replaces the record: the run before the last is gone
    world.container.state = "exited"
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    lines = docker_ops.console_history(IID, 500, None, agent_settings)
    assert lines[:2] == ["new 1", "new 2"] and lines[2].startswith(MARK) and len(lines) == 3


def test_console_history_survives_a_broken_record(world, agent_settings):
    folder = agent_settings.state_dir / "last-run"
    folder.mkdir()
    (folder / f"{IID}.json").write_text("{broken")
    world.container.state = "running"
    assert docker_ops.console_history(IID, 500, None, agent_settings)[-1] == "Saving worlds"


# --- the log stream -----------------------------------------------------------------------------------
def _stream(api, auth, wanted: int, tail: int = 10) -> list:
    """Messages of the log stream until ``wanted`` log lines arrived (states and logs, in order)."""
    seen: list = []
    lines = 0
    with api.websocket_connect(f"/v1/instances/{IID}/logs/stream?tail={tail}", headers=auth) as ws:
        for _ in range(200):
            msg = ws.receive_json()
            if msg["type"] == "state":
                seen.append(("state", msg["state"]))
            elif msg["type"] == "logs":
                seen.extend(("log", line) for line in msg["lines"])
                lines += len(msg["lines"])
            if lines >= wanted:
                return seen
    raise AssertionError(f"only {lines} of {wanted} lines arrived: {seen}")


def test_stream_of_a_server_without_a_container_opens_with_its_last_run(api, auth, world, agent_settings):
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    seen = _stream(api, auth, wanted=3, tail=3)
    assert seen[0] == ("state", "missing")
    logs = [text for kind, text in seen if kind == "log"]
    assert logs[:2] == ['Done (3.2s)! For help, type "help"', "Saving worlds"] and logs[2].startswith(MARK)


def test_stream_shows_the_previous_run_in_front_of_a_new_container(api, auth, world, agent_settings):
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    world.container = FakeContainer(
        world.calls, status="exited", container_id="new-container", output=b"new 1\nnew 2\n"
    )
    seen = _stream(api, auth, wanted=5, tail=5)
    logs = [text for kind, text in seen if kind == "log"]
    assert logs[0] == 'Done (3.2s)! For help, type "help"' and logs[1] == "Saving worlds"
    assert logs[2].startswith(MARK) and logs[3:] == ["new 1", "new 2"]
    # a full tail of the new container leaves no room for the old run
    seen = _stream(api, auth, wanted=2, tail=2)
    assert [text for kind, text in seen if kind == "log"] == ["new 1", "new 2"]


def test_stream_reads_a_container_created_after_it_opened_from_its_first_line(
    api, auth, world, agent_settings, monkeypatch
):
    """A console that is open while the server is stopped (no container) and then started: the
    new container's whole output arrives, without a time filter that could cut its first lines."""
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    calls = world.calls
    real_get = docker_ops.get_container
    looks = []

    def get(instance_id):
        looks.append(1)
        if len(looks) == 2:  # the server was started between the first and the second look
            world.container = FakeContainer(
                calls, status="running", container_id="new-container", output=b"new 1\nnew 2\n"
            )
        return real_get(instance_id)

    monkeypatch.setattr(docker_ops, "get_container", get)
    seen = _stream(api, auth, wanted=6, tail=10)
    assert ("state", "missing") in seen and ("state", "running") in seen
    assert [text for kind, text in seen if kind == "log"][-2:] == ["new 1", "new 2"]
    new_reads = [entry[1] for entry in calls if isinstance(entry, tuple) and entry[1].get("follow")]
    assert new_reads[0] == {"tail": "all", "follow": True, "stream": True}  # no "since"


# --- routes: retire, last-run, delete, apply -----------------------------------------------------------
def test_retire_route(api, auth, world, agent_settings):
    world.container.finished_at = _ts(2)
    early = api.post(f"/v1/instances/{IID}/retire", headers=auth)  # the default settle time is 10 s
    assert early.status_code == 409 and early.json()["error"]["code"] == "not_settled"
    done = api.post(f"/v1/instances/{IID}/retire", json={"settle_seconds": 1}, headers=auth)
    assert done.status_code == 200
    body = done.json()
    assert (body["retired"], body["recorded"], body["reason"]) == (True, True, "")
    assert (
        body["last_run"]["exit_code"] == 0
        and body["last_run"]["log_lines"] == 3
        and "log" not in body["last_run"]
    )
    again = api.post(f"/v1/instances/{IID}/retire", json={"settle_seconds": 1}, headers=auth).json()
    assert (again["retired"], again["reason"]) == (False, "no_container") and again["last_run"][
        "log_lines"
    ] == 3
    assert (
        api.post(f"/v1/instances/{IID}/retire", json={"settle_seconds": -1}, headers=auth).status_code == 422
    )
    assert api.post(f"/v1/instances/{IID}/retire").status_code == 401


def test_retire_route_gives_way_to_a_lifecycle_operation_and_to_the_lock(api, auth, world, agent_settings):
    release = threading.Event()
    op = registry.start("apply", [IID], lambda _op: release.wait(10) and {})
    try:
        busy = api.post(f"/v1/instances/{IID}/retire", json={"settle_seconds": 0}, headers=auth)
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "instance_busy"
    finally:
        release.set()
    for _ in range(100):
        if registry.get(op.id).finished_at:
            break
        threading.Event().wait(0.02)
    main_wait = main.RETIRE_LOCK_WAIT
    main.RETIRE_LOCK_WAIT = 0.05
    try:
        with docker_ops.instance_lock(IID):  # a start or restart of this server is in progress
            locked = api.post(f"/v1/instances/{IID}/retire", json={"settle_seconds": 0}, headers=auth)
        assert locked.status_code == 409 and locked.json()["error"]["code"] == "instance_busy"
    finally:
        main.RETIRE_LOCK_WAIT = main_wait
    assert world.calls == [] and world.container.removed is False
    assert (
        api.post(f"/v1/instances/{IID}/retire", json={"settle_seconds": 0}, headers=auth).json()["retired"]
        is True
    )


def test_restart_holds_the_lock_from_the_stop_to_the_start(api, auth, world, agent_settings, monkeypatch):
    world.container.state = "running"
    attempts: list[str] = []
    real_start = docker_ops.start

    def start_after_a_retire_attempt(instance_id):
        # between the stop and the start of a restart, a retire of the (now exited) container arrives
        try:
            with docker_ops.instance_lock(instance_id, wait=0.05):
                attempts.append("got the lock")
        except AgentError as exc:
            attempts.append(exc.code)
        real_start(instance_id)

    monkeypatch.setattr(docker_ops, "start", start_after_a_retire_attempt)
    r = api.post(f"/v1/instances/{IID}/restart", json={"timeout": 5}, headers=auth)
    assert r.status_code == 200 and r.json()["running"] is True
    assert attempts == ["instance_busy"] and world.calls[-2:] == ["docker stop", "docker start"]


def test_forget_last_run_route(api, auth, world, agent_settings):
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    assert api.delete(f"/v1/instances/{IID}/last-run", headers=auth).json() == {"deleted": True}
    assert api.delete(f"/v1/instances/{IID}/last-run", headers=auth).json() == {"deleted": False}
    assert not record_file(agent_settings).exists()
    assert api.get(f"/v1/instances/{IID}/status", headers=auth).json() == {"exists": False, "operation": None}
    assert api.delete(f"/v1/instances/{IID}/last-run").status_code == 401


def _wait_op(api, auth, op_id: str) -> dict:
    for _ in range(200):
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] in ("succeeded", "failed"):
            return op
        threading.Event().wait(0.02)
    raise AssertionError("operation did not finish")


def test_delete_data_removes_the_record(api, auth, world, agent_settings):
    docker_ops.retire(agent_settings, IID, settle_seconds=0)
    (agent_settings.instances_dir / str(IID)).mkdir()
    r = api.request("DELETE", f"/v1/instances/{IID}/data", json={"confirm": str(IID)}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert not record_file(agent_settings).exists() and op["result"]["warnings"] == []


def test_delete_data_of_a_server_with_a_container_writes_no_record(api, auth, world, agent_settings):
    (agent_settings.instances_dir / str(IID)).mkdir()
    r = api.request("DELETE", f"/v1/instances/{IID}/data", json={"confirm": str(IID)}, headers=auth)
    assert _wait_op(api, auth, r.json()["operation"]["id"])["status"] == "succeeded"
    assert world.calls == ["docker rm"] and not record_file(agent_settings).exists()


def test_removing_a_container_keeps_its_last_run_best_effort(world, agent_settings, monkeypatch):
    # archive, migration and the removal inside an apply: the console of the last run survives
    assert docker_ops.remove_container(IID, timeout=30, settings=agent_settings) is True
    assert (
        world.calls[-1] == "docker rm"
        and lastrun.load(agent_settings, IID)["log"]["lines"][-1] == "Saving worlds"
    )

    world.container = FakeContainer(world.calls, container_id="second", output=b"x\n")
    monkeypatch.setattr(
        lastrun, "write", lambda *a, **k: (_ for _ in ()).throw(lastrun.LastRunError("disk full"))
    )
    assert (
        docker_ops.remove_container(IID, timeout=30, settings=agent_settings) is True
    )  # the removal was asked for
    assert world.container.removed is True
    assert lastrun.load(agent_settings, IID)["container_id"] == "c0ffee"  # the older record is still there
