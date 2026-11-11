"""Findings of the all-games real-stack run (2026-11-11) on the panel side: items 19, 21, 23."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command

from apps.instances.models import Instance
from apps.instances.spec import build_spec
from apps.jobs.models import JobStatus

from .test_games_satisfactory import _claimed_instance, run
from .test_terraria import make_tr


def _shutdown() -> str:
    out = io.StringIO()
    call_command("host_shutdown", "--timeout", "60", stdout=out)
    return out.getvalue()


# --- item 19: the Unraid hook's log of a clean Terraria stop --------------------------------------
def test_host_shutdown_counts_the_games_clean_exit_codes(make_instance, fake_agent):
    inst = make_tr(make_instance, "vanilla", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    original = fake_agent._set_exited
    fake_agent._set_exited = lambda c, _code: original(c, 143)  # the image's saving SIGTERM handler
    out = _shutdown()
    assert f"stopped   {inst.name} in" in out and "not a clean stop" not in out


def test_host_shutdown_still_reports_a_kill(make_instance, fake_agent):
    inst = make_tr(make_instance, "vanilla", desired_state="running")
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    original = fake_agent._set_exited
    fake_agent._set_exited = lambda c, _code: original(c, 137)
    assert "exit code 137: not a clean stop" in _shutdown()


# --- item 21: "stop" through the REST command endpoint and schedules ----------------------------------
@pytest.mark.parametrize("command", ["stop", "/stop", " STOP "])
def test_minecraft_stop_command_goes_through_power(clients, make_instance, fake_agent, command):
    inst = make_instance()
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    resp = clients["owner"].post(f"/api/instances/{inst.id}/command", {"command": command}, format="json")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "stop_via_power", resp.json()
    assert not [c for c in fake_agent.commands if c[0] == str(inst.id)]


def test_minecraft_other_commands_still_run(clients, make_instance, fake_agent):
    inst = make_instance()
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    resp = clients["owner"].post(f"/api/instances/{inst.id}/command", {"command": "say stopping soon"}, format="json")
    assert resp.status_code == 200, resp.json()


def test_minecraft_stop_schedule_is_refused(clients, make_instance):
    inst = make_instance()
    resp = clients["owner"].post(
        f"/api/instances/{inst.id}/schedules",
        {"name": "x", "cron": "0 4 * * *", "action": "command", "payload": {"command": "stop"}},
        format="json",
    )
    assert resp.status_code == 400, resp.json()
    assert "stop" in str(resp.json()).lower()


# --- item 23: the Satisfactory backup job says that it saved first -----------------------------------
def test_satisfactory_backup_log_says_it_saved_first(make_instance, fake_agent, django_capture_on_commit_callbacks):
    inst = _claimed_instance(make_instance, fake_agent, running=True)
    job = run("backup", inst, django_capture_on_commit_callbacks)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("saved" in line.lower() and "before" in line.lower() for line in job.log), job.log


def test_a_stop_command_schedule_saved_earlier_is_not_run(make_instance, fake_agent):
    from apps.instances import scheduling
    from apps.instances.models import Schedule

    inst = make_instance()
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    inst.refresh_from_db()
    sched = Schedule.objects.create(instance=inst, name="old", cron="0 4 * * *", action="command", payload={"command": "stop"})
    assert scheduling.execute(sched).startswith("Refused")
    assert not [c for c in fake_agent.commands if c[0] == str(inst.id)]


# --- item 25: integration runs can move their ports and pin the image channel ---------------------
def test_integration_port_range_can_be_overridden(monkeypatch):
    from .integration_support import it_ports

    assert it_ports((25600, 25650)) == (25600, 25650) or "MCPANEL_IT_PORTS" in __import__("os").environ
    monkeypatch.setenv("MCPANEL_IT_PORTS", "31000-31020")
    assert it_ports((25600, 25650)) == (31000, 31020)
    monkeypatch.setenv("MCPANEL_IT_PORTS", "80-90")
    with pytest.raises(AssertionError):
        it_ports((25600, 25650))
