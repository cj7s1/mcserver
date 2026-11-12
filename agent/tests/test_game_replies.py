"""B-3 and B-9 of the security review (2026-11-11): replies and files of the game are parsed defensively,
a stop always reaches ``docker stop``, and text of the game reaches errors and log lines clipped and as
one line.

Adopted from the reviewers' tests (audit-2026-11-09/security-review/b-agent-fs/tests/test_b_game_replies.py;
its B-2 test of a trickling answer belongs to round 2 and is not here yet).

A JSON text that is only opening brackets is valid input for ``json.loads`` up to the point where it
raises RecursionError, which is not a ValueError.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from mcagent import docker_ops, drivers, profiles
from mcagent.docker_ops import StopResult
from mcagent.errors import AgentError, game_text

# 200 KB: far inside every size limit of the drivers (4 MiB frames, 8 MiB answers, 2 MiB config)
DEEP = "[" * 200_000


def _running_container(stopped: list[int], game: str | None = None) -> SimpleNamespace:
    labels = {"io.mcpanel.instance": str(uuid.uuid4())}
    if game:
        labels["io.mcpanel.game"] = game
    c = SimpleNamespace(
        status="running",
        name="mc-test",
        id="0" * 64,
        labels=labels,
        attrs={"Config": {"Env": []}, "State": {"Running": True, "ExitCode": 0}},
        reload=lambda: None,
    )

    def stop(timeout=None):
        stopped.append(timeout)
        c.status = "exited"
        c.attrs["State"] = {"Running": False, "ExitCode": 0}

    c.stop = stop
    return c


def _unexpected(*_args, **_kwargs):
    # what json.loads raises for DEEP inside the driver's own parsing
    raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")


def test_json_loads_raises_recursion_error_for_deep_nesting():
    """The premise of the tests below."""
    with pytest.raises(RecursionError):
        json.loads(DEEP)


def test_http_api_answer_with_deep_nesting_is_not_json():
    from mcagent.drivers.http_api import ApiResponse

    assert ApiResponse(200, {}, DEEP.encode()).json() is None


def test_webrcon_skips_a_frame_with_deep_nesting():
    from mcagent.drivers import webrcon

    class Socket:
        def __init__(self) -> None:
            self.frames = [DEEP, json.dumps({"Identifier": 7, "Message": "ok"})]

        def send(self, _text: str) -> None:
            pass

        def recv(self, timeout: float | None = None) -> str:
            return self.frames.pop(0)

    frame = webrcon._talk(Socket(), 7, "status", time.monotonic() + 5, 5.0)
    assert frame["Message"] == "ok"


def test_webrcon_info_with_deep_nesting_is_an_agent_error(agent_settings, monkeypatch):
    from mcagent.drivers.webrcon import WebRconDriver

    driver = drivers.for_profile(profiles.get("rust"))
    assert isinstance(driver, WebRconDriver)
    monkeypatch.setattr(driver, "run", lambda *a, **k: DEEP)
    with pytest.raises(AgentError) as err:
        driver.game_action(agent_settings, _running_container([]), "info", {}, 5.0)
    assert err.value.code == "rcon_failed"


def test_tshock_token_lookup_survives_a_config_with_deep_nesting(agent_settings):
    from mcagent.drivers.tshock import TShockDriver

    instance = uuid.uuid4()
    folder = agent_settings.instances_dir / str(instance)
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(DEEP)  # the game writes this file
    container = SimpleNamespace(labels={"io.mcpanel.instance": str(instance)}, name="mc-test")
    driver = TShockDriver(port=7878, config_file="config.json", username="mcpanel")
    assert driver.secret(agent_settings, container) is None


@pytest.mark.parametrize("game", ["rust", "palworld", "satisfactory"])
def test_stop_reaches_docker_stop_when_the_save_fails_in_an_unexpected_way(game, monkeypatch):
    """The driver's save before the stop raises something that is not an AgentError (a reply the
    parser did not expect). The stop must still be sent to Docker."""
    driver = drivers.for_profile(profiles.get(game))
    for name in ("run", "call", "save_now", "request"):
        if hasattr(driver, name):
            monkeypatch.setattr(driver, name, _unexpected, raising=False)
    monkeypatch.setattr(driver, "secret", lambda _settings, _c: "A1b2C3d4E5f6G7h8I9j0", raising=False)
    stopped: list[int] = []
    result = StopResult()
    handled = driver.stop(None, _running_container(stopped), 30, result)
    # either the driver stopped the container itself, or it leaves the plain docker stop to the caller
    assert stopped == [30] or handled is False
    assert result.warnings, "the failed save is reported as a warning"


def test_graceful_stop_sends_docker_stop_whatever_the_driver_raises(agent_settings, monkeypatch):
    """Defence in depth: a driver stop that raises anything but a Docker error still ends in docker stop."""
    stopped: list[int] = []
    c = _running_container(stopped, game="rust")
    monkeypatch.setattr(type(drivers.for_profile(profiles.get("rust"))), "stop", _unexpected)
    monkeypatch.setattr(docker_ops, "fix_ownership", lambda *a, **k: None)
    result = docker_ops.graceful_stop(agent_settings, c, 30)
    assert stopped == [30] and result.exit_code == 0
    assert any("RecursionError" in w for w in result.warnings)


def test_graceful_stop_lets_dockers_own_errors_through(agent_settings, monkeypatch):
    from docker.errors import APIError

    def docker_fails(*_a, **_k):
        raise APIError("daemon trouble")

    c = _running_container([], game="rust")
    monkeypatch.setattr(type(drivers.for_profile(profiles.get("rust"))), "stop", docker_fails)
    with pytest.raises(APIError):
        docker_ops.graceful_stop(agent_settings, c, 30)


# --- B-9: text of the game --------------------------------------------------------------------------------
def test_error_text_of_the_game_is_clipped_and_one_line():
    from mcagent.drivers.http_api import GameApiError
    from mcagent.drivers.satisfactory import SatisfactoryDriver

    forged = "A" * 1_000_000 + "\n2026-11-11 00:00:00,000 ERROR mcagent forged line"
    with pytest.raises(GameApiError) as err:
        SatisfactoryDriver._result("HealthCheck", 500, {"errorCode": "boom", "errorMessage": forged})
    text = str(err.value)
    assert len(text) <= 2000
    assert "\n" not in text


def test_a_known_error_code_of_the_game_still_maps():
    from mcagent.drivers.http_api import GameApiError
    from mcagent.drivers.satisfactory import SatisfactoryDriver

    with pytest.raises(GameApiError) as err:
        SatisfactoryDriver._result("ClaimServer", 400, {"errorCode": "server_claimed", "errorMessage": "x"})
    assert err.value.api_code == "server_claimed" and err.value.status == 409


def test_game_text():
    assert game_text("a\r\nb\x1b[31mc\u2028d\x00") == "a b [31mc d"
    assert len(game_text("x" * 5000)) == 300 and game_text("x" * 5000).endswith("...")
    assert game_text("short") == "short" and game_text(None) == "None"


def test_tshock_error_text_is_clipped(agent_settings, monkeypatch):
    from mcagent.drivers import tshock

    driver = tshock.TShockDriver(port=7878, config_file="config.json", username="mcpanel")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, _n):
            return json.dumps({"status": "400", "error": "E" * 10_000 + "\nforged"}).encode()

    monkeypatch.setattr(tshock._OPENER, "open", lambda *a, **k: Response())
    monkeypatch.setattr(driver, "secret", lambda *_a: "t" * 20)
    monkeypatch.setattr(driver, "address", lambda *_a: ("10.0.0.2", 7878), raising=False)
    try:
        driver.call(agent_settings, _running_container([]), "/v2/server/status", {}, timeout=1.0)
    except AgentError as exc:
        assert len(str(exc)) <= 400 and "\n" not in str(exc)
    else:  # pragma: no cover
        pytest.fail("an error status must raise")
