"""Backup safeguards: the backup overview fields of the instance payload, the create wizard's
"Nightly backup" option and the ``ensure_backup_schedules`` management command."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import InstanceAccess, Role
from apps.audit.models import AuditEvent
from apps.backups.models import Backup, BackupKind, BackupStatus
from apps.instances import services
from apps.instances.models import Instance, Schedule
from apps.jobs.models import Job, JobStatus

pytestmark = pytest.mark.django_db

VALID = {"name": "Survival", "accept_eula": True, "server_type": "VANILLA", "minecraft_version": "1.20.4", "memory_mb": 1024}
NIGHTLY_CRON = re.compile(r"^(\d{1,2}) ([34]) \* \* \*$")


def create(client, **overrides):
    return client.post("/api/instances/", {**VALID, **overrides}, format="json")


def backup(instance, *, age: timedelta, status: str = BackupStatus.SUCCEEDED, kind: str = BackupKind.MANUAL) -> Backup:
    row = Backup.objects.create(instance=instance, name=f"{kind} {status}", kind=kind, status=status, snapshot_id="ab" * 32)
    Backup.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)  # created_at is auto_now_add
    row.refresh_from_db()
    return row


def schedule(instance, **fields) -> Schedule:
    values = {"name": "s", "action": "backup", "cron": "0 4 * * *"}
    values.update(fields)
    return Schedule.objects.create(instance=instance, **values)


def custom_role(key: str, *codes: str) -> Role:
    return Role.objects.create(key=key, name=key, permissions=list(codes))


def listed(client, instance, query: str = "") -> dict:
    return next(row for row in client.get(f"/api/instances/{query}").json() if row["id"] == str(instance.id))


# =====================================================================================
# 1. last_backup_at / backup_schedules in the instance payload
# =====================================================================================
def test_a_server_without_backups_or_schedules_reports_none(clients, make_instance):
    inst = make_instance()
    for data in (clients["owner"].get(f"/api/instances/{inst.id}").json(), listed(clients["owner"], inst)):
        assert data["last_backup_at"] is None
        assert data["backup_schedules"] == 0


def test_last_backup_is_the_newest_successful_one_of_any_kind(clients, make_instance, settings):
    settings.TIME_ZONE = "America/Los_Angeles"  # the panel's TZ: timestamps carry its offset, not "Z"
    inst, other = make_instance(), make_instance()
    backup(inst, age=timedelta(days=20), kind=BackupKind.SCHEDULED)
    newest = backup(inst, age=timedelta(days=9), kind=BackupKind.PRE_UPDATE)  # a safety backup counts too
    backup(inst, age=timedelta(days=3), status=BackupStatus.FAILED)
    backup(inst, age=timedelta(days=2), status=BackupStatus.DELETED)  # pruned by retention
    backup(inst, age=timedelta(hours=1), status=BackupStatus.RUNNING)
    backup(other, age=timedelta(minutes=5))  # another server's backup is not this server's

    detail = clients["owner"].get(f"/api/instances/{inst.id}").json()
    row = listed(clients["owner"], inst)
    assert detail["last_backup_at"] == row["last_backup_at"]  # the annotation and the single lookup agree
    assert datetime.fromisoformat(detail["last_backup_at"]) == newest.created_at
    # rendered like the model's own timestamps (panel timezone), not as a second format
    offset = re.compile(r"-0[78]:00$")
    assert (
        offset.search(detail["created_at"]) and offset.search(detail["last_backup_at"]) and offset.search(row["last_backup_at"])
    )


def test_backup_schedules_counts_enabled_backup_schedules_only(clients, make_instance):
    inst, other = make_instance(), make_instance()
    schedule(inst, name="nightly")
    schedule(inst, name="weekly", cron="30 3 * * 0")
    schedule(inst, name="paused", enabled=False)
    schedule(inst, name="snap", action="snapshot", cron="0 * * * *")  # same disk: not a backup
    schedule(inst, name="restart", action="restart", cron="0 5 * * *")
    schedule(other, name="nightly")
    for inst_, expected in ((inst, 2), (other, 1)):
        assert clients["owner"].get(f"/api/instances/{inst_.id}").json()["backup_schedules"] == expected
        assert listed(clients["owner"], inst_)["backup_schedules"] == expected


def test_last_backup_time_needs_the_backup_permission(clients, make_instance, make_user, client_for, roles):
    inst = make_instance()
    backup(inst, age=timedelta(days=1))
    schedule(inst)
    granted = make_user("granted")
    InstanceAccess.objects.create(user=granted, instance=inst, role=roles["operator"])
    for who, sees in ((clients["operator"], True), (client_for(granted), True), (clients["viewer"], False)):
        for data in (who.get(f"/api/instances/{inst.id}").json(), listed(who, inst)):
            assert (data["last_backup_at"] is not None) is sees
            assert data["backup_schedules"] == 1  # schedules are visible to everyone who sees the server


def test_archived_servers_carry_the_fields_too(clients, make_instance):
    inst = make_instance(archived_at=timezone.now(), status="archived")
    backup(inst, age=timedelta(days=30))
    row = listed(clients["owner"], inst, "?archived=true")
    assert row["last_backup_at"] is not None and row["backup_schedules"] == 0


@pytest.mark.parametrize("who", ["granted", "global_operator", "owner"])
def test_instance_list_query_count_does_not_grow_with_backups_and_schedules(
    make_user, client_for, make_instance, roles, users, who
):
    user = make_user("granted") if who == "granted" else users["operator" if who == "global_operator" else "owner"]
    c = client_for(user)

    def add(n: int) -> None:
        for _ in range(n):
            inst = make_instance()
            InstanceAccess.objects.create(user=user, instance=inst, role=roles["operator"])
            for days in (1, 2, 3):
                backup(inst, age=timedelta(days=days))
            schedule(inst, name="nightly")
            schedule(inst, name="weekly", cron="30 3 * * 0")

    def count() -> int:
        with CaptureQueriesContext(connection) as ctx:
            resp = c.get("/api/instances/")
        assert resp.status_code == 200
        assert all(row["last_backup_at"] and row["backup_schedules"] == 2 for row in resp.json())
        return len(ctx.captured_queries)

    add(1)
    one = count()
    add(5)
    assert count() == one


# =====================================================================================
# 2. The nightly backup slot
# =====================================================================================
def test_nightly_slot_is_between_three_and_five_and_never_on_a_round_minute():
    crons = {services.nightly_backup_cron(uuid.UUID(int=n)) for n in range(500)}
    slots = set()
    for cron in crons:
        match = NIGHTLY_CRON.match(cron)
        assert match, cron
        minute, hour = int(match[1]), int(match[2])
        assert hour in (3, 4) and 1 <= minute <= 59 and minute % 5 != 0, cron
        slots.add((hour, minute))
    assert slots == set(services.NIGHTLY_BACKUP_SLOTS)  # every slot is used: servers spread over both hours


def test_nightly_slot_is_stable_per_server_and_differs_between_servers():
    a, b = uuid.UUID("4193b1f0-0000-4000-8000-000000000001"), uuid.UUID("4193b1f0-0000-4000-8000-000000000002")
    assert services.nightly_backup_cron(a) == services.nightly_backup_cron(str(a))
    assert services.nightly_backup_cron(a) != services.nightly_backup_cron(b)
    random_ids = [uuid.uuid4() for _ in range(40)]
    assert len({services.nightly_backup_cron(i) for i in random_ids}) > 20


@pytest.mark.parametrize(
    ("expr", "minutes"),
    [
        ("0 4 * * *", {0}),
        ("15,45 3-4 * * 1-5", {15, 45}),
        ("*/20 * * * *", {0, 20, 40}),
        ("* * * * *", set(range(60))),
        ("not a cron", set()),
    ],
)
def test_cron_minutes(expr, minutes):
    assert services.cron_minutes(expr) == minutes


# =====================================================================================
# 3. The create wizard's "Nightly backup" option
# =====================================================================================
def test_create_with_nightly_backup_adds_the_schedule(clients, node, fake_agent, mojang_java):
    resp = create(clients["admin"], nightly_backup=True)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    inst = Instance.objects.get(pk=body["instance"]["id"])
    sched = Schedule.objects.get(instance=inst)
    # like the Schedules page's "Nightly backup" template, at this server's own minute, and also
    # while the server is stopped (so a stopped or imported server gets its first backup too)
    assert (sched.name, sched.action, sched.payload, sched.enabled, sched.only_when_running) == (
        "Nightly backup",
        "backup",
        {},
        True,
        False,
    )
    assert sched.cron == services.nightly_backup_cron(inst.id)
    assert NIGHTLY_CRON.match(sched.cron) and not sched.cron.startswith("0 ")
    assert sched.next_run_at is None  # filled in by the scheduler tick, as for a schedule made by hand
    assert body["instance"]["backup_schedules"] == 1
    assert body["backup_schedule"]["id"] == sched.id and body["backup_schedule"]["cron"] == sched.cron
    event = AuditEvent.objects.get(action="schedule.created")
    assert event.actor_username == "admin" and event.instance_id == inst.id
    assert event.details["cron"] == sched.cron and event.details["via"] == "server_created"
    listing = clients["admin"].get(f"/api/instances/{inst.id}/schedules").json()
    assert [s["name"] for s in listing] == ["Nightly backup"]


@pytest.mark.parametrize("overrides", [{}, {"nightly_backup": False}])
def test_create_without_the_option_adds_no_schedule(clients, node, fake_agent, mojang_java, overrides):
    resp = create(clients["owner"], **overrides)
    assert resp.status_code == 201, resp.content
    assert resp.json()["backup_schedule"] is None
    assert resp.json()["instance"]["backup_schedules"] == 0
    assert not Schedule.objects.exists()
    assert not AuditEvent.objects.filter(action="schedule.created").exists()


def test_imported_server_gets_the_schedule_too(clients, node, fake_agent, mojang_java):
    # the wizard's import mode is a normal create that stays stopped; the archive is uploaded afterwards
    resp = create(clients["owner"], start=False, nightly_backup=True)
    assert resp.status_code == 201, resp.content
    assert Schedule.objects.filter(instance_id=resp.json()["instance"]["id"], action="backup", enabled=True).count() == 1


def test_creator_without_global_view_gets_the_schedule_through_the_manager_grant(
    make_user, client_for, node, fake_agent, mojang_java
):
    creator = make_user("creator", role=custom_role("creator", "instance.create"))
    resp = create(client_for(creator), nightly_backup=True)
    assert resp.status_code == 201, resp.content
    inst = Instance.objects.get(pk=resp.json()["instance"]["id"])
    assert InstanceAccess.objects.get(user=creator, instance=inst).role.key == "server_manager"
    assert Schedule.objects.filter(instance=inst, action="backup").count() == 1


@pytest.mark.parametrize(
    "codes",
    [
        ("instance.create", "instance.view"),
        ("instance.create", "instance.view", "instance.backup"),  # may back up, but not manage schedules
        ("instance.create", "instance.view", "instance.schedules"),  # may manage schedules, but not back up
    ],
)
def test_create_skips_the_schedule_silently_without_the_schedule_permissions(
    make_user, client_for, node, fake_agent, mojang_java, codes
):
    creator = make_user("limited", role=custom_role("limited", *codes))
    resp = create(client_for(creator), nightly_backup=True)
    assert resp.status_code == 201, resp.content  # the server is still created
    body = resp.json()
    assert body["backup_schedule"] is None and body["instance"]["backup_schedules"] == 0
    assert Instance.objects.filter(pk=body["instance"]["id"]).exists()
    assert not Schedule.objects.exists()
    assert not AuditEvent.objects.filter(action="schedule.created").exists()
    # exactly the rule of creating it by hand
    by_hand = client_for(creator).post(
        f"/api/instances/{body['instance']['id']}/schedules",
        {"name": "Nightly backup", "action": "backup", "cron": "17 3 * * *"},
        format="json",
    )
    assert by_hand.status_code == 403


def test_the_created_schedule_takes_a_backup_that_shows_in_the_payload(
    clients, node, fake_agent, mojang_java, django_capture_on_commit_callbacks
):
    from apps.instances.scheduling import execute

    with django_capture_on_commit_callbacks(execute=True):
        resp = create(clients["owner"], start=False, nightly_backup=True)
    inst_id = resp.json()["instance"]["id"]
    assert clients["owner"].get(f"/api/instances/{inst_id}").json()["last_backup_at"] is None
    sched = Schedule.objects.select_related("instance").get(instance_id=inst_id)
    with django_capture_on_commit_callbacks(execute=True):
        assert execute(sched).startswith("Queued job")
    job = Job.objects.get(instance_id=inst_id, kind="backup")
    assert job.status == JobStatus.SUCCEEDED, job.error
    taken = Backup.objects.get(instance_id=inst_id)
    assert taken.kind == BackupKind.SCHEDULED and taken.status == BackupStatus.SUCCEEDED
    data = clients["owner"].get(f"/api/instances/{inst_id}").json()
    assert datetime.fromisoformat(data["last_backup_at"]) == taken.created_at


# =====================================================================================
# 4. manage.py ensure_backup_schedules
# =====================================================================================
def ensure(*args: str) -> list[str]:
    out = StringIO()
    call_command("ensure_backup_schedules", *args, stdout=out)
    return out.getvalue().splitlines()


@pytest.fixture
def fleet(make_instance):
    return {
        "bare": make_instance(name="Bare"),
        "scheduled": make_instance(name="Has backup"),
        "paused": make_instance(name="Paused backup"),
        "snapshots": make_instance(name="Snapshots only"),
        "archived": make_instance(name="Old", archived_at=timezone.now(), status="archived"),
    }


@pytest.fixture
def fleet_schedules(fleet):
    schedule(fleet["scheduled"], name="Weekly backup", cron="30 2 * * 0")
    schedule(fleet["paused"], name="Nightly backup", enabled=False)
    schedule(fleet["snapshots"], name="Hourly snapshot", action="snapshot", cron="0 * * * *")
    return set(Schedule.objects.values_list("pk", flat=True))


def test_ensure_backup_schedules_adds_one_where_none_is_enabled(fleet, fleet_schedules):
    lines = ensure()
    per_server, summary = lines[:-1], lines[-1]
    assert len(per_server) == len(fleet)  # one line per server
    by_name = {name: next(line for line in per_server if line.startswith(f"{inst.name} (")) for name, inst in fleet.items()}
    for name in ("bare", "paused", "snapshots"):
        inst = fleet[name]
        added = Schedule.objects.exclude(pk__in=fleet_schedules).get(instance=inst)
        assert (added.name, added.action, added.payload, added.enabled) == ("Nightly backup", "backup", {}, True)
        assert NIGHTLY_CRON.match(added.cron)
        assert "added 'Nightly backup'" in by_name[name] and f"({added.cron})" in by_name[name]
        assert str(inst.id) in by_name[name]
    assert "1 paused backup schedule(s) left paused" in by_name["paused"]
    assert "already has backup schedule 'Weekly backup' (30 2 * * 0)" in by_name["scheduled"]
    assert by_name["archived"].endswith("archived, skipped")
    new = Schedule.objects.exclude(pk__in=fleet_schedules)
    assert new.count() == 3
    assert not new.filter(instance__in=[fleet["scheduled"], fleet["archived"]]).exists()
    assert summary.startswith("Added 3 schedule(s); 1 server(s) already had one; 1 archived server(s) skipped.")

    # audited, actor: system
    events = AuditEvent.objects.filter(action="schedule.created")
    assert events.count() == 3
    for event in events:
        assert event.actor is None and event.actor_username == ""
        assert event.target_type == "schedule" and event.details["via"] == "ensure_backup_schedules"
        assert event.details["cron"] == Schedule.objects.get(pk=event.target_id).cron
    assert {e.instance_id for e in events} == {fleet[n].id for n in ("bare", "paused", "snapshots")}

    # running it again changes nothing
    again = ensure()
    assert again[-1].startswith("Added 0 schedule(s); 4 server(s) already had one; 1 archived server(s) skipped.")
    assert Schedule.objects.exclude(pk__in=fleet_schedules).count() == 3
    assert AuditEvent.objects.filter(action="schedule.created").count() == 3


def test_ensure_backup_schedules_dry_run_changes_nothing(fleet, fleet_schedules):
    lines = ensure("--dry-run")
    assert lines[-1] == "Dry run: nothing was changed."
    assert lines[-2].startswith("Would add 3 schedule(s); 1 server(s) already had one; 1 archived server(s) skipped.")
    assert sum("would add 'Nightly backup'" in line for line in lines) == 3
    assert not any(": added " in line for line in lines)
    assert set(Schedule.objects.values_list("pk", flat=True)) == fleet_schedules
    assert not AuditEvent.objects.exists()
    # the dry run announces exactly what the real run then does
    announced = {line.split(": would add ")[0]: line.split(": would add ")[1] for line in lines if ": would add " in line}
    real = {line.split(": added ")[0]: line.split(": added ")[1] for line in ensure() if ": added " in line}
    assert announced == real and len(real) == 3


def test_ensure_backup_schedules_avoids_minutes_of_the_servers_other_schedules(make_instance):
    # id 0 -> the first slot (03:01); its minute and the next ones are taken by other schedules
    inst = make_instance(id=uuid.UUID(int=0))
    assert services.nightly_backup_cron(inst.id) == "1 3 * * *"
    schedule(inst, name="Hourly snapshot", action="snapshot", cron="1 * * * *")
    schedule(inst, name="Restart", action="restart", cron="2 6 * * *")  # another hour, same minute of the hour
    schedule(inst, name="Paused command", action="command", cron="3,4 3 * * *", enabled=False, payload={"command": "list"})
    free = make_instance(id=uuid.UUID(int=len(services.NIGHTLY_BACKUP_SLOTS)))  # same slot, nothing in the way
    lines = ensure()
    added = Schedule.objects.get(instance=inst, action="backup")
    assert added.cron == "6 3 * * *"  # 03:05 is never used (round minute), 03:06 is the next free slot
    assert Schedule.objects.get(instance=free, action="backup").cron == "1 3 * * *"
    assert any("daily at 03:06 (6 3 * * *)" in line for line in lines)
    used_by_others = set()
    for cron in Schedule.objects.filter(instance=inst).exclude(pk=added.pk).values_list("cron", flat=True):
        used_by_others |= services.cron_minutes(cron)
    assert int(added.cron.split()[0]) not in used_by_others


def test_ensure_backup_schedules_wraps_around_and_reports_when_no_minute_is_free(make_instance):
    last = len(services.NIGHTLY_BACKUP_SLOTS) - 1
    wraps = make_instance(name="Wraps", id=uuid.UUID(int=last))  # last slot: 04:59
    assert services.nightly_backup_cron(wraps.id) == "59 4 * * *"
    schedule(wraps, name="At 59", action="restart", cron="59 * * * *")
    busy = make_instance(name="Every minute", id=uuid.UUID(int=last + 1))
    schedule(busy, name="Ticker", action="command", cron="* * * * *", payload={"command": "list"})
    lines = ensure()
    assert Schedule.objects.get(instance=wraps, action="backup").cron == "1 3 * * *"  # back to the first slot
    assert Schedule.objects.get(instance=busy, action="backup").cron == "1 3 * * *"  # its own slot is kept
    line = next(line for line in lines if line.startswith("Every minute ("))
    assert "every minute is used by this server's other schedules" in line
