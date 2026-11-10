"""Periodic sampling of running servers (resource usage + online players)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.utils import timezone

from apps.core.errors import ApiError
from apps.instances.models import Instance, Status
from apps.nodes.client import AgentClient

from .models import MetricSample, PlayerSeen

log = logging.getLogger(__name__)
SAMPLE_INTERVAL = timedelta(seconds=60)
RETENTION = timedelta(days=8)


def record_players(instance: Instance, names: list[str], now) -> None:  # type: ignore[no-untyped-def]
    for name in names[:500]:
        name = name.strip()[:16]
        if not name:
            continue
        seen = PlayerSeen.objects.filter(instance=instance, name=name).first()
        if seen is None:
            PlayerSeen.objects.create(instance=instance, name=name, first_seen=now, last_seen=now, minutes_online=1)
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
    from apps.players.service import NAME_RE, online_players

    if client.supports(PING_FEATURE):
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
        return {"online": answer.get("online"), "max": answer.get("max"), "players": names}
    try:
        return online_players(instance)
    except ApiError:
        return None


@shared_task(name="apps.metrics.tasks.sample_all", ignore_result=True, expires=50)
def sample_all() -> int:
    now = timezone.now()
    count = 0
    for inst in Instance.objects.select_related("node").filter(status__in=[Status.RUNNING, Status.UNHEALTHY]):
        client = AgentClient(inst.node, timeout=15)
        try:
            stats = client.stats(inst.id)
        except ApiError as exc:
            log.debug("stats failed for %s: %s", inst.pk, exc)
            continue
        if not stats.get("running"):
            continue
        players = player_count(inst, client)
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
        count += 1
    return count


@shared_task(name="apps.metrics.tasks.cleanup", ignore_result=True)
def cleanup() -> int:
    deleted, _ = MetricSample.objects.filter(taken_at__lt=timezone.now() - RETENTION).delete()
    return deleted
