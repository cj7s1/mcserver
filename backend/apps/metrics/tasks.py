"""Periodic sampling of running servers (resource usage + online players)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.core.errors import ApiError
from apps.instances.models import Instance, Status
from apps.nodes.client import AgentClient

from .models import MetricSample, PlayerSeen

log = logging.getLogger(__name__)
SAMPLE_INTERVAL = timedelta(seconds=60)
RETENTION = timedelta(days=8)
# player history rows of players not seen for this long are removed (cleanup): chat-made or one-time names
PLAYER_SEEN_RETENTION = timedelta(days=90)
# a count the game (or a player's chat line, or a compromised server) reports is kept only in 0..this (C-1)
MAX_PLAYER_COUNT = 100_000
MAX_PLAYER_NAMES = 200
NAME_LENGTH = 64  # PlayerSeen.name; Factorio, Steam and Terraria names are longer than Minecraft's 16


def bounded_count(value: Any) -> int | None:
    """A player count as reported (an int, an integral float, a digit string), or None when it is not a number
    in 0..MAX_PLAYER_COUNT: the value comes from the game server, and with Terraria's console from its chat."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 9:
        value = int(value.strip())
    if not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_PLAYER_COUNT else None


def bounded_players(answer: dict[str, Any] | None) -> dict[str, Any] | None:
    """``player_count``'s answer with counts in range (else None) and at most MAX_PLAYER_NAMES text names."""
    if not isinstance(answer, dict):
        return None
    out = dict(answer)
    out["online"] = bounded_count(answer.get("online"))
    out["max"] = bounded_count(answer.get("max"))
    names = answer.get("players")
    out["players"] = [n for n in names if isinstance(n, str)][:MAX_PLAYER_NAMES] if isinstance(names, list) else []
    return out


def record_players(instance: Instance, names: list[str], now) -> None:  # type: ignore[no-untyped-def]
    for name in names[:MAX_PLAYER_NAMES]:
        name = str(name).strip()[:NAME_LENGTH]
        if not name:
            continue
        # (get_or_create: two overlapping runs no longer fail on the unique constraint)
        seen, created = PlayerSeen.objects.get_or_create(
            instance=instance, name=name, defaults={"first_seen": now, "last_seen": now, "minutes_online": 1}
        )
        if created:
            continue
        new_session = now - seen.last_seen > SAMPLE_INTERVAL * 3
        seen.minutes_online += 1
        if new_session:
            seen.sessions += 1
        seen.last_seen = now
        seen.save(update_fields=["minutes_online", "sessions", "last_seen"])


PING_FEATURE = "ping"  # the agent's name for GET /v1/instances/{id}/ping in GET /v1/info ("features")


def player_count(instance: Instance, client: AgentClient) -> dict[str, Any] | None:
    """Players online: ``{"online", "max", "players"}``, or None when unknown.

    Asked with a Minecraft status ping through the agent (``AgentClient.ping``): the server
    does not log it. An RCON ``list`` would add two lines ("RCON Client ... started / shutting
    down") to the console and the server log every minute, so it is used only with agents that
    cannot ping (no ``ping`` feature). A ping lists at most a sample of the names (Minecraft
    sends up to 12), so the player history of a busy server misses some names.
    """
    from apps import games
    from apps.players.service import NAME_RE

    definition = games.get_or_none(getattr(instance, "game", None))
    if definition is None:
        return None
    caps = definition.capabilities(instance)
    # the agent's ping is the Minecraft Server List Ping (capability "query")
    if "query" in caps and client.supports(PING_FEATURE):
        try:
            answer = client.ping(instance.id)
        except ApiError as exc:
            log.debug("ping failed for %s: %s", instance.pk, exc)
            return None
        names = []
        for entry in answer.get("sample") or []:
            name = entry.get("name") if isinstance(entry, dict) else entry
            # the sample is whatever the server (or a plugin) puts there: only real player names
            if isinstance(name, str) and NAME_RE.match(name.strip()) and name.strip() not in names:
                names.append(name.strip())
        return bounded_players({"online": answer.get("online"), "max": answer.get("max"), "players": names})
    if "players" not in caps:
        return None
    try:
        return bounded_players(definition.online_players(instance))
    except ApiError:
        return None


@shared_task(name="apps.metrics.tasks.sample_all", ignore_result=True, expires=50)
def sample_all() -> int:
    now = timezone.now()
    count = 0
    for inst in Instance.objects.select_related("node").filter(status__in=[Status.RUNNING, Status.UNHEALTHY]):
        # one server's bad answer never costs the servers after it their sample (C-1)
        try:
            count += _sample(inst, now)
        except Exception:
            log.exception("metrics sample of %s failed", inst.pk)
    return count


def _sample(inst: Instance, now) -> int:  # type: ignore[no-untyped-def]
    client = AgentClient(inst.node, timeout=15)
    try:
        stats = client.stats(inst.id)
    except ApiError as exc:
        log.debug("stats failed for %s: %s", inst.pk, exc)
        return 0
    if not stats.get("running"):
        return 0
    players = player_count(inst, client)
    with transaction.atomic():
        MetricSample.objects.create(
            instance=inst,
            taken_at=now,
            cpu_percent=stats.get("cpu_percent"),
            memory_bytes=stats.get("memory_bytes"),
            memory_limit_bytes=stats.get("memory_limit_bytes"),
            network_rx_bytes=stats.get("network_rx_bytes"),
            network_tx_bytes=stats.get("network_tx_bytes"),
            players_online=(players or {}).get("online"),
            players_max=(players or {}).get("max"),
        )
        if players and players.get("players"):
            record_players(inst, players["players"], now)
    return 1


@shared_task(name="apps.metrics.tasks.cleanup", ignore_result=True)
def cleanup() -> int:
    now = timezone.now()
    deleted, _ = MetricSample.objects.filter(taken_at__lt=now - RETENTION).delete()
    PlayerSeen.objects.filter(last_seen__lt=now - PLAYER_SEEN_RETENTION).delete()
    return deleted
