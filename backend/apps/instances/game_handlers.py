"""Job handlers of games other than Minecraft (multi-game phases 2 and 3).

* ``setup``: the first-start steps of a game (``GameDefinition.setup_steps``), recorded in
  ``Instance.setup_state``. The install job enqueues it when steps are pending.
* ``saves_reset`` / ``saves_import``: replace the save a server loads (the definition's ``saves``
  model; Factorio: ``saves/<save_name>.zip``). Stop, safety backup, move every save aside (kept in
  the server folder), then a new map (reset: the map-gen settings and seed of the request) or the
  uploaded save (import), and start again when the server was running.
* ``wait_first_start``: the install job's bounded retry of an image's own download step
  (SteamCMD) for games that mark exit codes as retryable (``retryable_install_exits``).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from django.utils import timezone

from apps import games
from apps.core.errors import ApiError, ConflictError
from apps.jobs.models import Job
from apps.jobs.runner import JobContext, claim_lock, register
from apps.nodes.client import AgentClient

from .handlers import (
    _instance,
    _log_stop,
    _set_desired,
    put_back,
    start_server,
    starting_again,
)
from .models import DesiredState, Instance
from .reconcile import reconcile_instance

log = logging.getLogger(__name__)

SETUP_LOCK_WAIT = 900  # seconds the setup job waits for the install job that enqueued it


# --- setup steps ------------------------------------------------------------------------------------
def record_setup(instance: Instance, step_id: str, status: str, message: str = "") -> None:
    """Write one step's state into ``Instance.setup_state`` (only that column)."""
    instance.refresh_from_db(fields=["setup_state"])
    state = dict(instance.setup_state or {})
    steps = dict(state.get("steps") or {})
    steps[step_id] = {"status": status, "at": timezone.now().isoformat(), "message": message[:500]}
    state["steps"] = steps
    instance.setup_state = state
    Instance.objects.filter(pk=instance.pk).update(setup_state=state)


def pending_automatic_steps(instance: Instance) -> list[Any]:
    """Steps the setup job can run now: ``auto`` ones, and ``secret`` ones whose secret is set."""
    definition = games.get_or_none(instance.game)
    status = definition.setup_status(instance) if definition is not None else None
    if not status or not status["needed"]:
        return []
    secrets = instance.secret_values
    out = []
    for step in definition.setup_steps:
        entry = next(s for s in status["steps"] if s["id"] == step.id)
        if entry["status"] == "done":
            continue
        if step.kind == "auto" or (step.kind == "secret" and step.secret and secrets.get(step.secret)):
            out.append(step)
        else:
            break  # steps run in order: a manual step or a missing secret blocks the rest
    return out


def require_steps_before(instance: Instance, step_id: str) -> None:
    """Refuse a step whose earlier steps are not done (409 ``setup_order``): e.g. Satisfactory's
    ``publish`` before the claim would publish an unclaimed server."""
    definition = games.for_instance(instance)
    status = definition.setup_status(instance) or {"steps": []}
    for step in status.get("steps") or []:  # this server's steps, in their order
        if step["id"] == step_id:
            return
        if step["status"] != "done":
            raise ConflictError(
                f"Run the setup step '{step.get('label') or step['id']}' first; the steps run in order",
                code="setup_order",
                details={"step": step_id, "needs": step["id"]},
            )


def enqueue_setup(instance: Instance, *, user: Any = None, step: str | None = None) -> Job | None:
    """Queue the ``setup`` job (non-exclusive: it takes the lock when it runs, after an install)."""
    from apps.jobs.runner import enqueue

    if not pending_automatic_steps(instance) and step is None:
        return None
    if step is not None:
        require_steps_before(instance, step)
    return enqueue(
        "setup",
        title=f"Set up {instance.name}",
        instance=instance,
        user=user,
        params={"step": step} if step else {},
        exclusive=False,
    )


@register("setup")
def setup(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Run the pending automatic setup steps of the server's game, in order."""
    claim_lock(job, ctx, wait=SETUP_LOCK_WAIT)
    instance = _instance(job)
    definition = games.for_instance(instance)
    only = (job.params or {}).get("step")
    done: list[str] = []
    steps = pending_automatic_steps(instance)
    if only:
        steps = [s for s in steps if s.id == only] or [s for s in definition.setup_steps if s.id == only]
        if not steps:
            raise ApiError(f"Unknown setup step '{only}'", code="invalid_step")
        require_steps_before(instance, only)  # (again: the state may have changed since it was queued)
    for i, step in enumerate(steps):
        ctx.check_cancelled()
        ctx.progress(0.05 + 0.9 * i / max(len(steps), 1), step.label, force=True)
        record_setup(instance, step.id, "running")
        try:
            message = definition.run_setup_step(instance, step, ctx) or ""
        except games.SetupWaiting as exc:
            # cannot run yet (the server is stopped): pending with the reason, the job ends quietly
            record_setup(instance, step.id, "pending", str(exc))
            ctx.log(f"Setup step '{step.label}' waits: {exc}")
            return {"done": done, "waiting": step.id}
        except Exception as exc:
            detail = str(getattr(exc, "detail", exc))
            record_setup(instance, step.id, "failed", detail)
            ctx.log(f"Setup step '{step.label}' failed: {detail}")
            raise
        record_setup(instance, step.id, "done", message)
        ctx.log(f"Setup step '{step.label}' done" + (f": {message}" if message else ""))
        done.append(step.id)
    return {"done": done}


# --- the first start of games whose image downloads the game itself -----------------------------------
FIRST_START_POLL = 3.0
FIRST_START_TIMEOUT = 30 * 60


START_WATCH_TIMEOUT = 10 * 60


def wait_first_start(
    instance: Instance, ctx: JobContext, client: AgentClient, *, span: tuple[float, float], first: bool = True
) -> None:
    """Wait until a server that was just installed (``first``) or started by the Start job is ready; an
    exit with one of the definition's ``retryable_install_exits`` (the image's download failed, e.g.
    SteamCMD's "Missing configuration") starts it again, up to ``install_attempts`` starts in all.
    Other exits and the last retryable one fail the job. Only for games that declare retryable exit
    codes (Satisfactory, whose restart policy is "no": Docker never retries for it).

    The Start job (``first`` False) also handles the start wrapper's refusal (exit 78, Satisfactory: a
    published container without a verified claim): the ports are closed (``mark_unclaimed``), the server
    is started again unpublished and its setup verifies the claim; and a server that is not ready
    within START_WATCH_TIMEOUT is left starting (the job ends with a note, it does not fail)."""
    definition = games.for_instance(instance)
    codes = definition.retryable_install_exits
    if not codes:
        return
    refused_exit = getattr(definition, "start_refused_exit", None)
    attempts = 1
    timeout = FIRST_START_TIMEOUT if first else START_WATCH_TIMEOUT
    deadline = time.monotonic() + timeout
    seen_started: str | None = None
    what = "first start" if first else "start"
    while time.monotonic() < deadline:
        ctx.check_cancelled()
        status = client.status(instance.id)
        if status.get("running") and status.get("health") in ("healthy", None):
            ctx.log("The server is ready")
            return
        if status.get("exists") and not status.get("running") and not status.get("restarting"):
            code = status.get("exit_code")
            started = status.get("started_at")
            if not first and refused_exit is not None and code == refused_exit and started != seen_started:
                seen_started = started
                definition.mark_unclaimed(
                    instance,
                    "The server's start guard refused to start it with published ports (its claim was not verified); "
                    "the claim is verified again before the ports are opened",
                )
                ctx.log(
                    "warning: the server's start guard refused a start with published ports because its claim was not "
                    "verified; it is started without published ports and its setup verifies the claim again"
                )
                instance.refresh_from_db()
                start_server(instance, ctx, span=span, admit=False, watch=False)
                continue
            if code in codes and started != seen_started:
                seen_started = started
                if attempts >= definition.install_attempts:
                    raise ConflictError(
                        f"The server's {what} failed {attempts} times while the image downloaded the game "
                        f"(exit code {code}). See the console log.",
                        code="install_failed" if first else "start_failed",
                    )
                attempts += 1
                ctx.log(
                    f"The image's download step failed (exit code {code}); starting it again ({attempts}/{definition.install_attempts})"
                )
                client.start(instance.id)
            elif code not in codes:
                raise ConflictError(
                    f"The server exited during its {what} (exit code {code}). See the console log.",
                    code="install_failed" if first else "start_failed",
                )
        ctx.progress(
            span[0],
            "Waiting for the first start to finish (the image downloads the game)"
            if first
            else "Waiting for the server to start (the image checks the game files first)",
        )
        ctx.sleep_until(time.monotonic() + FIRST_START_POLL)
    if not first:
        ctx.log(
            f"The server was not ready within {START_WATCH_TIMEOUT // 60} minutes; it keeps starting (its status shows "
            "when it is ready)"
        )
        return
    # not a success: the server never became ready (the job fails; the server goes on starting)
    raise ConflictError(
        f"The server did not become ready within {FIRST_START_TIMEOUT // 60} minutes of its first start (the image may "
        "still be downloading the game). It keeps starting: its status shows when it is ready, and its setup goes on "
        "then (Overview).",
        code="first_start_timeout",
    )


# --- saves of games other than Minecraft -------------------------------------------------------------
def _saves(instance: Instance) -> Any:
    definition = games.for_instance(instance)
    if definition.saves is None:
        raise ConflictError(f"{definition.label} servers have no save files the panel manages", code="unsupported_by_game")
    return definition.saves


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _replace_save(job: Job, ctx: JobContext, *, what: str, place: Any) -> dict[str, Any]:
    """The common part of saves_reset and saves_import: stop, safety backup, set aside, ``place``, start.

    ``place(client, instance)`` puts the new save (or the map settings) in place after the saves were
    moved aside. A failure before anything was moved starts a server that was running again; after
    that the server is left stopped (its saves are in saves-replaced/, the safety backup is listed).
    """
    from apps.backups.handlers import run_backup
    from apps.backups.models import BackupKind
    from apps.jobs.runner import JobCancelled, WorkerStopping

    instance = _instance(job)
    client = AgentClient(instance.node)
    saves = _saves(instance)
    p = job.params or {}
    was_running = False
    changed = False
    safety_id = None
    moved: list[str] = []
    stamp = _stamp()
    try:
        if Instance.objects.filter(pk=instance.pk, archived_at__isnull=False).exists():
            raise ConflictError("This server is archived; unarchive it first", code="archived")
        status = client.status(instance.id)
        # a container that Docker restarts (a crash loop) is stopped too: it would start on the saves
        # while they are replaced
        if status.get("running") or status.get("restarting") or status.get("status") == "restarting":
            ctx.progress(0.02, "Stopping server", force=True)
            was_running = True
            ctx.remember(stopped_server=True)
            _log_stop(ctx, client.stop(instance.id, timeout=instance.stop_timeout))
            reconcile_instance(instance.id)
        if p.get("backup", True):
            ctx.check_cancelled()
            ctx.progress(0.05, "Creating a safety backup of the current state", force=True)
            safety = run_backup(
                instance,
                ctx,
                name=f"Before {what}",
                kind=BackupKind.PRE_RESTORE,
                user=job.user,
                span=(0.05, 0.6),
                job=job,
                policy_excludes=False,
            )
            safety_id = str(safety.id)
            if safety.warnings:
                raise ConflictError(
                    "The safety backup finished with warnings (see the log of this job), so nothing was changed.",
                    code="safety_backup_incomplete",
                )
        else:
            ctx.log("No safety backup was requested")
        ctx.check_cancelled()
        ctx.progress(0.65, "Moving the current saves aside", force=True)
        changed = True
        moved = saves.set_aside(client, instance, stamp)
        if moved:
            ctx.log(f"Moved aside into {saves.describe()['set_aside_folder']}/{stamp}/: {', '.join(moved)}")
        ctx.progress(0.75, what.capitalize(), force=True)
        place(client, instance)
    except (WorkerStopping, JobCancelled):
        raise
    except Exception as exc:
        if not changed:
            put_back(job, instance, ctx, exc, was_running=was_running, untouched=True)
        elif was_running:
            _set_desired(instance, DesiredState.STOPPED)
            ctx.log(
                f"{what.capitalize()} failed after the saves were moved aside, so the server was left stopped. "
                f"The previous saves are in {saves.describe()['set_aside_folder']}/{stamp}/"
                + (" and in the safety backup taken by this job." if safety_id else ".")
            )
        raise
    if was_running:
        ctx.progress(0.9, "Starting server", force=True)
        starting_again(ctx, lambda: start_server(instance, ctx, span=(0.9, 0.09), admit=False))
    return {
        "set_aside": moved,
        "folder": f"{saves.describe()['set_aside_folder']}/{stamp}" if moved else None,
        "backup_id": safety_id,
        "restarted": was_running,
    }


@register("saves_reset")
def saves_reset(job: Job, ctx: JobContext) -> dict[str, Any]:
    """A new map: the server creates it from the map-gen settings (with the chosen seed) at its next start."""
    p = job.params or {}
    own = getattr(_saves(_instance(job)), "run_reset", None)
    if own is not None:
        return own(job, ctx)  # a game that makes its new world through its API (Satisfactory)
    chosen: dict[str, Any] = {"seed": p.get("seed")}

    def place(client: AgentClient, instance: Instance) -> None:
        values = dict(p.get("settings") or {})
        saves = _saves(instance)
        if hasattr(saves, "prepare_reset"):
            # a game whose new world is described by its own settings (Terraria: size, difficulty, seed)
            chosen["seed"] = saves.prepare_reset(client, instance, values, p.get("seed"))
            ctx.log(f"The seed of the new world: {chosen['seed']}")
            return
        values["seed"] = p.get("seed")  # None: random
        saves.write_map_gen(client, instance, values)
        ctx.log("The seed of the new map: " + ("random" if p.get("seed") is None else str(p.get("seed"))))

    result = _replace_save(job, ctx, what="resetting the world", place=place)
    result["seed"] = chosen["seed"]
    return result


@register("saves_import")
def saves_import(job: Job, ctx: JobContext) -> dict[str, Any]:
    """Put an uploaded save in place as the save the server loads."""
    from .handlers import drop_import_archive

    p = job.params or {}
    own = getattr(_saves(_instance(job)), "run_import", None)
    if own is not None:
        return own(job, ctx)  # a game that loads an uploaded save through its API (Satisfactory)

    def place(client: AgentClient, instance: Instance) -> None:
        target = _saves(instance).current_path(instance)
        client.rename(instance.id, p["path"], target)
        ctx.log(f"The uploaded save is now {target}")
        notes = getattr(_saves(instance), "notes", None)
        for note in notes(instance) if notes is not None else []:
            ctx.log(f"Note: {note}")

    try:
        return _replace_save(job, ctx, what="importing a save", place=place)
    except Exception:
        instance = _instance(job)
        drop_import_archive(AgentClient(instance.node), instance, p.get("path", ""))
        raise


__all__ = [
    "enqueue_setup",
    "pending_automatic_steps",
    "record_setup",
    "saves_import",
    "saves_reset",
    "setup",
    "wait_first_start",
]
