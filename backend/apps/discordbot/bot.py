"""The Discord side of the bot (B3): discord.py's gateway client and the guild's slash commands.

Outbound only: the process connects to Discord's gateway; nothing listens. Commands are registered for the
configured guild only, and an interaction from anywhere else gets "this bot only answers in its own server".
Every command answers through ``apps.discordbot.logic`` (plain Django code, run in a thread); this module only
turns Discord's objects into a ``logic.Caller`` and the replies into messages, so the handlers below are tested
with fakes of the interaction objects.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from asgiref.sync import sync_to_async
from django.db import connections

from . import config, logic, state

log = logging.getLogger(__name__)
FOLLOW_POLL_SECONDS = 5.0
SETTINGS_POLL_SECONDS = 30.0
STATUS_EVERY_SECONDS = 60.0
# After a failed connection the process stays up and tries again (it never makes the panel unhealthy):
RETRY_FIRST = 5.0  # no connection (network, Discord down): 5, 10, 20 ... seconds
RETRY_MAX = 300.0  # ... at most 5 minutes apart
REJECTED_RETRY = 1800.0  # Discord refused the token or the guild: every 30 minutes, or at once on a settings change
WAIT_POLL_SECONDS = 10.0  # while waiting: how often the settings are read (and the status refreshed)
WRONG_GUILD = "This bot only answers in its own Discord server."
WATCH_FAILURES_MAX = 5  # the settings watcher's failures in a row before the connection is made again


def _fresh_connections() -> None:
    """``close_old_connections()`` for this long-lived process: a database connection the server dropped (an
    upgrade recreates Postgres) is closed, so the next query opens a new one. Not inside a transaction (tests)."""
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close_if_unusable_or_obsolete()


def _db(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``sync_to_async(fn)`` with fresh database connections before and after (as Django does per request)."""

    def call(*args: Any, **kwargs: Any) -> Any:
        _fresh_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            _fresh_connections()

    return sync_to_async(call)


def no_mentions() -> Any:
    """Nothing the bot posts pings anyone (a member's nickname or a server name may hold @everyone or a role)."""
    import discord

    return discord.AllowedMentions.none()


_MARKDOWN = re.compile(r"([\\*_~`|\[\]>])")
_MENTION = re.compile(r"@(everyone|here|[!&]?[0-9]{15,22})")
_LINE_START = re.compile(r"(?m)^(\s*)([#-])")
_URL = re.compile(r"(?i)\b(https?|discord\.gg)(:|/)")


def clean(text: str) -> str:
    """What the bot posts, with Discord's markdown, mentions, headings, quotes and links made plain text: names
    (of members and servers) and error texts are printed as they are, never formatted, linked or pinging."""
    text = _MARKDOWN.sub(r"\\\1", str(text))
    text = _MENTION.sub("@\u200b\\1", text)
    text = _LINE_START.sub(r"\1\\\2", text)
    return _URL.sub("\\1\u200b\\2", text)


def caller_of(interaction: Any) -> logic.Caller:
    user = interaction.user
    roles = getattr(user, "roles", None) or []
    name = getattr(user, "display_name", None) or getattr(user, "name", None) or str(user.id)
    return logic.Caller(id=str(user.id), name=str(name), role_ids=frozenset(str(getattr(r, "id", r)) for r in roles))


def right_guild(interaction: Any, settings: config.BotSettings) -> bool:
    return bool(settings.guild_id) and str(getattr(interaction, "guild_id", "") or "") == settings.guild_id


async def _send(interaction: Any, reply: logic.Reply) -> None:
    await interaction.response.send_message(clean(reply.text), ephemeral=reply.ephemeral, allowed_mentions=no_mentions())


async def _announce(client: Any, settings: config.BotSettings, text: str, interaction: Any = None) -> None:
    if not text or not settings.channel_id or client is None:
        return
    if interaction is not None and str(getattr(interaction, "channel_id", "") or "") == settings.channel_id:
        return  # (the command's own answer is already in that channel)
    try:
        channel = client.get_channel(int(settings.channel_id))
        if channel is not None:
            await channel.send(clean(text), allowed_mentions=no_mentions())
    except Exception as exc:  # an announcement is a courtesy: never the command's failure
        log.info("could not announce in the channel: %s", type(exc).__name__)


async def follow(interaction: Any, client: Any, settings: config.BotSettings, instance_id: str, job_id: str) -> str:
    """Follow a start until it is decided or FOLLOW_SECONDS passed; sends and returns the follow-up."""
    deadline = time.monotonic() + logic.FOLLOW_SECONDS
    message = None
    while time.monotonic() < deadline:
        message = await _db(logic.follow_up)(instance_id, job_id)
        if message is not None:
            break
        await asyncio.sleep(FOLLOW_POLL_SECONDS)
    if message is None:
        message = await _db(logic.follow_timeout)(instance_id)
    try:
        await interaction.followup.send(clean(message), allowed_mentions=no_mentions())
    except Exception as exc:  # the interaction may have expired (15 minutes)
        log.info("could not send the follow-up: %s", type(exc).__name__)
    await _announce(client, settings, message, interaction)
    return message


# --- the command handlers ------------------------------------------------------------------------------------
async def handle_servers(interaction: Any, settings: config.BotSettings) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True, allowed_mentions=no_mentions())
        return
    await _send(interaction, await _db(logic.servers)(caller_of(interaction)))


async def handle_status(interaction: Any, settings: config.BotSettings, server: str) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True, allowed_mentions=no_mentions())
        return
    await _send(interaction, await _db(logic.status)(caller_of(interaction), server))


async def handle_join(interaction: Any, settings: config.BotSettings, server: str) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True, allowed_mentions=no_mentions())
        return
    # (reading the join password may ask the node: Discord wants an answer within 3 seconds, so defer first)
    await interaction.response.defer(ephemeral=True, thinking=True)
    reply = await _db(logic.join)(caller_of(interaction), server)
    await interaction.followup.send(clean(reply.text), ephemeral=True, allowed_mentions=no_mentions())


async def handle_start(interaction: Any, settings: config.BotSettings, server: str, client: Any = None) -> asyncio.Task | None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True, allowed_mentions=no_mentions())
        return None
    caller = caller_of(interaction)
    instance, refusal = await _db(logic.start_check)(caller, server, settings)
    if refusal is not None:
        await _send(interaction, refusal)
        return None
    # (the memory admission may ask the node: defer, then answer in public)
    await interaction.response.defer(thinking=True)
    reply = await _db(logic.start_run)(caller, instance)
    await interaction.followup.send(clean(reply.text), allowed_mentions=no_mentions())
    if reply.follow is None:
        return None
    await _announce(client, settings, reply.announce, interaction)
    return asyncio.create_task(follow(interaction, client, settings, *reply.follow))


async def handle_stop(
    interaction: Any, settings: config.BotSettings, server: str, confirm: bool = False, client: Any = None
) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True, allowed_mentions=no_mentions())
        return
    caller = caller_of(interaction)
    instance, refusal = await _db(logic.stop_check)(caller, server, settings, confirm=confirm)
    if refusal is not None:
        await _send(interaction, refusal)
        return
    await interaction.response.defer(thinking=True)
    reply = await _db(logic.stop_run)(caller, instance)
    await interaction.followup.send(clean(reply.text), allowed_mentions=no_mentions())
    await _announce(client, settings, reply.announce, interaction)


async def handle_complete(interaction: Any, settings: config.BotSettings, current: str) -> list[str]:
    if not right_guild(interaction, settings):
        return []
    return await _db(logic.complete)(current)


# --- discord.py ------------------------------------------------------------------------------------------------
def make_client(settings: config.BotSettings) -> Any:
    """The gateway client with the guild's commands (imports discord.py only here)."""
    import discord
    from discord import app_commands

    class PanelBot(discord.Client):
        def __init__(self) -> None:
            intents = discord.Intents.none()
            intents.guilds = True  # the guild's roles and channels (no privileged intent)
            super().__init__(intents=intents, allowed_mentions=no_mentions())
            self.settings = settings
            self.tree = app_commands.CommandTree(self)
            self.settings_changed = False  # (set by _watch before it ends the connection)
            self.watch_failed = False  # (the watcher could not read the settings WATCH_FAILURES_MAX times)
            self.was_ready = False
            self.tasks: set[asyncio.Task] = set()
            guild = discord.Object(id=int(settings.guild_id))

            async def names(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
                return [app_commands.Choice(name=n, value=n) for n in await handle_complete(interaction, self.settings, current)]

            @self.tree.command(name="servers", description="The game servers you can use here", guild=guild)
            async def servers_cmd(interaction: discord.Interaction) -> None:
                await handle_servers(interaction, self.settings)

            @self.tree.command(name="status", description="Is a server running, and who is on it", guild=guild)
            @app_commands.describe(server="The server")
            @app_commands.autocomplete(server=names)
            async def status_cmd(interaction: discord.Interaction, server: str) -> None:
                await handle_status(interaction, self.settings, server)

            @self.tree.command(name="join", description="How to join a server (only you see the answer)", guild=guild)
            @app_commands.describe(server="The server")
            @app_commands.autocomplete(server=names)
            async def join_cmd(interaction: discord.Interaction, server: str) -> None:
                await handle_join(interaction, self.settings, server)

            @self.tree.command(name="start", description="Start a server", guild=guild)
            @app_commands.describe(server="The server")
            @app_commands.autocomplete(server=names)
            async def start_cmd(interaction: discord.Interaction, server: str) -> None:
                task = await handle_start(interaction, self.settings, server, client=self)
                if task is not None:
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)

            @self.tree.command(name="stop", description="Stop a server (its world is saved first)", guild=guild)
            @app_commands.describe(server="The server", confirm="Stop it even while players are online")
            @app_commands.autocomplete(server=names)
            async def stop_cmd(interaction: discord.Interaction, server: str, confirm: bool = False) -> None:
                await handle_stop(interaction, self.settings, server, confirm, client=self)

        async def setup_hook(self) -> None:
            await self.tree.sync(guild=discord.Object(id=int(self.settings.guild_id)))
            self.tasks.add(asyncio.create_task(self._watch()))

        async def on_ready(self) -> None:
            self.was_ready = True
            await sync_to_async(state.write_status)(
                **self.describe(), connected=True, state="connected", last_error="", retry_at=None
            )
            log.info("connected to Discord")

        async def on_resumed(self) -> None:
            await sync_to_async(state.write_status)(connected=True, state="connected", last_error="", retry_at=None)

        async def on_disconnect(self) -> None:
            # (discord.py itself connects again, with its own growing delay, while the client is not closed)
            if not self.is_closed():
                await sync_to_async(state.write_status)(
                    connected=False, state="connecting", last_error="The connection to Discord was lost; reconnecting"
                )

        async def on_guild_join(self, guild: Any) -> None:
            if str(guild.id) != self.settings.guild_id:
                log.info("the bot was added to another Discord server; it does not answer there")

        def describe(self) -> dict[str, Any]:
            guild = self.get_guild(int(self.settings.guild_id))
            found = None
            roles: list[dict[str, str]] = []
            channels: list[dict[str, str]] = []
            if guild is not None:
                found = {"id": str(guild.id), "name": guild.name}
                roles = [{"id": str(r.id), "name": r.name} for r in guild.roles if not r.is_default() and not r.managed]
                channels = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
            return {
                "guild": found,
                "roles": roles,
                "channels": channels,
                "application_id": str(self.application_id or ""),
                "bot_name": str(self.user) if self.user else "",
            }

        async def _watch(self) -> None:
            """Every SETTINGS_POLL_SECONDS: a change of token, guild or "enabled" ends this connection (run()
            connects again with the new settings, or ends when the bot was turned off); roles and channel apply at
            once. An error (the database is gone for a moment) is logged and tried again; after
            WATCH_FAILURES_MAX in a row the connection ends too, so run() starts over with fresh settings (or
            the process exits and the supervisor starts it again)."""
            started = self.settings.fingerprint()
            last_status = 0.0
            failures = 0
            while not self.is_closed():
                await asyncio.sleep(SETTINGS_POLL_SECONDS)
                if self.is_closed():
                    return  # (the connection ended meanwhile; run() goes on with a new client)
                try:
                    current = await _db(config.load)()
                    if current.fingerprint() != started:
                        log.info("the bot's settings changed; reconnecting")
                        self.settings_changed = True
                        await self.close()
                        return
                    self.settings = current  # roles and channel take effect at once
                    if not self.is_ready():
                        await sync_to_async(state.write_status)()  # (reconnecting: still alive)
                    elif time.monotonic() - last_status >= STATUS_EVERY_SECONDS:
                        await sync_to_async(state.write_status)(**self.describe(), connected=True)
                        last_status = time.monotonic()
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    log.warning("could not read the bot's settings (%s), %d time(s) in a row", type(exc).__name__, failures)
                    if failures >= WATCH_FAILURES_MAX:
                        self.watch_failed = True
                        await self.close()
                        return

    return PanelBot()


def _failure(exc: BaseException) -> tuple[str, str]:
    """("rejected" | "network", the text for the settings page) of a failed connection. Never the token."""
    import aiohttp
    import discord

    if isinstance(exc, discord.LoginFailure):
        return "rejected", "Discord refused the bot token"
    if isinstance(exc, discord.PrivilegedIntentsRequired):
        return "rejected", "Discord asks for a privileged intent"
    if isinstance(exc, discord.ConnectionClosed) and getattr(exc, "code", None) == 4004:
        return "rejected", "Discord refused the bot token"
    if isinstance(exc, discord.HTTPException):
        code = getattr(exc, "status", 0)
        if code in (401, 403, 404):
            return "rejected", f"Discord answered HTTP {code}: is the bot in the Discord server whose id you entered?"
        return "network", f"Discord answered HTTP {code or '?'}"
    if isinstance(exc, (TimeoutError, OSError, aiohttp.ClientError, discord.GatewayNotFound, discord.ConnectionClosed)):
        return "network", f"No connection to Discord ({type(exc).__name__})"
    return "network", f"The connection to Discord failed ({type(exc).__name__})"


async def connect_once(settings: config.BotSettings) -> tuple[str, str, bool]:
    """One connection with ``settings`` until it ends: ("changed", "", ready) after a settings change, else
    ("rejected" | "network", why, whether it had been connected)."""
    client = make_client(settings)
    await sync_to_async(state.write_status)(connected=False, state="connecting", retry_at=None)
    try:
        await client.start(settings.token)
    except Exception as exc:
        kind, message = _failure(exc)
        return kind, message, bool(getattr(client, "was_ready", False))
    finally:
        if not client.is_closed():
            await client.close()
    if getattr(client, "settings_changed", False):
        return "changed", "", bool(getattr(client, "was_ready", False))
    if getattr(client, "watch_failed", False):
        return "network", "The bot could not read its settings from the panel's database", False
    return "network", "The connection to Discord ended", bool(getattr(client, "was_ready", False))


async def wait_for_change(settings: config.BotSettings, seconds: float) -> bool:
    """Wait up to ``seconds``; True as soon as the token, guild or "enabled" changed. The status is refreshed
    meanwhile, so the settings page can tell a waiting bot from a stopped process."""
    deadline = time.monotonic() + seconds
    started = settings.fingerprint()
    while time.monotonic() < deadline:
        await asyncio.sleep(min(WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        current = await _db(config.load)()
        if current.fingerprint() != started:
            return True
        await sync_to_async(state.write_status)()
    return False


async def run() -> int:
    """Stay connected while the settings say so. A failed connection never ends the process: it tries again
    (no connection: after 5 s, doubling up to 5 minutes; a refused token or guild: after 30 minutes), and at once
    when the settings change. 78 only when there is nothing to do (off, no token or no guild): the supervisor
    keeps the process idle until its next try or a wake-up from the settings page."""
    delay = RETRY_FIRST
    while True:
        settings = await _db(config.load)()
        if not settings.usable:
            await sync_to_async(state.write_status)(connected=False, state="idle", last_error="", retry_at=None)
            return 78
        kind, message, was_ready = await connect_once(settings)
        if kind == "changed":
            delay = RETRY_FIRST
            continue
        if was_ready:
            delay = RETRY_FIRST  # (it had been connected: a new outage starts with the short delay)
        wait = REJECTED_RETRY if kind == "rejected" else delay
        if kind == "network":
            delay = min(RETRY_MAX, delay * 2)
        await sync_to_async(state.write_status)(
            connected=False, state="error", last_error=message, error_kind=kind, retry_at=time.time() + wait
        )
        log.warning("%s; trying again in %.0f s, or at once when the bot's settings change", message, wait)
        if await wait_for_change(settings, wait):
            delay = RETRY_FIRST
