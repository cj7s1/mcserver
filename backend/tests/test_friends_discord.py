"""B3 of the feature plan of 2026-11-12: the Discord bot (apps/discordbot), tested with fakes of discord.py's
interaction objects; nothing here talks to Discord."""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.core.cache import cache
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.core.errors import ConflictError
from apps.core.models import SystemSetting
from apps.discordbot import bot, config, logic, state
from apps.instances.models import Instance
from apps.jobs.models import Job, JobStatus
from apps.metrics.models import MetricSample

from .test_games_six import create

pytestmark = pytest.mark.django_db

GUILD = "123456789012345678"
ROLE = "223456789012345678"
OTHER_ROLE = "323456789012345678"
CHANNEL = "423456789012345678"
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Gx-test.very-secret-bot-token-value"
SETTINGS = config.BotSettings(enabled=True, guild_id=GUILD, roles=(ROLE,), channel_id=CHANNEL, token=TOKEN)
FRIEND = logic.Caller(id="523456789012345678", name="Robin", role_ids=frozenset({ROLE}))
STRANGER = logic.Caller(id="623456789012345678", name="Sam", role_ids=frozenset())


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


def _available(make_instance: Any, **fields: Any) -> Instance:
    fields.setdefault("discord_available", True)
    return make_instance(**fields)


def _players(inst: Instance, count: int | None, *, age: int = 20) -> None:
    MetricSample.objects.create(
        instance=inst, taken_at=timezone.now() - timedelta(seconds=age), players_online=count, players_max=20
    )


# --- logic: only servers "available in Discord" exist ------------------------------------------------------
def test_only_available_servers_exist_for_the_bot(make_instance: Any) -> None:
    shown = _available(make_instance, name="Creative")
    make_instance(name="Hidden")
    _available(make_instance, name="Old", archived_at=timezone.now())
    assert logic.complete("") == ["Creative"] and logic.complete("hid") == []
    listing = logic.servers(FRIEND).text
    assert "Creative" in listing and "Hidden" not in listing and "Old" not in listing
    assert logic.find("creative") == shown and logic.find("Hidden") is None
    for command in (logic.status, logic.join):
        assert command(FRIEND, "Hidden").text == "No server called “Hidden” is available in Discord."
    assert "No server called “Hidden” is available in Discord" in logic.start(FRIEND, "Hidden", SETTINGS).text
    assert "No server called" in logic.stop(FRIEND, "Hidden", SETTINGS).text
    # two servers of the same name: neither is guessed
    _available(make_instance, name="creative")
    assert logic.find("Creative") is None


def test_status_shows_the_fresh_player_count_and_the_address(make_instance: Any) -> None:
    SystemSetting.set("public_address", "play.example.org")
    inst = _available(make_instance, name="Survival", status="running")
    _players(inst, 2)
    text = logic.status(FRIEND, "survival").text
    assert "**Survival** (Minecraft" in text and "running, 2 players online" in text
    assert f"Address: play.example.org:{inst.game_port}" in text
    MetricSample.objects.all().delete()
    _players(inst, 2, age=600)  # an old count is not shown
    assert "players online" not in logic.status(FRIEND, "Survival").text


# --- start and stop ------------------------------------------------------------------------------------------
def test_a_start_goes_through_the_panels_power_as_the_service_user(
    make_instance: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    inst = _available(make_instance, name="Survival", status="stopped")
    with django_capture_on_commit_callbacks(execute=False):
        reply = logic.start(FRIEND, "Survival", SETTINGS)
    assert reply.ephemeral is False and "Starting Survival" in reply.text
    assert reply.announce == "Robin is starting Survival."
    job = Job.objects.get(pk=reply.follow[1])
    assert reply.follow[0] == str(inst.pk) and job.kind == "start"
    event = AuditEvent.objects.get(action="instance.start")
    assert event.actor_username == logic.SERVICE_USERNAME and event.actor is not None
    assert event.details["discord_user"] == "Robin" and event.details["discord_user_id"] == FRIEND.id
    assert event.details["via"] == "discord" and event.details["job"] == str(job.pk)
    service = event.actor
    assert service.is_active is False and not service.has_usable_password()


def test_who_may_start_and_stop(make_instance: Any) -> None:
    inst = _available(make_instance, name="Survival", status="stopped")
    assert "You may not start Survival" in logic.start(STRANGER, "Survival", SETTINGS).text
    # the server's own roles replace the default roles
    Instance.objects.filter(pk=inst.pk).update(discord_roles=[OTHER_ROLE])
    assert "You may not start" in logic.start(FRIEND, "Survival", SETTINGS).text
    other = logic.Caller(id="723456789012345678", name="Kim", role_ids=frozenset({OTHER_ROLE}))
    assert logic.may_control(other, Instance.objects.get(pk=inst.pk), SETTINGS)
    # no default roles and none of its own: nobody
    Instance.objects.filter(pk=inst.pk).update(discord_roles=[])
    nobody = config.BotSettings(enabled=True, guild_id=GUILD, token=TOKEN)
    assert not logic.may_control(FRIEND, Instance.objects.get(pk=inst.pk), nobody)
    assert not Job.objects.exists()


def test_the_panels_refusal_is_the_reply(make_instance: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _available(make_instance, name="Survival", status="stopped")

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise ConflictError("Not enough free memory on test-node for this server", code="insufficient_memory")

    monkeypatch.setattr("apps.instances.services.power", refuse)
    reply = logic.start(FRIEND, "Survival", SETTINGS)
    assert reply.text == "Survival was not started: Not enough free memory on test-node for this server"
    assert reply.follow is None and reply.ephemeral is True
    _available(make_instance, name="Running", status="running")
    assert logic.start(FRIEND, "Running", SETTINGS).text == "Running is running already."


def test_stop_asks_while_players_are_online_and_uses_the_stop_job(
    make_instance: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    inst = _available(make_instance, name="Survival", status="running", desired_state="running")
    _players(inst, 2)
    asked = logic.stop(FRIEND, "Survival", SETTINGS)
    assert "2 players are online on Survival" in asked.text and "confirm: True" in asked.text and asked.ephemeral
    assert not Job.objects.exists()
    with django_capture_on_commit_callbacks(execute=False):
        reply = logic.stop(FRIEND, "Survival", SETTINGS, confirm=True)
    assert "Stopping Survival (the world is saved first)" in reply.text and reply.ephemeral is False
    assert Job.objects.get().kind == "stop"
    event = AuditEvent.objects.get(action="instance.stop")
    assert event.actor_username == logic.SERVICE_USERNAME and event.details["discord_user_id"] == FRIEND.id
    stopped = _available(make_instance, name="Idle", status="stopped")
    assert logic.stop(FRIEND, stopped.name, SETTINGS).text == "Idle is stopped already."


def test_rate_limits_per_member(make_instance: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _available(make_instance, name="Survival", status="stopped")
    monkeypatch.setattr(logic, "_now", lambda: 1_000_000.0)
    monkeypatch.setattr(logic, "QUERY_LIMIT", (3, 60))
    for _ in range(3):
        assert "Survival" in logic.servers(FRIEND).text
    assert logic.servers(FRIEND).text.startswith("Slow down: at most 3 commands per 1 minute")
    assert "Survival" in logic.servers(STRANGER).text  # per member
    monkeypatch.setattr(logic, "_now", lambda: 1_000_060.0)  # the next minute
    assert "Survival" in logic.servers(FRIEND).text
    monkeypatch.setattr(logic, "CONTROL_LIMIT", (1, 600))
    monkeypatch.setattr("apps.instances.services.power", lambda *_a, **_k: SimpleNamespace(pk="job-1"))
    assert "Starting Survival" in logic.start(FRIEND, "Survival", SETTINGS).text
    assert logic.start(FRIEND, "Survival", SETTINGS).text == "Slow down: at most 1 starts and stops per 10 minutes."


def test_join_shows_the_password_only_when_the_server_allows_it(
    clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    SystemSetting.set("public_address", "203.0.113.5")
    vh = create(clients, django_capture_on_commit_callbacks, "valheim", secrets={"server_password": "sesame-42"})
    Instance.objects.filter(pk=vh.pk).update(discord_available=True)
    text = logic.join(FRIEND, vh.name).text
    assert "Password: ask me for it" in text and "sesame-42" not in text and "203.0.113.5" in text
    Instance.objects.filter(pk=vh.pk).update(discord_share_password=True)
    assert "Password: sesame-42" in logic.join(FRIEND, vh.name).text


def test_the_follow_up_of_a_start(make_instance: Any) -> None:
    inst = _available(make_instance, name="Survival", status="starting")
    job = Job.objects.create(kind="start", instance=inst, status=JobStatus.RUNNING)
    assert logic.follow_up(str(inst.pk), str(job.pk)) is None
    Job.objects.filter(pk=job.pk).update(status=JobStatus.SUCCEEDED)
    Instance.objects.filter(pk=inst.pk).update(status="running")
    assert logic.follow_up(str(inst.pk), str(job.pk)) == "Survival is ready."
    Job.objects.filter(pk=job.pk).update(status=JobStatus.FAILED, error="Port 25565 is in use")
    assert logic.follow_up(str(inst.pk), str(job.pk)) == "Survival did not start: Port 25565 is in use"
    assert logic.follow_timeout(str(inst.pk)) == "Survival is still starting after 14 minutes; see the panel for how it goes."


def test_the_service_user_cannot_sign_in() -> None:
    from apps.accounts.models import User

    first = logic.service_user()
    first.set_password("someone-set-this-1")
    first.is_active = True
    first.save()
    again = logic.service_user()
    assert again.pk == first.pk and User.objects.filter(username=logic.SERVICE_USERNAME).count() == 1
    assert not again.has_usable_password() and again.is_active is False


# --- the discord.py side, with fakes of its interaction objects --------------------------------------------
class FakeResponse:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.deferred: dict[str, bool] | None = None

    async def send_message(self, text: str, *, ephemeral: bool = False) -> None:
        self.sent.append((text, ephemeral))

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.deferred = {"ephemeral": ephemeral, "thinking": thinking}


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    async def send(self, text: str, *, ephemeral: bool = False) -> None:
        self.sent.append((text, ephemeral))


class FakeInteraction:
    def __init__(self, caller: logic.Caller = FRIEND, *, guild_id: str | None = GUILD, channel_id: str = "1") -> None:
        roles = [SimpleNamespace(id=int(r)) for r in caller.role_ids]
        self.user = SimpleNamespace(id=int(caller.id), display_name=caller.name, name=caller.name.lower(), roles=roles)
        self.guild_id = int(guild_id) if guild_id else None
        self.channel_id = int(channel_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def texts(self) -> list[str]:
        return [t for t, _e in self.response.sent + self.followup.sent]


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FakeClient:
    def __init__(self) -> None:
        self.channel = FakeChannel()

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.channel if str(channel_id) == CHANNEL else None


def test_commands_answer_only_in_the_configured_guild(make_instance: Any) -> None:
    _available(make_instance, name="Survival")
    for guild in ("999999999999999999", None):  # another Discord server, a direct message
        interaction = FakeInteraction(guild_id=guild)
        async_to_sync(bot.handle_servers)(interaction, SETTINGS)
        async_to_sync(bot.handle_start)(interaction, SETTINGS, "Survival")
        assert interaction.response.sent == [(bot.WRONG_GUILD, True)] * 2
        assert async_to_sync(bot.handle_complete)(interaction, SETTINGS, "") == []
    interaction = FakeInteraction()
    async_to_sync(bot.handle_servers)(interaction, SETTINGS)
    assert "Survival" in interaction.texts()[0] and interaction.response.sent[0][1] is True
    assert async_to_sync(bot.handle_complete)(interaction, SETTINGS, "surv") == ["Survival"]


def test_the_caller_is_the_member_with_its_roles() -> None:
    caller = bot.caller_of(FakeInteraction(FRIEND))
    assert caller == logic.Caller(id=FRIEND.id, name="Robin", role_ids=frozenset({ROLE}))


def test_a_start_from_discord_is_answered_announced_and_followed(
    make_instance: Any, fake_agent: Any, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    inst = _available(make_instance, name="Survival", status="stopped")
    monkeypatch.setattr(bot, "FOLLOW_POLL_SECONDS", 0.01)
    interaction, client = FakeInteraction(), FakeClient()

    def ready() -> None:
        Job.objects.filter(instance=inst).update(status=JobStatus.SUCCEEDED)
        Instance.objects.filter(pk=inst.pk).update(status="running")

    async def scenario() -> None:
        task = await bot.handle_start(interaction, SETTINGS, "survival", client=client)
        assert task is not None
        await sync_to_async(ready)()
        await task

    with django_capture_on_commit_callbacks(execute=False):
        async_to_sync(scenario)()
    assert interaction.response.deferred == {"ephemeral": False, "thinking": True}
    assert interaction.followup.sent[0] == ("Starting Survival… I will tell you when it is ready.", False)
    assert interaction.followup.sent[-1][0] == "Survival is ready."
    assert client.channel.sent == ["Robin is starting Survival.", "Survival is ready."]
    # used in the announcement channel itself: no second copy there
    Instance.objects.filter(pk=inst.pk).update(status="stopped")
    cache.clear()
    here, client = FakeInteraction(channel_id=CHANNEL), FakeClient()

    async def again() -> None:
        task = await bot.handle_start(here, SETTINGS, "Survival", client=client)
        await sync_to_async(ready)()
        await task

    with django_capture_on_commit_callbacks(execute=False):
        async_to_sync(again)()
    assert client.channel.sent == [] and here.followup.sent[-1][0] == "Survival is ready."


def test_refusals_are_only_shown_to_the_member(make_instance: Any) -> None:
    _available(make_instance, name="Survival", status="stopped")
    interaction, client = FakeInteraction(STRANGER), FakeClient()
    assert async_to_sync(bot.handle_start)(interaction, SETTINGS, "Survival", client=client) is None
    assert interaction.response.sent == [("You may not start Survival: that needs one of the roles the owner chose.", True)]
    assert interaction.response.deferred is None and client.channel.sent == []


def test_join_is_deferred_and_private(make_instance: Any, fake_agent: Any) -> None:
    _available(make_instance, name="Survival")
    interaction = FakeInteraction()
    async_to_sync(bot.handle_join)(interaction, SETTINGS, "Survival")
    assert interaction.response.deferred == {"ephemeral": True, "thinking": True}
    assert interaction.followup.sent[0][1] is True and "How to join:" in interaction.followup.sent[0][0]


def test_the_client_has_the_five_guild_commands_and_no_privileged_intent() -> None:
    import discord

    client = bot.make_client(SETTINGS)
    names = {c.name for c in client.tree.get_commands(guild=discord.Object(id=int(GUILD)))}
    assert names == {"servers", "status", "join", "start", "stop"}
    assert client.tree.get_commands() == []  # nothing global
    assert not client.intents.members and not client.intents.presences and not client.intents.message_content
    assert client.describe() == {"guild": None, "roles": [], "channels": [], "application_id": "", "bot_name": ""}


class FakeGatewayClient:
    """discord.py's client as run() sees it: ``start`` fails with ``error``, or ``then`` runs (a settings change)."""

    def __init__(self, error: BaseException | None = None, *, ready: bool = False, then: Any = None) -> None:
        self.error, self.then = error, then
        self.closed = False
        self.was_ready = ready
        self.settings_changed = False
        self.tokens: list[str] = []

    async def start(self, token: str) -> None:
        self.tokens.append(token)
        if self.then is not None:
            await sync_to_async(self.then)()
        if self.error is not None:
            raise self.error
        self.settings_changed = True  # (as _watch does before it closes the connection)

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


def _configure(enabled: bool = True) -> None:
    SystemSetting.set("discord_enabled", enabled)
    SystemSetting.set("discord_guild_id", GUILD)
    SystemSetting.set_secret(config.TOKEN_KEY, TOKEN)


def _disable() -> None:
    SystemSetting.set("discord_enabled", False)


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> Any:
    """run() with these fake clients one after the other; the waits between them are recorded, not slept."""
    plan = SimpleNamespace(clients=[], waits=[], statuses=[])

    def next_client(_settings: Any) -> FakeGatewayClient:
        return plan.clients.pop(0)

    async def no_wait(_settings: Any, seconds: float) -> bool:
        plan.waits.append(seconds)
        plan.statuses.append(await sync_to_async(state.read_status)())
        return False

    monkeypatch.setattr(bot, "make_client", next_client)
    monkeypatch.setattr(bot, "wait_for_change", no_wait)
    return plan


def test_run_without_settings_is_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "make_client", lambda _s: pytest.fail("no client without settings"))
    assert async_to_sync(bot.run)() == 78
    _configure(enabled=False)
    assert async_to_sync(bot.run)() == 78 and state.read_status()["state"] == "idle"


def test_discord_unreachable_at_start_and_later_never_ends_the_process(gateway: Any) -> None:
    import discord

    _configure()
    closed = discord.ConnectionClosed(SimpleNamespace(close_code=1006), shard_id=None)
    gateway.clients = [
        FakeGatewayClient(OSError("network is unreachable")),  # no network at the start
        FakeGatewayClient(TimeoutError()),
        FakeGatewayClient(discord.GatewayNotFound()),
        FakeGatewayClient(closed, ready=True),  # connected, then the connection broke for good
        FakeGatewayClient(OSError("down again")),
        FakeGatewayClient(then=_disable),  # the owner turns the bot off: only now does the process end
    ]
    assert async_to_sync(bot.run)() == 78
    # growing delays, back to the short one after it had been connected
    assert gateway.waits == [5.0, 10.0, 20.0, 5.0, 10.0]
    first = gateway.statuses[0]
    assert first["state"] == "error" and first["error_kind"] == "network" and first["connected"] is False
    assert first["last_error"] == "No connection to Discord (OSError)" and first["retry_at"] > 0
    assert gateway.statuses[2]["last_error"] == "No connection to Discord (GatewayNotFound)"
    assert state.read_status()["state"] == "idle"


def test_the_delay_is_capped(gateway: Any) -> None:
    _configure()
    gateway.clients = [FakeGatewayClient(OSError("down")) for _ in range(9)] + [FakeGatewayClient(then=_disable)]
    assert async_to_sync(bot.run)() == 78
    assert gateway.waits == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]


def test_a_rejected_token_or_guild_waits_long_and_never_shows_the_token(gateway: Any, caplog: pytest.LogCaptureFixture) -> None:
    import discord

    caplog.set_level(logging.DEBUG)
    _configure()
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"code": 50001, "message": "Missing Access"})
    gateway.clients = [
        FakeGatewayClient(discord.LoginFailure(f"Improper token has been passed: {TOKEN}")),
        FakeGatewayClient(forbidden),  # a wrong guild id: the command sync is refused
        FakeGatewayClient(discord.PrivilegedIntentsRequired(None)),
        FakeGatewayClient(then=_disable),
    ]
    assert async_to_sync(bot.run)() == 78
    assert gateway.waits == [bot.REJECTED_RETRY] * 3
    assert [s["last_error"] for s in gateway.statuses] == [
        "Discord refused the bot token",
        "Discord answered HTTP 403: is the bot in the Discord server whose id you entered?",
        "Discord asks for a privileged intent",
    ]
    assert all(s["error_kind"] == "rejected" for s in gateway.statuses)
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(gateway.statuses)


def test_a_settings_change_is_picked_up_while_it_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    import discord

    _configure()
    monkeypatch.setattr(bot, "WAIT_POLL_SECONDS", 0.01)
    new_token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Gx-new.another-secret-value"
    clients = [
        FakeGatewayClient(
            discord.LoginFailure("Improper token has been passed."),
            then=lambda: SystemSetting.set_secret(config.TOKEN_KEY, new_token),  # the owner pastes a new token
        ),
        FakeGatewayClient(then=_disable),
    ]
    second = clients[1]
    monkeypatch.setattr(bot, "make_client", lambda _s: clients.pop(0))
    began = time.monotonic()
    assert async_to_sync(bot.run)() == 78
    assert time.monotonic() - began < 5  # not the 30 minutes of a refused token
    assert second.tokens == [new_token]
    # nothing changed: the wait ends at its time, and the status stays fresh meanwhile
    settings = config.load()
    state.clear_status()
    assert async_to_sync(bot.wait_for_change)(settings, 0.05) is False
    assert state.read_status().get("updated_at")


def test_a_lost_connection_is_shown_while_discord_py_reconnects() -> None:
    client = bot.make_client(SETTINGS)
    async_to_sync(client.on_disconnect)()
    status = state.read_status()
    assert status["state"] == "connecting" and status["connected"] is False
    assert status["last_error"] == "The connection to Discord was lost; reconnecting"


# --- the settings API ----------------------------------------------------------------------------------------
def test_the_settings_api(clients: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    wake = tmp_path / "discord.wake"
    monkeypatch.setenv("MCPANEL_DISCORD_WAKE", str(wake))
    assert clients["operator"].get("/api/system/discord").status_code == 403
    assert clients["operator"].patch("/api/system/discord", {"enabled": True}, format="json").status_code == 403
    empty = clients["owner"].get("/api/system/discord").json()
    assert empty["enabled"] is False and empty["token_set"] is False and empty["status"]["state"] == "disabled"
    assert empty["status"]["invite_url"] is None
    resp = clients["owner"].patch(
        "/api/system/discord",
        {"enabled": True, "guild_id": GUILD, "roles": [ROLE], "channel_id": CHANNEL, "token": TOKEN},
        format="json",
    )
    assert resp.status_code == 200 and TOKEN not in resp.content.decode() and "token" not in resp.json()
    body = resp.json()
    assert body["token_set"] is True and body["roles"] == [ROLE] and body["status"]["state"] == "not_running"
    assert wake.exists()  # the supervisor starts the bot now
    assert config.load().token == TOKEN and TOKEN not in json.dumps(SystemSetting.objects.get(key=config.TOKEN_KEY).value)
    event = AuditEvent.objects.get(action="system.discord_updated")
    assert event.details["bot_credential"] == "set" and TOKEN not in json.dumps(event.details)
    # what the running bot reports
    state.write_status(
        connected=True,
        state="connected",
        guild={"id": GUILD, "name": "Friends"},
        roles=[{"id": ROLE, "name": "Players"}],
        channels=[{"id": CHANNEL, "name": "general"}],
        application_id="823456789012345678",
        bot_name="MCPanel#0001",
    )
    live = clients["owner"].get("/api/system/discord").json()["status"]
    assert live["state"] == "connected" and live["connected"] is True and live["guild_roles"][0]["name"] == "Players"
    assert live["invite_url"] == (
        "https://discord.com/oauth2/authorize?client_id=823456789012345678&scope=bot+applications.commands&permissions=3072"
    )
    # a status not refreshed for STALE_SECONDS: the process is not running
    status = cache.get(state.STATUS_KEY)
    status["updated_at"] -= state.STALE_SECONDS + 1
    cache.set(state.STATUS_KEY, status)
    assert clients["owner"].get("/api/system/discord").json()["status"]["state"] == "not_running"
    # a failed connection: the error and when it tries again
    state.write_status(connected=False, state="error", last_error="No connection to Discord (OSError)", retry_at=1234.0)
    failed = clients["owner"].get("/api/system/discord").json()["status"]
    assert failed["state"] == "error" and failed["retry_at"] == 1234.0 and failed["last_error"].startswith("No connection")
    # bad ids, and removing the token
    assert clients["owner"].patch("/api/system/discord", {"guild_id": "abc"}, format="json").status_code == 400
    assert clients["owner"].patch("/api/system/discord", {"roles": ["12"]}, format="json").status_code == 400
    cleared = clients["owner"].patch("/api/system/discord", {"token": ""}, format="json").json()
    assert cleared["token_set"] is False and cleared["status"]["state"] == "no_token"
    assert AuditEvent.objects.filter(action="system.discord_updated").first().details["bot_credential"] == "removed"


def test_per_server_discord_fields(clients: Any, make_instance: Any) -> None:
    inst = make_instance()
    url = f"/api/instances/{inst.id}"
    got = clients["owner"].get(url).json()
    assert got["discord_available"] is False and got["discord_roles"] == [] and got["discord_share_password"] is False
    resp = clients["owner"].patch(
        url, {"discord_available": True, "discord_roles": [OTHER_ROLE, ROLE, ROLE], "discord_share_password": True}, format="json"
    )
    assert resp.status_code == 200, resp.json()
    inst.refresh_from_db()
    assert inst.discord_available and inst.discord_roles == sorted([ROLE, OTHER_ROLE]) and inst.discord_share_password
    assert clients["owner"].patch(url, {"discord_roles": ["not-an-id"]}, format="json").status_code == 400
