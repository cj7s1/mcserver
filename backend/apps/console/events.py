"""Fan-out of live updates (instance status, job progress) over Channels groups."""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

log = logging.getLogger(__name__)

EVENTS_GROUP = "mcpanel.events"


def broadcast(
    event: str, payload: dict[str, Any], instance_id: str | None = None, *, owner_id: str | bool | None = False
) -> None:
    """``instance_id``: only users who may see that server get it. ``owner_id`` (an event without a server, such
    as the job of a deleted server): only that user (``None``: nobody in particular) and users who see every
    server get it, as ``GET /api/jobs`` lists such jobs. Without either, every signed-in user gets it."""
    layer = get_channel_layer()
    if layer is None:
        return
    message = {"type": "panel.event", "event": event, "payload": payload, "instance_id": instance_id}
    if owner_id is not False and not instance_id:
        message["owner_id"] = str(owner_id) if owner_id is not None else ""
    try:
        async_to_sync(layer.group_send)(EVENTS_GROUP, message)
    except Exception as exc:  # never let a broken channel layer fail an operation
        log.warning("event broadcast failed: %s", exc)
