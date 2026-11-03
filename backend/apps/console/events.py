"""Fan-out of live updates (instance status, job progress) over Channels groups."""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

log = logging.getLogger(__name__)

EVENTS_GROUP = "mcpanel.events"


def broadcast(event: str, payload: dict[str, Any], instance_id: str | None = None) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    message = {"type": "panel.event", "event": event, "payload": payload, "instance_id": instance_id}
    try:
        async_to_sync(layer.group_send)(EVENTS_GROUP, message)
    except Exception as exc:  # never let a broken channel layer fail an operation
        log.warning("event broadcast failed: %s", exc)
