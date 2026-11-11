"""The ``palworld-api`` driver against a fake Palworld REST API (shapes from the probe of 2026-11-10)."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from mcagent import docker_ops, drivers
from mcagent.docker_ops import StopResult
from mcagent.drivers.palworld import PalworldDriver
from mcagent.errors import AgentError
from mcagent.profiles import palworld

PASSWORD = "Adm1n-Pal_secret"
IID = uuid.uuid4()


class FakePalworld:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self.password = PASSWORD
        self.online: set[str] = set()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=b"", ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self):
                expected = "Basic " + base64.b64encode(f"admin:{server.password}".encode()).decode()
                return self.headers.get("Authorization") == expected

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                server.calls.append((method, self.path, body, self.headers.get("Authorization")))
                if not self._auth_ok():
                    return self._reply(401, b"Unauthorized", "text/plain")
                path = self.path
                if method == "GET" and path == "/v1/api/info":
                    return self._reply(
                        200,
                        json.dumps(
                            {
                                "version": "v1.0.5.102999",
                                "servername": "pal",
                                "description": "",
                                "worldguid": "ABC",
                            }
                        ).encode(),
                    )
                if method == "GET" and path == "/v1/api/players":
                    return self._reply(
                        200,
                        json.dumps(
                            {
                                "players": [
                                    {"name": "Kai", "userId": "steam_76561190000000001", "ip": "10.0.0.2"}
                                ]
                            }
                        ).encode(),
                    )
                if method == "GET" and path == "/v1/api/settings":
                    return self._reply(
                        200, json.dumps({"Difficulty": "None", "ExpRate": 1.0}).encode(), "text/plain"
                    )
                if method == "GET" and path == "/v1/api/metrics":
                    return self._reply(
                        200,
                        json.dumps(
                            {
                                "currentplayernum": 1,
                                "serverfps": 59,
                                "maxplayernum": 8,
                                "days": 2,
                                "uptime": 46,
                            }
                        ).encode(),
                    )
                if method == "POST" and path == "/v1/api/announce":
                    return self._reply(200 if body and body.get("message") else 400)
                if method == "POST" and path == "/v1/api/kick":
                    return self._reply(200 if body and body.get("userid") in server.online else 400)
                if method == "POST" and path in (
                    "/v1/api/ban",
                    "/v1/api/unban",
                    "/v1/api/save",
                    "/v1/api/shutdown",
                ):
                    return self._reply(200)
                return self._reply(
                    404, b'{"errorCode": "errors.com.epicgames.httpserver.route_handler_not_found"}'
                )

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeContainer:
    def __init__(self, env=None, status="running"):
        self.id = "pal1"
        self.name = docker_ops.container_name(IID)
        self.status = status
        self.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.game": "palworld"}
        self.attrs = {
            "Config": {
                "Env": [
                    f"{k}={v}" for k, v in (env if env is not None else {"ADMIN_PASSWORD": PASSWORD}).items()
                ]
            },
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "127.0.0.1"}}},
            "HostConfig": {},
            "State": {"StartedAt": "2026-11-11T10:00:00Z"},
        }
        self.stopped_with = None

    def stop(self, timeout):
        self.stopped_with = timeout
        self.status = "exited"


@pytest.fixture
def pal(monkeypatch):
    fake = FakePalworld()
    driver = drivers.for_profile(palworld.PROFILE)
    assert isinstance(driver, PalworldDriver)
    driver.port = fake.port  # the fake listens on a free port of 127.0.0.1
    yield SimpleNamespace(fake=fake, driver=driver)
    fake.close()


def test_profile_uses_the_driver_with_plain_http_and_the_admin_password_variable():
    driver = drivers.for_profile(palworld.PROFILE)
    assert isinstance(driver, PalworldDriver)
    assert driver.scheme == "http" and driver.port == 8212 and driver.secret_env == "ADMIN_PASSWORD"


def test_basic_auth_and_info(pal, agent_settings):
    result = pal.driver.game_action(agent_settings, FakeContainer(), "info", {}, 10)
    assert result["version"] == "v1.0.5.102999"
    method, path, _body, auth = pal.fake.calls[-1]
    assert (method, path) == ("GET", "/v1/api/info")
    assert auth == "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()


@pytest.mark.parametrize("action", ["players", "settings", "metrics"])
def test_reads(pal, agent_settings, action):
    result = pal.driver.game_action(agent_settings, FakeContainer(), action, {}, 10)
    if action == "players":
        assert result == {"players": [{"name": "Kai", "userId": "steam_76561190000000001", "ip": "10.0.0.2"}]}
    elif action == "settings":
        assert result["Difficulty"] == "None"  # JSON with a text/plain content type
    else:
        assert result["currentplayernum"] == 1


def test_announce_kick_ban_unban_save_shutdown(pal, agent_settings):
    c = FakeContainer()
    pal.fake.online.add("steam_76561190000000002")
    d = pal.driver
    assert d.game_action(agent_settings, c, "announce", {"message": "  hello\tall  "}, 10) == {"ok": True}
    assert pal.fake.calls[-1][2] == {"message": "hello all"}
    assert d.game_action(
        agent_settings, c, "kick", {"userid": "steam_76561190000000002", "message": "bye"}, 10
    )["ok"]
    assert pal.fake.calls[-1][1:3] == (
        "/v1/api/kick",
        {"userid": "steam_76561190000000002", "message": "bye"},
    )
    assert d.game_action(agent_settings, c, "ban", {"userid": "steam_76561190000000003"}, 10)["ok"]
    assert pal.fake.calls[-1][2] == {"userid": "steam_76561190000000003"}
    assert d.game_action(agent_settings, c, "unban", {"userid": "steam_76561190000000003"}, 10)["ok"]
    assert d.game_action(agent_settings, c, "save", {}, 10) == {"ok": True}
    assert pal.fake.calls[-1][:2] == ("POST", "/v1/api/save")
    assert d.game_action(agent_settings, c, "shutdown", {"waittime": 5}, 10) == {"ok": True, "waittime": 5}
    assert pal.fake.calls[-1][2] == {"waittime": 5, "message": "The server is shutting down"}


def test_a_refused_request_is_api_rejected(pal, agent_settings):
    with pytest.raises(AgentError) as exc:
        pal.driver.game_action(
            agent_settings, FakeContainer(), "kick", {"userid": "steam_76561190000000009"}, 10
        )
    assert (exc.value.code, exc.value.status) == ("api_rejected", 409)


def test_a_wrong_password_is_api_unauthorized(pal, agent_settings):
    pal.fake.password = "something-else-entirely"
    with pytest.raises(AgentError) as exc:
        pal.driver.game_action(agent_settings, FakeContainer(), "info", {}, 10)
    assert (exc.value.code, exc.value.status) == ("api_unauthorized", 409)
    assert PASSWORD not in str(exc.value)


def test_no_password_is_refused_before_any_request(pal, agent_settings):
    with pytest.raises(AgentError) as exc:
        pal.driver.game_action(agent_settings, FakeContainer(env={}), "info", {}, 10)
    assert exc.value.code == "api_unauthorized"
    assert pal.fake.calls == []


@pytest.mark.parametrize(
    "action,params",
    [
        ("announce", {}),
        ("announce", {"message": "x" * 501}),
        ("announce", {"message": 5}),
        ("kick", {"userid": "steam 1"}),
        ("ban", {"userid": "a\nb"}),
        ("unban", {}),
        ("shutdown", {"waittime": -1}),
        ("shutdown", {"waittime": True}),
        ("shutdown", {"waittime": "5"}),
    ],
)
def test_invalid_parameters(pal, agent_settings, action, params):
    with pytest.raises(AgentError) as exc:
        pal.driver.game_action(agent_settings, FakeContainer(), action, params, 10)
    assert exc.value.code == "invalid_request"
    assert pal.fake.calls == []


def test_unknown_action_and_no_console(pal, agent_settings):
    with pytest.raises(AgentError) as exc:
        pal.driver.game_action(agent_settings, FakeContainer(), "stop", {}, 10)
    assert exc.value.code == "action_unsupported"
    with pytest.raises(AgentError) as exc:
        asyncio.run(pal.driver.command(agent_settings, FakeContainer(), "save", 5))
    assert exc.value.code == "command_unsupported"


def test_stop_saves_then_stops(pal, agent_settings):
    c = FakeContainer()
    result = StopResult()
    assert pal.driver.stop(agent_settings, c, 60, result) is True
    assert pal.fake.calls[-1][:2] == ("POST", "/v1/api/save")
    assert result.flushed and c.stopped_with == 60 and not result.warnings


def test_stop_without_the_api_is_a_warning(agent_settings):
    driver = drivers.for_profile(palworld.PROFILE)
    driver.port = 9  # nothing listens there
    c = FakeContainer()
    result = StopResult()
    driver.stop(agent_settings, c, 60, result)
    assert not result.flushed and c.stopped_with == 60
    assert "not saved" in result.warnings[0]


def test_saving_channel_and_save(pal, agent_settings, monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(docker_ops, "running_container", lambda iid: c)
    send = pal.driver.channel(agent_settings, IID)
    steps = []
    pal.driver.save(send, lambda i, m: steps.append(m))
    assert pal.fake.calls[-1][:2] == ("POST", "/v1/api/save")
    assert steps == ["Saving the world (REST save)"]
    with pytest.raises(AgentError):
        send("save-off", 5)


def test_status_query_asks_metrics_only(pal, agent_settings):
    target = pal.driver.query_endpoint(agent_settings, FakeContainer())
    answer = asyncio.run(target.query(5))
    assert (answer["online"], answer["max"], answer["sample"]) == (1, 8, [])
    assert [call[1] for call in pal.fake.calls] == ["/v1/api/metrics"]


def test_broadcast_announces(pal, agent_settings):
    pal.driver.broadcast(agent_settings, FakeContainer(), "Restart in 5 minutes")
    assert pal.fake.calls[-1][1:3] == ("/v1/api/announce", {"message": "Restart in 5 minutes"})
