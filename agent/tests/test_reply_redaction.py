"""B-10 of the security review (2026-11-11): console replies over RCON, WebRCON and TShock REST are redacted
like the console's, the live log redacts a line before it cuts it, and the spec's RCON password has a
character pattern."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from mcagent import docker_ops, drivers, main
from mcagent.schemas import InstanceSpec

IID = uuid.UUID("5b1d3c2a-7e6f-4a8b-9c0d-1e2f3a4b5c6d")
SECRET = "Rcon-Secret-0123456789abcdef"


class Running:
    def __init__(self, game: str, env: dict[str, str]):
        self.id = "c" * 64
        self.name = f"mcpanel-{game}"
        self.status = "running"
        self.labels = {"io.mcpanel.game": game, "io.mcpanel.instance": str(IID), "io.mcpanel.managed": "true"}
        self.attrs = {"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}, "State": {"Running": True}}


@pytest.mark.parametrize(
    ("game", "env"), [("rust", "RUST_RCON_PASSWORD"), ("project-zomboid", "RCON_PASSWORD")]
)
def test_a_command_reply_that_echoes_a_secret_is_redacted(api, auth, monkeypatch, game, env):
    c = Running(game, {env: SECRET})
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)
    driver_cls = type(drivers.for_container(c))

    async def reply(self, settings, c, text, timeout):
        return f"the password is {SECRET}\nok"

    monkeypatch.setattr(driver_cls, "command", reply)
    r = api.post(f"/v1/instances/{IID}/command", json={"command": "status"}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["output"] == "the password is ********\nok"


def test_a_minecraft_reply_is_left_as_it_is(api, auth, monkeypatch):
    c = Running("x", {})
    c.labels = {"io.mcpanel.instance": str(IID), "io.mcpanel.managed": "true"}  # Minecraft: no game label
    monkeypatch.setattr(docker_ops, "running_container", lambda _iid: c)

    async def reply(self, settings, c, text, timeout):
        return "There are 0 of a max of 20 players online"

    monkeypatch.setattr(type(drivers.for_container(c)), "command", reply)
    r = api.post(f"/v1/instances/{IID}/command", json={"command": "list"}, headers=auth)
    assert r.status_code == 200 and r.json()["output"] == "There are 0 of a max of 20 players online"


def test_a_secret_across_the_cut_of_a_long_line_is_hidden_whole():
    redact = lambda text: text.replace(SECRET, docker_ops.REDACTED)  # noqa: E731
    splitter = main._LineSplitter(main.LOG_LINE_MAX_BYTES + main.LOG_REDACT_MARGIN)
    start = main.LOG_LINE_MAX_BYTES - 10  # the secret starts 10 bytes before the cut
    line = "x" * start + SECRET + "y" * 40_000 + "\n"
    (out,) = splitter.feed(line.encode())
    shown = main._redact_line(out, redact)
    assert SECRET[:10] not in shown and shown.endswith(main.LOG_TRUNCATED_SUFFIX)
    assert len(shown.encode()) <= main.LOG_LINE_MAX_BYTES + len(main.LOG_TRUNCATED_SUFFIX)


def test_short_lines_and_no_redactor_are_unchanged():
    assert main._redact_line("plain", None) == "plain"
    assert main._redact_line("a " + SECRET, lambda t: t.replace(SECRET, "********")) == "a ********"


@pytest.mark.parametrize("password", ["has blank 12345", "quote'12345678", "semi;12345678", "x" * 7])
def test_the_rcon_password_has_a_character_pattern(password):
    with pytest.raises(ValidationError):
        InstanceSpec(
            name="t", image="itzg/minecraft-server:java21", memory_limit_mb=1024, rcon_password=password
        )


def test_the_panels_passwords_are_accepted():
    for password in ("A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6", "s3cret-rcon-password", "under_score_ok"):
        InstanceSpec(
            name="t", image="itzg/minecraft-server:java21", memory_limit_mb=1024, rcon_password=password
        )
