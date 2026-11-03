"""Software update and modpack update/rollback through the API with the fake agent."""

import pytest
import respx
from httpx import Response

from apps.backups.models import Backup
from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus

MR = "https://api.modrinth.com/v2"


def _post(c, capture, url, body):
    with capture(execute=True):
        return c.post(url, body, format="json")


@pytest.fixture
def owner(client_for, users):
    return client_for(users["owner"])


@pytest.fixture
def vanilla(make_instance, fake_agent, django_capture_on_commit_callbacks):
    from apps.jobs import runner

    inst = make_instance(
        name="Upd",
        server_type="VANILLA",
        minecraft_version="1.20.4",
        java_version=17,
        image="itzg/minecraft-server:stable-java17",
    )
    fake_agent.prepare(inst.id, {"server.properties": "motd=x\n"})
    with django_capture_on_commit_callbacks(execute=True):
        runner.enqueue("install", title="install", instance=inst, params={"start": True})
    inst.refresh_from_db()
    return inst


def test_software_update_backs_up_and_switches_java(vanilla, owner, fake_agent, monkeypatch, django_capture_on_commit_callbacks):
    monkeypatch.setattr(
        "apps.catalog.providers.game_versions",
        lambda st, include_snapshots=False: [{"version": v, "stable": True} for v in ("1.21.1", "1.20.4", "1.20.1")],
    )
    monkeypatch.setattr(
        "apps.instances.views.resolve_java",
        lambda mc, st: {"java_version": 21, "required": 21, "minecraft_version": mc, "reason": "t"},
    )
    r = _post(owner, django_capture_on_commit_callbacks, f"/api/instances/{vanilla.id}/software", {"minecraft_version": "1.20.1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "downgrade_unconfirmed"
    r = _post(owner, django_capture_on_commit_callbacks, f"/api/instances/{vanilla.id}/software", {"minecraft_version": "9.9"})
    assert r.status_code == 400
    r = _post(owner, django_capture_on_commit_callbacks, f"/api/instances/{vanilla.id}/software", {"minecraft_version": "1.21.1"})
    assert r.status_code == 202, r.json()
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    vanilla.refresh_from_db()
    assert (vanilla.minecraft_version, vanilla.java_version, vanilla.image) == (
        "1.21.1",
        21,
        "itzg/minecraft-server:stable-java21",
    )
    backup = Backup.objects.get(instance=vanilla, kind="pre_update")
    assert backup.meta["software_before"]["minecraft_version"] == "1.20.4"
    c = fake_agent.containers[str(vanilla.id)]
    assert c["spec"]["env"]["VERSION"] == "1.21.1" and c["running"]


def _modrinth_mocks():
    project = {
        "id": "P1",
        "slug": "pack",
        "title": "Pack",
        "project_type": "modpack",
        "server_side": "required",
        "game_versions": ["1.20.1"],
        "loaders": ["forge"],
    }

    def version(vid, date):
        return {
            "id": vid,
            "project_id": "P1",
            "name": vid,
            "version_number": vid,
            "version_type": "release",
            "status": "listed",
            "loaders": ["forge"],
            "game_versions": ["1.20.1"],
            "date_published": date,
            "files": [
                {
                    "primary": True,
                    "filename": f"{vid}.mrpack",
                    "url": f"https://cdn.modrinth.com/{vid}.mrpack",
                    "size": 1,
                    "hashes": {"sha1": "a" * 40, "sha512": "b" * 128},
                }
            ],
            "dependencies": [],
        }

    respx.get(f"{MR}/project/P1").mock(return_value=Response(200, json=project))
    respx.get(f"{MR}/version/V1").mock(return_value=Response(200, json=version("V1", "2026-01-01T00:00:00Z")))
    respx.get(f"{MR}/version/V2").mock(return_value=Response(200, json=version("V2", "2026-06-01T00:00:00Z")))


@respx.mock
def test_modpack_update_and_rollback(make_instance, fake_agent, owner, monkeypatch, django_capture_on_commit_callbacks):
    _modrinth_mocks()
    monkeypatch.setattr("apps.modpacks.mrpack.inspect_mrpack", lambda url, sha1=None: {"available": False})
    monkeypatch.setattr(
        "apps.modpacks.handlers.resolve_java",
        lambda mc, st: {"java_version": 17, "required": 17, "minecraft_version": mc, "reason": "t"},
    )
    inst = make_instance(
        name="Pack",
        server_type="MODRINTH",
        minecraft_version="1.20.1",
        java_version=17,
        image="itzg/minecraft-server:stable-java17",
        modpack_provider="modrinth",
        modpack_project_id="P1",
        modpack_slug="pack",
        modpack_name="Pack",
        modpack_version_id="V1",
        modpack_version_name="V1",
        modpack_loader="forge",
    )
    fake_agent.prepare(inst.id, {"server.properties": "motd=x\n"})
    plan = owner.get(f"/api/instances/{inst.id}/modpack/plan/V2").json()
    assert plan["target"]["modpack_version_id"] == "V2" and plan["minecraft_changed"] is False

    r = _post(owner, django_capture_on_commit_callbacks, f"/api/instances/{inst.id}/modpack/update", {"version_id": "V2"})
    assert r.status_code == 202
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.modpack_version_id == "V2"
    assert fake_agent.containers[str(inst.id)]["spec"]["env"]["MODRINTH_VERSION"] == "V2"
    backup = Backup.objects.get(instance=inst, kind="pre_update")
    assert backup.meta["modpack_before"]["modpack_version_id"] == "V1"

    # same release again is refused
    r = _post(owner, django_capture_on_commit_callbacks, f"/api/instances/{inst.id}/modpack/update", {"version_id": "V2"})
    assert Job.objects.get(pk=r.json()["id"]).error_code == "same_release"

    # rollback needs explicit confirmation, then restores the pre-update backup and the old pin
    r = _post(
        owner, django_capture_on_commit_callbacks, f"/api/instances/{inst.id}/modpack/rollback", {"backup_id": str(backup.id)}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirmation_required"
    r = _post(
        owner,
        django_capture_on_commit_callbacks,
        f"/api/instances/{inst.id}/modpack/rollback",
        {"backup_id": str(backup.id), "confirm": True},
    )
    job = Job.objects.get(pk=r.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    inst.refresh_from_db()
    assert inst.modpack_version_id == "V1"
    assert fake_agent.containers[str(inst.id)]["spec"]["env"]["MODRINTH_VERSION"] == "V1"
    assert Instance.objects.get(pk=inst.pk).active_job_id is None or not Job.objects.get(pk=inst.active_job_id).is_active
