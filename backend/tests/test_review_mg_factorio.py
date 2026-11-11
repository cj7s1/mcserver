# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Review of the multi-game branch (2026-11-10), item 2: Factorio loads the newest save.

After an unclean exit (kill, crash, power loss) the newest state of a Factorio server is an autosave
(``saves/_autosaveN.zip``), not ``saves/world.zip``; with ``LOAD_LATEST_SAVE=false`` the server went
back to the stale ``world.zip``. Now it loads the newest save, and the panel's saves page and export
name that save. A reset or an import leaves exactly one save (the new one), so it is the newest.
"""

from __future__ import annotations

import time
from typing import Any

from apps.instances.spec import build_spec
from apps.jobs.models import Job, JobStatus

from .test_games_phase2 import factorio, save_zip, start_container  # noqa: F401


def _age(fake_agent: Any, inst: Any, rel: str, seconds: float) -> None:
    fake_agent.fs[str(inst.id)]["mtime"][rel] = time.time() - seconds


def test_factorio_loads_the_newest_save(factorio):
    env = build_spec(factorio)["env"]
    assert env["LOAD_LATEST_SAVE"] == "true" and env["SAVE_NAME"] == "world" and env["GENERATE_NEW_SAVE"] == "true"


def test_unclean_exit_newest_autosave_is_current_and_exported(clients, factorio, fake_agent):
    # world.zip was written at the last clean stop; the server ran on and autosaved, then was killed
    fake_agent.put_file(factorio.id, "saves/world.zip", b"stale")
    fake_agent.put_file(factorio.id, "saves/_autosave2.zip", b"newest")
    fake_agent.put_file(factorio.id, "saves/_autosave1.zip", b"older")
    fake_agent.put_file(factorio.id, "saves/_autosave3.tmp.zip", b"partial")  # never a save to load
    _age(fake_agent, factorio, "saves/world.zip", 3600)
    _age(fake_agent, factorio, "saves/_autosave1.zip", 1200)
    _age(fake_agent, factorio, "saves/_autosave2.zip", 600)
    data = clients["viewer"].get(f"/api/instances/{factorio.id}/saves").json()
    assert data["current"] == "_autosave2"
    assert [s["name"] for s in data["saves"] if s["current"]] == ["_autosave2"]
    resp = clients["owner"].get(f"/api/instances/{factorio.id}/saves/export")
    assert resp.status_code == 200 and b"".join(resp.streaming_content) == b"newest"
    assert "Factory-_autosave2.zip" in resp["Content-Disposition"]


def test_without_saves_the_named_save_is_current(clients, factorio, fake_agent):
    data = clients["viewer"].get(f"/api/instances/{factorio.id}/saves").json()
    assert data["current"] == "world"


def test_import_leaves_the_upload_as_the_only_and_newest_save(clients, factorio, fake_agent, django_capture_on_commit_callbacks):
    import io

    fake_agent.put_file(factorio.id, "saves/world.zip", b"old")
    fake_agent.put_file(factorio.id, "saves/_autosave1.zip", b"auto")
    start_container(fake_agent, factorio, running=False)
    f = io.BytesIO(save_zip("other"))
    f.name = "mine.zip"
    with django_capture_on_commit_callbacks(execute=True):
        resp = clients["owner"].post(
            f"/api/instances/{factorio.id}/saves/import",
            {"file": f, "confirm_name": "Factory", "backup": "false"},
            format="multipart",
        )
    job = Job.objects.get(pk=resp.json()["id"])
    assert job.status == JobStatus.SUCCEEDED, job.error
    saves = [p for p in fake_agent.fs[str(factorio.id)]["files"] if p.startswith("saves/")]
    assert saves == ["saves/world.zip"]  # the only save: what --start-server-load-latest loads
    data = clients["viewer"].get(f"/api/instances/{factorio.id}/saves").json()
    assert data["current"] == "world"
