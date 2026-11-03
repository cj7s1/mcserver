"""WebSocket consumers.

* ConsoleConsumer: relays the agent's live container log stream to the browser and
  executes console commands through RCON (permission-checked per command).
* EventsConsumer: pushes instance status/job updates the user is allowed to see.

Both rely on the Django session (cookie) for authentication and on
AllowedHostsOriginValidator for cross-site WebSocket protection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import websockets
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.core.errors import ApiError

from .events import EVENTS_GROUP

log = logging.getLogger(__name__)


@database_sync_to_async
def _load_instance(user: Any, instance_id: str) -> tuple[Any, frozenset[str]] | None:
    from apps.instances.models import Instance

    if not user.is_authenticated:
        return None
    inst = Instance.objects.select_related("node").filter(pk=instance_id).first()
    if inst is None:
        return None
    perms = user.instance_permissions(inst.pk)
    if "instance.view" not in perms:
        return None
    return inst, perms


@database_sync_to_async
def _run_command(user: Any, instance: Any, command: str) -> str:
    from apps.audit import service as audit
    from apps.nodes.client import AgentClient

    output = AgentClient(instance.node).command(instance.id, command)
    audit.record("instance.command", actor=user, instance=instance, target=instance, details={"command": command[:300]})
    return output


class ConsoleConsumer(AsyncJsonWebsocketConsumer):
    relay_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.instance_id = str(self.scope["url_route"]["kwargs"]["instance_id"])
        user = self.scope.get("user")
        loaded = await _load_instance(user, self.instance_id) if user else None
        if loaded is None:
            await self.close(code=4403)
            return
        self.instance, self.perms = loaded
        if "instance.console" not in self.perms:
            await self.close(code=4403)
            return
        await self.accept()
        await self.send_json({"type": "hello", "can_command": "instance.command" in self.perms})
        self.relay_task = asyncio.create_task(self._relay())

    async def disconnect(self, code: int) -> None:
        if self.relay_task:
            self.relay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.relay_task

    async def _relay(self) -> None:
        from apps.nodes.client import AgentClient

        client = await database_sync_to_async(AgentClient)(self.instance.node)
        url = client.ws_url(f"/v1/instances/{self.instance.id}/logs/stream?tail=300")
        tls_kwargs: dict[str, Any] = {}
        backoff = 1.0
        first = True
        while True:
            try:
                if url.startswith("wss://") and not tls_kwargs:
                    # may read and pin the agent certificate (blocking I/O + DB) on first use
                    tls_kwargs["ssl"] = await database_sync_to_async(lambda: client.ssl_context)()
                async with websockets.connect(
                    url,
                    additional_headers=client.headers,
                    open_timeout=10,
                    ping_interval=20,
                    max_size=8 * 1024 * 1024,
                    **tls_kwargs,
                ) as ws:
                    await self.send_json({"type": "agent", "connected": True})
                    backoff = 1.0
                    if not first:
                        # resubscribed: the agent re-sends the tail; tell the client to reset its buffer
                        await self.send_json({"type": "reset"})
                    first = False
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "ping":
                            continue
                        await self.send_json(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("console relay for %s lost: %s", self.instance_id, exc)
                with contextlib.suppress(Exception):
                    await self.send_json(
                        {"type": "agent", "connected": False, "message": "Lost connection to the node agent; retrying"}
                    )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    async def receive_json(self, content: dict[str, Any], **kwargs: Any) -> None:
        if content.get("type") != "command":
            return
        req_id = content.get("id")
        if "instance.command" not in self.perms:
            await self.send_json({"type": "command_result", "id": req_id, "ok": False, "error": "You may not send commands"})
            return
        command = str(content.get("command", "")).strip().lstrip("/")
        if not command or len(command) > 1400 or "\n" in command or "\r" in command:
            await self.send_json({"type": "command_result", "id": req_id, "ok": False, "error": "Invalid command"})
            return
        try:
            output = await _run_command(self.scope["user"], self.instance, command)
            await self.send_json({"type": "command_result", "id": req_id, "ok": True, "command": command, "output": output})
        except ApiError as exc:
            await self.send_json(
                {
                    "type": "command_result",
                    "id": req_id,
                    "ok": False,
                    "command": command,
                    "error": str(exc.detail),
                    "code": exc.error_code,
                }
            )


@database_sync_to_async
def _can_view(user: Any, instance_id: str) -> bool:
    return "instance.view" in user.instance_permissions(instance_id)


class EventsConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self) -> None:
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return
        self.user = user
        self._cache: dict[str, tuple[float, bool]] = {}
        await self.channel_layer.group_add(EVENTS_GROUP, self.channel_name)
        await self.accept()

    async def disconnect(self, code: int) -> None:
        with contextlib.suppress(Exception):
            await self.channel_layer.group_discard(EVENTS_GROUP, self.channel_name)

    async def panel_event(self, message: dict[str, Any]) -> None:
        instance_id = message.get("instance_id")
        if instance_id:
            cached = self._cache.get(instance_id)
            if cached is None or time.monotonic() - cached[0] > 30:
                cached = (time.monotonic(), await _can_view(self.user, instance_id))
                self._cache[instance_id] = cached
            if not cached[1]:
                return
        await self.send_json({"type": message["event"], "instance_id": instance_id, "payload": message["payload"]})
