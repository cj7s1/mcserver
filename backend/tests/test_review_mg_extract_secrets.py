# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Extract and Import never write the server's secret files (real-container gate, logs 71/72).

A user with file write access uploaded a zip with ``config/rconpw`` (Factorio) or
``.mcpanel/api-token`` (Satisfactory) plus another file, extracted it with overwrite at the server
root, and the extract job replaced the secret (Factorio then started with the attacker's RCON
password). The job ``extract_archive`` (Extract in the file manager, Import of a server archive) now
names the protected paths to the agent (feature ``extract_protected``), which skips those entries
after its own normalisation; an older agent is refused for games with secret files; an extract into
a protected folder is refused. Minecraft (and Terraria) send the request as before.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from django.core.cache import cache

from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus
from apps.nodes.client import AgentClient

from .test_games_phase2 import factorio  # noqa: F401
from .test_games_satisfactory import _claimed_instance
from .test_terraria import tshock  # noqa: F401

FACTORIO = ["config/rconpw"]
SATISFACTORY = [".mcpanel", "saved/ServerSettings.7777.sav"]


def code(resp: Any) -> str:
    return resp.json()["error"]["code"]


@pytest.fixture
def sf(make_instance, fake_agent):
    inst = _claimed_instance(make_instance, fake_agent, steps=("claim", "access", "new_game", "publish"), running=False)
    state = dict(inst.setup_state)
    state["ports_open"] = True
    Instance.objects.filter(pk=inst.pk).update(setup_state=state)
    inst.refresh_from_db()
    return inst


@pytest.fixture
def extracts(fake_agent, monkeypatch):
    """The extract requests the panel sends (the FakeAgent does not simulate the endpoint); the
    operation answers like an agent that skipped what it was told to protect."""
    calls: list[dict[str, Any]] = []

    def extract(_client: Any, instance_id: Any, path: str, destination: str = "", **kwargs: Any) -> dict[str, Any]:
        calls.append({"path": path, "destination": destination, **kwargs})
        protected = kwargs.get("protected") or []
        result = {"files": 1, "skipped_count": 0, "existing_count": 0, "protected_count": len(protected), "protected": protected}
        if kwargs.get("delete_archive"):
            fake_agent._root(str(instance_id))["files"].pop(path, None)
        return fake_agent._op("import", str(instance_id), result)

    monkeypatch.setattr(AgentClient, "extract", extract)
    cache.clear()
    return calls


def _old_agent(fake_agent: Any) -> None:
    fake_agent.features = [f for f in fake_agent.features if f != "extract_protected"]
    cache.clear()


def _extract(client: Any, inst: Any, capture: Any, **body: Any) -> Any:
    body = {"path": "up.zip", "destination": "", "overwrite": True, **body}
    with capture(execute=True):
        return client.post(f"/api/instances/{inst.id}/files/extract", body, format="json")


def _import(client: Any, inst: Any, capture: Any) -> Any:
    f = io.BytesIO(b"PK\x05\x06" + b"\x00" * 18)
    f.name = "server.zip"
    with capture(execute=True):
        return client.post(f"/api/instances/{inst.id}/import?overwrite=true", {"file": f}, format="multipart")


def _job(inst: Any) -> Job:
    return Job.objects.filter(instance=inst, kind="extract_archive").latest("created_at")


@pytest.mark.parametrize("game", ["factorio", "satisfactory"])
def test_extract_names_the_secret_files_to_the_agent(
    clients, factorio, sf, fake_agent, extracts, django_capture_on_commit_callbacks, game
):
    inst, expected = (factorio, FACTORIO) if game == "factorio" else (sf, SATISFACTORY)
    fake_agent.put_file(inst.id, "up.zip", b"PK")
    resp = _extract(clients["manager"], inst, django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.json()
    job = _job(inst)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert extracts[-1]["protected"] == expected
    assert any("not extracted" in line and expected[0] in line for line in job.log), job.log


@pytest.mark.parametrize("game", ["factorio", "satisfactory"])
def test_import_names_the_secret_files_to_the_agent(
    clients, factorio, sf, fake_agent, extracts, django_capture_on_commit_callbacks, game
):
    inst, expected = (factorio, FACTORIO) if game == "factorio" else (sf, SATISFACTORY)
    fake_agent.put_file(inst.id, "config/server-settings.json", "{}")  # (the server folder exists)
    resp = _import(clients["owner"], inst, django_capture_on_commit_callbacks)
    assert resp.status_code == 202, resp.json()
    job = _job(inst)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert extracts[-1]["protected"] == expected and extracts[-1]["path"].startswith(".mcpanel-import-")
    assert any("not extracted" in line for line in job.log), job.log


def test_a_satisfactory_import_checks_the_claim_again(clients, sf, fake_agent, extracts, django_capture_on_commit_callbacks):
    # an import rewrites saved/ like a restore: the ports close until the claim was checked again
    assert _import(clients["owner"], sf, django_capture_on_commit_callbacks).status_code == 202
    sf.refresh_from_db()
    assert not sf.setup_state.get("ports_open")


def test_minecraft_and_tshock_send_the_request_as_before(
    clients, make_instance, tshock, fake_agent, extracts, django_capture_on_commit_callbacks
):
    mc = make_instance()
    for inst in (mc, tshock):  # TShock's config.json is written like the file API writes it
        fake_agent.put_file(inst.id, "up.zip", b"PK")
        resp = _extract(clients["owner"], inst, django_capture_on_commit_callbacks)
        assert resp.status_code == 202 and _job(inst).status == JobStatus.SUCCEEDED, resp.json()
        assert "protected" not in extracts[-1]
        assert _import(clients["owner"], inst, django_capture_on_commit_callbacks).status_code == 202
        assert "protected" not in extracts[-1]
    # also on an agent without the feature
    _old_agent(fake_agent)
    fake_agent.put_file(mc.id, "up.zip", b"PK")
    assert _extract(clients["owner"], mc, django_capture_on_commit_callbacks).status_code == 202
    assert _job(mc).status == JobStatus.SUCCEEDED and "protected" not in extracts[-1]


def test_an_agent_without_the_feature_is_refused_for_games_with_secret_files(
    clients, factorio, sf, fake_agent, extracts, django_capture_on_commit_callbacks
):
    _old_agent(fake_agent)
    for inst in (factorio, sf):
        fake_agent.put_file(inst.id, "up.zip", b"PK")
        resp = _extract(clients["owner"], inst, django_capture_on_commit_callbacks)
        assert resp.status_code == 409 and code(resp) == "agent_outdated", resp.json()
        uploads_before = set(fake_agent._root(str(inst.id))["files"])
        resp = _import(clients["owner"], inst, django_capture_on_commit_callbacks)
        assert resp.status_code == 409 and code(resp) == "agent_outdated", resp.json()
        assert set(fake_agent._root(str(inst.id))["files"]) == uploads_before  # nothing was uploaded
    assert extracts == [] and not Job.objects.filter(kind="extract_archive").exists()


def test_a_job_queued_before_the_agent_was_downgraded_fails_closed(
    factorio, fake_agent, extracts, django_capture_on_commit_callbacks
):
    from apps.jobs import runner

    archive = ".mcpanel-import-0123456789ab.zip"
    fake_agent.put_file(factorio.id, archive, b"PK")
    _old_agent(fake_agent)
    params = {"path": archive, "destination": "", "strip": "auto", "overwrite": True, "delete_archive": True}
    with django_capture_on_commit_callbacks(execute=True):
        job = runner.enqueue("extract_archive", title="x", instance=factorio, params=params)
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and "too old" in job.error
    assert extracts == []
    assert fake_agent.get_file(factorio.id, archive) is None  # the panel's own upload is not left behind


@pytest.mark.parametrize(
    ("game", "destination"),
    [
        ("factorio", "config/rconpw"),
        ("factorio", "./config//rconpw"),
        ("factorio", "CONFIG/RconPW"),
        ("satisfactory", ".mcpanel"),
        ("satisfactory", ".MCPanel/sub"),
        ("satisfactory", "/.mcpanel/"),
        ("satisfactory", "saved/ServerSettings.7777.sav"),
    ],
)
def test_an_extract_into_a_protected_path_is_refused(
    clients, factorio, sf, fake_agent, extracts, django_capture_on_commit_callbacks, game, destination
):
    inst = factorio if game == "factorio" else sf
    fake_agent.put_file(inst.id, "up.zip", b"PK")
    resp = _extract(clients["owner"], inst, django_capture_on_commit_callbacks, destination=destination)
    assert resp.status_code == 403 and code(resp) == "managed_file", resp.json()
    assert extracts == []


def test_an_extract_into_another_folder_is_fine(clients, factorio, sf, fake_agent, extracts, django_capture_on_commit_callbacks):
    for inst, destination in ((factorio, "config"), (factorio, "mods"), (sf, "saved/server")):
        fake_agent.put_file(inst.id, "up.zip", b"PK")
        resp = _extract(clients["owner"], inst, django_capture_on_commit_callbacks, destination=destination)
        assert resp.status_code == 202, (destination, resp.json())
        assert extracts[-1]["destination"] == destination and extracts[-1]["protected"]
