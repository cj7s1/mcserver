"""Satisfactory on the panel: the definition (catalog, spec, ports that share a number, the messaging
port 1:1), creating a server (the explicit join choice), the claim flow as setup steps with every
failure point (no port is published before the claim, the join password and the game are done), the
launch settings and the server options through the API, saves through the API (new game, import,
export), the player count, and what the shared pieces got for it (control secret value, setup that
waits for a running server, the start job resuming setup, secrets hook, virtual configs).

The agent side (the driver against a fake HTTPS server) is agent/tests/test_games_satisfactory.py.
"""

from __future__ import annotations

import io
from typing import Any

import pytest

from apps import games
from apps.games.satisfactory import IMAGE_TAG, TOKEN_FILE, SatisfactoryDefinition, server_name_for, session_name_for
from apps.instances.models import Instance
from apps.instances.spec import build_spec, rcon_password
from apps.jobs import runner
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient, AgentError

IMAGE = f"wolveix/satisfactory-server:{IMAGE_TAG}"
TOKEN = "eyJwbCI6IkFQSVRva2VuIn0.0123456789abcdef"


def err(resp: Any) -> str:
    return resp.json()["error"]["code"]


def run(kind: str, inst: Instance, capture: Any, **params: Any) -> Job:
    with capture(execute=True):
        job = runner.enqueue(kind, title=kind, instance=inst, params=params)
    job.refresh_from_db()
    return job


class FakeGame:
    """``AgentClient.game_action`` of an agent with the satisfactory-api driver, in memory."""

    def __init__(self, fake_agent: Any) -> None:
        self.agent = fake_agent
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.claimed = False
        self.admin_password: str | None = None
        self.client_password = ""
        self.sessions: dict[str, list[str]] = {}
        self.current: str | None = None
        self.options = {
            "FG.AutosaveInterval": "300.0",
            "FG.DSAutoPause": "True",
            "FG.DSAutoSaveOnDisconnect": "True",
            "FG.NetworkQuality": "1",
            "FG.SendGameplayData": "False",
        }
        self.fail: dict[str, Exception] = {}
        self.ports_seen_at_claim: list[Any] = []
        self.saved_at: dict[str, str] = {}  # save name -> the game's saveDateTime
        self.lost_claim = False

    def __call__(
        self, _client: Any, instance_id: Any, action: str, params: dict[str, Any] | None = None, timeout: float = 60
    ) -> dict[str, Any]:
        params = dict(params or {})
        self.calls.append((action, params))
        c = self.agent.containers.get(str(instance_id))
        if c is None or not c["running"]:
            raise AgentError("The server is not running", "not_running", 409)
        if action in self.fail:
            raise self.fail[action]
        if action == "claim":
            self.ports_seen_at_claim.append(list(c["ports"]))
            existing = self.claimed
            if existing and params["admin_password"] != self.admin_password:
                raise AgentError("claimed with another admin password", "claimed_elsewhere", 409)
            self.claimed, self.admin_password = True, params["admin_password"]
            return {"token": TOKEN, "claimed": "existing" if existing else "new"}
        if action == "verify_claim":
            if c["ports"]:
                raise AgentError("only unpublished", "claim_unverified", 409)
            # (a server set up before the test, _claimed_instance, counts as claimed with the panel's password)
            if self.lost_claim or (self.admin_password is not None and params.get("admin_password") != self.admin_password):
                raise AgentError("not claimed by this panel", "server_unclaimed", 409)
            self.agent.claim_markers[str(instance_id)] = True
            return {"claim_verified": True}
        if action == "status":
            return {
                "api": "healthy",
                "state": {
                    "activeSessionName": self.current or "SERVER-SESSION528984508",
                    "isGameRunning": bool(self.current),
                },
            }
        if action == "load_game":
            for session, saves in self.sessions.items():
                if params["save_name"] in saves:
                    self.current = session
                    return {"session": session, "state": {"activeSessionName": session}}
            raise AgentError("no such save", "api_error", 502)
        if action == "client_password":
            self.client_password = params["password"]
            return {"protected": bool(params["password"])}
        if action == "sessions":
            return {
                "sessions": [
                    {"name": n, "saves": [{"name": s, "session": n, "saved_at": self.saved_at.get(s, "")} for s in saves]}
                    for n, saves in self.sessions.items()
                ],
                "current": self.current,
                "game_loaded": bool(self.current),
            }
        if action == "new_game":
            name = params["session_name"]
            self.sessions.setdefault(name, []).insert(0, f"{name.replace(' ', '_')}_autosave_0")
            self.current = name
            self.agent.put_file(instance_id, f"saved/server/{name.replace(' ', '_')}_autosave_0.sav", b"SAVE")
            return {"session": name, "state": {"activeSessionName": name}}
        if action == "save":
            if not self.current:
                return {"save_name": None}
            name = f"{self.current.replace(' ', '_')}_mcpanel"
            self.agent.put_file(instance_id, f"saved/server/{name}.sav", b"SAVED-" + self.current.encode())
            if name not in self.sessions[self.current]:
                self.sessions[self.current].insert(0, name)
            return {"save_name": name}
        if action == "upload_save":
            data = self.agent.get_file(instance_id, params["path"])
            self.agent.put_file(instance_id, f"saved/server/{params['save_name']}.sav", data)
            self.sessions.setdefault("imported", []).insert(0, params["save_name"])
            self.current = "imported"
            return {"save_name": params["save_name"], "loaded": True, "session": "imported", "state": {}}
        if action == "options":
            return {"options": dict(self.options), "pending": {}}
        if action == "apply_options":
            self.options.update(params["options"])
            return {"options": dict(self.options), "pending": {}}
        raise AgentError(f"no operation {action}", "action_unsupported", 409)

    def actions(self) -> list[str]:
        return [a for a, _p in self.calls]


@pytest.fixture
def game(fake_agent: Any, monkeypatch: pytest.MonkeyPatch) -> FakeGame:
    fake = FakeGame(fake_agent)
    monkeypatch.setattr(AgentClient, "game_action", lambda client, *a, **k: fake(client, *a, **k))
    return fake


def body(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "game": "satisfactory",
        "name": "Ficsit Works",
        "options": {"access": "password"},
        "secrets": {"client_password": "join-me"},
        "start": True,
        "nightly_backup": False,
    }
    data.update(over)
    return data


@pytest.fixture
def created(clients: Any, fake_agent: Any, game: FakeGame, django_capture_on_commit_callbacks: Any) -> Instance:
    """A server created through the API, installed and set up (the install enqueues the setup job)."""
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body(), format="json")
    assert resp.status_code == 201, resp.json()
    return Instance.objects.get(pk=resp.json()["instance"]["id"])


# --- the definition ----------------------------------------------------------------------------------
def test_definition_and_catalog_entry(clients):
    definition = games.get("satisfactory")
    assert isinstance(definition, SatisfactoryDefinition)
    entry = clients["owner"].get("/api/catalog/games").json()
    sf = next(g for g in (entry if isinstance(entry, list) else entry["games"]) if g["id"] == "satisfactory")
    assert sf["memory_model"] == "plain" and sf["default_memory_mb"] == 12288 and sf["min_memory_mb"] == 8192
    assert set(sf["capabilities"]) == {
        "players",
        "query",
        "consistent_backups",
        "world_reset",
        "world_import_export",
        "config_files",
    }
    assert "software_update" not in sf["capabilities"] and "console_commands" not in sf["capabilities"]
    assert sf["fixed_version"]["id"] == "stable" and "has_versions" not in sf
    assert [(p["name"], p["protocol"], p["container_port"], p.get("same_port_as")) for p in sf["ports"]] == [
        ("game", "udp", 7777, None),
        ("api", "tcp", 7777, "game"),
        ("messaging", "tcp", 8888, None),
    ]
    access = sf["options"][0]
    assert access["key"] == "access" and access["default"] is None and access["options"] == ["password", "open"]
    assert sf["secrets"][0]["required_when"] == {"option": "access", "value": "password"}
    assert [s["id"] for s in sf["setup_steps"]] == ["claim", "access", "new_game", "publish"]
    assert sf["world"]["extension"] == ".sav" and sf["world"]["via_api"] is True
    assert [f["id"] for f in sf["config_files"]] == ["launch", "server-options"]


def test_names_for_the_claim_and_sessions():
    inst = Instance(name="Ficsit / Works #1 <b>")
    assert server_name_for(inst) == "Ficsit Works 1 b"
    assert server_name_for(Instance(name="!!!")) == "MCPanel server"
    assert session_name_for(inst).startswith("Ficsit Works 1 b ")


# --- create ---------------------------------------------------------------------------------------------
def test_create_requires_an_explicit_join_choice(clients, fake_agent):
    post = lambda **kw: clients["owner"].post("/api/instances/", body(**kw), format="json")  # noqa: E731
    resp = post(options={})
    assert resp.status_code == 400 and "options" in resp.json()["error"]["details"]["fields"]
    resp = post(options={"access": "anyone"})
    assert resp.status_code == 400
    resp = post(secrets={})
    assert resp.status_code == 400 and "secrets.client_password" in resp.json()["error"]["details"]["fields"]
    assert post(memory_mb=4096).status_code == 400  # Satisfactory needs at least 8 GB
    assert post(env={"STEAMBETA": "true"}).status_code == 400
    assert not Instance.objects.filter(game="satisfactory").exists()


def test_create_and_install_starts_without_ports(clients, fake_agent, game, django_capture_on_commit_callbacks, monkeypatch):
    # the setup job is checked on its own below: here only the install
    monkeypatch.setattr("apps.instances.game_handlers.enqueue_setup", lambda *a, **k: None)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            "/api/instances/",
            body(options={"access": "open"}, secrets={}, settings={"launch": {"max_players": 8}}),
            format="json",
        )
    assert resp.status_code == 201, resp.json()
    data = resp.json()["instance"]
    inst = Instance.objects.get(pk=data["id"])
    assert inst.minecraft_version == "stable" and inst.image == IMAGE and inst.memory_mb == 12288
    assert inst.restart_policy == "no" and inst.stop_timeout == 120
    assert inst.game_options["access"] == "open" and inst.game_options["launch"]["max_players"] == 8
    assert [(p["name"], p["protocol"], p["host_port"]) for p in inst.ports] == [
        ("game", "udp", 7777),
        ("api", "tcp", 7777),
        ("messaging", "tcp", 8888),
    ]
    spec = fake_agent.containers[str(inst.id)]["spec"]
    assert spec["game"] == "satisfactory" and spec["ports"] == []  # nothing published before the claim
    assert spec["env"]["MAXPLAYERS"] == "8" and spec["env"]["SERVERMESSAGINGPORT"] == "8888"
    assert spec["env"]["DISABLESEASONALEVENTS"] == "false" and spec["restart_policy"] == "no"
    assert data["setup"]["needed"] is True and data["setup"]["next"] == "claim" and data["setup"]["ports_open"] is False
    assert fake_agent.get_file(inst.id, TOKEN_FILE) is None  # no token before the claim


def test_a_password_given_with_open_is_a_password(clients, fake_agent, monkeypatch):
    monkeypatch.setattr("apps.instances.game_handlers.enqueue_setup", lambda *a, **k: None)
    resp = clients["owner"].post("/api/instances/", body(options={"access": "open"}, start=False), format="json")
    assert resp.status_code == 201
    assert Instance.objects.get(pk=resp.json()["instance"]["id"]).game_options["access"] == "password"


def test_game_and_api_share_one_free_port_number(clients, fake_agent, monkeypatch):
    monkeypatch.setattr("apps.instances.game_handlers.enqueue_setup", lambda *a, **k: None)
    fake_agent.add_foreign_port(7777, "web", "tcp")  # only TCP 7777 is taken
    fake_agent.add_foreign_port(8888, "searx", "tcp")
    resp = clients["owner"].post("/api/instances/", body(start=False), format="json")
    assert resp.status_code == 201, resp.json()
    ports = {p["name"]: p["host_port"] for p in Instance.objects.get(pk=resp.json()["instance"]["id"]).ports}
    assert ports["game"] == ports["api"] == 7778 and ports["messaging"] == 8889
    resp = clients["owner"].post("/api/instances/", body(name="B", start=False, ports={"game": 7777}), format="json")
    assert resp.status_code == 409 and err(resp) == "port_conflict"


def test_messaging_port_is_published_one_to_one(make_instance):
    inst = make_instance(
        name="S",
        game="satisfactory",
        server_type="",
        minecraft_version="stable",
        image=IMAGE,
        memory_mb=12288,
        jvm_flags_preset="none",
        game_port=29010,
        ports=[
            {"name": "game", "container_port": 7777, "protocol": "udp", "host_port": 29010},
            {"name": "api", "container_port": 7777, "protocol": "tcp", "host_port": 29010},
            {"name": "messaging", "container_port": 8888, "protocol": "tcp", "host_port": 29011},
        ],
        game_options={"access": "open"},
        setup_state={"ports_open": True},
        restart_policy="unless-stopped",
    )
    spec = build_spec(inst)
    assert [(p["container_port"], p["host_port"], p["protocol"]) for p in spec["ports"]] == [
        (7777, 29010, "udp"),
        (7777, 29010, "tcp"),
        (29011, 29011, "tcp"),
    ]
    assert spec["env"]["SERVERMESSAGINGPORT"] == "29011"
    # only the agent starts it, through its start wrapper (review of 2026-11-11): never Docker by itself
    assert spec["restart_policy"] == "no"
    inst.restart_policy = "on-failure"
    assert build_spec(inst)["restart_policy"] == "no"


# --- the claim flow --------------------------------------------------------------------------------------------
def test_install_runs_the_whole_setup(created, fake_agent, game):
    inst = created
    inst.refresh_from_db()
    setup = Job.objects.get(instance=inst, kind="setup")
    assert setup.status == JobStatus.SUCCEEDED, setup.error
    # new_game asks whether a game is loaded first; publish verifies the claim on the unpublished container
    assert game.actions() == ["claim", "client_password", "status", "sessions", "new_game", "verify_claim"]
    claim = game.calls[0][1]
    assert claim == {"server_name": "Ficsit Works", "admin_password": rcon_password(inst)}
    assert game.ports_seen_at_claim == [[]]  # the container had no published port while unclaimed
    assert game.client_password == "join-me"
    # the token: encrypted in the database, written for the agent, never in the job log or audit
    assert inst.secret_values["api_token"] == TOKEN
    assert fake_agent.text(inst.id, TOKEN_FILE) == TOKEN + "\n"
    assert TOKEN not in str(setup.log) and TOKEN not in str(setup.result)
    from apps.audit.models import AuditEvent

    assert not any(TOKEN in str(e.details) or "join-me" in str(e.details) for e in AuditEvent.objects.all())
    # published only at the end, by recreating the container
    assert inst.setup_state["ports_open"] is True
    spec = fake_agent.containers[str(inst.id)]["spec"]
    assert {(p["host_port"], p["protocol"]) for p in spec["ports"]} == {(7777, "udp"), (7777, "tcp"), (8888, "tcp")}
    assert all(s["status"] == "done" for s in inst.setup_state["steps"].values())
    assert inst.game_options["session_name"].startswith("Ficsit Works ")
    assert games.for_instance(inst).setup_status(inst)["needed"] is False


def test_claim_failure_leaves_the_server_unpublished(clients, fake_agent, game, django_capture_on_commit_callbacks):
    game.fail["claim"] = AgentError("PasswordlessLogin: boom (internal)", "api_error", 502)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body(), format="json")
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    setup = Job.objects.get(instance=inst, kind="setup")
    assert setup.status == JobStatus.FAILED and "boom" in str(setup.error)
    inst.refresh_from_db()
    assert inst.setup_state["steps"]["claim"]["status"] == "failed"
    assert not inst.setup_state.get("ports_open") and fake_agent.containers[str(inst.id)]["spec"]["ports"] == []
    assert "api_token" not in inst.secret_values and fake_agent.get_file(inst.id, TOKEN_FILE) is None
    # run it again once the server is fine
    del game.fail["claim"]
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True


@pytest.mark.parametrize(
    ("action", "step"),
    [("client_password", "access"), ("sessions", "new_game"), ("new_game", "new_game")],
)
def test_every_later_failure_point_keeps_the_ports_closed(
    clients, fake_agent, game, django_capture_on_commit_callbacks, action, step
):
    game.fail[action] = AgentError(f"{action} failed", "api_error", 502)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body(), format="json")
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    inst.refresh_from_db()
    assert inst.setup_state["steps"][step]["status"] == "failed"
    assert inst.setup_state["steps"]["claim"]["status"] == "done"
    assert not inst.setup_state.get("ports_open") and fake_agent.containers[str(inst.id)]["spec"]["ports"] == []


def test_publish_failure_is_retried(clients, fake_agent, game, django_capture_on_commit_callbacks, monkeypatch):
    real_apply = fake_agent.apply
    refused: list[int] = []

    def apply(instance_id, spec, **kw):
        if spec["ports"] and not refused:  # the agent's publish guard said no once
            refused.append(1)
            raise AgentError("not claimed", "server_unclaimed", 409)
        return real_apply(instance_id, spec, **kw)

    monkeypatch.setattr(fake_agent, "apply", apply)
    monkeypatch.setattr(AgentClient, "apply", fake_agent._proxy("apply"))
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body(), format="json")
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    inst.refresh_from_db()
    assert inst.setup_state["steps"]["publish"]["status"] == "failed" and refused
    assert fake_agent.containers[str(inst.id)]["running"]  # started again from the old container
    assert fake_agent.containers[str(inst.id)]["spec"]["ports"] == []
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert len(fake_agent.containers[str(inst.id)]["spec"]["ports"]) == 3


def test_access_without_the_password_fails_clearly(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent, access="password", steps=("claim",))
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.FAILED and "join password" in str(job.error)
    inst.refresh_from_db()
    assert inst.setup_state["steps"]["access"]["status"] == "failed" and not inst.setup_state.get("ports_open")


def test_setup_waits_for_a_stopped_server_and_the_start_resumes_it(clients, fake_agent, game, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post("/api/instances/", body(start=False), format="json")
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    setup = Job.objects.get(instance=inst, kind="setup")
    assert setup.status == JobStatus.SUCCEEDED and setup.result["waiting"] == "claim"
    inst.refresh_from_db()
    assert inst.setup_state["steps"]["claim"] == {**inst.setup_state["steps"]["claim"], "status": "pending"}
    assert "not running" in inst.setup_state["steps"]["claim"]["message"]
    assert game.calls == []
    job = run("start", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True and game.actions()[0] == "claim"
    assert Job.objects.filter(instance=inst, kind="setup").count() == 2


def test_new_game_step_keeps_an_existing_game(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access"))
    game.sessions = {"Old world": ["Old_world_autosave_0"]}
    game.current = "Old world"
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert "new_game" not in game.actions()
    inst.refresh_from_db()
    assert (
        inst.game_options["session_name"] == "Old world"
        and "plays 'Old world'" in inst.setup_state["steps"]["new_game"]["message"]
    )


def test_publish_of_a_stopped_server_happens_at_its_next_start(
    make_instance, fake_agent, game, django_capture_on_commit_callbacks
):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game"), running=False)
    job = run("setup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.setup_state["ports_open"] is True
    assert len(build_spec(inst)["ports"]) == 3


def test_changing_the_join_password_reapplies_it(created, clients, game, django_capture_on_commit_callbacks):
    inst = created
    url = f"/api/instances/{inst.id}/secrets"
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].put(url, {"values": {"client_password": "new-pass"}}, format="json")
    assert resp.status_code == 200
    assert game.client_password == "new-pass"
    inst.refresh_from_db()
    assert inst.setup_state["steps"]["access"]["status"] == "done" and inst.game_options["access"] == "password"
    # clearing it opens the server: only with the explicit choice (review 2026-11-10, item 9)
    resp = clients["owner"].put(url, {"values": {"client_password": None}}, format="json")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "confirmation_required"
    inst.refresh_from_db()
    assert game.client_password == "new-pass" and inst.game_options["access"] == "password"
    assert inst.secret_values.get("client_password") == "new-pass"
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].put(url, {"values": {"client_password": None}, "access": "open"}, format="json")
    assert resp.status_code == 200
    inst.refresh_from_db()
    assert game.client_password == "" and inst.game_options["access"] == "open"
    # the API token is not a secret users see or set
    names = [s["key"] for s in clients["owner"].get(url).json()["secrets"]]
    assert names == ["client_password"]
    resp = clients["owner"].put(url, {"values": {"api_token": "x"}}, format="json")
    assert resp.status_code == 400


def _claimed_instance(make_instance, fake_agent, *, access="open", steps=(), running=True) -> Instance:
    from django.utils import timezone

    inst = make_instance(
        name="Claimed",
        game="satisfactory",
        server_type="",
        minecraft_version="stable",
        image=IMAGE,
        memory_mb=12288,
        jvm_flags_preset="none",
        game_port=7777,
        restart_policy="on-failure",
        ports=[
            {"name": "game", "container_port": 7777, "protocol": "udp", "host_port": 7777},
            {"name": "api", "container_port": 7777, "protocol": "tcp", "host_port": 7777},
            {"name": "messaging", "container_port": 8888, "protocol": "tcp", "host_port": 8888},
        ],
        game_options={"access": access, "launch": {"max_players": 4}},
        setup_state={"steps": {s: {"status": "done", "at": timezone.now().isoformat(), "message": ""} for s in steps}},
        rcon_password="a" * 32,
    )
    inst.set_secret_values({"api_token": TOKEN})
    inst.save(update_fields=["secrets"])
    fake_agent.apply(inst.id, build_spec(inst), start=running)
    if running:
        Instance.objects.filter(pk=inst.pk).update(status="running", installed_at=timezone.now())
        inst.refresh_from_db()
    return inst


# --- shared pieces -------------------------------------------------------------------------------------------
def test_control_secret_file_is_the_api_token(make_instance, fake_agent, clients):
    inst = _claimed_instance(make_instance, fake_agent)
    from apps.instances.handlers import write_control_secret

    write_control_secret(inst, AgentClient(inst.node))
    assert fake_agent.text(inst.id, TOKEN_FILE) == TOKEN + "\n"
    resp = clients["owner"].get(f"/api/instances/{inst.id}/files/text", {"path": TOKEN_FILE})
    assert resp.status_code == 200 and TOKEN not in resp.json()["content"] and resp.json()["redacted"] is True
    resp = clients["owner"].put(f"/api/instances/{inst.id}/files/text", {"path": TOKEN_FILE, "content": "x"}, format="json")
    assert resp.status_code == 403
    # before the claim nothing is written (not the admin password)
    other = make_instance(
        name="New", game="satisfactory", image=IMAGE, minecraft_version="stable", game_port=7790, server_type=""
    )
    write_control_secret(other, AgentClient(other.node))
    assert fake_agent.get_file(other.id, TOKEN_FILE) is None


def test_factorio_still_writes_its_rcon_password(make_instance, fake_agent):
    inst = make_instance(
        name="F",
        game="factorio",
        server_type="",
        minecraft_version="2.1.23",
        image="factoriotools/factorio:2.1.23-rootless",
        game_port=34197,
        rcon_password="r" * 32,
    )
    from apps.instances.handlers import write_control_secret

    fake_agent.prepare(inst.id)
    write_control_secret(inst, AgentClient(inst.node))
    assert fake_agent.text(inst.id, "config/rconpw") == "r" * 32 + "\n"


def test_patching_the_game_port_moves_the_api_port(make_instance, fake_agent, clients):
    inst = _claimed_instance(make_instance, fake_agent)
    url = f"/api/instances/{inst.id}"
    resp = clients["owner"].patch(url, {"ports": [{"name": "game", "host_port": 29010}]}, format="json")
    assert resp.status_code == 200, resp.json()
    inst.refresh_from_db()
    assert {p["name"]: p["host_port"] for p in inst.ports} == {"game": 29010, "api": 29010, "messaging": 8888}
    resp = clients["owner"].patch(url, {"ports": [{"name": "api", "host_port": 29020}]}, format="json")
    assert resp.status_code == 400 and err(resp) == "invalid_port"


def test_no_software_update(make_instance, fake_agent, clients):
    inst = _claimed_instance(make_instance, fake_agent)
    resp = clients["owner"].post(f"/api/instances/{inst.id}/software", {"version": "1.0"}, format="json")
    assert resp.status_code in (400, 409)
    d = games.for_instance(inst)
    assert d.describe_software(d.software_state(inst)) == "Satisfactory (stable branch)"


# --- config: launch settings and server options ---------------------------------------------------------------
def test_launch_settings_and_server_options(make_instance, fake_agent, game, clients):
    inst = _claimed_instance(make_instance, fake_agent)
    url = f"/api/instances/{inst.id}/config"
    files = {f["id"]: f for f in clients["owner"].get(url).json()["files"]}
    assert files["launch"]["values"]["max_players"] == 4 and files["launch"]["exists"] is True
    assert files["server-options"]["values"] == {
        "FG.AutosaveInterval": 300,
        "FG.DSAutoPause": True,
        "FG.DSAutoSaveOnDisconnect": True,
        "FG.NetworkQuality": "1",
        "FG.SendGameplayData": False,
    }
    hash_before = build_spec(inst)
    resp = clients["owner"].patch(f"{url}/launch", {"values": {"max_players": 12}}, format="json")
    assert resp.status_code == 200, resp.json()
    assert resp.json()["changed"] == ["max_players"] and resp.json()["restart_required"] is True
    inst.refresh_from_db()
    assert build_spec(inst)["env"]["MAXPLAYERS"] == "12" and build_spec(inst) != hash_before
    resp = clients["owner"].patch(
        f"{url}/server-options", {"values": {"FG.AutosaveInterval": 600, "FG.DSAutoPause": False}}, format="json"
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["restart_required"] is False
    assert game.calls[-1] == ("apply_options", {"options": {"FG.AutosaveInterval": "600.0", "FG.DSAutoPause": "False"}})
    assert resp.json()["values"]["FG.AutosaveInterval"] == 600
    assert clients["owner"].patch(f"{url}/launch", {"values": {"max_players": 0}}, format="json").status_code == 400
    assert clients["owner"].patch(f"{url}/launch", {"values": {"MAXPLAYERS": 3}}, format="json").status_code == 400


def test_server_options_are_left_out_while_stopped(make_instance, fake_agent, game, clients):
    inst = _claimed_instance(make_instance, fake_agent, running=False)
    files = [f["id"] for f in clients["owner"].get(f"/api/instances/{inst.id}/config").json()["files"]]
    assert files == ["launch"]
    resp = clients["owner"].patch(
        f"/api/instances/{inst.id}/config/server-options", {"values": {"FG.DSAutoPause": False}}, format="json"
    )
    assert resp.status_code == 409 and err(resp) == "not_running"


# --- saves ---------------------------------------------------------------------------------------------------
def test_saves_list_export_and_new_game(make_instance, fake_agent, game, clients, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent)
    game(None, inst.id, "new_game", {"session_name": "First"})
    Instance.objects.filter(pk=inst.pk).update(game_options={**inst.game_options, "session_name": "First"})
    data = clients["owner"].get(f"/api/instances/{inst.id}/saves").json()
    assert data["current"] == "First" and data["from_api"] is True
    assert [(s["name"], s["session"], s["current"]) for s in data["saves"]] == [("First_autosave_0", "First", True)]
    # export: SaveGame first, then that file
    resp = clients["owner"].get(f"/api/instances/{inst.id}/saves/export")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == b"SAVED-First"
    assert "First_mcpanel.sav" in resp["Content-Disposition"] and resp["Content-Type"] == "application/octet-stream"
    # a new game through the API, after a safety backup; the old session's saves stay
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{inst.id}/saves/reset",
            {"confirm_name": inst.name, "settings": {"session_name": "Second"}, "backup": True},
            format="json",
        )
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result["session"] == "Second" and job.result["backup_id"]
    assert fake_agent.called("create_backup") and game.calls[-1] == (
        "new_game",
        {"session_name": "Second", "admin_password": rcon_password(inst)},
    )
    inst.refresh_from_db()
    assert inst.game_options["session_name"] == "Second"
    assert fake_agent.get_file(inst.id, "saved/server/First_autosave_0.sav") == b"SAVE"
    bad = clients["owner"].post(
        f"/api/instances/{inst.id}/saves/reset", {"confirm_name": inst.name, "settings": {"map_size": 1}}, format="json"
    )
    assert bad.status_code == 400


def test_new_game_of_a_stopped_server_starts_and_stops_it(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent, running=False)
    job = run("saves_reset", inst, django_capture_on_commit_callbacks, settings={"session_name": "Fresh"}, backup=False)
    assert job.status == JobStatus.SUCCEEDED, job.error
    # no safety backup: the running game is saved first; an existing session's name is refused
    assert game.actions() == ["save", "sessions", "new_game"]
    assert not fake_agent.containers[str(inst.id)]["running"]
    inst.refresh_from_db()
    assert inst.desired_state == "stopped"


def test_import_uploads_through_the_api(make_instance, fake_agent, game, clients, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent)
    upload = io.BytesIO(b"\x01\x02SAVEDATA")
    upload.name = "My Factory.sav"
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{inst.id}/saves/import",
            {"file": upload, "confirm_name": inst.name, "backup": "false"},
            format="multipart",
        )
    assert resp.status_code == 202, resp.json()
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    action, params = game.calls[-1]
    assert action == "upload_save" and params["save_name"] == "My_Factory" and params["load"] is True
    assert fake_agent.get_file(inst.id, "saved/server/My_Factory.sav") == b"\x01\x02SAVEDATA"
    assert fake_agent.get_file(inst.id, params["path"]) is None  # the upload is removed afterwards
    inst.refresh_from_db()
    assert inst.game_options["session_name"] == "imported"
    wrong = io.BytesIO(b"x")
    wrong.name = "world.zip"
    resp = clients["owner"].post(
        f"/api/instances/{inst.id}/saves/import", {"file": wrong, "confirm_name": inst.name}, format="multipart"
    )
    assert resp.status_code == 400


def test_saves_need_the_claim(make_instance, fake_agent, game, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent)
    inst.set_secret_values({})
    inst.save(update_fields=["secrets"])
    job = run("saves_reset", inst, django_capture_on_commit_callbacks, settings={}, backup=False)
    assert job.status == JobStatus.FAILED and "setup" in str(job.error)


# --- players --------------------------------------------------------------------------------------------------
def test_players_are_a_count(make_instance, fake_agent, clients):
    inst = _claimed_instance(make_instance, fake_agent)
    fake_agent.ping_answer = {"online": 3, "max": 8, "sample": [], "motd": "Claimed", "version": "CL 517554"}
    data = clients["owner"].get(f"/api/instances/{inst.id}/players").json()
    assert data["count_only"] is True and data["online"] == {"online": 3, "max": 8, "players": []}
    from apps.metrics.tasks import player_count

    assert player_count(inst, AgentClient(inst.node)) == {"online": 3, "max": 8, "players": []}
    resp = clients["owner"].post(f"/api/instances/{inst.id}/players", {"action": "kick", "name": "x"}, format="json")
    assert resp.status_code == 409
