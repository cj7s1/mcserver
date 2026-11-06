"""PUT /files/upload with and without the optional SHA-256 check (used for SpigotMC installs)."""

from __future__ import annotations

import hashlib
import uuid

JAR = b"PK\x03\x04 plugin bytes"


def _instance(agent_settings):
    iid = uuid.uuid4()
    (agent_settings.instances_dir / str(iid) / "plugins").mkdir(parents=True)
    return iid


def test_upload_with_matching_sha256_commits(api, auth, agent_settings):
    iid = _instance(agent_settings)
    digest = hashlib.sha256(JAR).hexdigest()
    r = api.put(
        f"/v1/instances/{iid}/files/upload",
        params={"path": "plugins/a.jar", "sha256": digest.upper()},
        content=JAR,
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "plugins/a.jar", "size": len(JAR), "sha256": digest}
    assert (agent_settings.instances_dir / str(iid) / "plugins" / "a.jar").read_bytes() == JAR


def test_upload_with_wrong_sha256_leaves_nothing_behind(api, auth, agent_settings):
    iid = _instance(agent_settings)
    r = api.put(
        f"/v1/instances/{iid}/files/upload",
        params={"path": "plugins/a.jar", "sha256": "0" * 64},
        content=JAR,
        headers=auth,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "hash_mismatch"
    plugins = agent_settings.instances_dir / str(iid) / "plugins"
    assert list(plugins.iterdir()) == []  # no file and no temporary left over


def test_upload_rejects_malformed_sha256(api, auth, agent_settings):
    iid = _instance(agent_settings)
    r = api.put(
        f"/v1/instances/{iid}/files/upload",
        params={"path": "plugins/a.jar", "sha256": "not-a-hash"},
        content=JAR,
        headers=auth,
    )
    assert r.status_code == 422
    assert not (agent_settings.instances_dir / str(iid) / "plugins" / "a.jar").exists()


def test_upload_without_sha256_still_works(api, auth, agent_settings):
    iid = _instance(agent_settings)
    r = api.put(
        f"/v1/instances/{iid}/files/upload", params={"path": "plugins/b.jar"}, content=JAR, headers=auth
    )
    assert r.status_code == 200, r.text
    assert r.json()["sha256"] == hashlib.sha256(JAR).hexdigest()
