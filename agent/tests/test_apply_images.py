"""apply (PUT /v1/instances/{id}/container): runtime image updates and a safe replacement.

* agent-docker#4: ``pull="newer"`` tries a bounded pull and falls back to the image on the host;
  the result says which image ran before and runs now; a superseded runtime image (no tag left,
  no container) is removed, a tagged one never.
* agent-docker#5: the new container is created before the old one is removed (under a
  temporary name), so a spec Docker refuses, or a CPU limit above the host's, leaves the old
  container as it was.

Docker is a fake; nothing here talks to a real daemon.
"""

from __future__ import annotations

import json
import threading
import time

import docker.errors
import pytest

from mcagent import docker_ops, main
from mcagent.docker_ops import AgentError

from .test_lastrun import IID, PASSWORD, FakeContainer

OLD = "sha256:" + "11" * 32
NEW = "sha256:" + "22" * 32
TAG = "itzg/minecraft-server:java21"
SPEC = {
    "name": "s",
    "image": TAG,
    "memory_limit_mb": 2048,
    "rcon_password": PASSWORD,
    "ports": [{"container_port": 25565, "host_port": 25565}],
}


class Container(FakeContainer):
    def __init__(self, calls, *, image_id=OLD, name=None, **kwargs):
        super().__init__(calls, **kwargs)
        self.image_id = image_id
        if name:
            self.name = name

    @property
    def attrs(self) -> dict:
        return {**super().attrs, "Image": self.image_id}

    def rename(self, name) -> None:
        self.calls.append(("rename", self.name, name))
        self.name = name

    def remove(self, v=False, force=False) -> None:
        self.calls.append(("docker rm", self.name))
        if self.remove_error is not None:
            raise self.remove_error
        self.removed = True


class Image:
    def __init__(self, image_id, tags, repo="itzg/minecraft-server"):
        self.id = image_id
        self.attrs = {"Id": image_id, "RepoTags": list(tags), "RepoDigests": [f"{repo}@sha256:{'9' * 64}"]}


class Docker:
    """Containers by name, images by id and tag, and the calls that changed something."""

    def __init__(self, calls):
        self.calls = calls
        self.by_name: dict[str, Container] = {}
        self.image_store: dict[str, Image] = {}
        self.containers = self
        self.images = _Images(self)
        self.api = self

    # containers
    def get(self, name):
        for c in self.by_name.values():
            if (c.name == name or c.id == name) and not c.removed:
                return c
        raise docker.errors.NotFound("No such container")

    def list(self, all=False, filters=None, ignore_removed=False):
        out = [c for c in self.by_name.values() if not c.removed]
        ancestor = (filters or {}).get("ancestor")
        if ancestor:
            out = [c for c in out if c.image_id == ancestor]
        return out

    def add(self, c: Container) -> Container:
        self.by_name[c.id] = c
        return c

    def inspect_container(self, container_id):
        return self.get(container_id).attrs

    # images
    def tag(self, image_id: str, tags: list[str]) -> None:
        for img in self.image_store.values():
            img.attrs["RepoTags"] = [t for t in img.attrs["RepoTags"] if t not in tags]
        self.image_store.setdefault(image_id, Image(image_id, [])).attrs["RepoTags"] += tags


class _Images:
    def __init__(self, fake: Docker):
        self.fake = fake

    def get(self, ref):
        store = self.fake.image_store
        if ref in store:
            return store[ref]
        for img in store.values():
            if ref in img.attrs["RepoTags"]:
                return img
        raise docker.errors.ImageNotFound(f"No such image: {ref}")

    def remove(self, image_id, noprune=False):
        """Like Docker's ``DELETE /images/{id}`` without force: an image with one tag is untagged
        and deleted; only one with several tags is refused (a conflict)."""
        img = self.get(image_id)
        if len(img.attrs["RepoTags"]) > 1:
            raise docker.errors.APIError("conflict: image is referenced in multiple repositories")
        self.fake.calls.append(("docker rmi", image_id))
        del self.fake.image_store[img.id]


@pytest.fixture
def host(monkeypatch, agent_settings):
    """One server whose exited container runs OLD (the tag's current image)."""
    calls: list = []
    fake = Docker(calls)
    fake.tag(OLD, [TAG])
    fake.add(Container(calls, image_id=OLD, container_id="old-container"))
    monkeypatch.setattr(docker_ops, "client", lambda: fake)
    monkeypatch.setattr(docker_ops, "get_settings", lambda: agent_settings)
    monkeypatch.setattr(docker_ops, "run_command_sync", lambda *a, **k: "")
    monkeypatch.setattr(docker_ops, "host_cpus", lambda: 24)
    created: list[dict] = []

    def create(settings, instance_id, spec, spec_hash, *, name=None, warnings=None):
        docker_ops.build_create_kwargs(settings, instance_id, spec, spec_hash)  # the real checks
        if fake.refuse_create:
            raise AgentError("Docker refused to create the container: no space left", "create_failed", 502)
        image = fake.images.get(spec.image).id
        c = Container(calls, image_id=image, container_id=f"new-{len(created)}", status="created")
        c.name = name or docker_ops.container_name(instance_id)
        calls.append(("docker create", c.name, image))
        created.append({"name": c.name, "image": image})
        return fake.add(c)

    fake.refuse_create = False
    monkeypatch.setattr(docker_ops, "create_container", create)
    fake.created = created
    return fake


def _pull_moves_the_tag(fake: Docker, new_id: str = NEW):
    def pull(image, progress=None):
        fake.calls.append(("docker pull", image))
        fake.tag(new_id, [image])

    return pull


def _apply(api, auth, **body) -> dict:
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": SPEC, **body}, headers=auth)
    assert r.status_code == 200, r.text
    op_id = r.json()["operation"]["id"]
    deadline = time.monotonic() + 30
    while True:
        op = api.get(f"/v1/operations/{op_id}", headers=auth).json()
        if op["status"] not in ("pending", "running"):
            return op
        assert time.monotonic() < deadline, op
        time.sleep(0.02)


def _alive(fake: Docker) -> list[tuple[str, str]]:
    return sorted((c.name, c.image_id) for c in fake.by_name.values() if not c.removed)


# -- agent-docker#4: pull "newer" ------------------------------------------------------------------
def test_pull_newer_moves_the_server_to_the_new_build_and_removes_the_superseded_image(
    api, auth, host, monkeypatch
):
    monkeypatch.setattr(docker_ops, "pull_image", _pull_moves_the_tag(host))
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "succeeded", op
    result = op["result"]
    assert result["image_id_before"] == OLD and result["image_id"] == NEW and result["pulled"] is True
    assert result["warnings"] == [] and result["images_removed"] == [OLD]
    assert _alive(host) == [(docker_ops.container_name(IID), NEW)]
    assert ("docker rmi", OLD) in host.calls and OLD not in host.image_store


def test_an_image_another_server_still_uses_is_kept(api, auth, host, monkeypatch):
    other = docker_ops.container_name(IID).replace("mcpanel-", "mcpanel-other")
    host.add(Container(host.calls, image_id=OLD, container_id="other", name=other))
    monkeypatch.setattr(docker_ops, "pull_image", _pull_moves_the_tag(host))
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "succeeded", op
    assert op["result"]["images_removed"] == [] and OLD in host.image_store
    assert not [c for c in host.calls if c[0] == "docker rmi"]


def test_a_tagged_image_is_never_removed(api, auth, host, monkeypatch):
    """Docker would delete an image that has one tag (it untags it first): the agent itself must
    leave every tagged image alone (review r3: the fake used to refuse, hiding the guard)."""
    host.tag(OLD, ["itzg/minecraft-server:stable-java17"])  # OLD keeps a tag of its own
    monkeypatch.setattr(docker_ops, "pull_image", _pull_moves_the_tag(host))
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "succeeded", op
    assert op["result"]["images_removed"] == [] and OLD in host.image_store
    assert not [c for c in host.calls if c[0] == "docker rmi"]


def test_the_fake_removes_an_image_with_one_tag_like_docker(host):
    host.images.remove(OLD)  # OLD carries TAG
    assert OLD not in host.image_store


def test_an_image_of_another_repository_is_never_removed(agent_settings, host):
    host.image_store["sha256:" + "33" * 32] = Image("sha256:" + "33" * 32, [], repo="library/postgres")
    assert docker_ops.remove_superseded_images(agent_settings, {"sha256:" + "33" * 32}) == []


def test_pull_newer_that_fails_is_a_warning_and_the_local_image_is_used(api, auth, host, monkeypatch):
    def offline(image, progress=None):
        raise AgentError(
            "Image pull failed: dial tcp: lookup registry-1.docker.io: no such host", "pull_failed"
        )

    monkeypatch.setattr(docker_ops, "pull_image", offline)
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "succeeded", op
    result = op["result"]
    assert result["pulled"] is False and result["image_id"] == OLD == result["image_id_before"]
    assert len(result["warnings"]) == 1 and "no such host" in result["warnings"][0]
    assert result["images_removed"] == []


def test_pull_newer_is_given_up_after_its_time(api, auth, host, monkeypatch):
    release = threading.Event()

    def slow(image, progress=None):
        release.wait(10)

    monkeypatch.setattr(docker_ops, "pull_image", slow)
    monkeypatch.setattr(main, "PULL_NEWER_TIMEOUT", 0.3)
    started = time.monotonic()
    op = _apply(api, auth, pull="newer")
    release.set()
    assert op["status"] == "succeeded", op
    assert time.monotonic() - started < 5
    assert op["result"]["pulled"] is False and "did not finish within" in op["result"]["warnings"][0]


def test_pull_newer_without_a_local_image_must_pull(api, auth, host, monkeypatch):
    host.image_store.clear()

    def offline(image, progress=None):
        raise AgentError("Image pull failed: no such host", "pull_failed", 502)

    monkeypatch.setattr(docker_ops, "pull_image", offline)
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "failed" and op["error_code"] == "pull_failed"
    assert _alive(host) == [(docker_ops.container_name(IID), OLD)]  # the old container is still there


def test_pull_missing_still_does_not_look_for_updates(api, auth, host, monkeypatch):
    monkeypatch.setattr(docker_ops, "pull_image", _pull_moves_the_tag(host))
    op = _apply(api, auth)
    assert op["status"] == "succeeded", op
    assert op["result"]["pulled"] is False and op["result"]["image_id"] == OLD
    assert not [c for c in host.calls if c[0] == "docker pull"]


def test_info_advertises_pull_newer_and_ping(api, auth, monkeypatch):
    monkeypatch.setattr(docker_ops, "docker_info", lambda: {})
    monkeypatch.setattr(main.storage, "capabilities", lambda _settings: {})
    monkeypatch.setattr(main, "restic_available", lambda _settings: None)
    features = api.get("/v1/info", headers=auth).json()["features"]
    assert {"retire", "pull_newer", "ping"} <= set(features)


# -- agent-docker#5: the old container stays until the new one exists ------------------------------
def test_recreate_creates_the_new_container_before_removing_the_old_one(api, auth, host):
    host.by_name["old-container"].state = "running"
    op = _apply(api, auth)
    assert op["status"] == "succeeded", op
    name = docker_ops.container_name(IID)
    steps = [
        c
        for c in host.calls
        if (isinstance(c, tuple) and c[0] != "logs") or c in ("docker stop", "docker start")
    ]
    assert steps == [
        ("docker create", docker_ops.replacement_name(IID), OLD),
        "docker stop",
        ("docker rm", name),
        ("rename", docker_ops.replacement_name(IID), name),
        "docker start",  # it was running
    ]
    assert _alive(host) == [(name, OLD)] and op["result"]["recreated"] is True


def test_a_container_docker_refuses_to_create_leaves_the_old_one_as_it_was(api, auth, host):
    host.by_name["old-container"].state = "running"
    host.refuse_create = True
    op = _apply(api, auth)
    assert op["status"] == "failed" and op["error_code"] == "create_failed"
    assert "docker stop" not in host.calls and not [c for c in host.calls if c[0] == "docker rm"]
    old = host.by_name["old-container"]
    assert old.status == "running" and not old.removed


def test_a_cpu_limit_above_the_hosts_cpus_is_refused_before_anything_changes(api, auth, host):
    r = api.put(f"/v1/instances/{IID}/container", json={"spec": {**SPEC, "cpu_limit": 32}}, headers=auth)
    op_id = r.json()["operation"]["id"]
    deadline = time.monotonic() + 30
    while (op := api.get(f"/v1/operations/{op_id}", headers=auth).json())["status"] in ("pending", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert op["status"] == "failed" and op["error_code"] == "cpu_limit" and "24 CPUs" in op["error"]
    assert host.created == [] and not host.by_name["old-container"].removed


def test_a_replacement_left_by_an_interrupted_apply_is_removed_first(api, auth, host):
    stale = Container(
        host.calls, container_id="stale", name=docker_ops.replacement_name(IID), status="created"
    )
    host.add(stale)
    assert [c["id"] for c in docker_ops.list_managed()] == ["old-container"]  # never listed as the server
    op = _apply(api, auth)
    assert op["status"] == "succeeded", op
    assert stale.removed and host.calls[0] == ("docker rm", docker_ops.replacement_name(IID))
    assert _alive(host) == [(docker_ops.container_name(IID), OLD)]


def test_a_running_container_under_the_replacement_name_is_not_removed(host):
    host.add(
        Container(host.calls, container_id="busy", name=docker_ops.replacement_name(IID), status="running")
    )
    with pytest.raises(AgentError) as err:
        docker_ops.remove_stale_replacement(IID)
    assert err.value.code == "instance_busy"


def test_a_server_without_a_container_is_created_under_its_own_name(api, auth, host):
    host.by_name.clear()
    op = _apply(api, auth)
    assert op["status"] == "succeeded", op
    assert host.created == [{"name": docker_ops.container_name(IID), "image": OLD}]
    assert op["result"]["recreated"] is False and op["result"]["image_id_before"] == OLD


# -- apply failures clean up the new container (review r3 finding 12) ---------------------------------
def test_a_create_that_fails_leaves_no_replacement_container(api, auth, host):
    host.refuse_create = True
    op = _apply(api, auth)
    assert op["status"] == "failed" and op["error_code"] == "create_failed"
    assert _alive(host) == [(docker_ops.container_name(IID), OLD)]
    assert not [c for c in host.calls if c[0] == "docker rm"]


def test_an_old_container_docker_does_not_remove_leaves_no_replacement(api, auth, host):
    old = host.by_name["old-container"]
    old.remove_error = docker.errors.APIError("removal of container old-container is already in progress")
    op = _apply(api, auth)
    assert op["status"] == "failed", op
    replacement = docker_ops.replacement_name(IID)
    assert ("docker rm", replacement) in host.calls  # the new one, removed again
    assert _alive(host) == [(docker_ops.container_name(IID), OLD)] and not old.removed
    assert not [c for c in host.calls if c[0] == "rename"]


def test_a_rename_that_fails_after_the_create_removes_the_new_container(api, auth, host, monkeypatch):
    def refuse(self, name):
        self.calls.append(("rename", self.name, name))
        raise docker.errors.APIError("Conflict. The container name is already in use")

    monkeypatch.setattr(Container, "rename", refuse)
    op = _apply(api, auth)
    assert op["status"] == "failed" and op["error_code"] == "rename_failed", op
    replacement = docker_ops.replacement_name(IID)
    assert host.calls.count(("docker rm", replacement)) == 1
    # the old container was removed before the rename; the server has no container now (the
    # panel creates it again at the next start), and no replacement is left under the temp name
    assert _alive(host) == []


# -- delete and archive remove a stale replacement (review r3 finding 11) -----------------------------
def _stale(host, status="created"):
    return host.add(
        Container(host.calls, container_id="stale", name=docker_ops.replacement_name(IID), status=status)
    )


def _wait_op(api, auth, op_id):
    deadline = time.monotonic() + 30
    while (op := api.get(f"/v1/operations/{op_id}", headers=auth).json())["status"] in ("pending", "running"):
        assert time.monotonic() < deadline, op
        time.sleep(0.02)
    return op


@pytest.mark.parametrize("with_container", [True, False])
def test_deleting_a_server_removes_a_replacement_an_interrupted_apply_left(
    api, auth, host, agent_settings, with_container
):
    if not with_container:
        host.by_name.clear()
    stale = _stale(host)
    (agent_settings.instances_dir / str(IID)).mkdir(parents=True, exist_ok=True)
    r = api.request("DELETE", f"/v1/instances/{IID}/data", json={"confirm": str(IID)}, headers=auth)
    op = _wait_op(api, auth, r.json()["operation"]["id"])
    assert op["status"] == "succeeded", op
    assert stale.removed and _alive(host) == []


@pytest.mark.parametrize("with_container", [True, False])
def test_archiving_removes_a_replacement_an_interrupted_apply_left(api, auth, host, with_container):
    if not with_container:
        host.by_name.clear()
    stale = _stale(host)
    r = api.delete(f"/v1/instances/{IID}/container", headers=auth)
    assert r.status_code == 200 and r.json() == {"removed": with_container}
    assert stale.removed and _alive(host) == []


def test_archiving_leaves_a_foreign_container_under_the_replacement_name_alone(api, auth, host):
    foreign = _stale(host)
    foreign.labels = {"com.example": "theirs"}
    r = api.delete(f"/v1/instances/{IID}/container", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "foreign_container"
    assert not foreign.removed and not host.by_name["old-container"].removed


# -- review r3 #27: pull="newer" asks the registry at most once per tag per 24 h -----------------------
def _counting_pull(fake: Docker, pulls: list, new_id: str = NEW):
    def pull(image, progress=None):
        pulls.append(image)
        fake.tag(new_id, [image])

    return pull


def test_pull_newer_checks_a_tag_once_per_day(api, auth, host, monkeypatch, agent_settings):
    pulls: list[str] = []
    monkeypatch.setattr(docker_ops, "pull_image", _counting_pull(host, pulls))
    assert _apply(api, auth, pull="newer")["result"]["pulled"] is True
    op = _apply(api, auth, pull="newer")  # the next start, an hour later or a minute later
    assert op["status"] == "succeeded" and op["result"]["pulled"] is False and op["result"]["warnings"] == []
    assert pulls == [TAG]
    # another tag is checked on its own
    other = "itzg/minecraft-server:java17"
    host.tag(OLD, [other])
    r = api.put(
        f"/v1/instances/{IID}/container",
        json={"spec": {**SPEC, "image": other}, "pull": "newer"},
        headers=auth,
    )
    assert r.status_code == 200
    deadline = time.monotonic() + 30
    while api.get(f"/v1/operations/{r.json()['operation']['id']}", headers=auth).json()["status"] in (
        "pending",
        "running",
    ):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert pulls == [TAG, other]


def test_pull_newer_checks_again_after_a_day(api, auth, host, monkeypatch, agent_settings):
    pulls: list[str] = []
    monkeypatch.setattr(docker_ops, "pull_image", _counting_pull(host, pulls))
    docker_ops.remember_pull(agent_settings, TAG, now=time.time() - docker_ops.PULL_CHECK_INTERVAL - 1)
    assert _apply(api, auth, pull="newer")["result"]["pulled"] is True
    assert pulls == [TAG]


def test_a_check_recorded_in_the_future_does_not_count(agent_settings):
    docker_ops.remember_pull(agent_settings, TAG, now=time.time() + 3600)  # the clock was set back since
    assert docker_ops.recently_checked(agent_settings, TAG) is None


def test_a_failed_check_is_not_remembered(api, auth, host, monkeypatch, agent_settings):
    calls: list[str] = []

    def offline(image, progress=None):
        calls.append(image)
        raise AgentError("Image pull failed: no such host", "pull_failed", 502)

    monkeypatch.setattr(docker_ops, "pull_image", offline)
    assert len(_apply(api, auth, pull="newer")["result"]["warnings"]) == 1
    assert len(_apply(api, auth, pull="newer")["result"]["warnings"]) == 1
    assert calls == [TAG, TAG] and docker_ops.recently_checked(agent_settings, TAG) is None


def test_a_pull_of_a_missing_image_counts_as_the_check(api, auth, host, monkeypatch, agent_settings):
    pulls: list[str] = []
    host.image_store.clear()
    monkeypatch.setattr(docker_ops, "pull_image", _counting_pull(host, pulls, new_id=OLD))
    assert _apply(api, auth, pull="newer")["result"]["pulled"] is True
    assert docker_ops.recently_checked(agent_settings, TAG) is not None
    assert _apply(api, auth, pull="newer")["result"]["pulled"] is False and pulls == [TAG]


def test_the_record_survives_a_restart_and_drops_old_entries(agent_settings):
    now = time.time()
    docker_ops.remember_pull(
        agent_settings, "itzg/minecraft-server:java8", now=now - 2 * docker_ops.PULL_CHECK_INTERVAL
    )
    docker_ops.remember_pull(agent_settings, TAG, now=now)
    stored = json.loads((agent_settings.state_dir / docker_ops.PULL_CHECK_FILE).read_text())
    assert stored == {TAG: now}
    (agent_settings.state_dir / docker_ops.PULL_CHECK_FILE).write_text("not json")
    assert docker_ops.recently_checked(agent_settings, TAG) is None  # unreadable: check again


# -- review r3 #27: the superseded image under Docker's containerd image store ------------------------
def _pull_moves_the_tag_containerd(fake: Docker, new_id: str = NEW):
    """Docker 29 with the containerd image store: the image a tag left keeps no RepoTags and no
    RepoDigests (seen on the test host: ``docker image inspect`` of a superseded image shows
    ``[] []``)."""

    def pull(image, progress=None):
        fake.calls.append(("docker pull", image))
        before = fake.images.get(image).id
        fake.tag(new_id, [image])
        if before != new_id and not fake.image_store[before].attrs["RepoTags"]:
            fake.image_store[before].attrs["RepoDigests"] = []

    return pull


def test_a_superseded_image_without_repo_digests_is_removed(api, auth, host, monkeypatch):
    monkeypatch.setattr(docker_ops, "pull_image", _pull_moves_the_tag_containerd(host))
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "succeeded", op
    assert op["result"]["images_removed"] == [OLD] and OLD not in host.image_store


def test_the_image_of_the_last_run_without_repo_digests_is_removed(
    api, auth, host, monkeypatch, agent_settings
):
    """A stopped server has no container: the image of its last run comes from the record."""
    from mcagent import lastrun

    host.by_name.clear()
    monkeypatch.setattr(lastrun, "summary", lambda _s, _iid: {"image_id": OLD, "image": TAG})
    host.image_store[OLD].attrs.update(RepoTags=[], RepoDigests=[])  # dangling, containerd store
    host.tag(NEW, [TAG])
    op = _apply(api, auth)
    assert op["status"] == "succeeded", op
    assert op["result"]["images_removed"] == [OLD]


def test_a_nameless_image_is_judged_by_the_reference_it_was_known_by(agent_settings, host):
    other = "sha256:" + "44" * 32
    host.image_store[other] = Image(other, [], repo="library/postgres")
    host.image_store[other].attrs["RepoDigests"] = []
    assert docker_ops.remove_superseded_images(agent_settings, {other: "library/postgres:16"}) == []
    assert docker_ops.remove_superseded_images(agent_settings, {other: None}) == []
    # Docker's own names win over what the caller says
    host.image_store[other].attrs["RepoDigests"] = [f"library/postgres@sha256:{'9' * 64}"]
    assert docker_ops.remove_superseded_images(agent_settings, {other: TAG}) == []
    assert other in host.image_store


def test_the_image_the_container_ran_is_known_by_the_containers_image_reference(api, auth, host, monkeypatch):
    """The server ran OLD, the tag had moved to MID since (another server's start), the pull
    moves it to NEW: both OLD (known by the container's image) and MID (by the tag) go."""
    mid = "sha256:" + "55" * 32
    host.tag(mid, [TAG])
    host.image_store[OLD].attrs["RepoDigests"] = []  # dangling under the containerd store
    monkeypatch.setattr(docker_ops, "pull_image", _pull_moves_the_tag_containerd(host))
    op = _apply(api, auth, pull="newer")
    assert op["status"] == "succeeded", op
    assert sorted(op["result"]["images_removed"]) == sorted([OLD, mid])
