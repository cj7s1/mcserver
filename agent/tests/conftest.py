"""Shared fixtures for the agent test suite.

``agent_settings`` is an isolated :class:`Settings` rooted in a temporary directory.
``api`` is a FastAPI TestClient whose ``get_settings`` dependency returns those
settings. The client is *not* used as a context manager, so the startup hook (which
creates directories under the real data root and the Docker network) never runs.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from mcagent.config import Settings, get_settings

TOKEN = "test-agent-token-0123456789"


@pytest.fixture
def agent_settings(tmp_path, monkeypatch) -> Settings:
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    settings = Settings(
        token=TOKEN,
        data_root=tmp_path / "data",
        restic_password="test-restic-password",
        runtime_uid=os.getuid(),
        runtime_gid=os.getgid(),
    )
    for d in (
        settings.instances_dir,
        settings.backups_dir,
        settings.staging_dir,
        settings.trash_dir,
        settings.state_dir,
    ):
        d.mkdir(parents=True)
    return settings


class _NoDocker:
    def __getattr__(self, name: str):  # pragma: no cover - only reached on a test bug
        raise AssertionError(f"test touched the Docker client ({name})")


@pytest.fixture
def no_docker(monkeypatch):
    """Fail loudly if code under test talks to Docker."""
    from mcagent import docker_ops

    monkeypatch.setattr(docker_ops, "client", lambda: _NoDocker())


@pytest.fixture
def api(agent_settings, monkeypatch) -> Iterator:
    from fastapi.testclient import TestClient

    from mcagent.main import app

    monkeypatch.setenv("AGENT_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_DATA_ROOT", str(agent_settings.data_root))
    get_settings.cache_clear()
    app.dependency_overrides[get_settings] = lambda: agent_settings
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_settings, None)
        get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _claim_marker_state(monkeypatch):
    """The claim marker's per-process state starts empty in every test, and its settle wait is short
    (the fake game writes its settings file synchronously)."""
    from mcagent import claimmarker

    monkeypatch.setattr(claimmarker, "_generation", {})
    monkeypatch.setattr(claimmarker, "_changing", {})
    monkeypatch.setattr(claimmarker, "SETTLE_STABLE", 0.05)
    monkeypatch.setattr(claimmarker, "SETTLE_MAX", 2.0)


@pytest.fixture(autouse=True)
def _readiness_state(monkeypatch):
    """The readiness probes' per-process state (and the ready marks' file) starts empty in every test."""
    from mcagent import docker_ops

    monkeypatch.setattr(docker_ops, "_health_state", {})
    monkeypatch.setattr(docker_ops, "_ready_runs", {})
    monkeypatch.setattr(docker_ops, "_ready_file", None)
