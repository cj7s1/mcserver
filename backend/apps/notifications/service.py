"""Outbound notifications (Discord / generic webhook). Delivery runs in the worker."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .models import EVENTS, NotificationChannel

log = logging.getLogger(__name__)
COLORS = {"failed": 0xE5484D, "crash": 0xE5484D, "succeeded": 0x30A46C, "running": 0x30A46C, "stopped": 0x8B8D98}


def validate_url(kind: str, url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("Enter a full http(s) URL")
    if kind == "discord":
        if parsed.scheme != "https" or parsed.hostname not in (
            "discord.com",
            "discordapp.com",
            "ptb.discord.com",
            "canary.discord.com",
        ):
            raise ValueError("Discord webhooks look like https://discord.com/api/webhooks/...")
        if not parsed.path.startswith("/api/webhooks/"):
            raise ValueError("Discord webhooks look like https://discord.com/api/webhooks/...")
    return url.strip()


def _payload(channel: NotificationChannel, event: str, title: str, message: str, data: dict[str, Any]) -> dict[str, Any]:
    if channel.kind == "discord":
        color = next((c for k, c in COLORS.items() if k in event), 0x5B5BD6)
        fields = [
            {"name": k.replace("_", " ").title(), "value": str(v)[:1000], "inline": True}
            for k, v in data.items()
            if v not in (None, "") and k in ("server", "job", "status", "detail", "error")
        ]
        return {
            "username": "MCPanel",
            "embeds": [
                {
                    "title": title[:250],
                    "description": message[:3500],
                    "color": color,
                    "fields": fields[:10],
                    "timestamp": timezone.now().isoformat(),
                }
            ],
        }
    return {"event": event, "title": title, "message": message, "data": data, "sent_at": timezone.now().isoformat()}


@shared_task(
    name="apps.notifications.deliver",
    ignore_result=True,
    autoretry_for=(httpx.TransportError,),
    retry_backoff=True,
    max_retries=3,
)
def deliver(channel_id: int, event: str, title: str, message: str, data: dict[str, Any]) -> None:
    channel = NotificationChannel.objects.filter(pk=channel_id, enabled=True).first()
    if channel is None:
        return
    try:
        resp = httpx.post(
            channel.url,
            json=_payload(channel, event, title, message, data),
            timeout=10,
            headers={"User-Agent": settings.MCPANEL_USER_AGENT},
        )
        error = "" if resp.status_code < 400 else f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}"
        NotificationChannel.objects.filter(pk=channel_id).update(last_error=error[:500])
        raise
    NotificationChannel.objects.filter(pk=channel_id).update(last_sent_at=timezone.now(), last_error=error[:500])


def notify(event: str, title: str, message: str, *, instance: Any = None, data: dict[str, Any] | None = None) -> int:
    """Queue a notification to every matching channel. Never raises."""
    if event not in EVENTS:
        return 0
    try:
        qs = NotificationChannel.objects.filter(enabled=True)
        if instance is not None:
            qs = qs.filter(instance__isnull=True) | qs.filter(instance=instance)
        else:
            qs = qs.filter(instance__isnull=True)
        payload = dict(data or {})
        if instance is not None:
            payload.setdefault("server", instance.name)
        count = 0
        for ch in qs:
            if event in (ch.events or []):
                deliver.delay(ch.pk, event, title, message, payload)
                count += 1
        return count
    except Exception as exc:  # notifications must never break the caller
        log.warning("notify(%s) failed: %s", event, exc)
        return 0
