"""World reset: the agent, the panel and the web UI must agree on names, fields and error codes.

The three layers are separate packages, so each keeps its own copy of a few names ("keep in
sync with ..."). These tests read the other layers' sources and compare them with what the
panel sends, returns and simulates, so a change on one side fails here and not on a server.
They are skipped when the sources are not next to ``backend/`` (an installed panel image).
"""

from __future__ import annotations

import ast
import gzip
import inspect
import re
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

from apps.instances import world
from apps.instances.views import WorldResetSerializer
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient

from .conftest import FakeAgent

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent" / "mcagent"
FRONTEND = REPO / "frontend" / "src"

needs_agent = pytest.mark.skipif(not (AGENT / "world.py").is_file(), reason="agent sources are not next to backend/")
needs_frontend = pytest.mark.skipif(
    not (FRONTEND / "lib" / "types.ts").is_file(), reason="frontend sources are not next to backend/"
)


# --- reading the other layers' sources ----------------------------------------------------------------
def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _constant(module: ast.Module, name: str) -> Any:
    """The value of a module-level constant written as a literal (or ``frozenset(<literal>)``)."""
    for node in module.body:
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == name:
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
                return frozenset(ast.literal_eval(value.args[0]))
            return ast.literal_eval(value)
    raise AssertionError(f"{name} is not defined")


def _function(module: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} is not defined")


def _agent_error_codes(node: ast.AST) -> set[str]:
    """The codes of every ``AgentError(message, "code", status)`` raised inside ``node``."""
    codes = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "AgentError":
            code = call.args[1] if len(call.args) > 1 else None
            assert isinstance(code, ast.Constant) and isinstance(code.value, str), ast.dump(call)
            codes.add(code.value)
    return codes


def _class_fields(module: ast.Module, name: str) -> list[str]:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return [n.target.id for n in node.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
    raise AssertionError(f"class {name} is not defined")


def _ts_interface(path: Path, name: str) -> set[str]:
    """The property names of ``export interface <name> { ... }`` (first level only)."""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"export interface {name} \{{\n(.*?)\n\}}", text, re.DOTALL)
    assert match, f"interface {name} not found in {path.name}"
    return set(re.findall(r"^  (\w+)\??:", match.group(1), re.MULTILINE))


def _ts_string_list(path: Path, name: str) -> list[str]:
    """The strings of ``export const <name> ... = [ ... ]``."""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}\b[^=]*=\s*\[(.*?)\]", text, re.DOTALL)
    assert match, f"{name} not found in {path.name}"
    return re.findall(r'"([^"]+)"', match.group(1))


# --- agent <-> panel -----------------------------------------------------------------------------------
@needs_agent
def test_world_constants_match_the_agent():
    agent_world = _module(AGENT / "world.py")
    keep = _constant(agent_world, "RESET_KEEP")
    assert tuple(keep) == world.RESET_KEEP  # the same options, offered in the same order
    assert _constant(agent_world, "RESERVED_FOLDERS") == world.RESERVED_FOLDERS
    # the simulated agent of the test-suite works with the agent's own tables
    assert keep == FakeAgent.WORLD_RESET_KEEP
    assert _constant(agent_world, "KEEPABLE_NAMES") == FakeAgent.WORLD_KEEPABLE_NAMES
    assert _constant(agent_world, "WORLD_MARKERS") == FakeAgent.WORLD_MARKERS
    assert _constant(agent_world, "RESERVED_FOLDERS") == FakeAgent.WORLD_RESERVED_FOLDERS
    assert {name for roles in keep.values() for entries in roles.values() for name, _kind in entries} == _constant(
        agent_world, "KEEPABLE_NAMES"
    )
    assert _constant(agent_world, "STAGING_PREFIX") == ".mcpanel-world-"  # what the handler's log and the UI name


@needs_agent
def test_untouched_codes_are_exactly_the_agents_nothing_changed_codes():
    """Every failure of the agent's reset is either on the handler's "untouched" list or leaves the server stopped."""
    agent_world = _module(AGENT / "world.py")
    route = _function(_module(AGENT / "main.py"), "world_reset")
    in_operation = (
        _agent_error_codes(_function(agent_world, "level_parts"))
        | _agent_error_codes(_function(agent_world, "_reset_plan"))
        | _agent_error_codes(_function(agent_world, "reset"))
    )
    assert "world_rollback_failed" in in_operation
    # after world_rollback_failed the old world is in the aside folder: never "untouched"
    assert in_operation - {"world_rollback_failed"} <= world.WORLD_RESET_UNTOUCHED
    # confirmation_mismatch is only ever raised before the operation exists (a refused request)
    assert (in_operation | _agent_error_codes(route)) - {
        "world_rollback_failed",
        "confirmation_mismatch",
    } == world.WORLD_RESET_UNTOUCHED
    # what the operation runner and the panel's client report when the outcome is not known
    assert not world.WORLD_RESET_UNTOUCHED & {
        "world_rollback_failed",
        "internal_error",
        "operation_failed",
        "operation_lost",
        "operation_timeout",
        "agent_error",
        "agent_unavailable",
    }


@needs_agent
def test_reset_request_matches_the_agents_schema_and_route(node, monkeypatch):
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def fake_json(_client: Any, method: str, path: str, **kwargs: Any) -> Any:
        seen.append((method, path, kwargs))
        return {"operation": {"id": "op-1"}}

    monkeypatch.setattr(AgentClient, "_json", fake_json)
    iid = uuid.uuid4()
    AgentClient(node).world_reset(iid, level_name="world", layout="vanilla", expect=["world"], keep=[])
    method, path, kwargs = seen[-1]
    assert sorted(kwargs["json"]) == sorted(_class_fields(_module(AGENT / "schemas.py"), "WorldResetRequest"))
    assert kwargs["json"]["confirm"] == str(iid)
    main = (AGENT / "main.py").read_text(encoding="utf-8")
    assert '@app.post("/v1/instances/{instance_id}/world/reset"' in main
    assert (method, path) == ("POST", f"/v1/instances/{iid}/world/reset")
    # the operation kind the agent registers is the one that blocks power actions meanwhile
    assert 'registry.start("world_reset"' in main
    assert "world_reset" in _constant(_module(AGENT / "operations.py"), "BLOCKS_LIFECYCLE")
    # GET /world: the query parameter the client sends for sizes=False
    info = _function(_module(AGENT / "main.py"), "world_info")
    assert {"level_name", "layout", "sizes"} <= {a.arg for a in info.args.args}


@needs_agent
def test_live_container_rule_is_the_same_on_both_sides():
    """``is_live`` is the agent route's check, and the simulated agent decides alike for every container state."""

    def returned(function: ast.AST) -> str:
        (ret,) = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
        return ast.dump(ret.value)

    panel = _function(ast.parse(textwrap.dedent(inspect.getsource(world.is_live))), "is_live")
    assert returned(panel) == returned(_function(_module(AGENT / "main.py"), "_container_live"))
    states = [
        {"exists": False},
        {"running": False, "restarting": False, "status": "exited"},
        {"running": True, "restarting": False, "status": "running"},
        {"running": True, "restarting": True, "status": "restarting"},
        {"running": False, "restarting": True, "status": "restarting"},
        {"running": False, "restarting": False, "status": "restarting"},
        {"running": True, "restarting": False, "status": "paused"},
        {"running": False, "restarting": False, "status": "created"},
        {"running": False, "restarting": False, "status": "dead"},
    ]
    for state in states:
        assert world.is_live(state) is FakeAgent._live(state), state
    assert [world.is_live(state) for state in states] == [False, False, True, True, True, True, True, False, False]


# --- panel <-> web UI ----------------------------------------------------------------------------------
LEVEL_DAT = gzip.compress(b"\x0a\x00\x00\x00")


@pytest.fixture
def paper(make_instance, fake_agent):
    inst = make_instance(name="Paper", server_type="PAPER")
    fake_agent.prepare(inst.id, {"server.properties": "level-name=bruh\nlevel-seed=42\n"})
    for name in ("bruh", "bruh_nether", "bruh_the_end"):
        fake_agent.put_file(inst.id, f"{name}/level.dat", LEVEL_DAT)
        fake_agent.put_file(inst.id, f"{name}/region/r.0.0.mca", name)
    fake_agent.put_file(inst.id, "bruh/datapacks/pack.zip", "dp")
    return inst


@needs_frontend
@pytest.mark.django_db
def test_plan_has_the_fields_of_the_frontend_type(clients, paper):
    plan = clients["owner"].get(f"/api/instances/{paper.id}/world/reset").json()
    types = FRONTEND / "lib" / "types.ts"
    assert set(plan) == _ts_interface(types, "WorldResetPlan")
    assert {key for folder in plan["folders"] for key in folder} == _ts_interface(types, "WorldResetFolder")
    assert set(plan["seed"]) == {"value", "source"} and all(set(b) == {"code", "message"} for b in plan["blockers"])
    text = types.read_text(encoding="utf-8")
    # the unions of the type are the values the backend can send
    for state in ("missing", "empty", "world", "pending", "unknown", "not_a_directory"):
        assert f'"{state}"' in text
    assert f"export type WorldResetSeedMode = {' | '.join(repr(m) for m in world.SEED_MODES)};".replace("'", '"') in text
    assert f"export type WorldResetKeep = {' | '.join(repr(k) for k in world.RESET_KEEP)};".replace("'", '"') in text


@needs_frontend
@pytest.mark.django_db
def test_request_and_result_have_the_fields_of_the_frontend_model(clients, paper, django_capture_on_commit_callbacks):
    model = FRONTEND / "pages" / "instance" / "settings" / "reset-world-model.ts"
    # the body the dialog sends: WorldResetBody plus the typed name
    assert _ts_interface(model, "WorldResetBody") | {"confirm_name"} == set(WorldResetSerializer().fields)
    assert _ts_string_list(model, "KEEP_OPTIONS") == list(world.RESET_KEEP)
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{paper.id}/world/reset",
            {
                "confirm_name": paper.name,
                "folders": ["bruh", "bruh_nether", "bruh_the_end"],
                "stop_if_running": False,
                "pre_restore_backup": True,
                "seed_mode": "custom",
                "seed": "7",
                "keep": ["datapacks"],
            },
            format="json",
        )
    assert resp.status_code == 202, resp.content
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.kind == "world_reset"
    # the result the outcome toast reads
    assert set(job.result) == _ts_interface(model, "WorldResetResult")
    assert job.result["kept"] == ["bruh/datapacks"] and job.result["deleted"] == ["bruh", "bruh_nether", "bruh_the_end"]


@needs_frontend
def test_frontend_knows_every_code_that_makes_a_confirmed_plan_outdated():
    model = FRONTEND / "pages" / "instance" / "settings" / "reset-world-model.ts"
    outdated = set(_ts_string_list(model, "PLAN_OUTDATED_CODES"))
    source = (REPO / "backend" / "apps" / "instances" / "world.py").read_text(encoding="utf-8")
    blockers = set(re.findall(r'_blocker\(\s*"(\w+)"', source))
    assert blockers >= {
        "invalid_level_name",
        "level_name_conflict",
        "world_location_overridden",
        "not_a_directory",
        "not_a_world",
        "no_world",
    }
    # every blocker of the plan, and the three request checks that compare the request with the plan
    assert outdated == blockers | {"world_changed", "must_stop", "seed_env_set"}


@needs_frontend
def test_frontend_reads_the_job_errors_the_handler_writes():
    """The outcome toast tells "nothing changed" from "reset, but not started" by the start of the job error."""
    model = (FRONTEND / "pages" / "instance" / "settings" / "reset-world-model.ts").read_text(encoding="utf-8")
    handler = (REPO / "backend" / "apps" / "instances" / "handlers.py").read_text(encoding="utf-8")
    for prefix in ("The world was not changed", "The world was reset"):
        assert f'error.startsWith("{prefix}")' in model
        assert f'f"{prefix}' in handler
    utils = FRONTEND / "pages" / "activity" / "job-utils.ts"
    assert 'world_reset: ["instance.restore", "instance.files.write"]' in utils.read_text(encoding="utf-8")
    from apps.jobs.views import ABANDON_PERMISSIONS

    assert ABANDON_PERMISSIONS["world_reset"] == ("instance.restore", "instance.files.write")
    kinds = FRONTEND / "pages" / "instance" / "backups" / "world-utils.ts"
    assert "world_reset" in _ts_string_list(kinds, "WORLD_JOB_KINDS")
