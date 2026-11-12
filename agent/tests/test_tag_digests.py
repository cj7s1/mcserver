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


# --- R-2: a re-pushed pinned tag never makes a server unstartable while the pinned image is here ----------
def test_a_pinned_tag_is_not_pulled_newer_while_the_local_image_is_the_pinned_one(settings, monkeypatch):
    """Adopted from audit-2026-11-09/fixes/review/agent-net/test_r_pinned_tag_repush.py, turned around."""
    from mcagent import main

    local = {"attrs": {"Id": "sha256:" + "c" * 64, "RepoDigests": [f"x@{PZ_DIGEST}"]}}
    pulls: list[str] = []
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(images=_Images(local["attrs"])))
    monkeypatch.setattr(docker_ops, "local_image_id", lambda image: local["attrs"]["Id"])
    monkeypatch.setattr(docker_ops, "recently_checked", lambda s, image: None)
    monkeypatch.setattr(docker_ops, "pull_image_bounded", lambda image, t, p: pulls.append(image))
    monkeypatch.setattr(docker_ops, "remember_pull", lambda s, image: None)
    op = SimpleNamespace(update=lambda *a, **k: None)
    assert main._pull_for_apply(op, PZ_IMAGE, "newer", [], settings, PZ) is False
    assert pulls == []
    # another tag of the same image is checked as before
    other = "indifferentbroccoli/projectzomboid-server-docker:v1.3.0"
    assert main._pull_for_apply(op, other, "newer", [], settings, PZ) is True and pulls == [other]


class _TwoImages:
    """The local tag names the re-pushed image; the pinned one is still here under its digest."""

    def __init__(self, pinned_present: bool):
        self.pinned_present = pinned_present

    def get(self, ref):
        if ref == PZ_IMAGE:
            return SimpleNamespace(
                attrs={"Id": OTHER, "RepoDigests": [f"repo@{OTHER}"], "Config": {}}, id=OTHER
            )
        if self.pinned_present and ref in (
            f"indifferentbroccoli/projectzomboid-server-docker@{PZ_DIGEST}",
            PZ_DIGEST,
        ):
            return SimpleNamespace(
                attrs={"Id": PZ_DIGEST, "RepoDigests": [], "Config": {"Volumes": None}}, id=PZ_DIGEST
            )
        raise ImageNotFound("no such image")


def test_a_moved_tag_falls_back_to_the_pinned_image_that_is_still_here(settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(images=_TwoImages(True)))
    ref = docker_ops.check_tag_digest(settings, PZ, PZ_IMAGE)
    assert ref == f"indifferentbroccoli/projectzomboid-server-docker@{PZ_DIGEST}"


def test_create_container_uses_the_pinned_image_and_says_so(settings, monkeypatch):
    created: list[dict] = []
    fake = SimpleNamespace(
        images=_TwoImages(True),
        api=SimpleNamespace(
            api_version="1.47", create_container=lambda **kw: created.append(kw) or {"Id": "n"}
        ),
        containers=SimpleNamespace(get=lambda cid: SimpleNamespace(id=cid)),
    )
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "ensure_network", lambda _s: None)
    (settings.instances_dir / str(IID)).mkdir(parents=True)
    spec = InstanceSpec(
        name="pz", image=PZ_IMAGE, memory_limit_mb=8192, rcon_password="x" * 16, game="project-zomboid",
        env={"SERVER_NAME": "pz"},
    )  # fmt: skip
    warnings: list[str] = []
    docker_ops.create_container(settings, IID, spec, "h", warnings=warnings)
    assert created[-1]["image"] == f"indifferentbroccoli/projectzomboid-server-docker@{PZ_DIGEST}"
    assert warnings and "pinned image" in warnings[0]


def test_refused_only_when_no_pinned_image_is_here(settings, monkeypatch):
    monkeypatch.setattr(docker_ops, "client", lambda: SimpleNamespace(images=_TwoImages(False)))
    with pytest.raises(AgentError) as err:
        docker_ops.check_tag_digest(settings, PZ, PZ_IMAGE)
    assert err.value.code == "image_digest_mismatch"
