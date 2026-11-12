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
import time
from typing import Any

from asgiref.sync import sync_to_async

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


def caller_of(interaction: Any) -> logic.Caller:
    user = interaction.user
    roles = getattr(user, "roles", None) or []
    name = getattr(user, "display_name", None) or getattr(user, "name", None) or str(user.id)
    return logic.Caller(id=str(user.id), name=str(name), role_ids=frozenset(str(getattr(r, "id", r)) for r in roles))


def right_guild(interaction: Any, settings: config.BotSettings) -> bool:
    return bool(settings.guild_id) and str(getattr(interaction, "guild_id", "") or "") == settings.guild_id


async def _send(interaction: Any, reply: logic.Reply) -> None:
    await interaction.response.send_message(reply.text, ephemeral=reply.ephemeral)


async def _announce(client: Any, settings: config.BotSettings, text: str, interaction: Any = None) -> None:
    if not text or not settings.channel_id or client is None:
        return
    if interaction is not None and str(getattr(interaction, "channel_id", "") or "") == settings.channel_id:
        return  # (the command's own answer is already in that channel)
    try:
        channel = client.get_channel(int(settings.channel_id))
        if channel is not None:
            await channel.send(text)
    except Exception as exc:  # an announcement is a courtesy: never the command's failure
        log.info("could not announce in the channel: %s", type(exc).__name__)


async def follow(interaction: Any, client: Any, settings: config.BotSettings, instance_id: str, job_id: str) -> str:
    """Follow a start until it is decided or FOLLOW_SECONDS passed; sends and returns the follow-up."""
    deadline = time.monotonic() + logic.FOLLOW_SECONDS
    message = None
    while time.monotonic() < deadline:
        message = await sync_to_async(logic.follow_up)(instance_id, job_id)
        if message is not None:
            break
        await asyncio.sleep(FOLLOW_POLL_SECONDS)
    if message is None:
        message = await sync_to_async(logic.follow_timeout)(instance_id)
    try:
        await interaction.followup.send(message)
    except Exception as exc:  # the interaction may have expired (15 minutes)
        log.info("could not send the follow-up: %s", type(exc).__name__)
    await _announce(client, settings, message, interaction)
    return message


# --- the command handlers ------------------------------------------------------------------------------------
async def handle_servers(interaction: Any, settings: config.BotSettings) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True)
        return
    await _send(interaction, await sync_to_async(logic.servers)(caller_of(interaction)))


async def handle_status(interaction: Any, settings: config.BotSettings, server: str) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True)
        return
    await _send(interaction, await sync_to_async(logic.status)(caller_of(interaction), server))


async def handle_join(interaction: Any, settings: config.BotSettings, server: str) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True)
        return
    # (reading the join password may ask the node: Discord wants an answer within 3 seconds, so defer first)
    await interaction.response.defer(ephemeral=True, thinking=True)
    reply = await sync_to_async(logic.join)(caller_of(interaction), server)
    await interaction.followup.send(reply.text, ephemeral=True)


async def handle_start(interaction: Any, settings: config.BotSettings, server: str, client: Any = None) -> asyncio.Task | None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True)
        return None
    caller = caller_of(interaction)
    instance, refusal = await sync_to_async(logic.start_check)(caller, server, settings)
    if refusal is not None:
        await _send(interaction, refusal)
        return None
    # (the memory admission may ask the node: defer, then answer in public)
    await interaction.response.defer(thinking=True)
    reply = await sync_to_async(logic.start_run)(caller, instance)
    await interaction.followup.send(reply.text)
    if reply.follow is None:
        return None
    await _announce(client, settings, reply.announce, interaction)
    return asyncio.create_task(follow(interaction, client, settings, *reply.follow))


async def handle_stop(
    interaction: Any, settings: config.BotSettings, server: str, confirm: bool = False, client: Any = None
) -> None:
    if not right_guild(interaction, settings):
        await interaction.response.send_message(WRONG_GUILD, ephemeral=True)
        return
    caller = caller_of(interaction)
    instance, refusal = await sync_to_async(logic.stop_check)(caller, server, settings, confirm=confirm)
    if refusal is not None:
        await _send(interaction, refusal)
        return
    await interaction.response.defer(thinking=True)
    reply = await sync_to_async(logic.stop_run)(caller, instance)
    await interaction.followup.send(reply.text)
    await _announce(client, settings, reply.announce, interaction)


async def handle_complete(interaction: Any, settings: config.BotSettings, current: str) -> list[str]:
    if not right_guild(interaction, settings):
        return []
    return await sync_to_async(logic.complete)(current)


# --- discord.py ------------------------------------------------------------------------------------------------
def make_client(settings: config.BotSettings) -> Any:
    """The gateway client with the guild's commands (imports discord.py only here)."""
    import discord
    from discord import app_commands

    class PanelBot(discord.Client):
        def __init__(self) -> None:
            intents = discord.Intents.none()
            intents.guilds = True  # the guild's roles and channels (no privileged intent)
            super().__init__(intents=intents)
            self.settings = settings
            self.tree = app_commands.CommandTree(self)
            self.settings_changed = False  # (set by _watch before it ends the connection)
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
            """Every SETTINGS_POLL_SECONDS: a change of token, guild or "enabled" ends this connection (the
            supervisor starts the process again at once through the wake file, or keeps it idle when off)."""
            started = self.settings.fingerprint()
            last_status = 0.0
            while not self.is_closed():
                await asyncio.sleep(SETTINGS_POLL_SECONDS)
                if self.is_closed():
                    return  # (the connection ended meanwhile; run() goes on with a new client)
                current = await sync_to_async(config.load)()
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
    return "network", "The connection to Discord ended", bool(getattr(client, "was_ready", False))


async def wait_for_change(settings: config.BotSettings, seconds: float) -> bool:
    """Wait up to ``seconds``; True as soon as the token, guild or "enabled" changed. The status is refreshed
    meanwhile, so the settings page can tell a waiting bot from a stopped process."""
    deadline = time.monotonic() + seconds
    started = settings.fingerprint()
    while time.monotonic() < deadline:
        await asyncio.sleep(min(WAIT_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        current = await sync_to_async(config.load)()
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
        settings = await sync_to_async(config.load)()
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
