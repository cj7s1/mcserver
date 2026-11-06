"""WebSocket consumers.

* ConsoleConsumer: relays the agent's live container log stream to the browser and
  executes console commands through RCON (permission-checked per command).
* EventsConsumer: pushes instance status/job updates the user is allowed to see.

Both rely on the Django session (cookie) for authentication and on
AllowedHostsOriginValidator for cross-site WebSocket protection.

Access is not frozen at connect time: before every console command, and every
``REVALIDATE_SECONDS`` while a socket is open, the session is looked up again (logout,
expiry and password changes end it), the account is reloaded (deleted or disabled accounts
end it) and the permissions are recomputed. A socket whose access is gone is closed
(4401 = no valid session, 4403 = no permission).

Network calls to the agent (RCON commands, reading a node certificate) run in the thread
pool (``thread_sensitive=False``): in the shared sync thread they would block every other
WebSocket and every sync view of the process while they wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from importlib import import_module
from typing import Any

import websockets
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.core.errors import ApiError

from .events import EVENTS_GROUP

log = logging.getLogger(__name__)

REVALIDATE_SECONDS = 30.0
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403


def _session_key(scope: dict[str, Any]) -> str | None:
    session = scope.get("session")
    return getattr(session, "session_key", None) if session is not None else None


def session_user(session_key: str | None, user_id: Any) -> Any | None:
    """The active user the session ``session_key`` still authenticates, if it is ``user_id``.

    Mirrors django.contrib.auth.get_user() without its side effects (it would flush or
    cycle the browser's session): the session must still exist, name the same user and
    carry a valid session auth hash (a password change invalidates it), and the
    authentication backend must still return the user (ModelBackend refuses inactive ones).
    """
    from django.conf import settings
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model, load_backend
    from django.utils.crypto import constant_time_compare

    if not session_key or user_id is None:
        return None
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    try:
        data = store.load()
    except Exception:  # unreadable/corrupt session data
        return None
    raw_id, backend_path = data.get(SESSION_KEY), data.get(BACKEND_SESSION_KEY)
    if raw_id is None or backend_path not in settings.AUTHENTICATION_BACKENDS:
        return None
    try:
        pk = get_user_model()._meta.pk.to_python(raw_id)
    except Exception:
        return None
    if pk != user_id:
        return None
    user = load_backend(backend_path).get_user(pk)
    if user is None or not user.is_active:
        return None
    session_hash = data.get(HASH_SESSION_KEY) or ""
    hashes = [user.get_session_auth_hash(), *user.get_session_auth_fallback_hash()]
    if not session_hash or not any(constant_time_compare(session_hash, h) for h in hashes):
        return None
    return user


def _instance_access(user: Any, instance_id: str) -> tuple[Any, frozenset[str]] | None:
    from apps.instances.models import Instance

    if not user or not user.is_authenticated or not user.is_active or getattr(user, "must_change_password", False):
        return None
    inst = Instance.objects.select_related("node").filter(pk=instance_id).first()
    if inst is None:
        return None
    perms = user.instance_permissions(inst.pk)
    if "instance.view" not in perms:
        return None
    return inst, perms


@database_sync_to_async
def _load_instance(user: Any, instance_id: str) -> tuple[Any, frozenset[str]] | None:
    return _instance_access(user, instance_id)


@database_sync_to_async
def _revalidate(session_key: str | None, user_id: Any, instance_id: str | None) -> tuple[Any, Any] | None:
    """(fresh user, instance access or None) while the session is valid, else None."""
    user = session_user(session_key, user_id)
    if user is None:
        return None
    return user, (_instance_access(user, instance_id) if instance_id else None)


def _agent_command(node: Any, instance_id: Any, command: str) -> str:
    from apps.nodes.client import AgentClient

    return AgentClient(node).command(instance_id, command)


def _audit_command(user: Any, instance: Any, command: str) -> None:
    from apps.audit import service as audit

    audit.record("instance.command", actor=user, instance=instance, target=instance, details={"command": command[:300]})


async def _run_command(user: Any, instance: Any, command: str) -> str:
    # RCON round trip (up to 30 s) off the shared sync thread; the audit write stays on it.
    output = await database_sync_to_async(_agent_command, thread_sensitive=False)(instance.node, instance.id, command)
    await database_sync_to_async(_audit_command)(user, instance, command)
    return output


class ConsoleConsumer(AsyncJsonWebsocketConsumer):
    relay_task: asyncio.Task | None = None
    watch_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.instance_id = str(self.scope["url_route"]["kwargs"]["instance_id"])
        user = self.scope.get("user")
        loaded = await _load_instance(user, self.instance_id) if user else None
        if loaded is None:
            await self.close(code=CLOSE_FORBIDDEN)
            return
        self.instance, self.perms = loaded
        if "instance.console" not in self.perms:
            await self.close(code=CLOSE_FORBIDDEN)
            return
        self.user = user
        self.user_id = user.pk
        self.session_key = _session_key(self.scope)
        self._closing = False
        await self.accept()
        await self.send_json({"type": "hello", "can_command": "instance.command" in self.perms})
        self.relay_task = asyncio.create_task(self._relay())
        self.watch_task = asyncio.create_task(self._watch())

    async def disconnect(self, code: int) -> None:
        for task in (self.relay_task, self.watch_task):
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _end(self, code: int) -> None:
        if self._closing:
            return
        self._closing = True
        if self.relay_task:
            self.relay_task.cancel()
        await self.close(code=code)

    async def _check_access(self) -> bool:
        """Re-read session, account and permissions; close the socket when access is gone."""
        if self._closing:
            return False
        result = await _revalidate(self.session_key, self.user_id, self.instance_id)
        if result is None:
            await self._end(CLOSE_UNAUTHENTICATED)
            return False
        user, access = result
        if access is None or "instance.console" not in access[1]:
            await self._end(CLOSE_FORBIDDEN)
            return False
        could_command = "instance.command" in self.perms
        self.user, (self.instance, self.perms) = user, access
        if could_command != ("instance.command" in self.perms):
            await self.send_json({"type": "hello", "can_command": "instance.command" in self.perms})
        return True

    async def _watch(self) -> None:
        while not self._closing:
            await asyncio.sleep(REVALIDATE_SECONDS)
            try:
                await self._check_access()
            except asyncio.CancelledError:
                raise
            except Exception:  # a transient DB error must not kill the watchdog silently
                log.exception("console access re-check failed for %s", self.instance_id)

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
                    # may read and pin the agent certificate (network I/O + DB) on first use
                    tls_kwargs["ssl"] = await database_sync_to_async(lambda: client.ssl_context, thread_sensitive=False)()
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
        if not await self._check_access():  # revoked grants, disabled users and logged-out sessions
            return
        if "instance.command" not in self.perms:
            await self.send_json({"type": "command_result", "id": req_id, "ok": False, "error": "You may not send commands"})
            return
        command = str(content.get("command", "")).strip().lstrip("/")
        if not command or len(command) > 1400 or "\n" in command or "\r" in command:
            await self.send_json({"type": "command_result", "id": req_id, "ok": False, "error": "Invalid command"})
            return
        try:
            output = await _run_command(self.user, self.instance, command)
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
    watch_task: asyncio.Task | None = None

    async def connect(self) -> None:
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not user.is_active:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return
        if getattr(user, "must_change_password", False):
            await self.close(code=CLOSE_FORBIDDEN)
            return
        self.user = user
        self.user_id = user.pk
        self.session_key = _session_key(self.scope)
        self._closing = False
        self._cache: dict[str, tuple[float, bool]] = {}
        await self.channel_layer.group_add(EVENTS_GROUP, self.channel_name)
        await self.accept()
        self.watch_task = asyncio.create_task(self._watch())

    async def disconnect(self, code: int) -> None:
        if self.watch_task and self.watch_task is not asyncio.current_task():
            self.watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.watch_task
        with contextlib.suppress(Exception):
            await self.channel_layer.group_discard(EVENTS_GROUP, self.channel_name)

    async def _check_access(self) -> bool:
        if self._closing:
            return False
        result = await _revalidate(self.session_key, self.user_id, None)
        user = result[0] if result else None
        if user is None or getattr(user, "must_change_password", False):
            self._closing = True
            with contextlib.suppress(Exception):
                await self.channel_layer.group_discard(EVENTS_GROUP, self.channel_name)
            await self.close(code=CLOSE_UNAUTHENTICATED if user is None else CLOSE_FORBIDDEN)
            return False
        self.user = user  # fresh role/flags for the per-instance checks below
        self._cache.clear()
        return True

    async def _watch(self) -> None:
        while not self._closing:
            await asyncio.sleep(REVALIDATE_SECONDS)
            try:
                await self._check_access()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("events access re-check failed")

    async def panel_event(self, message: dict[str, Any]) -> None:
        if self._closing:
            return
        instance_id = message.get("instance_id")
        if instance_id:
            cached = self._cache.get(instance_id)
            if cached is None or time.monotonic() - cached[0] > REVALIDATE_SECONDS:
                cached = (time.monotonic(), await _can_view(self.user, instance_id))
                self._cache[instance_id] = cached
            if not cached[1]:
                return
        await self.send_json({"type": message["event"], "instance_id": instance_id, "payload": message["payload"]})
