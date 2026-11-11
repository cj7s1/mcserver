"""Before ``upgrade.sh rollback`` goes back to a release that knows only Minecraft servers.

    python manage.py rollback_other_games --undo instances=0004_schedule_deferred_at --undo metrics=0001_initial
    python manage.py rollback_other_games --archive --undo instances=... --undo metrics=...

``--undo APP=TARGET`` names the migrations the rollback is about to undo (``TARGET`` may be
``zero``), exactly as ``upgrade.sh`` passes them to ``manage migrate``. Undoing instances
``0005_instance_game`` drops the columns that say what a server is (``game``, ``ports``,
``game_options``, ``secrets``, ``setup_state``): the old release would take every server for a
Minecraft server and start the Minecraft image in its folder. Undoing it also brings back the
unique index on (node, game_port) for every server, which a UDP and a TCP server sharing one port
number break (undoing the migration fails then). Undoing metrics ``0002_games_phase2`` narrows
player names to 16 characters again and fails on longer ones.

Without ``--archive`` (the check, also for ``upgrade.sh rollback --dry-run``) nothing is changed;
the command lists those servers, the port numbers servers of one node share and the number of long
player names. With ``--archive`` (``upgrade.sh rollback --archive-other-games``, run inside the
running panel container) for every server of another game:

* a server that is not archived is stopped by the panel's own Stop job (the agent's graceful stop:
  the game's driver saves first), its enabled schedules are disabled and the panel's Archive job
  archives it (the agent removes its container). A job that holds the server, or a stop that
  fails, ends the command with nothing more done to that server;
* the server's row as it is (``to_jsonb``: the game, its variant, named ports, pinned options,
  secrets and control secret as the stored ciphertext, setup state, game port, image, ...) and the
  ids of the schedules this disabled go into the table ``mcpanel_rollback_other_games``. Django's
  migrations do not manage it, so undoing them leaves it alone and every later database dump has
  it. When the server shares its game port with another server of the node, the row also says
  which free number (above 65535: never a real port, so the old release can never publish it)
  it gets while the old release runs: migration instances ``0007_restore_other_games`` sets it
  when it is undone, right before ``0006`` brings the unique index back.

Player names longer than 16 characters go into ``mcpanel_rollback_player_names`` and out of the
player history. When the panel is upgraded again, migration ``0007_restore_other_games`` puts all
of it back (servers the owner deleted meanwhile are skipped), leaves the servers archived for the
owner to unarchive, and drops both tables.

Exit status: 0 = nothing the rollback would lose or the old schema would refuse (check), or all
of it was archived and recorded (``--archive``); 3 = check only: such servers or names exist and
``--archive`` takes care of them; 4 = blocked: a job holds such a server, a stop or an archive
failed, or port numbers are shared in a way archiving does not settle; 1 = the command failed.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from apps.core.errors import ApiError
from apps.instances.models import Instance, Schedule, ServerType
from apps.jobs.models import ACTIVE_STATUSES, Job, JobStatus

# Shared with migrations/0007_restore_other_games.py (a migration does not import app code).
SIDE_TABLE = "mcpanel_rollback_other_games"
SIDE_PLAYERS = "mcpanel_rollback_player_names"
GAME_MIGRATION = ("instances", "0005_instance_game")
NAMES_MIGRATION = ("metrics", "0002_games_phase2")
RESTORE_MIGRATION = ("instances", "0007_restore_other_games")
OLD_NAME_LENGTH = 16
# game_port of a server while the old release runs, when another server of the node has its number
ROLLBACK_PORT_BASE = 65536

EXIT_NEEDS_ARCHIVE = 3
EXIT_BLOCKED = 4
# how long a Stop or Archive job of one server may take, on top of the server's stop timeout
JOB_MARGIN_SECONDS = 300

CREATE_SIDE_TABLES = (
    f"""CREATE TABLE IF NOT EXISTS {SIDE_TABLE} (
        instance_id uuid PRIMARY KEY,
        name varchar(80) NOT NULL,
        game varchar(32) NOT NULL,
        data jsonb NOT NULL,
        schedules_disabled jsonb NOT NULL DEFAULT '[]'::jsonb,
        rollback_game_port integer NULL,
        recorded_at timestamptz NOT NULL DEFAULT now()
    )""",
    f"""COMMENT ON TABLE {SIDE_TABLE} IS 'MCPanel: servers of other games that upgrade.sh rollback
--archive-other-games archived. The next upgrade puts them back (migration instances 0007) and drops
this table. Not managed by Django migrations.'""",
    f"""CREATE TABLE IF NOT EXISTS {SIDE_PLAYERS} (
        instance_id uuid NOT NULL,
        name varchar(64) NOT NULL,
        first_seen timestamptz NOT NULL,
        last_seen timestamptz NOT NULL,
        minutes_online integer NOT NULL,
        sessions integer NOT NULL,
        PRIMARY KEY (instance_id, name)
    )""",
    f"""COMMENT ON TABLE {SIDE_PLAYERS} IS 'MCPanel: player history names longer than 16 characters, kept
by upgrade.sh rollback --archive-other-games. The next upgrade puts them back and drops this table.'""",
)


class Blocked(Exception):
    """Something archiving cannot settle: the command ends with EXIT_BLOCKED."""


def printable(text: Any) -> str:
    return "".join(c if c.isprintable() else "?" for c in str(text))


class Command(BaseCommand):
    help = "Before a rollback to a release without other games: list (or archive and record) the servers it would break"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument(
            "--undo",
            action="append",
            default=[],
            metavar="APP=TARGET",
            help="A migration target of the rollback (as for `manage migrate APP TARGET`); repeat per app",
        )
        parser.add_argument(
            "--archive",
            action="store_true",
            help="Stop, archive and record the servers of other games, and put long player names aside",
        )

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        # Only the command's own lines: upgrade.sh shows what it prints (the job runner and the
        # audit log write a log record per step).
        previous = logging.root.manager.disable
        logging.disable(max(previous, logging.WARNING))
        try:
            self._run(opts)
        finally:
            logging.disable(previous)

    # -- what the rollback undoes --------------------------------------------------------
    def _undone(self, undo: list[str]) -> set[tuple[str, str]]:
        targets: list[tuple[str, str | None]] = []
        for item in undo:
            app, sep, target = item.partition("=")
            if not sep or not app or not target:
                raise CommandError(f"--undo needs APP=TARGET, not '{item}'")
            targets.append((app, None if target == "zero" else target))
        executor = MigrationExecutor(connection)
        try:
            plan = executor.migration_plan(targets)
        except (KeyError, ValueError) as exc:
            raise CommandError(f"unknown migration target ({exc})") from exc
        return {(migration.app_label, migration.name) for migration, backwards in plan if backwards}

    def _run(self, opts: dict[str, Any]) -> None:
        undone = self._undone(list(opts["undo"]))
        drops_game = GAME_MIGRATION in undone and self._has_column("instances_instance", "game")
        narrows_names = NAMES_MIGRATION in undone
        if not drops_game and not narrows_names:
            self._say("The rollback keeps every server's game and every player name: nothing to do.")
            return

        servers = self._other_games() if drops_game else []
        remap, unsettled = self._shared_ports(drops_game)
        long_names = self._long_names() if narrows_names else 0
        busy = [s for s in servers if s["job"] is not None]

        self._report(servers, remap, unsettled, long_names, archive=opts["archive"])
        if busy or unsettled:
            for s in busy:
                self._say(f"  {self._label(s)}: an operation is in progress ({printable(s['job'])}): wait until it is done")
            if unsettled:
                self._say("  Archiving does not settle the shared port numbers above: give one of the servers another port.")
            raise CommandError("the rollback cannot go on now (see above); nothing was changed", returncode=EXIT_BLOCKED)
        if not servers and not remap and not long_names:
            self._say("No server of another game, no shared port number, no long player name: nothing to do.")
            return
        if not opts["archive"]:
            raise CommandError(
                "the rollback would lose or be refused what is listed above (archiving takes care of it: "
                "upgrade.sh rollback --archive-other-games)",
                returncode=EXIT_NEEDS_ARCHIVE,
            )
        self._archive(servers, remap, narrows_names)

    # -- what is there ---------------------------------------------------------------------
    @staticmethod
    def _has_column(table: str, column: str) -> bool:
        with connection.cursor() as cur:
            return column in {c.name for c in connection.introspection.get_table_description(cur, table)}

    @staticmethod
    def _other_games() -> list[dict[str, Any]]:
        rows = (
            Instance.objects.exclude(game="minecraft")
            .order_by("name", "id")
            .values("id", "name", "game", "server_type", "archived_at", "active_job_id", "stop_timeout")
        )
        out = []
        for row in rows:
            job = None
            if row["active_job_id"]:
                job = (
                    Job.objects.filter(pk=row["active_job_id"], status__in=ACTIVE_STATUSES)
                    .values_list("title", flat=True)
                    .first()
                )
            out.append({**row, "job": job})
        return out

    @staticmethod
    def _shared_ports(drops_game: bool) -> tuple[dict[Any, int], list[list[dict[str, Any]]]]:
        """The servers that get another game_port while the old release runs, and the groups of
        servers sharing a number that archiving cannot settle (two Minecraft servers: the current
        index never lets that happen)."""
        if not drops_game:
            return {}, []
        rows = list(Instance.objects.order_by("created_at", "id").values("id", "name", "game", "node_id", "game_port"))
        used: dict[Any, set[int]] = defaultdict(set)
        groups: dict[tuple[Any, int], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            used[row["node_id"]].add(row["game_port"])
            groups[(row["node_id"], row["game_port"])].append(row)
        remap: dict[Any, int] = {}
        unsettled: list[list[dict[str, Any]]] = []
        for (node_id, _port), group in groups.items():
            if len(group) < 2:
                continue
            minecraft = [row for row in group if row["game"] == "minecraft"]
            if len(minecraft) > 1:
                unsettled.append(group)
                continue
            keep = minecraft[0] if minecraft else group[0]  # (the oldest)
            for row in group:
                if row is keep:
                    continue
                number = ROLLBACK_PORT_BASE
                while number in used[node_id]:
                    number += 1
                used[node_id].add(number)
                remap[row["id"]] = number
        return remap, unsettled

    @staticmethod
    def _long_names() -> int:
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM metrics_playerseen WHERE char_length(name) > %s", [OLD_NAME_LENGTH])
            return int(cur.fetchone()[0])

    @staticmethod
    def _label(server: dict[str, Any] | Instance) -> str:
        name = server.name if isinstance(server, Instance) else server["name"]
        return f"'{printable(name)}'"

    def _report(
        self,
        servers: list[dict[str, Any]],
        remap: dict[Any, int],
        unsettled: list[list[dict[str, Any]]],
        long_names: int,
        *,
        archive: bool,
    ) -> None:
        if servers:
            self._say("Servers of games other than Minecraft (the version the rollback goes back to does not know them):")
            for s in servers:
                variant = s["server_type"] if s["server_type"] and s["server_type"] not in ServerType.values else ""
                game = f"{s['game']} ({printable(variant)})" if variant else s["game"]
                note = ", archived already" if s["archived_at"] else ""
                self._say(f"  {s['id']}  {self._label(s)}  {printable(game)}{note}")
        if remap or unsettled:
            self._say("Port numbers several servers of one node share (the old database allows each number once per node):")
            by_id = {row["id"]: row for row in Instance.objects.filter(pk__in=list(remap)).values("id", "name", "game_port")}
            for instance_id, number in sorted(remap.items(), key=lambda item: item[1]):
                row = by_id[instance_id]
                self._say(
                    f"  {self._label(row)} (game port {row['game_port']}) gets {number} while the old version runs "
                    f"(another server of the node has {row['game_port']}); the next upgrade gives it {row['game_port']} back"
                )
            for group in unsettled:
                names = ", ".join(f"{self._label(row)} ({row['game']})" for row in group)
                self._say(f"  port {group[0]['game_port']}: {names}")
        if long_names:
            self._say(
                f"Player names longer than {OLD_NAME_LENGTH} characters in the player history (the old limit): {long_names}; "
                "they are put aside and come back with the next upgrade"
            )
        if servers and not archive:
            self._say(
                "Archiving stops each of these servers through the panel (the game saves first), archives it and keeps "
                f"what the rollback would remove in the table {SIDE_TABLE}."
            )

    # -- archiving -------------------------------------------------------------------------
    def _archive(self, servers: list[dict[str, Any]], remap: dict[Any, int], narrows_names: bool) -> None:
        with connection.cursor() as cur:
            for statement in CREATE_SIDE_TABLES:
                cur.execute(statement)
        restore_applied = RESTORE_MIGRATION in MigrationRecorder(connection).applied_migrations()
        if restore_applied:
            # Left over by a rollback that never got to undoing the migrations: what the table holds
            # is older than the servers as they are now (their columns were never dropped).
            self._put_back_stale_names()
            with connection.cursor() as cur:
                cur.execute(f"DELETE FROM {SIDE_TABLE}")  # noqa: S608 - constant table name
        done: list[str] = []
        try:
            for server in servers:
                self._archive_one(server, remap.get(server["id"]), keep_first=not restore_applied)
                done.append(self._label(server))
        except Blocked as exc:
            self._say(f"  {exc}")
            if done:
                self._say(f"  Archived and recorded before that: {', '.join(done)} (they stay archived).")
            raise CommandError("archiving did not finish (see above)", returncode=EXIT_BLOCKED) from exc
        if narrows_names:
            moved = self._put_aside_names()
            if moved:
                self._say(f"Put aside {moved} player name(s) longer than {OLD_NAME_LENGTH} characters ({SIDE_PLAYERS}).")
        self._say(
            f"Done: {len(servers)} server(s) archived and recorded in {SIDE_TABLE}. The next upgrade puts them back, "
            "still archived."
        )

    def _archive_one(self, server: dict[str, Any], rollback_port: int | None, *, keep_first: bool) -> None:
        from apps.audit import service as audit

        label = self._label(server)
        instance = Instance.objects.select_related("node").get(pk=server["id"])
        disabled: list[int] = []
        if instance.archived_at is None:
            stop = self._job("stop", f"Stop server: {instance.name} (before a rollback)", instance)
            self._say(f"  {label}: stopped ({self._stop_note(stop)})")
            disabled = self._disable_schedules(instance)
            self._job("archive", f"Archive server: {instance.name} (before a rollback)", instance)
            what = "archived"
        else:
            disabled = self._disable_schedules(instance)
            what = "archived already"
        # stop and archive cleared it already; an archived server must not come back after a reboot
        Instance.objects.filter(pk=instance.pk).update(resume_after_boot=False, resume_flagged_at=None)
        conflict = (
            "DO UPDATE SET schedules_disabled = (SELECT COALESCE(jsonb_agg(DISTINCT v), '[]'::jsonb) "
            "FROM jsonb_array_elements(side.schedules_disabled || EXCLUDED.schedules_disabled) AS v)"
            if keep_first
            else "DO NOTHING"
        )
        with transaction.atomic(), connection.cursor() as cur:
            # keep_first: a rollback that did not finish undid 0007 already, which set the
            # rollback port: the first record holds the port the server really has.
            cur.execute(
                f"INSERT INTO {SIDE_TABLE} AS side (instance_id, name, game, data, schedules_disabled, rollback_game_port) "  # noqa: S608
                f"SELECT i.id, i.name, i.game, to_jsonb(i), %s::jsonb, %s FROM instances_instance i WHERE i.id = %s "
                f"ON CONFLICT (instance_id) {conflict}",
                [json.dumps(disabled), rollback_port, instance.pk],
            )
        details: dict[str, Any] = {"game": instance.game, "schedules_disabled": len(disabled)}
        if rollback_port is not None:
            details["rollback_game_port"] = rollback_port
        try:
            # no actor: the audit log shows it as done by the system
            audit.record("instance.archived_for_rollback", instance=instance, target=instance, details=details)
        except Exception as exc:  # the record matters more than its audit entry
            self.stderr.write(f"could not write the audit entry for {label}: {exc}")
        schedules = f", {len(disabled)} schedule(s) disabled" if disabled else ""
        port = f", game port {rollback_port} while the old version runs" if rollback_port is not None else ""
        self._say(f"  {label}: {what}{schedules}{port}, recorded")

    def _job(self, kind: str, title: str, instance: Instance) -> Job:
        """Run one of the panel's own jobs on the server and wait for its end.

        ``enqueue`` takes the server's job lock (refused when another job holds it) and sends the
        job to the worker; it is also run here at once, so it does not wait behind other jobs in
        the queue. The runner lets only one of the two run it (``run_job`` locks the job's row).
        """
        from apps.jobs.runner import enqueue, run_job

        try:
            job = enqueue(kind, title=title, instance=instance)
        except ApiError as exc:
            raise Blocked(f"{self._label(instance)}: {exc.detail}; nothing was done to it") from exc
        run_job(str(job.id))
        deadline = time.monotonic() + int(instance.stop_timeout or 90) + JOB_MARGIN_SECONDS
        job.refresh_from_db()
        while job.status in ACTIVE_STATUSES:
            if time.monotonic() > deadline:
                raise Blocked(f"{self._label(instance)}: the job '{title}' did not end in time; see the Activity page")
            time.sleep(0.5)
            job.refresh_from_db()
        if job.status != JobStatus.SUCCEEDED:
            reason = printable(job.error or job.status)
            raise Blocked(f"{self._label(instance)}: '{title}' {job.status}: {reason}")
        return job

    @staticmethod
    def _stop_note(job: Job) -> str:
        lines = [line.split(" ", 1)[-1] for line in job.log or []]
        notes = [line for line in lines if line.startswith(("The world was saved", "warning:"))]
        return "; ".join(printable(line) for line in notes) or "graceful stop through the agent"

    @staticmethod
    def _disable_schedules(instance: Instance) -> list[int]:
        with transaction.atomic():
            ids = list(Schedule.objects.select_for_update().filter(instance=instance, enabled=True).values_list("id", flat=True))
            Schedule.objects.filter(pk__in=ids).update(enabled=False)
        return ids

    @staticmethod
    def _put_aside_names() -> int:
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SIDE_PLAYERS} (instance_id, name, first_seen, last_seen, minutes_online, sessions) "  # noqa: S608
                "SELECT instance_id, name, first_seen, last_seen, minutes_online, sessions FROM metrics_playerseen "
                "WHERE char_length(name) > %s ON CONFLICT (instance_id, name) DO NOTHING",
                [OLD_NAME_LENGTH],
            )
            cur.execute("DELETE FROM metrics_playerseen WHERE char_length(name) > %s", [OLD_NAME_LENGTH])
            return int(cur.rowcount)

    @staticmethod
    def _put_back_stale_names() -> None:
        """Names put aside by a rollback that never undid the migrations: they belong in the player
        history again (the column is still wide enough)."""
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                f"INSERT INTO metrics_playerseen (instance_id, name, first_seen, last_seen, minutes_online, sessions) "  # noqa: S608
                f"SELECT p.instance_id, p.name, p.first_seen, p.last_seen, p.minutes_online, p.sessions FROM {SIDE_PLAYERS} p "
                "WHERE EXISTS (SELECT 1 FROM instances_instance i WHERE i.id = p.instance_id) "
                "ON CONFLICT (instance_id, name) DO NOTHING"
            )
            cur.execute(f"DELETE FROM {SIDE_PLAYERS}")  # noqa: S608

    def _say(self, line: str) -> None:
        self.stdout.write(line)
        self.stdout.flush()
