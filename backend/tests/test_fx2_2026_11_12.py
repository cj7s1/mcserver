"""FX2-3 and FX2-5 of the second real-container run (audit-2026-11-09/fx-e2e/BUGS-2.md, 2026-11-12)."""

from __future__ import annotations

from typing import Any

import pytest
from django.utils import timezone

from apps.jobs.models import Job

pytestmark = pytest.mark.django_db


# --- FX2-3: a failed start; FX2-5: offline mode ---------------------------------------------------------------------------------------------
def test_a_start_that_failed_before_the_container_ran(make_instance: Any) -> None:
    from apps.instances import state
    from apps.jobs.models import JobStatus

    inst = make_instance(name="M", desired_state="running", installed_at=timezone.now())
    created = {"exists": True, "status": "created", "exit_code": 128, "running": False}
    status, detail = state.compute(inst, created)
    assert status == "failed" and "exited unexpectedly" not in detail and detail.startswith("The start failed before")
    Job.objects.create(
        kind="start",
        title="Start",
        instance=inst,
        status=JobStatus.FAILED,
        error="Bind for 127.0.0.1:25565 failed: port is already allocated",
    )
    assert state.compute(inst, created)[1] == (
        "The start failed before the server ran: Bind for 127.0.0.1:25565 failed: port is already allocated"
    )
    # a server that did run and exited keeps its text
    exited = {"exists": True, "status": "exited", "exit_code": 1, "running": False}
    assert state.compute(inst, exited)[1].startswith("Server exited unexpectedly (exit code 1)")


def test_the_share_card_warns_about_offline_mode(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    inst = make_instance()
    fake_agent.put_file(inst.id, "server.properties", "white-list=true\nonline-mode=false\n")
    card = clients["owner"].get(f"/api/instances/{inst.id}/share").json()
    checks = {c["id"]: c for c in card["checklist"]}
    assert checks["online_mode"]["ok"] is False and "without a Minecraft account" in checks["online_mode"]["hint"]
    assert card["protection"]["on"] is False and checks["protection"]["ok"] is False  # (names can be faked)
    fake_agent.put_file(inst.id, "server.properties", "white-list=true\nonline-mode=true\n")
    card = clients["owner"].get(f"/api/instances/{inst.id}/share").json()
    checks = {c["id"]: c for c in card["checklist"]}
    assert checks["online_mode"] == {**checks["online_mode"], "ok": True, "hint": ""} and card["protection"]["on"] is True


def test_the_open_warning_follows_offline_mode(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    from apps.instances.share import OFFLINE_WARNING

    inst = make_instance()
    for whitelist in ("true", "false"):
        fake_agent.put_file(inst.id, "server.properties", f"white-list={whitelist}\nonline-mode=false\n")
        card = clients["owner"].get(f"/api/instances/{inst.id}/share").json()
        assert card["open_warning"] == OFFLINE_WARNING and "Minecraft account who" not in card["open_warning"]
        protection = {c["id"]: c for c in card["checklist"]}["protection"]
        assert protection["ok"] is False and "without a Minecraft account" in protection["hint"]
    fake_agent.put_file(inst.id, "server.properties", "white-list=false\nonline-mode=true\n")
    card = clients["owner"].get(f"/api/instances/{inst.id}/share").json()
    assert card["open_warning"].startswith("Anyone with a Minecraft account who knows the address")
    assert {c["id"]: c for c in card["checklist"]}["protection"]["hint"] == card["open_warning"]
