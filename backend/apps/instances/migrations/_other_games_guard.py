"""The guard that migrations 0005 and 0006 run first when they are undone (not a migration: the
loader skips modules whose name starts with "_").

Undoing 0005/0006 drops what a server of another game is (game, ports, game_options, secrets,
setup_state). The guard refuses while a server of another game exists that
``manage.py rollback_other_games --archive`` (``upgrade.sh rollback --archive-other-games``) has not
recorded in ``mcpanel_rollback_other_games``, so nothing is dropped before it was kept.

``manage.py migrate --plan`` shows the ``__doc__`` of a RunPython's code instead of running it;
the guard's ``__doc__`` asks the database, so the plan (and ``upgrade.sh``'s check of it) says
``IRREVERSIBLE (other games)`` while such servers exist. Forward the operation does nothing.
"""

from __future__ import annotations

from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections

SIDE_TABLE = "mcpanel_rollback_other_games"
PLAN_REFUSAL = "IRREVERSIBLE (other games)"  # within the 40 characters `migrate --plan` shows
PLAN_OK = "checked: other games are kept"


def unrecorded_other_games(connection: Any) -> list[tuple[str, str, str]]:
    """(id, name, game) of the servers of other games the side table does not hold."""
    with connection.cursor() as cur:
        tables = set(connection.introspection.table_names(cur))
        if "instances_instance" not in tables:
            return []
        columns = {c.name for c in connection.introspection.get_table_description(cur, "instances_instance")}
        if "game" not in columns:
            return []
        recorded = f"AND id NOT IN (SELECT instance_id FROM {SIDE_TABLE})" if SIDE_TABLE in tables else ""  # noqa: S608
        cur.execute(
            f"SELECT id, name, game FROM instances_instance WHERE game <> 'minecraft' {recorded} ORDER BY name, id"  # noqa: S608
        )
        return [(str(i), str(n), str(g)) for i, n, g in cur.fetchall()]


class KeepOtherGames:
    """The reverse code of the guard (a callable with a docstring that depends on the database)."""

    def __init__(self, migration: str) -> None:
        self.migration = migration
        self.__name__ = f"keep_other_games_{migration}"

    @property
    def __doc__(self) -> str:  # type: ignore[override]
        try:
            return PLAN_REFUSAL if unrecorded_other_games(connections[DEFAULT_DB_ALIAS]) else PLAN_OK
        except Exception:  # the plan is only a description: never fail it
            return PLAN_OK

    def __call__(self, apps: Any, schema_editor: Any) -> None:
        servers = unrecorded_other_games(schema_editor.connection)
        if not servers:
            return
        listed = "; ".join(f"{i} '{n}' ({g})" for i, n, g in servers[:20])
        more = f" and {len(servers) - 20} more" if len(servers) > 20 else ""
        raise RuntimeError(
            f"Undoing instances {self.migration} would drop what these servers of other games are (game, ports, "
            f"settings, secrets, setup state): {listed}{more}. Archive and record them first: "
            "./scripts/upgrade.sh rollback --archive-other-games (or manage.py rollback_other_games --archive "
            "--undo instances=<target>). Nothing was undone by this migration."
        )


def noop(apps: Any, schema_editor: Any) -> None:
    """nothing (the guard only acts when the migration is undone)"""
