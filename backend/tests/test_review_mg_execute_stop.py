# ruff: noqa: F811 (the fixtures imported from other test modules are parameters of the tests)
"""Minecraft: ``execute … run stop`` is a stop command too (real-stack run, log 82).

``POST /command`` and command schedules refused ``stop`` (400 ``stop_via_power``), but
``execute run stop`` passed, stopped the server over RCON, and Docker started it again. The final
``run`` target of a line decides: the REST endpoint, schedules (new and saved earlier) and the
console's typed-stop path treat it like ``stop``. Lines that only mention the words still run.
"""

from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async

from apps import games
from apps.audit.models import AuditEvent
from apps.instances.models import Instance, Schedule
from apps.instances.spec import build_spec

from .test_web_audit_2026_11_09 import _close_db, _ws_setup, ws_env  # noqa: F401

STOPS = [
    "execute run stop",
    "/execute run stop",
    "execute run /stop",
    "//EXECUTE   Run   STOP  ",
    "execute as @a at @s run execute run stop",
    'execute as @a[name="x run say"] if entity @s run stop',
    'execute if score "p obj matches 1 run stop',  # an unquoted score holder that starts with a quote
    'execute as @e[name=" run "] run stop',  # the word "run" inside a quoted selector argument
    "minecraft:execute run minecraft:stop",
    "minecraft:stop",
    "/Minecraft:Stop",
    "return run stop",
    "execute run return run stop",
]
NOT_STOPS = [
    "say stop",
    "execute run say stop",
    'tellraw @a "run stop"',
    "execute run stop now",
    "say execute run",
    "execute stop",
    "stopwatch",
    "execute run stopwatch",
    "run stop",
]


@pytest.mark.parametrize("text", STOPS)
def test_minecraft_final_run_target_stop_is_a_stop_command(text):
    from apps.console.consumers import is_stop_command

    assert games.get("minecraft").is_stop_command(text), text
    assert is_stop_command(text), text  # the console's helper (Minecraft by default)


@pytest.mark.parametrize("text", NOT_STOPS)
def test_minecraft_lines_that_mention_stop_are_not_stop_commands(text):
    assert not games.get("minecraft").is_stop_command(text), text


def test_other_games_keep_their_stop_words():
    # Factorio and Terraria have no command that runs another command: only their own words count
    factorio, terraria = games.get("factorio"), games.get("terraria")
    assert factorio.is_stop_command("/quit") and not factorio.is_stop_command("execute run stop")
    assert terraria.is_stop_command("exit") and not terraria.is_stop_command("execute run stop")


def _running(make_instance, fake_agent):
    inst = make_instance()
    fake_agent.apply(inst.id, build_spec(inst), start=True)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    inst.refresh_from_db()
    return inst


@pytest.mark.parametrize("command", ["execute run stop", "/execute as @a at @s run execute run /STOP"])
def test_command_endpoint_refuses_execute_run_stop(clients, make_instance, fake_agent, command):
    inst = _running(make_instance, fake_agent)
    resp = clients["owner"].post(f"/api/instances/{inst.id}/command", {"command": command}, format="json")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "stop_via_power", resp.json()
    assert not [c for c in fake_agent.commands if c[0] == str(inst.id)]
    # a command that only says the words still runs
    resp = clients["owner"].post(f"/api/instances/{inst.id}/command", {"command": "execute run say stop"}, format="json")
    assert resp.status_code == 200, resp.json()


def test_execute_run_stop_schedule_is_refused_and_one_saved_earlier_is_skipped(clients, make_instance, fake_agent):
    from apps.instances import scheduling

    inst = _running(make_instance, fake_agent)
    resp = clients["owner"].post(
        f"/api/instances/{inst.id}/schedules",
        {"name": "x", "cron": "0 4 * * *", "action": "command", "payload": {"command": "execute run stop"}},
        format="json",
    )
    assert resp.status_code == 400 and "stop schedule" in str(resp.json()), resp.json()
    sched = Schedule.objects.create(
        instance=inst, name="old", cron="0 4 * * *", action="command", payload={"command": "execute as @a run stop"}
    )
    assert scheduling.execute(sched).startswith("Refused")
    assert not [c for c in fake_agent.commands if c[0] == str(inst.id)]


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_console_execute_run_stop_becomes_the_stop_job(ws_env):
    from apps.jobs.models import Job

    from .test_security_audit_authz import WsClient

    inst, _user, key = await sync_to_async(_ws_setup)(
        ["instance.view", "instance.console", "instance.command", "instance.control"]
    )
    comm = WsClient(f"/ws/instances/{inst.id}/console/", key)
    assert (await comm.connect())[0]
    await comm.receive_json_from()  # hello
    await comm.send_json_to({"type": "command", "id": "c1", "command": "/execute as @a at @s run execute run stop"})
    notice = await comm.receive_json_from()
    result = await comm.receive_json_from()
    assert notice["type"] == "notice" and notice["level"] == "info" and "Stop button" in notice["message"]
    assert result["ok"] is True and result["id"] == "c1"
    assert ws_env == []  # nothing went to RCON
    job = await sync_to_async(lambda: Job.objects.get(instance=inst))()
    assert job.kind == "stop" and notice["job_id"] == str(job.id)
    assert await sync_to_async(lambda: AuditEvent.objects.filter(action="instance.stop").count())() == 1
    # a line that only mentions the words goes to the server
    await comm.send_json_to({"type": "command", "id": "c2", "command": "execute run say stop"})
    result = await comm.receive_json_from()
    assert result["ok"] is True and ws_env == ["execute run say stop"]
    await comm.disconnect()
    await _close_db()
