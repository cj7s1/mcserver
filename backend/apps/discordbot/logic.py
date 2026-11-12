"""What the Discord bot does (B3 of the feature plan of 2026-11-12), without Discord: plain Django code the bot's
command handlers call (``apps.discordbot.bot``), so it is tested without a gateway.

* Only servers marked "available in Discord" exist for the bot: names, completion, ``/servers`` and every
  answer leave the others out ("No server called X is available in Discord", whether it exists or not).
* ``/servers``, ``/status``, ``/join``: everyone in the guild. ``/start``, ``/stop``: members with one of the
  server's own roles, else one of the bot's default roles.
* Starts and stops go through ``apps.instances.services.power`` like the web UI (memory admission without the
  override, ``secret_required``, a busy server: the panel's message is the reply), as the service user
  ``SERVICE_USERNAME`` with the Discord member's name and id in the audit details, rate limited per member.
* ``/stop`` while players are online asks to confirm (``confirm: True``). The bot never runs console commands,
  and ``/join`` shows the join password only when the server's ``discord_share_password`` allows it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from django.core.cache import cache

from apps.core.errors import ApiError

from . import config

log = logging.getLogger(__name__)

SERVICE_USERNAME = "discord-bot"
# per Discord member: starts and stops, and everything else
CONTROL_LIMIT = (4, 600)  # 4 per 10 minutes
QUERY_LIMIT = (20, 60)  # 20 a minute
COUNT_FRESH_SECONDS = 180  # a player count older than this is not shown (apps.metrics.players.FRESH_SECONDS)
FOLLOW_SECONDS = 14 * 60  # how long a start is followed (an interaction's follow-ups end after 15 minutes)
STATE_WORDS = {
    "running": "running",
    "starting": "starting",
    "installing": "installing (first start)",
    "unhealthy": "running, not responding",
    "stopping": "stopping",
    "stopped": "stopped",
    "created": "stopped",
    "restarting": "restarting",
    "updating": "updating",
    "backing_up": "running (backup)",
    "restoring": "restoring a backup",
    "failed": "failed",
    "unknown": "unknown",
    "archived": "archived",
}


@dataclass(frozen=True)
class Caller:
    """The Discord member who used a command."""

    id: str
    name: str
    role_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class Reply:
    text: str
    ephemeral: bool = True
    # a start to follow up on: (instance id, job id)
    follow: tuple[str, str] | None = None
    # what to announce in the announcement channel (if one is set)
    announce: str = ""


def _clip(text: str, limit: int = 1900) -> str:
    """Discord takes at most 2000 characters per message."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- servers --------------------------------------------------------------------------------------------
def available() -> Any:
    from apps.instances.models import Instance

    return Instance.objects.select_related("node").filter(discord_available=True, archived_at__isnull=True).order_by("name")


def find(name: str) -> Any:
    """The available server called ``name`` (ignoring case), else None. Two of the same name: None too."""
    wanted = str(name or "").strip().lower()
    matches = [i for i in available() if i.name.lower() == wanted]
    return matches[0] if len(matches) == 1 else None


def complete(text: str, limit: int = 25) -> list[str]:
    """Names of available servers containing ``text`` (Discord shows at most 25)."""
    wanted = str(text or "").strip().lower()
    return [i.name for i in available() if wanted in i.name.lower()][:limit]


def players(instance: Any) -> int | None:
    """The newest player count of a running server, if it is fresh."""
    from apps.metrics.players import current_players

    return current_players(instance)[0]


def _line(instance: Any) -> str:
    count = players(instance)
    who = f", {count} player{'' if count == 1 else 's'} online" if count is not None else ""
    return f"{instance.name} ({instance_label(instance)}): {STATE_WORDS.get(instance.status, instance.status)}{who}"


def instance_label(instance: Any) -> str:
    from apps import games

    definition = games.get_or_none(instance.game or games.DEFAULT_GAME)
    return definition.label if definition is not None else str(instance.game)


def _unknown(name: str) -> Reply:
    return Reply(f"No server called “{_clip(str(name), 80)}” is available in Discord.")


# --- permission and rate limits --------------------------------------------------------------------------
def may_control(caller: Caller, instance: Any, settings: config.BotSettings) -> bool:
    roles = tuple(str(r) for r in (instance.discord_roles or []) if config.SNOWFLAKE.match(str(r))) or settings.roles
    return bool(set(roles) & set(caller.role_ids))


def _now() -> float:
    return time.time()


def rate_limited(caller: Caller, kind: str) -> str | None:
    """None, or the reply when ``caller`` used ``kind`` (control / query) too often."""
    limit, window = CONTROL_LIMIT if kind == "control" else QUERY_LIMIT
    slot = int(_now() // window)
    key = f"discordbot:rate:{kind}:{caller.id}:{slot}"
    cache.add(key, 0, timeout=window + 5)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window + 5)
        used = 1
    if used > limit:
        return f"Slow down: at most {limit} {'starts and stops' if kind == 'control' else 'commands'} per {window // 60 or 1} minute{'s' if window >= 120 else ''}."
    return None


def service_user() -> Any:
    """The panel user the bot acts as (the audit log's actor). It cannot sign in: inactive, no usable password.
    Found by the id the panel keeps, never by its name alone (apps.accounts.reserved, review F-15)."""
    from apps.accounts.reserved import service_account

    return service_account()


def _who(caller: Caller) -> dict[str, str]:
    return {"via": "discord", "discord_user": caller.name[:100], "discord_user_id": caller.id}


# --- the commands ------------------------------------------------------------------------------------------
def servers(caller: Caller) -> Reply:
    if (limited := rate_limited(caller, "query")) is not None:
        return Reply(limited)
    rows = list(available())
    if not rows:
        return Reply("No server is available in Discord yet (the owner turns it on per server in the panel).")
    return Reply(_clip("\n".join(_line(i) for i in rows)))


def status(caller: Caller, name: str) -> Reply:
    if (limited := rate_limited(caller, "query")) is not None:
        return Reply(limited)
    instance = find(name)
    if instance is None:
        return _unknown(name)
    from apps.instances.share import _address, _ports

    host, ports = _address(instance)["host"], _ports(instance)  # (no agent call: Discord waits 3 seconds)
    address = f"\nAddress: {host}:{ports[0]['port']}" if host and ports else ""
    return Reply(_clip(_line(instance) + address))


def join(caller: Caller, name: str) -> Reply:
    if (limited := rate_limited(caller, "query")) is not None:
        return Reply(limited)
    instance = find(name)
    if instance is None:
        return _unknown(name)
    from apps.instances.share import share_card

    card = share_card(instance, None, with_password=bool(instance.discord_share_password))
    if card.get("invite_has_password"):
        from apps.audit import service as audit

        audit.record(
            "instance.secret_revealed",
            actor=service_user(),
            instance=instance,
            target=instance,
            details={"key": "join_password", **_who(caller)},
        )
    return Reply(_clip(card["invite_text"]), ephemeral=True)


def start_check(caller: Caller, name: str, settings: config.BotSettings) -> tuple[Any, Reply | None]:
    """The quick part of /start (database only): the server, or the refusal (only the member sees it)."""
    instance = find(name)
    if instance is None:
        return None, _unknown(name)
    if not may_control(caller, instance, settings):
        return None, Reply(f"You may not start {instance.name}: that needs one of the roles the owner chose.")
    if instance.status in ("running", "starting", "installing"):
        return None, Reply(f"{instance.name} is {STATE_WORDS.get(instance.status, instance.status)} already.")
    if (limited := rate_limited(caller, "control")) is not None:
        return None, Reply(limited)
    return instance, None


def start(caller: Caller, name: str, settings: config.BotSettings) -> Reply:
    instance, refusal = start_check(caller, name, settings)
    return refusal if refusal is not None else start_run(caller, instance)


def start_run(caller: Caller, instance: Any) -> Reply:
    """Start through the same service function as the web UI (memory admission, secrets, a busy server)."""
    from apps.instances.services import power

    try:
        job = power(instance, "start", service_user(), audit_details=_who(caller))
    except ApiError as exc:
        return Reply(f"{instance.name} was not started: {_clip(str(exc.detail), 600)}")
    return Reply(
        f"Starting {instance.name}… I will post here when it is ready; /status {instance.name} always shows how it goes.",
        ephemeral=False,
        follow=(str(instance.pk), str(job.pk)),
        announce=f"{caller.name} is starting {instance.name}.",
    )


def stop_check(caller: Caller, name: str, settings: config.BotSettings, *, confirm: bool = False) -> tuple[Any, Reply | None]:
    """The quick part of /stop: the server, or the refusal (or the question whether to stop it with players on)."""
    instance = find(name)
    if instance is None:
        return None, _unknown(name)
    if not may_control(caller, instance, settings):
        return None, Reply(f"You may not stop {instance.name}: that needs one of the roles the owner chose.")
    if instance.status in ("stopped", "created", "stopping", "failed"):
        return None, Reply(f"{instance.name} is {STATE_WORDS.get(instance.status, instance.status)} already.")
    count = players(instance)
    if count is None and not confirm:  # (unknown is never "empty": Valheim, an old or unread count)
        return None, Reply(
            f"The panel cannot tell whether anyone is playing on {instance.name} right now. "
            f"To stop it anyway, run /stop again with confirm: True."
        )
    if count and not confirm:
        return None, Reply(
            f"{count} player{'' if count == 1 else 's'} {'is' if count == 1 else 'are'} online on {instance.name}. "
            f"To stop it anyway, run /stop again with confirm: True."
        )
    if (limited := rate_limited(caller, "control")) is not None:
        return None, Reply(limited)
    return instance, None


def stop(caller: Caller, name: str, settings: config.BotSettings, *, confirm: bool = False) -> Reply:
    instance, refusal = stop_check(caller, name, settings, confirm=confirm)
    return refusal if refusal is not None else stop_run(caller, instance)


def stop_run(caller: Caller, instance: Any) -> Reply:
    """The ordinary stop job (it saves the world first), as from the web UI."""
    from apps.instances.services import power

    try:
        power(instance, "stop", service_user(), audit_details=_who(caller))
    except ApiError as exc:
        return Reply(f"{instance.name} was not stopped: {_clip(str(exc.detail), 600)}")
    return Reply(
        f"Stopping {instance.name} (the world is saved first).",
        ephemeral=False,
        announce=f"{caller.name} stopped {instance.name}.",
    )


def follow_up(instance_id: str, job_id: str) -> str | None:
    """One look at a start: the message once it is decided (ready, or failed with the panel's message), else None."""
    from apps.instances.models import Instance
    from apps.jobs.models import Job, JobStatus

    instance = Instance.objects.filter(pk=instance_id).first()
    job = Job.objects.filter(pk=job_id).first()
    if instance is None or job is None:
        return "The server is gone."
    if job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
        return f"{instance.name} did not start: {_clip(job.error or job.status, 600)}"
    if instance.status == "running" and job.status == JobStatus.SUCCEEDED:
        return f"{instance.name} is ready."
    if instance.status in ("failed", "stopped") and job.status == JobStatus.SUCCEEDED:
        return f"{instance.name} stopped again right after its start: {_clip(instance.status_detail or 'see the panel', 600)}"
    return None


def follow_timeout(instance_id: str) -> str:
    from apps.instances.models import Instance

    instance = Instance.objects.filter(pk=instance_id).first()
    name = instance.name if instance is not None else "The server"
    return f"{name} is still starting after {FOLLOW_SECONDS // 60} minutes; see the panel for how it goes."
