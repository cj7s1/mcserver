import json

import pytest

from apps.jobs import runner


@pytest.fixture
def failed_pack(make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = make_instance(
        name="BMC",
        server_type="MODRINTH",
        minecraft_version="1.20.1",
        modpack_provider="modrinth",
        modpack_project_id="4BV47HRn",
        modpack_version_id="bdRIjRgs",
        modpack_loader="forge",
    )
    checker = [
        {
            "displayName": "FallingTree",
            "pattern": "FallingTree-1.20.1-4.3.4.jar",
            "destination": "mods",
            "url": "https://www.curseforge.com/minecraft/mc-mods/falling-tree/download/5010620",
        },
        {"displayName": "Present", "pattern": "present.jar", "destination": "mods", "url": "https://example.org"},
    ]
    fake_agent.prepare(inst.id, {"config/missing_mods_checker.json": json.dumps(checker), "mods/present.jar": "x"})
    with django_capture_on_commit_callbacks(execute=True):
        runner.enqueue("install", title="install", instance=inst, params={"start": True})
    fake_agent.log_lines[str(inst.id)] = [
        "[18:20:33] [main/ERROR] [Missing Mods Checker/]: Error thrown while opening! Exiting",
        "java.awt.HeadlessException: ",
    ]
    fake_agent.containers[str(inst.id)]["restart_count"] = 18
    # the real pack never became ready: it crashed during every startup
    type(inst).objects.filter(pk=inst.pk).update(ready_at=None, installed_at=None)
    inst.refresh_from_db()
    return inst


def test_diagnostics_endpoint_reports_crash_loop_and_cause(failed_pack, client_for, users):
    r = client_for(users["owner"]).get(f"/api/instances/{failed_pack.id}/diagnostics")
    assert r.status_code == 200, r.json()
    codes = [i["code"] for i in r.json()["issues"]]
    assert codes[:2] == ["crash_loop", "missing_mods_checker"]
    assert r.json()["restart_count"] == 18


def test_manual_downloads_lists_missing_files(failed_pack, client_for, users):
    r = client_for(users["owner"]).get(f"/api/instances/{failed_pack.id}/manual-downloads")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["missing"] == 1
    items = {i["filename"]: i for i in body["items"]}
    assert items["present.jar"]["present"] is True
    assert items["FallingTree-1.20.1-4.3.4.jar"]["url"].startswith("https://www.curseforge.com/")


def test_diagnostics_requires_console_permission(failed_pack, client_for, users):
    assert client_for(users["norole"]).get(f"/api/instances/{failed_pack.id}/diagnostics").status_code == 404
