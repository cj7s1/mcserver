"""The ``webrcon`` driver (Rust) against a fake WebRCON server (websockets' own server)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from types import SimpleNamespace

import pytest
from websockets.sync.server import serve

from mcagent import docker_ops, drivers
from mcagent.docker_ops import StopResult
from mcagent.drivers import webrcon
from mcagent.drivers.webrcon import WebRconDriver
from mcagent.errors import AgentError
from mcagent.profiles import rust

PASSWORD = "Wr7pQ2xZ9kL4mN8vB3cD"
IID = uuid.uuid4()
PLAYERS = [{"SteamID": "76561190000000001", "DisplayName": "Kai", "Ping": 20, "ConnectedSeconds": 61}]


class FakeWebRcon:
    """Answers each command with a reply carrying its Identifier, after console noise (Identifier
    0/-1, a non-JSON frame and a reply to somebody else's id)."""

    def __init__(self, *, password=PASSWORD, mode="ok"):
        self.password = password
        self.mode = mode
        self.received: list[dict] = []
        self.paths: list[str] = []
        self.server = serve(self.handler, "127.0.0.1", 0, process_request=self.check)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def check(self, connection, request):
        self.paths.append(request.path)
        if request.path != f"/{self.password}":
            return connection.respond(401, "nope\n")
        return None

    def handler(self, ws):
        for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            if self.mode == "silent":
                continue
            if self.mode == "close":
                ws.close()
                return
            ws.send(json.dumps({"Identifier": 0, "Message": "[chat] someone said hi", "Type": "Chat"}))
            ws.send(json.dumps({"Identifier": -1, "Message": "Saving complete", "Type": "Generic"}))
            ws.send("not json at all")
            ws.send(json.dumps({"Identifier": msg["Identifier"] + 1, "Message": "for another client"}))
            command = msg["Message"]
            if command == "playerlist":
                reply = json.dumps(PLAYERS)
            elif command == "serverinfo":
                reply = json.dumps({"Hostname": "Wipe Day", "MaxPlayers": 50, "Players": 1})
            else:
                reply = f"ran: {command}"
            ws.send(
                json.dumps(
                    {"Identifier": msg["Identifier"], "Message": reply, "Type": "Generic", "Stacktrace": ""}
                )
            )

    def close(self):
        self.server.shutdown()
        self.thread.join(2)


class FakeContainer:
    def __init__(self, env=None):
        self.id = "rust1"
        self.name = docker_ops.container_name(IID)
        self.status = "running"
        self.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.game": "rust"}
        self.attrs = {
            "Config": {
                "Env": [
                    f"{k}={v}"
                    for k, v in (env if env is not None else {"RUST_RCON_PASSWORD": PASSWORD}).items()
                ]
            },
            "NetworkSettings": {"Networks": {"mcpanel_instances": {"IPAddress": "127.0.0.1"}}},
            "HostConfig": {},
        }
        self.stopped_with = None

    def stop(self, timeout):
        self.stopped_with = timeout
        self.status = "exited"


@pytest.fixture
def wr():
    servers = []

    def make(**kw):
        fake = FakeWebRcon(**kw)
        servers.append(fake)
        driver = drivers.for_profile(rust.PROFILE)
        assert isinstance(driver, WebRconDriver)
        driver.port = fake.port
        return SimpleNamespace(fake=fake, driver=driver)

    yield make
    for s in servers:
        s.close()


def test_profile_driver():
    driver = drivers.for_profile(rust.PROFILE)
    assert isinstance(driver, WebRconDriver)
    assert (driver.port, driver.secret_env, driver.a2s_port_env) == (
        28016,
        "RUST_RCON_PASSWORD",
        "RUST_SERVER_QUERYPORT",
    )


def test_command_reply_is_matched_by_identifier(wr, agent_settings):
    t = wr()
    out = asyncio.run(t.driver.command(agent_settings, FakeContainer(), "status", 5))
    assert out == "ran: status"
    sent = t.fake.received[-1]
    assert sent["Name"] == "WebRcon" and sent["Message"] == "status" and sent["Identifier"] >= 1001
    assert t.fake.paths == [f"/{PASSWORD}"]


def test_ids_increase_from_1001():
    first = webrcon.next_id()
    assert first >= webrcon.FIRST_ID
    assert webrcon.next_id() == first + 1


def test_players_and_info_are_parsed(wr, agent_settings):
    t = wr()
    assert t.driver.game_action(agent_settings, FakeContainer(), "players", {}, 5) == {"players": PLAYERS}
    assert t.fake.received[-1]["Message"] == "playerlist"
    assert t.driver.game_action(agent_settings, FakeContainer(), "info", {}, 5)["Hostname"] == "Wipe Day"


def test_moderation_commands(wr, agent_settings):
    t = wr()
    c = FakeContainer()
    d = t.driver
    sid = "76561190000000002"
    d.game_action(agent_settings, c, "kick", {"steamid": sid, "reason": "afk"}, 5)
    assert t.fake.received[-1]["Message"] == f'kick {sid} "afk"'
    d.game_action(agent_settings, c, "ban", {"steamid": sid, "name": "Kai", "reason": "cheating"}, 5)
    assert t.fake.received[-1]["Message"] == f'banid {sid} "Kai" "cheating"'
    d.game_action(agent_settings, c, "unban", {"steamid": sid}, 5)
    assert t.fake.received[-1]["Message"] == f"unban {sid}"
    d.game_action(agent_settings, c, "owner_add", {"steamid": sid, "name": "Kai"}, 5)
    assert [m["Message"] for m in t.fake.received[-2:]] == [f'ownerid {sid} "Kai" ""', "server.writecfg"]
    d.game_action(agent_settings, c, "owner_remove", {"steamid": sid}, 5)
    assert [m["Message"] for m in t.fake.received[-2:]] == [f"removeowner {sid}", "server.writecfg"]
    d.game_action(agent_settings, c, "say", {"message": "hello"}, 5)
    assert t.fake.received[-1]["Message"] == 'say "hello"'
    assert d.game_action(agent_settings, c, "save", {}, 5)["output"] == "ran: server.save"


@pytest.mark.parametrize(
    "action,params",
    [
        ("kick", {"steamid": "123"}),
        ("kick", {"steamid": "76561190000000002", "reason": 'x" ; quit "'}),
        ("ban", {"steamid": "76561190000000002", "name": "a;quit"}),
        ("ban", {"steamid": "76561190000000002", "reason": "a\\b"}),
        ("owner_add", {"steamid": "76561190000000002", "name": "x" * 129}),
        ("say", {}),
        ("say", {"message": "line\nbreak"}),
    ],
)
def test_invalid_parameters_send_nothing(wr, agent_settings, action, params):
    t = wr()
    with pytest.raises(AgentError) as exc:
        t.driver.game_action(agent_settings, FakeContainer(), action, params, 5)
    assert exc.value.code == "invalid_request"
    assert t.fake.received == []


def test_unknown_action(wr, agent_settings):
    with pytest.raises(AgentError) as exc:
        wr().driver.game_action(agent_settings, FakeContainer(), "quit", {}, 5)
    assert exc.value.code == "action_unsupported"


def test_wrong_password_is_refused_and_never_echoed(wr, agent_settings, caplog):
    t = wr(password="SomethingElse12345678")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AgentError) as exc:
        t.driver.run(agent_settings, FakeContainer(), "status", 5)
    assert exc.value.code == "rcon_refused"
    assert PASSWORD not in str(exc.value)
    # the agent's side logs nothing with the password (the fake server's own log is not the agent's)
    agent_side = [r.getMessage() for r in caplog.records if not r.name.startswith("websockets.server")]
    assert all(PASSWORD not in line for line in agent_side)


@pytest.mark.parametrize("password", ["short", "has space in it 12345", "slash/in/the/password123", ""])
def test_unusable_password_is_refused_before_connecting(wr, agent_settings, password):
    t = wr()
    with pytest.raises(AgentError) as exc:
        t.driver.run(agent_settings, FakeContainer(env={"RUST_RCON_PASSWORD": password}), "status", 5)
    assert exc.value.code == "rcon_unavailable"
    assert t.fake.paths == []
    if password:
        assert password not in str(exc.value)


def test_no_answer_is_a_timeout(wr, agent_settings):
    t = wr(mode="silent")
    with pytest.raises(AgentError) as exc:
        t.driver.run(agent_settings, FakeContainer(), "status", 0.6)
    assert (exc.value.code, exc.value.status) == ("rcon_timeout", 504)


def test_closed_connection_is_a_failure(wr, agent_settings):
    t = wr(mode="close")
    with pytest.raises(AgentError) as exc:
        t.driver.run(agent_settings, FakeContainer(), "status", 5)
    assert exc.value.code == "rcon_failed"


def test_nothing_listening(agent_settings):
    driver = drivers.for_profile(rust.PROFILE)
    driver.port = 9
    with pytest.raises(AgentError) as exc:
        driver.run(agent_settings, FakeContainer(), "status", 3)
    assert exc.value.code == "rcon_unavailable"
    assert PASSWORD not in str(exc.value)


@pytest.mark.parametrize("command", ["", "a\nb", "x" * 1025])
def test_command_lines_are_checked(wr, agent_settings, command):
    t = wr()
    with pytest.raises(AgentError):
        t.driver.run(agent_settings, FakeContainer(), command, 5)
    assert t.fake.received == []


def test_stop_saves_then_signals(wr, agent_settings):
    t = wr()
    c = FakeContainer()
    result = StopResult()
    assert t.driver.stop(agent_settings, c, 180, result) is True
    assert t.fake.received[-1]["Message"] == "server.save"
    assert result.flushed and c.stopped_with == 180


def test_stop_without_webrcon_is_a_warning(agent_settings):
    driver = drivers.for_profile(rust.PROFILE)
    driver.port = 9
    c = FakeContainer()
    result = StopResult()
    driver.stop(agent_settings, c, 180, result)
    assert not result.flushed and c.stopped_with == 180 and "not saved" in result.warnings[0]


def test_saving_channel(wr, agent_settings, monkeypatch):
    t = wr()
    c = FakeContainer()
    monkeypatch.setattr(docker_ops, "running_container", lambda iid: c)
    t.driver.save(t.driver.channel(agent_settings, IID), lambda i, m: None)
    assert t.fake.received[-1]["Message"] == "server.save"


def test_status_query_is_a2s_on_the_query_port(agent_settings):
    driver = drivers.for_profile(rust.PROFILE)
    c = FakeContainer(env={"RUST_RCON_PASSWORD": PASSWORD, "RUST_SERVER_QUERYPORT": "28101"})
    target = driver.query_endpoint(agent_settings, c)
    assert tuple(target) == ("127.0.0.1", 28101) and callable(target.query)
