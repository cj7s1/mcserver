"""Put back the servers of other games that ``upgrade.sh rollback --archive-other-games`` archived.

A rollback to a release from before multi-game undoes 0005/0006 (the columns that say what a server
is) and metrics 0002. Before that, ``manage.py rollback_other_games --archive`` stops and archives
every server of another game and copies its row into the table ``mcpanel_rollback_other_games``,
which no migration manages: it outlives the rollback and is in every database dump. When the panel
is upgraded again, 0005 and 0006 come back with their defaults (every server "minecraft", no
ports, ...) and this migration puts back what the table recorded: the game and its variant, named
ports, pinned options, secrets (the stored ciphertext), setup state, control secret, game port and
the other fields the old release may have changed for a server it took for Minecraft. The servers
stay archived (also one the owner unarchived meanwhile) for the owner to unarchive; the schedules
the archive step disabled are enabled again, and player names longer than 16 characters
(``mcpanel_rollback_player_names``) go back into the player history. Servers deleted meanwhile are
skipped. Then both tables are dropped. Without the tables (every other installation) it does
nothing, and running it again does nothing either.

Undone (on the way back, before 0006 brings back the unique index on (node, game_port) for every
server), it gives each recorded server that shares its game port with another server of the node
the free number above 65535 the archive step chose (``rollback_game_port``).
"""

from __future__ import annotations

import json
from typing import Any

from django.db import migrations

SIDE_TABLE = "mcpanel_rollback_other_games"
SIDE_PLAYERS = "mcpanel_rollback_player_names"
TEXT_COLUMNS = (
    "game",
    "edition",
    "server_type",
    "minecraft_version",
    "loader_version",
    "image",
    "secrets",
    "rcon_password",
)
JSON_COLUMNS = ("ports", "game_options", "setup_state", "env", "extra_ports")
INT_COLUMNS = ("game_port",)


def _tables(connection: Any) -> set[str]:
    with connection.cursor() as cur:
        return set(connection.introspection.table_names(cur))


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _as_ids(value: Any) -> list[int]:
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return [int(v) for v in value or [] if isinstance(v, int) or str(v).isdigit()]


def restore(apps: Any, schema_editor: Any) -> None:
    connection = schema_editor.connection
    tables = _tables(connection)
    with connection.cursor() as cur:
        if SIDE_TABLE in tables:
            cur.execute(f"SELECT instance_id, data, schedules_disabled FROM {SIDE_TABLE}")  # noqa: S608
            for instance_id, data, schedules in cur.fetchall():
                row = _as_dict(data)
                cur.execute("SELECT 1 FROM instances_instance WHERE id = %s", [instance_id])
                if cur.fetchone() is None:
                    continue  # deleted while the old release ran
                sets: list[str] = []
                params: list[Any] = []
                for column in TEXT_COLUMNS:
                    if isinstance(row.get(column), str):
                        sets.append(f"{column} = %s")
                        params.append(row[column])
                for column in JSON_COLUMNS:
                    if column in row and row[column] is not None:
                        sets.append(f"{column} = %s::jsonb")
                        params.append(json.dumps(row[column]))
                for column in INT_COLUMNS:
                    if isinstance(row.get(column), int):
                        sets.append(f"{column} = %s")
                        params.append(row[column])
                sets += [
                    "archived_at = COALESCE(archived_at, now())",
                    "status = 'archived'",
                    "desired_state = 'stopped'",
                    "resume_after_boot = false",
                    "resume_flagged_at = NULL",
                    "container_spec_hash = ''",
                ]
                cur.execute(f"UPDATE instances_instance SET {', '.join(sets)} WHERE id = %s", [*params, instance_id])  # noqa: S608
                ids = _as_ids(schedules)
                if ids:
                    cur.execute(
                        "UPDATE instances_schedule SET enabled = true WHERE instance_id = %s AND id = ANY(%s)",
                        [instance_id, ids],
                    )
            cur.execute(f"DROP TABLE {SIDE_TABLE}")
        if SIDE_PLAYERS in tables:
            cur.execute(
                "INSERT INTO metrics_playerseen (instance_id, name, first_seen, last_seen, minutes_online, sessions) "  # noqa: S608
                f"SELECT p.instance_id, p.name, p.first_seen, p.last_seen, p.minutes_online, p.sessions FROM {SIDE_PLAYERS} p "
                "WHERE EXISTS (SELECT 1 FROM instances_instance i WHERE i.id = p.instance_id) "
                "ON CONFLICT (instance_id, name) DO NOTHING"
            )
            cur.execute(f"DROP TABLE {SIDE_PLAYERS}")


def give_rollback_ports(apps: Any, schema_editor: Any) -> None:
    connection = schema_editor.connection
    if SIDE_TABLE not in _tables(connection):
        return
    with connection.cursor() as cur:
        cur.execute(
            f"UPDATE instances_instance i SET game_port = s.rollback_game_port FROM {SIDE_TABLE} s "  # noqa: S608
            "WHERE i.id = s.instance_id AND s.rollback_game_port IS NOT NULL"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0006_games_phase2"),
        # the player names it puts back need the wider column
        ("metrics", "0002_games_phase2"),
    ]

    operations = [
        migrations.RunPython(restore, give_rollback_ports),
    ]
