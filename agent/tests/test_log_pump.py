"""The console log pump never dies silently (audit agent-docker#6): a Docker error is reported and
retried, and when the pump ends the WebSocket is closed so the panel's relay connects again."""

from __future__ import annotations

import uuid

import docker.errors
import pytest
from starlette.websockets import WebSocketDisconnect

from mcagent import docker_ops, main


def _messages(ws, n: int) -> list[dict]:
    return [ws.receive_json() for _ in range(n)]


def test_a_docker_error_is_reported_and_the_pump_keeps_going(api, auth, monkeypatch):
    answers = iter([docker.errors.APIError("500 Server Error: daemon busy")])

    def get_container(_iid):
        for exc in answers:
            raise exc
        return None  # then: no container (a stopped server)

    monkeypatch.setattr(docker_ops, "get_container", get_container)
    monkeypatch.setattr(docker_ops, "previous_run_lines", lambda *_a: [])
    monkeypatch.setattr(main, "LOG_RETRY_SECONDS", 0.01)
    with api.websocket_connect(f"/v1/instances/{uuid.uuid4()}/logs/stream", headers=auth) as ws:
        first, second = _messages(ws, 2)
    assert first["type"] == "error" and "daemon busy" in first["message"]
    assert second == {"type": "state", "state": "missing"}


class _Vanishing:
    """A container whose inspect fails with a Docker error other than 404 (a daemon hiccup)."""

    status = "running"
    id = "c1"

    def reload(self):
        raise docker.errors.APIError("502 Bad Gateway")


def test_an_inspect_error_does_not_kill_the_pump(api, auth, monkeypatch):
    calls = {"n": 0}

    def get_container(_iid):
        calls["n"] += 1
        return _Vanishing() if calls["n"] == 1 else None

    monkeypatch.setattr(docker_ops, "get_container", get_container)
    monkeypatch.setattr(docker_ops, "previous_run_lines", lambda *_a: [])
    monkeypatch.setattr(main, "LOG_RETRY_SECONDS", 0.01)
    with api.websocket_connect(f"/v1/instances/{uuid.uuid4()}/logs/stream", headers=auth) as ws:
        first, second = _messages(ws, 2)
    assert first["type"] == "error" and "502" in first["message"]
    assert second == {"type": "state", "state": "missing"}


def test_the_socket_is_closed_when_the_pump_ends(api, auth, monkeypatch):
    def broken(*_a):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(docker_ops, "get_container", lambda _iid: None)
    monkeypatch.setattr(docker_ops, "previous_run_lines", broken)
    monkeypatch.setattr(main, "LOG_PING_SECONDS", 0.2)
    with api.websocket_connect(f"/v1/instances/{uuid.uuid4()}/logs/stream", headers=auth) as ws:
        seen = _messages(ws, 3)
        assert [m["type"] for m in seen] == ["state", "error", "closed"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()  # not a ping: the relay sees the end and connects again
