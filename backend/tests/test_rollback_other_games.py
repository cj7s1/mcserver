"""Rollback to the release before multi-game: `manage.py rollback_other_games` and migration
instances 0007, on the real migrations of the test database.

`upgrade.sh rollback` undoes instances 0006/0005 and metrics 0002 when it goes back to that release.
That drops what a server of another game is (game, ports, options, secrets, setup state), and the
unique (node, game_port) index it brings back refuses a UDP and a TCP server sharing a number. The
command lists those servers (the check of the rollback, also its dry run) and, with --archive,
stops and archives them through the panel's own jobs and records them in a table no migration
manages; 0007 gives the port numbers while the old release runs and puts everything back at the
next upgrade. tests/test_upgrade_script.py tests how upgrade.sh uses the command.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.instances.models import Instance, Schedule
from apps.jobs.models import Job, JobStatus
from apps.metrics.models import PlayerSeen

SIDE_TABLE = "mcpanel_rollback_other_games"
SIDE_PLAYERS = "mcpanel_rollback_player_names"
OLD = [("instances", "0004_schedule_deferred_at"), ("metrics", "0001_initial")]
UNDO = ["--undo", "instances=0004_schedule_deferred_at", "--undo", "metrics=0001_initial"]
LONG_NAME = "a-factorio-engineer-name"  # 24 characters: more than the old column takes


def latest() -> list[tuple[str, str]]:
    executor = MigrationExecutor(connection)
    return [key for key in executor.loader.graph.leaf_nodes() if key[0] in ("instances", "metrics")]


def migrate(targets: list[tuple[str, str]]) -> None:
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)


def tables() -> set[str]:
    with connection.cursor() as cur:
        return set(connection.introspection.table_names(cur))


def columns(table: str) -> set[str]:
    with connection.cursor() as cur:
        return {c.name for c in connection.introspection.get_table_description(cur, table)}


def run(*args: str) -> tuple[str, CommandError | None]:
    from io import StringIO

    out = StringIO()
    try:
        call_command("rollback_other_games", *args, stdout=out, stderr=StringIO())
    except CommandError as exc:
        return out.getvalue(), exc
    return out.getvalue(), None


@pytest.fixture
def schema_back_to_latest(transactional_db: Any) -> Iterator[None]:
    """Whatever a test does to the schema, the next test finds the latest one and no side table."""
    yield
    migrate(latest())
    with connection.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SIDE_TABLE}")
        cur.execute(f"DROP TABLE IF EXISTS {SIDE_PLAYERS}")


def factorio_fields(**more: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "name": "Factory",
        "game": "factorio",
        "server_type": "",
        "minecraft_version": "2.1.23",
        "image": "factoriotools/factorio:2.1.23-rootless",
        "memory_mb": 2048,
        "stop_timeout": 120,
        "jvm_flags_preset": "none",
        "game_port": 25600,
        "ports": [{"name": "game", "container_port": 34197, "protocol": "udp", "host_port": 25600}],
        "game_options": {"space_age": True, "save_name": "world"},
        "setup_state": {"steps": {"x": {"status": "done", "at": "2026-11-01T00:00:00Z", "message": ""}}},
        "rcon_password": "r" * 32,
    }
    fields.update(more)
    return fields


def tshock_fields(**more: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "name": "Terra",
        "game": "terraria",
        "server_type": "tshock",
        "minecraft_version": "1.4.4.9-5.2.4",
        "image": "ryshe/terraria:tshock-1.4.4.9-5.2.4",
        "game_port": 7777,
        "ports": [{"name": "game", "container_port": 7777, "protocol": "tcp", "host_port": 7777}],
        "game_options": {"world_name": "world"},
    }
    fields.update(more)
    return fields


# --- the check ----------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_check_lists_servers_of_other_games_and_shared_port_numbers_and_changes_nothing(make_instance):
    mc = make_instance(name="Survival", game_port=25600)
    factorio = make_instance(**factorio_fields())  # UDP 25600 next to Minecraft's TCP 25600: allowed now
    terra = make_instance(**tshock_fields(archived_at="2026-11-01T00:00:00Z"))
    out, error = run(*UNDO)
    assert error is not None and error.returncode == 3, out
    assert f"{factorio.id}  'Factory'  factorio" in out
    assert f"{terra.id}  'Terra'  terraria (tshock), archived already" in out
    assert str(mc.id) not in out
    assert "'Factory' (game port 25600) gets 65536 while the old version runs" in out
    # nothing was changed
    factorio.refresh_from_db()
    assert factorio.archived_at is None and factorio.game_port == 25600
    assert SIDE_TABLE not in tables() and not Job.objects.exists()


@pytest.mark.django_db
def test_nothing_to_do_without_other_games_or_when_the_game_column_stays(make_instance):
    make_instance(name="Survival", game_port=25600)
    out, error = run(*UNDO)
    assert error is None and "nothing to do" in out
    make_instance(**factorio_fields())
    # a rollback to a release that has the game column already keeps it
    out, error = run("--undo", "instances=0006_games_phase2")
    assert error is None and "keeps every server's game" in out


@pytest.mark.django_db
def test_a_job_that_holds_a_server_of_another_game_blocks_even_the_archive(make_instance):
    factorio = make_instance(**factorio_fields())
    job = Job.objects.create(kind="backup", title="Backup: Factory", instance=factorio, status=JobStatus.RUNNING, exclusive=True)
    Instance.objects.filter(pk=factorio.pk).update(active_job=job)
    out, error = run(*UNDO, "--archive")
    assert error is not None and error.returncode == 4, out
    assert "'Factory': an operation is in progress (Backup: Factory)" in out
    factorio.refresh_from_db()
    assert factorio.archived_at is None and SIDE_TABLE not in tables()


# --- archive, roll back, upgrade again ----------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_archive_then_rollback_then_upgrade_again_puts_the_servers_back_archived(
    make_instance, fake_agent, schema_back_to_latest
):
    from apps.instances.spec import build_spec

    mc = make_instance(name="Survival", game_port=25600)
    fake_agent.apply(mc.id, build_spec(mc), start=True)
    factorio = make_instance(**factorio_fields(resume_after_boot=True, desired_state="running"))
    factorio.set_secret_values({"token": "secret-token-value"})
    factorio.save(update_fields=["secrets"])
    fake_agent.apply(factorio.id, build_spec(factorio), start=True)
    terra = make_instance(**tshock_fields())  # stopped, no container; deleted while the old release runs
    nightly = Schedule.objects.create(instance=factorio, name="nightly", action="backup", cron="0 4 * * *")
    paused = Schedule.objects.create(instance=factorio, name="paused", action="restart", cron="0 5 * * *", enabled=False)
    PlayerSeen.objects.create(
        instance=factorio, name=LONG_NAME, first_seen="2026-11-01T00:00:00Z", last_seen="2026-11-02T00:00:00Z"
    )
    with connection.cursor() as cur:
        cur.execute("SELECT secrets, rcon_password FROM instances_instance WHERE id = %s", [factorio.pk])
        stored_secrets, stored_rcon = cur.fetchone()

    out, error = run(*UNDO, "--archive")
    assert error is None, out
    # the panel's own Stop job (the agent's graceful stop) and Archive job, one after the other
    assert [str(args[0]) for args, _ in fake_agent.called("stop")] == [str(factorio.id)]
    jobs = list(Job.objects.filter(instance=factorio).order_by("created_at").values_list("kind", "status"))
    assert jobs == [("stop", JobStatus.SUCCEEDED), ("archive", JobStatus.SUCCEEDED)]
    assert str(factorio.id) not in fake_agent.containers and fake_agent.containers[str(mc.id)]["running"]
    factorio.refresh_from_db()
    assert factorio.archived_at is not None and factorio.status == "archived"
    assert factorio.resume_after_boot is False and factorio.active_job_id is None
    assert not Schedule.objects.get(pk=nightly.pk).enabled and not Schedule.objects.get(pk=paused.pk).enabled
    assert "'Factory': stopped" in out and "1 schedule(s) disabled, game port 65536 while the old version runs" in out
    assert "'Terra': archived" in out and not PlayerSeen.objects.filter(name=LONG_NAME).exists()
    with connection.cursor() as cur:
        cur.execute(
            f"SELECT game, schedules_disabled, rollback_game_port, data FROM {SIDE_TABLE} WHERE instance_id = %s", [factorio.pk]
        )
        game, disabled, port, data = cur.fetchone()
    assert (game, port) == ("factorio", 65536)
    assert (json.loads(disabled) if isinstance(disabled, str) else disabled) == [nightly.pk]
    data = json.loads(data) if isinstance(data, str) else data
    assert data["secrets"] == stored_secrets and data["rcon_password"] == stored_rcon  # the ciphertext as stored
    assert "secret-token-value" not in json.dumps(data)

    # the rollback: without the archive step this fails (IntegrityError on unique_game_port_per_node)
    migrate(OLD)
    assert "game" not in columns("instances_instance") and "ports" not in columns("instances_instance")
    assert SIDE_TABLE in tables() and SIDE_PLAYERS in tables()
    with connection.cursor() as cur:
        cur.execute("SELECT game_port FROM instances_instance WHERE id = %s", [factorio.pk])
        assert cur.fetchone()[0] == 65536
        # what the old release may do meanwhile: take it for Minecraft, unarchive it, delete another server
        cur.execute(
            "UPDATE instances_instance SET server_type = 'VANILLA', image = 'itzg/minecraft-server:stable', "
            "env = '{\"TYPE\": \"PAPER\"}'::jsonb, archived_at = NULL, status = 'stopped' WHERE id = %s",
            [factorio.pk],
        )
        for table in ("audit_auditevent", "jobs_job"):
            cur.execute(f"UPDATE {table} SET instance_id = NULL WHERE instance_id = %s", [terra.pk])
        cur.execute("DELETE FROM instances_instance WHERE id = %s", [terra.pk])

    # upgrade again
    migrate(latest())
    factorio = Instance.objects.get(pk=factorio.pk)
    assert (
        factorio.game == "factorio" and factorio.server_type == "" and factorio.image == "factoriotools/factorio:2.1.23-rootless"
    )
    assert factorio.ports == factorio_fields()["ports"] and factorio.game_port == 25600
    assert factorio.game_options == {"space_age": True, "save_name": "world"}
    assert factorio.setup_state == factorio_fields()["setup_state"]
    assert factorio.secret_values == {"token": "secret-token-value"} and factorio.rcon_password == "r" * 32
    assert factorio.env == {}
    # still archived, for the owner to unarchive; the schedule the archive step disabled is back
    assert factorio.archived_at is not None and factorio.status == "archived" and factorio.desired_state == "stopped"
    assert Schedule.objects.get(pk=nightly.pk).enabled and not Schedule.objects.get(pk=paused.pk).enabled
    assert PlayerSeen.objects.filter(instance=factorio, name=LONG_NAME).exists()
    assert not Instance.objects.filter(pk=terra.pk).exists()  # deleted meanwhile: skipped
    assert Instance.objects.get(pk=mc.pk).game == "minecraft"
    assert SIDE_TABLE not in tables() and SIDE_PLAYERS not in tables()

    # running the migration once more (or on any other installation) changes nothing
    migrate([("instances", "0006_games_phase2")])
    migrate(latest())
    assert Instance.objects.get(pk=factorio.pk).game_port == 25600


@pytest.mark.django_db(transaction=True)
def test_a_record_left_by_a_rollback_that_never_undid_a_migration_is_replaced(make_instance, fake_agent, schema_back_to_latest):
    factorio = make_instance(**factorio_fields(game_options={"space_age": False, "save_name": "world"}))
    with connection.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {SIDE_TABLE} (instance_id uuid PRIMARY KEY, name varchar(80) NOT NULL, game varchar(32) NOT NULL, "
            "data jsonb NOT NULL, schedules_disabled jsonb NOT NULL DEFAULT '[]'::jsonb, rollback_game_port integer NULL, "
            "recorded_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute(
            f"INSERT INTO {SIDE_TABLE} (instance_id, name, game, data) VALUES (%s, 'Factory', 'factorio', %s::jsonb)",
            [factorio.pk, json.dumps({"game": "factorio", "game_options": {"stale": True}})],
        )
    out, error = run(*UNDO, "--archive")
    assert error is None, out
    with connection.cursor() as cur:
        cur.execute(f"SELECT data FROM {SIDE_TABLE} WHERE instance_id = %s", [factorio.pk])
        data = cur.fetchone()[0]
    data = json.loads(data) if isinstance(data, str) else data
    assert data["game_options"] == {"space_age": False, "save_name": "world"}


@pytest.mark.django_db(transaction=True)
def test_a_second_archive_after_a_rollback_that_stopped_half_way_keeps_the_first_record(
    make_instance, fake_agent, schema_back_to_latest
):
    make_instance(name="Survival", game_port=25600)
    factorio = make_instance(**factorio_fields())
    out, error = run(*UNDO, "--archive")
    assert error is None, out
    # the rollback undid 0007 (the rollback port is set) and then failed before 0005
    migrate([("instances", "0006_games_phase2")])
    assert Instance.objects.get(pk=factorio.pk).game_port == 65536
    nightly = Schedule.objects.create(instance=factorio, name="nightly", action="backup", cron="0 4 * * *")
    out, error = run(*UNDO, "--archive")  # the rollback once more
    assert error is None, out
    assert "'Factory': archived already, 1 schedule(s) disabled, recorded" in out
    with connection.cursor() as cur:
        cur.execute(f"SELECT data->>'game_port', rollback_game_port, schedules_disabled FROM {SIDE_TABLE}")
        port, rollback_port, disabled = cur.fetchone()
    assert (port, rollback_port) == ("25600", 65536)  # the port the server really has, from the first record
    assert (json.loads(disabled) if isinstance(disabled, str) else disabled) == [nightly.pk]
    migrate(OLD)
    migrate(latest())
    factorio = Instance.objects.get(pk=factorio.pk)
    assert factorio.game == "factorio" and factorio.game_port == 25600 and factorio.archived_at is not None
    assert Schedule.objects.get(pk=nightly.pk).enabled


# --- the guard of migrations 0005/0006 (apps/instances/migrations/_other_games_guard.py) ---------
def plan_to_old() -> str:
    from io import StringIO

    out = StringIO()
    call_command("migrate", "--plan", "instances", "0004_schedule_deferred_at", stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_the_plan_says_irreversible_while_servers_of_other_games_are_not_recorded(make_instance):
    make_instance(name="Survival", game_port=25600)
    assert "IRREVERSIBLE" not in plan_to_old()  # Minecraft servers only: every step can be undone
    factorio = make_instance(**factorio_fields())
    plan = plan_to_old()
    assert plan.count("Raw Python operation -> IRREVERSIBLE (other games)") == 2  # 0006 and 0005
    with connection.cursor() as cur:
        cur.execute(f"CREATE TABLE {SIDE_TABLE} (instance_id uuid PRIMARY KEY)")
        cur.execute(f"INSERT INTO {SIDE_TABLE} VALUES (%s)", [factorio.pk])
    plan = plan_to_old()
    assert "IRREVERSIBLE" not in plan and "Raw Python operation -> checked: other games are kept" in plan


@pytest.mark.django_db(transaction=True)
def test_undoing_0006_refuses_while_a_server_of_another_game_is_not_recorded(make_instance, schema_back_to_latest):
    factorio = make_instance(**factorio_fields())
    with pytest.raises(RuntimeError) as refused:
        migrate(OLD)
    assert "Undoing instances 0006" in str(refused.value) and f"{factorio.pk} 'Factory' (factorio)" in str(refused.value)
    assert "--archive-other-games" in str(refused.value)
    # nothing of what the server is was dropped
    assert {"game", "ports", "game_options", "secrets", "setup_state"} <= columns("instances_instance")
    factorio = Instance.objects.get(pk=factorio.pk)
    assert factorio.game == "factorio" and factorio.game_options == {"space_age": True, "save_name": "world"}
    # forward again: the guard does nothing (also on a database that has these migrations already)
    migrate(latest())
    assert MigrationExecutor(connection).migration_plan(latest()) == []
