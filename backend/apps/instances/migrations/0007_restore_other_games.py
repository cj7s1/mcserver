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
the free number above 65535 the archive step chose (``rollback_game_port``), and it parks every
recorded server on a node of its own, ``parked by upgrade.sh rollback (other games)``, whose agent
URL leads nowhere (``http://127.0.0.1:1/...``: refused at once, no name to resolve, no token sent).
The old release takes these servers for Minecraft servers: an Unarchive and a Start there would
run the Minecraft image in the folder of a Terraria or Factorio server. On the parked node every
job of the old release that would touch the server (start, recreate, clone, move to another node,
software update, delete) fails at its first request to the agent, before anything is changed. The
row keeps the id of its real node (``data->>'node_id'`` of the side table, the row as it was), so
going forward again moves it back there and removes the parked node once no server is on it (a
server whose node was deleted meanwhile stays on it, archived).
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
# The node the recorded servers are on while the old release runs (see above). Kept in step with
# rollback_other_games.PARKED_NODE_NAME and tests/test_rollback_other_games.py.
PARKED_NODE_NAME = "parked by upgrade.sh rollback (other games)"
PARKED_AGENT_URL = "http://127.0.0.1:1/parked-by-upgrade-rollback-other-games"
PARKED_NOTE = (
    "MCPanel: upgrade.sh rollback parked the servers of other games here (the version that runs does not know "
    "them). Its agent URL leads nowhere on purpose. The next './scripts/upgrade.sh run' moves them back to their "
    "node and removes this node."
)
# game_port of a parked server whose number another parked server has (as rollback_other_games)
ROLLBACK_PORT_BASE = 65536


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


def _parked_node_id(cur: Any) -> int | None:
    cur.execute("SELECT id FROM nodes_node WHERE name = %s", [PARKED_NODE_NAME])
    found = cur.fetchone()
    return int(found[0]) if found else None


def _node_back(cur: Any, row: dict[str, Any], current: int, parked: int | None) -> tuple[list[str], list[Any]]:
    """The real node of a parked server, when it still exists."""
    if parked is None or current != parked:
        return [], []
    try:
        node_id = int(row.get("node_id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [], []
    if node_id == parked:
        return [], []
    cur.execute("SELECT 1 FROM nodes_node WHERE id = %s", [node_id])
    if cur.fetchone() is None:
        return [], []  # deleted meanwhile: the server stays on the parked node, archived
    return ["node_id = %s"], [node_id]


def restore(apps: Any, schema_editor: Any) -> None:
    connection = schema_editor.connection
    tables = _tables(connection)
    with connection.cursor() as cur:
        if SIDE_TABLE in tables:
            parked = _parked_node_id(cur)
            cur.execute(f"SELECT instance_id, data, schedules_disabled FROM {SIDE_TABLE}")  # noqa: S608
            for instance_id, data, schedules in cur.fetchall():
                row = _as_dict(data)
                cur.execute("SELECT node_id FROM instances_instance WHERE id = %s", [instance_id])
                found = cur.fetchone()
                if found is None:
                    continue  # deleted while the old release ran
                sets, params = _node_back(cur, row, int(found[0]), parked)
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
            if parked is not None:
                cur.execute(
                    "DELETE FROM nodes_node n WHERE n.id = %s AND NOT EXISTS "
                    "(SELECT 1 FROM instances_instance i WHERE i.node_id = n.id)",
                    [parked],
                )
        if SIDE_PLAYERS in tables:
            cur.execute(
                "INSERT INTO metrics_playerseen (instance_id, name, first_seen, last_seen, minutes_online, sessions) "  # noqa: S608
                f"SELECT p.instance_id, p.name, p.first_seen, p.last_seen, p.minutes_online, p.sessions FROM {SIDE_PLAYERS} p "
                "WHERE EXISTS (SELECT 1 FROM instances_instance i WHERE i.id = p.instance_id) "
                "ON CONFLICT (instance_id, name) DO NOTHING"
            )
            cur.execute(f"DROP TABLE {SIDE_PLAYERS}")


def _parked_node(cur: Any) -> int:
    """The parked node (made, or the one a former rollback left), pointed nowhere again."""
    parked = _parked_node_id(cur)
    if parked is not None:
        cur.execute(
            "UPDATE nodes_node SET agent_url = %s, agent_token = '', use_settings_token = false, is_default = false, "
            "tls_fingerprint = '', tls_cert_pem = '' WHERE id = %s",
            [PARKED_AGENT_URL, parked],
        )
        return parked
    # every column (old and new release have the same nodes table): Django sets no database defaults
    cur.execute(
        "INSERT INTO nodes_node (name, agent_url, agent_token, use_settings_token, is_default, public_address, "
        "lan_address, tls_fingerprint, tls_cert_pem, last_seen_at, last_error, info, created_at) "
        "VALUES (%s, %s, '', false, false, '', '', '', '', NULL, %s, '{}'::jsonb, now()) RETURNING id",
        [PARKED_NODE_NAME, PARKED_AGENT_URL, PARKED_NOTE],
    )
    return int(cur.fetchone()[0])


def park(cur: Any) -> int:
    """Move the recorded servers to the parked node; returns how many. Their port numbers stay
    unique on it (the index 0006 brings back): servers of different nodes may share one."""
    cur.execute(
        f"SELECT i.id, i.game_port FROM instances_instance i JOIN {SIDE_TABLE} s ON s.instance_id = i.id "  # noqa: S608
        "ORDER BY s.recorded_at, i.id"
    )
    rows = cur.fetchall()
    if not rows:
        return 0
    parked = _parked_node(cur)
    ids = [instance_id for instance_id, _port in rows]
    cur.execute("SELECT game_port FROM instances_instance WHERE node_id = %s AND NOT (id = ANY(%s))", [parked, ids])
    used = {int(port) for (port,) in cur.fetchall()}
    for instance_id, port in rows:
        if port in used:
            port = ROLLBACK_PORT_BASE
            while port in used:
                port += 1
            cur.execute("UPDATE instances_instance SET game_port = %s WHERE id = %s", [port, instance_id])
        used.add(port)
    cur.execute("UPDATE instances_instance SET node_id = %s WHERE id = ANY(%s)", [parked, ids])
    return len(ids)


def give_rollback_ports(apps: Any, schema_editor: Any) -> None:
    """rollback ports; servers of other games parked on a node that cannot be reached"""
    connection = schema_editor.connection
    if SIDE_TABLE not in _tables(connection):
        return
    with connection.cursor() as cur:
        cur.execute(
            f"UPDATE instances_instance i SET game_port = s.rollback_game_port FROM {SIDE_TABLE} s "  # noqa: S608
            "WHERE i.id = s.instance_id AND s.rollback_game_port IS NOT NULL"
        )
        park(cur)


class Migration(migrations.Migration):
    dependencies = [
        ("instances", "0006_games_phase2"),
        # the player names it puts back need the wider column
        ("metrics", "0002_games_phase2"),
    ]

    operations = [
        migrations.RunPython(restore, give_rollback_ports),
    ]
