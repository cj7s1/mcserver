"""Agent HTTP API: bearer-token auth on every route, plus confined file access over HTTP."""

from __future__ import annotations

import os
import uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from .conftest import TOKEN

IID = str(uuid.uuid4())
SPEC = {
    "name": "s",
    "image": "itzg/minecraft-server:java21",
    "memory_limit_mb": 2048,
    "rcon_password": "s3cret-rcon-password",
    "ports": [{"container_port": 25565, "host_port": 25565}],
}

PROTECTED = [
    ("get", "/v1/info", None),
    ("get", "/v1/ports", None),
    ("get", "/v1/instances", None),
    ("get", "/v1/operations", None),
    ("get", f"/v1/operations/{'0' * 32}", None),
    ("post", f"/v1/instances/{IID}/prepare", {"files": {}}),
    ("put", f"/v1/instances/{IID}/container", {"spec": SPEC, "start": True}),
    ("delete", f"/v1/instances/{IID}/container", None),
    ("get", f"/v1/instances/{IID}/status", None),
    ("post", f"/v1/instances/{IID}/start", None),
    ("post", f"/v1/instances/{IID}/stop", {}),
    ("post", f"/v1/instances/{IID}/kill", None),
    ("post", f"/v1/instances/{IID}/command", {"command": "op attacker"}),
    ("get", f"/v1/instances/{IID}/logs", None),
    ("get", f"/v1/instances/{IID}/files/list?path=", None),
    ("get", f"/v1/instances/{IID}/files/text?path=server.properties", None),
    ("put", f"/v1/instances/{IID}/files/text", {"path": "x.txt", "content": "x"}),
    ("get", f"/v1/instances/{IID}/files/download?path=x", None),
    ("post", f"/v1/instances/{IID}/files/delete", {"paths": ["world"], "recursive": True}),
    (
        "post",
        f"/v1/instances/{IID}/files/fetch",
        {"url": "https://cdn.modrinth.com/x.jar", "path": "x.jar", "sha1": "0" * 40},
    ),
    ("get", f"/v1/instances/{IID}/backups", None),
    ("post", f"/v1/instances/{IID}/backups", {"backup_id": "b1"}),
    ("post", f"/v1/instances/{IID}/backups/restore", {"snapshot_id": "a1b2c3d4"}),
    ("delete", f"/v1/instances/{IID}/data", {"confirm": IID}),
]


def call(api, method, url, body, headers=None):
    if method in ("get",):
        return api.get(url, headers=headers or {})
    return api.request(method.upper(), url, json=body, headers=headers or {})


@pytest.mark.parametrize(("method", "url", "body"), PROTECTED)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": TOKEN},
        {"Authorization": f"Token {TOKEN}"},
        {"Authorization": f"bearer {TOKEN}"},
    ],
    ids=["none", "wrong", "suffix", "no-scheme", "token-scheme", "lowercase-scheme"],
)
def test_requests_without_valid_token_are_rejected(
    api, no_docker, agent_settings, method, url, body, headers
):
    resp = call(api, method, url, body, headers)
    assert resp.status_code == 401
    assert "WWW-Authenticate" not in resp.headers or resp.headers["WWW-Authenticate"]
    # nothing was created on disk by the rejected request
    assert not any(agent_settings.instances_dir.iterdir())


def test_health_is_public_and_reveals_nothing(api, monkeypatch):
    from mcagent import docker_ops

    class Up:
        def ping(self):
            return True

    monkeypatch.setattr(docker_ops, "client", lambda: Up())
    resp = api.get("/v1/health")
    assert resp.status_code == 200
    assert set(resp.json()) == {"status", "docker", "version"}
    assert resp.json()["status"] == "ok"

    class Down:
        def ping(self):
            raise RuntimeError("no socket")

    monkeypatch.setattr(docker_ops, "client", lambda: Down())
    assert api.get("/v1/health").json() == {
        "status": "degraded",
        "docker": False,
        "version": resp.json()["version"],
    }


def test_unconfigured_token_rejects_everything(api, agent_settings, no_docker):
    agent_settings.token = ""
    resp = api.get("/v1/operations", headers={"Authorization": "Bearer "})
    assert resp.status_code == 503
    assert api.get("/v1/operations", headers={"Authorization": "Bearer anything"}).status_code == 503


def test_token_file(api, agent_settings, tmp_path):
    secret = tmp_path / "token"
    secret.write_text("file-token-value\n")
    agent_settings.token_file = secret
    assert api.get("/v1/operations", headers={"Authorization": "Bearer file-token-value"}).status_code == 200
    assert api.get("/v1/operations", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 401


def test_valid_token_is_accepted(api, auth, no_docker):
    resp = api.get("/v1/operations", headers=auth)
    assert resp.status_code == 200
    assert isinstance(resp.json()["operations"], list)
    assert api.get(f"/v1/operations/{'0' * 32}", headers=auth).status_code == 404


def test_websocket_log_stream_requires_token(api, no_docker):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        with pytest.raises(WebSocketDisconnect) as exc:
            with api.websocket_connect(f"/v1/instances/{IID}/logs/stream", headers=headers) as ws:
                ws.receive_json()
        assert exc.value.code == 4401


# --- authenticated behaviour that needs no Docker -------------------------------------------------------
def test_prepare_and_confined_file_access(api, auth, agent_settings, no_docker, tmp_path):
    resp = api.post(
        f"/v1/instances/{IID}/prepare",
        json={"files": {"server.properties": "motd=hi\n", "config/a.toml": "x=1"}},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"created": True, "written": ["server.properties", "config/a.toml"]}
    inst_dir = agent_settings.instances_dir / IID
    assert (inst_dir / "server.properties").read_text() == "motd=hi\n"
    # prepare never overwrites existing files
    again = api.post(
        f"/v1/instances/{IID}/prepare", json={"files": {"server.properties": "motd=evil\n"}}, headers=auth
    )
    assert again.json()["written"] == []
    assert (inst_dir / "server.properties").read_text() == "motd=hi\n"

    assert (
        api.put(
            f"/v1/instances/{IID}/files/text", json={"path": "ops.json", "content": "[]"}, headers=auth
        ).status_code
        == 200
    )
    text = api.get(f"/v1/instances/{IID}/files/text", params={"path": "ops.json"}, headers=auth).json()
    assert text["content"] == "[]"
    listing = api.get(f"/v1/instances/{IID}/files/list", params={"path": ""}, headers=auth).json()
    assert [e["name"] for e in listing["entries"]] == ["config", "ops.json", "server.properties"]

    outside = tmp_path / "outside.txt"
    outside.write_text("host secret")
    for bad in ("../outside.txt", "../../outside.txt", "config/../../x"):
        r = api.get(f"/v1/instances/{IID}/files/text", params={"path": bad}, headers=auth)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_path"
    os.symlink(outside, inst_dir / "link.txt")
    r = api.get(f"/v1/instances/{IID}/files/text", params={"path": "link.txt"}, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "symlink"
    r = api.put(
        f"/v1/instances/{IID}/files/text", json={"path": "../escape.txt", "content": "x"}, headers=auth
    )
    assert r.status_code == 400
    assert not (agent_settings.instances_dir / "escape.txt").exists()


def test_prepare_rejects_traversal(api, auth, agent_settings, no_docker):
    r = api.post(f"/v1/instances/{IID}/prepare", json={"files": {"../../evil.sh": "#!/bin/sh"}}, headers=auth)
    assert r.status_code == 400
    assert not (agent_settings.data_root / "evil.sh").exists()


def test_apply_rejects_policy_violations_before_touching_docker(api, auth, no_docker):
    bad_image = {**SPEC, "image": "alpine:3"}
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": bad_image}, headers=auth)
    assert r.status_code == 403 and r.json()["error"]["code"] == "image_not_allowed"
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": {**SPEC, "network": "host"}}, headers=auth)
    assert r.status_code == 403 and r.json()["error"]["code"] == "network_not_allowed"
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": {**SPEC, "env": {"UID": "0"}}}, headers=auth)
    assert r.status_code == 422
    r = api.put(
        f"/v1/instances/{IID}/container", json={"spec": {**SPEC, "memory_limit_mb": 10**6}}, headers=auth
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "memory_limit"


def test_delete_data_requires_matching_confirmation(api, auth, agent_settings, no_docker):
    (agent_settings.instances_dir / IID).mkdir()
    r = api.request("DELETE", f"/v1/instances/{IID}/data", json={"confirm": str(uuid.uuid4())}, headers=auth)
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirmation_mismatch"
    assert (agent_settings.instances_dir / IID).exists()


def test_fetch_rejects_unlisted_hosts(api, auth, agent_settings, no_docker):
    (agent_settings.instances_dir / IID).mkdir()
    r = api.post(
        f"/v1/instances/{IID}/files/fetch",
        json={"url": "https://evil.example.com/mod.jar", "path": "mods/mod.jar", "sha1": "0" * 40},
        headers=auth,
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "url_not_allowed"


def test_invalid_instance_id_is_422(api, auth):
    assert api.get("/v1/instances/not-a-uuid/status", headers=auth).status_code == 422


def test_every_http_route_requires_the_token():
    """Guards routes added later: only the health probe and the schema are public."""
    from fastapi.routing import APIRoute

    from mcagent.main import app, require_token

    public = {"/v1/health"}
    unguarded = [
        (sorted(route.methods), route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        and route.path not in public
        and require_token not in [dep.call for dep in route.dependant.dependencies]
    ]
    assert unguarded == []
