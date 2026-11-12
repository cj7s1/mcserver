"""A-8 of the security review (2026-11-11): a version tag whose digest the probe recorded is only run when the
local image is that digest (the tag was not moved in the registry since)."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from docker.errors import ImageNotFound

from mcagent import docker_ops, profiles
from mcagent.config import Settings
from mcagent.errors import AgentError
from mcagent.schemas import InstanceSpec

IID = uuid.UUID("a8a8a8a8-0000-4000-8000-000000000008")
PZ = profiles.get("project-zomboid")
PZ_DIGEST = "sha256:0bfd0599c0cf3edf66ba40937ce3ae2a7c5de92073dc99d1c7e9a01fa581414e"
PZ_IMAGE = "indifferentbroccoli/projectzomboid-server-docker:v1.2.0"
OTHER = "sha256:" + "1" * 64


class _Images:
    def __init__(self, attrs):
        self.attrs = attrs

    def get(self, image):
        if self.attrs is None:
            raise ImageNotFound("no such image")
        return SimpleNamespace(attrs=self.attrs, id=self.attrs.get("Id"))


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    for var in list(os.environ):
        if var.startswith("AGENT_"):
            monkeypatch.delenv(var)
    return Settings(data_root=tmp_path / "data", token="t", runtime_uid=99, runtime_gid=100)


def _docker(monkeypatch, attrs):
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(images=_Images(attrs)))


def test_the_four_probed_tags_are_pinned():
    pinned = {
        g: dict(profiles.get(g).tag_digests) for g in ("palworld", "valheim", "hkmp", "project-zomboid")
    }
    assert pinned["project-zomboid"] == {"v1.2.0": (PZ_DIGEST,)}
    assert set(pinned["palworld"]) == {"v2.8.1"} and set(pinned["valheim"]) == {"1.4.1"}
    assert set(pinned["hkmp"]) == {"v2.4.3-2"}
    for game in ("minecraft", "factorio", "satisfactory", "cs2", "rust"):
        assert not profiles.get(game).tag_digests, game


@pytest.mark.parametrize(
    "attrs",
    [
        {
            "Id": "sha256:" + "f" * 64,
            "RepoDigests": [f"indifferentbroccoli/projectzomboid-server-docker@{PZ_DIGEST}"],
        },
        {
            "Id": PZ_DIGEST,
            "RepoDigests": [],
        },  # Docker's containerd image store: the id is the manifest digest
    ],
)
def test_the_known_digest_is_run(settings, monkeypatch, attrs):
    _docker(monkeypatch, attrs)
    docker_ops.check_tag_digest(settings, PZ, PZ_IMAGE)


def test_a_moved_tag_is_refused(settings, monkeypatch):
    _docker(
        monkeypatch,
        {"Id": OTHER, "RepoDigests": [f"indifferentbroccoli/projectzomboid-server-docker@{OTHER}"]},
    )
    with pytest.raises(AgentError) as err:
        docker_ops.check_tag_digest(settings, PZ, PZ_IMAGE)
    assert err.value.code == "image_digest_mismatch" and err.value.status == 409
    assert OTHER in str(err.value) and PZ_DIGEST in str(err.value)


def test_unpinned_tags_and_the_switch(settings, monkeypatch):
    _docker(monkeypatch, {"Id": OTHER, "RepoDigests": []})
    docker_ops.check_tag_digest(settings, PZ, "indifferentbroccoli/projectzomboid-server-docker:v1.3.0")
    docker_ops.check_tag_digest(settings, profiles.get("factorio"), "factoriotools/factorio:2.0.55-rootless")
    off = settings.model_copy(update={"image_digest_check": False})
    docker_ops.check_tag_digest(off, PZ, PZ_IMAGE)


def test_create_container_checks_the_digest(settings, monkeypatch):
    _docker(monkeypatch, {"Id": OTHER, "RepoDigests": [], "Config": {"Volumes": None}})
    spec = InstanceSpec(
        name="pz",
        image=PZ_IMAGE,
        memory_limit_mb=8192,
        rcon_password="x" * 16,
        game="project-zomboid",
        env={"SERVER_NAME": "pz"},
    )
    with pytest.raises(AgentError) as err:
        docker_ops.create_container(settings, IID, spec, "h")
    assert err.value.code == "image_digest_mismatch"
