"""The current player count of a server from the samples the worker takes every minute (no agent call).

Used by the Discord bot (``/servers``, ``/status``, ``/stop``) and the short instance answer of a Friend
(``players_online`` / ``players_max``). Unknown (None) when the server is not running, the newest sample is
older than FRESH_SECONDS, or that sample could not read the count.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from .models import MetricSample

FRESH_SECONDS = 180
RUNNING_STATES = ("running", "unhealthy", "backing_up")


def current_players(instance: Any) -> tuple[int | None, int | None]:
    """(players online, player slots) of ``instance``; each None when unknown."""
    if getattr(instance, "status", None) not in RUNNING_STATES:
        return None, None
    sample = (
        MetricSample.objects.filter(instance_id=instance.pk)
        .order_by("-taken_at")
        .values("taken_at", "players_online", "players_max")
        .first()
    )
    if not sample or timezone.now() - sample["taken_at"] > timedelta(seconds=FRESH_SECONDS):
        return None, None
    online, most = sample["players_online"], sample["players_max"]
    return (None if online is None else int(online)), (None if most is None else int(most))
