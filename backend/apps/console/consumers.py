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
# D-1 (I-7): the first frame of the client must be {"type": "auth", "token": <the session token>} within this
AUTH_TIMEOUT_SECONDS = 10.0
# ... and the server's first frame after it is this one, on every consumer
AUTH_OK = {"type": "auth_ok"}


@database_sync_to_async
def _token_ok(session_key: str | None, token: Any) -> bool:
    from apps.core import session_binding

    return session_binding.session_token_ok(session_key, token)


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


def is_stop_command(command: str, definition: Any = None) -> bool:
    """A stop word of the server's game typed in the console (Minecraft: ``stop``, any case, with
    leading slashes and surrounding blanks; ``definition`` defaults to Minecraft's).

    Sent over RCON it makes the server exit, and Docker's restart policy then starts it again at
    once: the server is not stopped, it restarts. The console runs the panel's Stop instead.
    """
    from apps import games

    return (definition or games.get(games.DEFAULT_GAME)).is_stop_command(command)


def _enqueue_stop(user: Any, instance: Any) -> Any:
    """The Stop button's job (apps.instances.services.power), audited as the user's ``instance.stop``."""
    from apps.audit import service as audit
    from apps.core.errors import ConflictError
    from apps.instances.models import Instance
    from apps.instances.services import enqueue_power

    fresh = Instance.objects.select_related("node").get(pk=instance.pk)
    if fresh.archived_at:
        raise ConflictError("This server is archived", code="archived")
    job = enqueue_power(fresh, "stop", user=user)
    audit.record("instance.stop", actor=user, instance=fresh, target=fresh, details={"job": str(job.id), "via": "console"})
    return job


class _GuardedConsumer(AsyncJsonWebsocketConsumer):
    """Cleanup that runs however the consumer ends.

    Channels calls ``disconnect()`` only for a ``websocket.disconnect`` message. A consumer that
    ends with an exception instead (a send to a browser that went away, a channel layer error)
    would leave its background tasks running for as long as the session lives.
    """

    _authed = False
    _auth_task: asyncio.Task | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with contextlib.suppress(Exception):
                await _cancel(self._auth_task)
            with contextlib.suppress(Exception):
                await self._cleanup()

    async def _cleanup(self) -> None:
        raise NotImplementedError

    async def authenticated(self) -> None:
        """The socket may be used: start sending (after the auth frame, D-1)."""
        raise NotImplementedError

    async def _accept_and_authenticate(self) -> None:
        """Accept the socket, then wait for the client's auth frame (``MCPANEL_SESSION_BINDING``): nothing is sent
        and no other frame is taken before it; no valid frame within AUTH_TIMEOUT_SECONDS closes with 4401.
        Without the binding the socket is used at once (an auth frame is then ignored)."""
        from apps.core import session_binding

        await self.accept()
        if not session_binding.enabled():
            self._authed = True
            await self.send_json(AUTH_OK)  # (the first frame in either mode; a later auth frame is ignored)
            await self.authenticated()
            return
        self._auth_task = asyncio.create_task(self._auth_deadline())

    async def _auth_deadline(self) -> None:
        await asyncio.sleep(AUTH_TIMEOUT_SECONDS)
        if not self._authed:
            await self._refuse_auth()

    async def _refuse_auth(self) -> None:
        self._closing = True
        with contextlib.suppress(Exception):  # the browser may be gone already
            await self.close(code=CLOSE_UNAUTHENTICATED)

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None, **kwargs: Any) -> None:
        """Ignore frames that are no JSON object (binary, invalid JSON, a list): they used to end
        the consumer with an exception. Before the auth frame (D-1) any other frame closes the socket."""
        content: Any = None
        if text_data is not None:
            try:
                content = await self.decode_json(text_data)
            except ValueError:
                content = None
        if not self._authed:
            if getattr(self, "_closing", False):
                return
            token = content.get("token") if isinstance(content, dict) and content.get("type") == "auth" else None
            if token is None or not await _token_ok(getattr(self, "session_key", None), token):
                await _cancel(self._auth_task)
                await self._refuse_auth()
                return
            self._authed = True
            await _cancel(self._auth_task)
            await self.send_json(AUTH_OK)  # the first server frame: the client may go on (before hello, events)
            await self.authenticated()
            return
        if isinstance(content, dict) and content.get("type") != "auth":
            await self.receive_json(content, **kwargs)

    async def _send(self, content: dict[str, Any]) -> bool:
        """send_json that reports a browser that went away (False) instead of raising."""
        try:
            await self.send_json(content)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True


async def _cancel(task: asyncio.Task | None) -> None:
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


class ConsoleConsumer(_GuardedConsumer):
    """Server -> browser messages: ``hello{can_command}``, ``agent{connected, message?}``,
    ``reset``, the agent's log messages (``logs``, ``log``, ``state``, ``error``, ``closed``),
    ``command_result{id, ok, command?, output? | error?, code?}`` and ``notice{level, message, job_id?}``
    (a message of the panel to show in the console, e.g. for a typed ``stop``)."""

    relay_task: asyncio.Task | None = None
    watch_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.instance_id = str(self.scope["url_route"]["kwargs"]["instance_id"])
        self._closing = False
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
        await self._accept_and_authenticate()

    async def authenticated(self) -> None:
        await self.send_json({"type": "hello", "can_command": "instance.command" in self.perms})
        self.relay_task = asyncio.create_task(self._relay())
        self.watch_task = asyncio.create_task(self._watch())

    async def disconnect(self, code: int) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        self._closing = True
        await _cancel(self.relay_task)
        await _cancel(self.watch_task)

    async def _end(self, code: int) -> None:
        if self._closing:
            return
        self._closing = True
        if self.relay_task:
            self.relay_task.cancel()
        with contextlib.suppress(Exception):  # the browser may be gone already
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
        while not self._closing:
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
                    if not await self._send({"type": "agent", "connected": True}):
                        return  # the browser is gone: never reconnect to the agent for it
                    backoff = 1.0
                    # resubscribed: the agent re-sends the tail; tell the client to reset its buffer
                    if not first and not await self._send({"type": "reset"}):
                        return
                    first = False
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "ping":
                            continue
                        if not await self._send(msg):
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("console relay for %s lost: %s", self.instance_id, exc)
                if not await self._send(
                    {"type": "agent", "connected": False, "message": "Lost connection to the node agent; retrying"}
                ):
                    return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    async def receive_json(self, content: Any, **kwargs: Any) -> None:
        if not isinstance(content, dict) or content.get("type") != "command":
            return
        req_id = content.get("id")
        if not await self._check_access():  # revoked grants, disabled users and logged-out sessions
            return
        if "instance.command" not in self.perms:
            await self._send({"type": "command_result", "id": req_id, "ok": False, "error": "You may not send commands"})
            return
        from apps import games

        definition = games.get_or_none(getattr(self.instance, "game", None))
        if definition is None or not definition.has(self.instance, "console_commands"):
            error = f"{definition.label if definition else 'This game'} servers have no console commands"
            await self._send({"type": "command_result", "id": req_id, "ok": False, "error": error, "code": "unsupported_by_game"})
            return
        command = definition.normalise_command(str(content.get("command", "")).strip())
        if not command or len(command) > 1400 or "\n" in command or "\r" in command:
            await self._send({"type": "command_result", "id": req_id, "ok": False, "error": "Invalid command"})
            return
        if is_stop_command(command, definition):
            await self._stop(req_id, command)
            return
        try:
            output = await _run_command(self.user, self.instance, command)
            result = {"type": "command_result", "id": req_id, "ok": True, "command": command, "output": output}
        except ApiError as exc:
            result = {
                "type": "command_result",
                "id": req_id,
                "ok": False,
                "command": command,
                "error": str(exc.detail),
                "code": exc.error_code,
            }
        if not await self._send(result):
            # the browser went away while the command ran (RCON can take 40 s): end cleanly
            await self._end(1000)

    async def _stop(self, req_id: Any, command: str) -> None:
        """A typed ``stop`` runs the panel's Stop (Docker would restart a server stopped over RCON).

        The answer is a ``notice`` (``level`` info/error, ``message``, the command's ``id``, and
        ``job_id`` of the stop job) followed by the ``command_result`` with the same ``id`` and the
        same text; a client that showed the notice can skip the result's text."""
        from apps import games

        definition = games.get_or_none(getattr(self.instance, "game", None))
        word = definition.stop_word if definition is not None else "stop"  # Minecraft: stop
        if "instance.control" not in self.perms:
            message = (
                f"Not sent: '{word}' over the console makes the server exit, and Docker starts it again at once. "
                "Stopping the server needs the permission to start and stop it, which you do not have."
            )
            await self._send({"type": "notice", "level": "error", "message": message, "id": req_id})
            await self._send(
                {
                    "type": "command_result",
                    "id": req_id,
                    "ok": False,
                    "command": command,
                    "error": message,
                    "code": "stop_refused",
                }
            )
            return
        try:
            job = await database_sync_to_async(_enqueue_stop)(self.user, self.instance)
        except ApiError as exc:
            message = f"Could not stop the server: {exc.detail}"
            await self._send({"type": "notice", "level": "error", "message": message, "id": req_id})
            await self._send(
                {
                    "type": "command_result",
                    "id": req_id,
                    "ok": False,
                    "command": command,
                    "error": message,
                    "code": exc.error_code,
                }
            )
            return
        message = (
            f"'{word}' was not sent to the server: the panel is stopping it instead (the same as the Stop button), "
            "so Docker does not start it again."
        )
        await self._send({"type": "notice", "level": "info", "message": message, "id": req_id, "job_id": str(job.id)})
        await self._send({"type": "command_result", "id": req_id, "ok": True, "command": command, "output": message})


@database_sync_to_async
def _can_view(user: Any, instance_id: str) -> bool:
    return "instance.view" in user.instance_permissions(instance_id)


@database_sync_to_async
def _sees_every_server(user: Any) -> bool:
    return "instance.view" in user.global_permissions()


# channels_redis forgets a group member after group_expiry (default 86400 s) unless it is added
# again: a socket open for a day would silently stop getting events. Re-add it well before that.
GROUP_REFRESH_SECONDS = 3600.0


class EventsConsumer(_GuardedConsumer):
    watch_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self._closing = False
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
        self._cache: dict[str, tuple[float, bool]] = {}
        await self._accept_and_authenticate()

    async def authenticated(self) -> None:
        # (events only after the auth frame, D-1)
        await self.channel_layer.group_add(EVENTS_GROUP, self.channel_name)
        self._joined = time.monotonic()
        self.watch_task = asyncio.create_task(self._watch())

    async def disconnect(self, code: int) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        self._closing = True
        await _cancel(self.watch_task)
        if getattr(self, "channel_name", None) and self.channel_layer is not None:
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

    async def _refresh_membership(self) -> None:
        if time.monotonic() - self._joined >= GROUP_REFRESH_SECONDS:
            await self.channel_layer.group_add(EVENTS_GROUP, self.channel_name)
            self._joined = time.monotonic()

    async def _watch(self) -> None:
        while not self._closing:
            await asyncio.sleep(min(REVALIDATE_SECONDS, GROUP_REFRESH_SECONDS))
            try:
                if await self._check_access():
                    await self._refresh_membership()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("events access re-check failed")

    async def panel_event(self, message: dict[str, Any]) -> None:
        if self._closing or not self._authed:
            return
        instance_id = message.get("instance_id")
        if instance_id:
            cached = self._cache.get(instance_id)
            if cached is None or time.monotonic() - cached[0] > REVALIDATE_SECONDS:
                cached = (time.monotonic(), await _can_view(self.user, instance_id))
                self._cache[instance_id] = cached
            if not cached[1]:
                return
        elif "owner_id" in message:
            # an event without a server (the job of a deleted server): its user and users who see every server
            if str(getattr(self.user, "pk", "")) != message["owner_id"]:
                cached = self._cache.get("*")
                if cached is None or time.monotonic() - cached[0] > REVALIDATE_SECONDS:
                    cached = (time.monotonic(), await _sees_every_server(self.user))
                    self._cache["*"] = cached
                if not cached[1]:
                    return
        await self.send_json({"type": message["event"], "instance_id": instance_id, "payload": message["payload"]})
