"""In-game warnings before a restart or stop (manual or scheduled).

A power action with ``warn_minutes > 0`` is enqueued as a *non-exclusive* job: during the
warning period other operations (a backup, file edits, a snapshot) keep working. The job
announces the action in chat at T-N min, at the standard marks below N (10, 5, 1 min) and at
T-10 s, and takes the instance lock only for the final 10 seconds and the action itself
(``runner.claim_lock``, waiting for another exclusive job to finish if one started meanwhile).

* No warning is sent (and the action runs at once) when the server is not running, nobody is
  online, or the console cannot be reached; if every player leaves during the countdown the
  action also runs early.
* If the server stops during the countdown the restart/stop is skipped.
* Cancelling (``POST /instances/<id>/power/cancel``) or abandoning the job stops the countdown
  within ~2 s and tells the players it was called off. An immediate stop/restart supersedes a
  pending countdown (no "cancelled" message: the server goes down anyway).
* Messages are built from fixed text plus an optional operator message that is validated
  (single line, printable, no formatting codes, at most 100 characters) and only ever placed
  inside a JSON text component built with ``json.dumps`` (``tellraw``), so it cannot inject
  commands, selectors or formatting.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps import games
from apps.core.errors import ApiError, ConflictError
from apps.jobs import runner
from apps.jobs.models import ACTIVE_STATUSES, Job, JobStatus
from apps.nodes.client import AgentClient

from .models import Instance

log = logging.getLogger(__name__)

WARNABLE_ACTIONS = ("restart", "stop")
WARN_MINUTES_CHOICES = (0, 1, 5, 10, 15)
# seconds before the action at which players are told (only marks <= the chosen period)
MARKS = (900, 600, 300, 60, 10)
FINAL_WARNING = 10
# how long the final phase waits for another exclusive job (e.g. a backup) before giving up
LOCK_WAIT_SECONDS = 20 * 60
MESSAGE_MAX = 100
RUNNING_STATUSES = ("running", "starting", "unhealthy")
_FORMATTING = re.compile(r"§.?")
_UNKNOWN_COMMAND = re.compile(r"unknown|incorrect|not found", re.I)
UNKNOWN_COMMAND = _UNKNOWN_COMMAND  # the Minecraft definition's tellraw -> say fallback (apps/games/minecraft.py)
_UNREACHABLE = object()


# --- validation / message building (pure) ---------------------------------------------
def validate_warn_minutes(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("warn_minutes must be a whole number of minutes")
    try:
        minutes = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("warn_minutes must be a whole number of minutes") from exc
    if minutes not in WARN_MINUTES_CHOICES:
        raise ValueError(f"warn_minutes must be one of {', '.join(map(str, WARN_MINUTES_CHOICES))}")
    return minutes


def validate_message(value: Any) -> str:
    """Strictly validate an operator-supplied warning message (raises ValueError)."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("The warning message must be text")
    text = value.strip()
    if any(not ch.isprintable() for ch in text):
        raise ValueError("The warning message must be a single line of plain text")
    if "§" in text:
        raise ValueError("Formatting codes (§) are not allowed in the warning message")
    if len(text) > MESSAGE_MAX:
        raise ValueError(f"The warning message can be at most {MESSAGE_MAX} characters")
    return text


def clean_message(value: Any) -> str:
    """Defensive normalisation at use time (stored params may predate validation rules)."""
    text = _FORMATTING.sub("", str(value or ""))
    text = "".join(ch for ch in text if ch.isprintable())
    return " ".join(text.split())[:MESSAGE_MAX]


def warning_marks(minutes: int) -> list[int]:
    total = int(minutes) * 60
    return [m for m in MARKS if m <= total] if total > 0 else []


def describe_remaining(seconds: int) -> str:
    if seconds >= 60:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _verb(action: str) -> str:
    return "restarting" if action == "restart" else "shutting down"


def warning_text(action: str, seconds: int) -> str:
    return f"The server is {_verb(action)} in {describe_remaining(seconds)}."


def cancelled_text(action: str) -> str:
    return "The server restart was cancelled." if action == "restart" else "The server shutdown was cancelled."


def tellraw_command(text: str, message: str = "", *, color: str = "yellow") -> str:
    """``tellraw @a <component>``. User text only ever ends up inside a JSON string value.

    A root compound with ``extra`` (not a JSON array) keeps the component valid both as
    JSON (up to 1.21.4) and as SNBT (1.21.5+, where commands parse text components as SNBT).
    """
    extra: list[dict[str, str]] = [{"text": "[Server] ", "color": "gold"}, {"text": text, "color": color}]
    note = clean_message(message)
    if note:
        extra.append({"text": f" {note}", "color": "gray"})
    component = {"text": "", "extra": extra}
    return "tellraw @a " + json.dumps(component, ensure_ascii=False, separators=(",", ":"))


# --- queries used by views/serializers ---------------------------------------------------
def pending_countdown(instance: Instance) -> Job | None:
    """The active warning job of this server, if any (queued, warning or final phase)."""
    jobs = getattr(instance, "active_power_jobs", None)
    if jobs is None:
        jobs = list(Job.objects.filter(instance=instance, kind__in=WARNABLE_ACTIONS, status__in=ACTIVE_STATUSES))
    candidates = [j for j in jobs if (j.params or {}).get("warn_minutes")]
    return min(candidates, key=lambda j: j.created_at) if candidates else None


def countdown_info(instance: Instance) -> dict[str, Any] | None:
    job = pending_countdown(instance)
    if job is None:
        return None
    state = (job.result or {}).get("countdown") or {}
    phase = state.get("phase") or "queued"
    if phase == "acting":
        return None  # the job holds the lock now and shows up as the active job
    return {
        "job_id": str(job.id),
        "action": job.kind,
        "title": job.title,
        "warn_minutes": int((job.params or {}).get("warn_minutes") or 0),
        "message": clean_message((job.params or {}).get("warn_message")),
        "phase": phase,
        "action_at": state.get("action_at"),
        "status_message": job.message,
    }


def cancel_countdown(job_id: Any, *, message: str, notify_players: bool = True) -> Job:
    """Cancel a warning job before its action starts. The job sees it within ~2 s."""
    with transaction.atomic():
        job = Job.objects.select_for_update().filter(pk=job_id).first()
        if job is None or job.status not in ACTIVE_STATUSES:
            raise ConflictError("The countdown already finished", code="not_active")
        state = dict((job.result or {}).get("countdown") or {})
        if state.get("phase") == "acting":
            raise ConflictError(f"The {job.kind} is already in progress and can no longer be cancelled", code="not_cancellable")
        state.update(phase="cancelled", notify_players=notify_players)
        job.message = message[:500]
        job.append_log(message)
        runner.finish(job, JobStatus.CANCELLED, result={**(job.result or {}), "countdown": state})
    return job


# --- the countdown itself (runs inside the restart/stop job) ------------------------------
class PowerCountdown:
    def __init__(self, job: Job, ctx: runner.JobContext, instance: Instance, action: str) -> None:
        self.job = job
        self.ctx = ctx
        self.instance = instance
        self.action = action
        self.client = AgentClient(instance.node)
        # (a game this panel does not know has no in-game messages)
        self.definition = games.get_or_none(instance.game) or games.GameDefinition()
        self.minutes = int((job.params or {}).get("warn_minutes") or 0)
        self.message = clean_message((job.params or {}).get("warn_message"))
        self.marks = warning_marks(self.minutes)
        self.warned = False
        self.state: dict[str, Any] = {"action": action, "warn_minutes": self.minutes}

    # -- public --------------------------------------------------------------------------
    def run(self) -> dict[str, Any] | None:
        """Warn players. Returns None when the action should go ahead (the job then holds the
        instance lock and can no longer be cancelled), or a result dict when it is skipped."""
        try:
            skipped = self._countdown()
            if skipped is not None:
                return skipped
            runner.claim_lock(self.job, self.ctx, wait=LOCK_WAIT_SECONDS)
            self._enter_action_phase()
            return None
        except Exception:  # cancelled, abandoned, lock wait timed out, agent failure
            self._announce_cancelled()
            raise

    # -- internals -------------------------------------------------------------------------
    def _countdown(self) -> dict[str, Any] | None:
        if not self.marks:
            return None
        if not self.definition.has(self.instance, "broadcast"):
            label = self.definition.label or self.instance.game
            self.ctx.log(f"{label} servers cannot show in-game messages; {self._doing()} without a warning")
            return None
        if not self._running():
            self.ctx.log("The server is not running, so no warning is needed")
            return None
        online = self._players()
        if online is _UNREACHABLE:
            self.ctx.log("Could not reach the server console (RCON) to warn players; continuing without a warning")
            return None
        if online == 0:
            self.ctx.log(f"No players online; {self._doing()} without a warning")
            return None
        total = self.marks[0]
        action_at = runner.monotonic() + total
        self._save_state(phase="warning", action_at=(timezone.now() + timedelta(seconds=total)).isoformat())
        for mark in self.marks:
            if mark <= FINAL_WARNING:
                break
            self.ctx.sleep_until(action_at - mark)
            if mark != total:
                if not self._running():
                    return self._stopped_meanwhile()
                if self._players() == 0:
                    self.ctx.log(f"All players left; {self._doing()} now")
                    return None
            self._warn(mark, progress=0.4 * (total - mark) / total)
        self.ctx.sleep_until(action_at - FINAL_WARNING)
        # final phase: hold the lock so nothing else starts in the last seconds
        runner.claim_lock(self.job, self.ctx, wait=LOCK_WAIT_SECONDS)
        if not self._running():
            return self._stopped_meanwhile()
        if self._players() == 0:
            self.ctx.log(f"All players left; {self._doing()} now")
            return None
        self._save_state(phase="final", action_at=(timezone.now() + timedelta(seconds=FINAL_WARNING)).isoformat())
        self._warn(FINAL_WARNING, progress=0.4)
        self.ctx.sleep_until(runner.monotonic() + FINAL_WARNING)
        return None

    def _doing(self) -> str:
        return "restarting" if self.action == "restart" else "stopping"

    def _running(self) -> bool:
        try:
            return bool(self.client.status(self.instance.id).get("running"))
        except ApiError as exc:
            # a short agent hiccup must not end a 15-minute countdown; the action reports real errors
            log.info("status check during countdown failed for %s: %s", self.instance.pk, exc)
            return True

    def _players(self) -> Any:
        if not self.definition.has(self.instance, "players"):
            return None  # unknown: warn anyway
        try:
            return self.definition.online_players(self.instance).get("online")  # None = output not understood
        except ApiError:
            return _UNREACHABLE

    def _send(self, text: str, *, color: str = "yellow") -> bool:
        try:
            # Minecraft: tellraw, or `say` on servers without it (apps/games/minecraft.py)
            self.definition.broadcast(self.client, self.instance, text, message=self.message, color=color)
        except ApiError as exc:
            self.ctx.log(f"Could not send the in-game message: {exc.detail}")
            return False
        return True

    def _warn(self, seconds: int, *, progress: float) -> None:
        text = warning_text(self.action, seconds)
        if self._send(text):
            self.warned = True
        self.ctx.progress(progress, f"Players warned: {text}", force=True)

    def _save_state(self, **changes: Any) -> None:
        self.state.update(changes)
        result = {**(self.job.result or {}), "countdown": dict(self.state)}
        # conditional write: never resurrect a job that was cancelled meanwhile
        if not Job.objects.filter(pk=self.job.pk, status=JobStatus.RUNNING).update(result=result):
            raise runner.JobCancelled("cancelled")
        self.job.result = result

    def _enter_action_phase(self) -> None:
        with transaction.atomic():
            row = Job.objects.select_for_update().filter(pk=self.job.pk).first()
            if row is None or row.status != JobStatus.RUNNING:
                raise runner.JobCancelled(row.status if row else "deleted")
            self.state["phase"] = "acting"
            result = {**(row.result or {}), "countdown": dict(self.state)}
            Job.objects.filter(pk=self.job.pk).update(result=result)
            self.job.result = result

    def _stopped_meanwhile(self) -> dict[str, Any]:
        note = (
            "The server stopped during the countdown; the restart was skipped"
            if self.action == "restart"
            else "The server already stopped during the countdown"
        )
        self.ctx.log(note)
        return {"skipped": note}

    def _announce_cancelled(self) -> None:
        if not self.warned:
            return
        row = Job.objects.filter(pk=self.job.pk).values("result").first()
        state = ((row or {}).get("result") or {}).get("countdown") or {}
        if state.get("notify_players") is False:
            return
        try:
            if self.client.status(self.instance.id).get("running"):
                self._send(cancelled_text(self.action), color="green")
                self.ctx.log("Players were told that the countdown was cancelled")
        except ApiError:
            pass
