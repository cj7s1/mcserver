"""Regression tests of the independent review of feature/unraid-and-friends (2026-11-12), from its proofs
(audit-2026-11-09/features/review/test_featreview.py). F-1 and F-8 are in test_friends_discord.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone

from apps.discordbot import config, logic
from apps.games import cs2, factorio, terraria, zomboid
from apps.instances import idle
from apps.instances.models import Instance
from apps.jobs.models import Job

from .test_friends_idle import _running, _samples

pytestmark = pytest.mark.django_db

GUILD = "123456789012345678"
ROLE = "223456789012345678"
SETTINGS = config.BotSettings(enabled=True, guild_id=GUILD, roles=(ROLE,), token="t")
FRIEND = logic.Caller(id="523456789012345678", name="Robin", role_ids=frozenset({ROLE}))


# --- F-2: an answer whose count was not read is unknown, never "empty" ------------------------------------------
@pytest.mark.parametrize("text", ["", "\n", "Spieler verbunden: keine", ": <bob> hello"])
def test_unread_console_answers_are_unknown(text: str) -> None:
    assert terraria.parse_playing(text)["online"] is None
    assert factorio.parse_players(text)["online"] is None
    assert zomboid.parse_players(text)["online"] is None
    assert cs2.status_count(text, cs2.parse_status(text)) is None


def test_counts_that_were_read_stay_counts() -> None:
    assert terraria.parse_playing(": No players connected.")["online"] == 0
    assert terraria.parse_playing(": Ann (172.18.0.1:5000)\n")["online"] == 1  # (names without the count line)
    assert factorio.parse_players("Online players (0):\n")["online"] == 0
    assert zomboid.parse_players("Players connected (0): \n")["online"] == 0
    assert cs2.status_count("players  : 0 humans, 2 bots (16/0 max) (not hibernating)", []) == 0
    assert terraria.known_count(None, []) is None and terraria.known_count(3, []) == 3


def test_a_player_list_answer_without_a_list_is_an_error() -> None:
    from apps.core.errors import ApiError
    from apps.games.palworld import PalworldDefinition
    from apps.games.rust import RustDefinition

    class Answer:
        def __init__(self, answer: Any) -> None:
            self.answer = answer

        def game_action(self, *_a: Any, **_k: Any) -> Any:
            return self.answer

    for definition in (RustDefinition(), PalworldDefinition()):
        for answer in ({}, {"players": None}, "garbage"):
            with pytest.raises(ApiError):
                definition._players(Answer(answer), SimpleNamespace(id="x"))
        assert definition._players(Answer({"players": []}), SimpleNamespace(id="x")) == []


def test_unknown_is_never_empty_for_the_idle_stop_and_the_friend(
    make_instance: Any, fake_agent: Any, make_user: Any, roles: dict[str, Any], client_for: Any
) -> None:
    from apps.accounts.models import InstanceAccess

    inst = _running(make_instance, fake_agent, minutes=10)
    _samples(inst, [None] * 15)
    assert idle.empty_since_window(inst, timezone.now()) is False
    friend = make_user("robin")
    InstanceAccess.objects.create(user=friend, instance=inst, role=roles["friend"])
    assert client_for(friend).get(f"/api/instances/{inst.id}").json()["players_online"] is None
    assert logic.players(inst) is None


# --- F-16: the idle stop never supersedes a pending countdown ---------------------------------------------------
def test_no_idle_stop_while_a_countdown_is_pending(make_instance: Any, fake_agent: Any) -> None:
    from apps.instances.services import enqueue_power

    inst = _running(make_instance, fake_agent, minutes=10)
    _samples(inst, [0] * 15)
    assert idle.due(inst, timezone.now()) is True
    countdown = enqueue_power(inst, "restart", user=None, warn_minutes=5, warn_message="Restart soon")
    inst.refresh_from_db()
    assert idle.due(inst, timezone.now()) is False
    assert idle.check_idle() == 0
    countdown.refresh_from_db()
    assert countdown.status in ("queued", "running") and not Job.objects.filter(instance=inst, kind="stop").exists()


# --- F-17: /stop asks when the count is unknown -----------------------------------------------------------------
def test_stop_asks_when_nobody_can_tell_who_plays(make_instance: Any, django_capture_on_commit_callbacks: Any) -> None:
    inst = make_instance(name="Valley", status="running", desired_state="running", discord_available=True)
    asked = logic.stop(FRIEND, "Valley", SETTINGS)
    assert "cannot tell whether anyone is playing on Valley" in asked.text and "confirm: True" in asked.text
    assert not Job.objects.filter(instance=inst).exists()
    with django_capture_on_commit_callbacks(execute=False):
        done = logic.stop(FRIEND, "Valley", SETTINGS, confirm=True)
    assert done.text.startswith("Stopping Valley") and Instance.objects.get(pk=inst.pk).jobs.get().kind == "stop"


# --- F-9, F-10, F-11 / FX2-1: a Friend sees, starts and stops, nothing else -----------------------------------
@pytest.fixture
def friend_on(make_instance: Any, make_user: Any, roles: dict[str, Any]) -> tuple[Any, Any]:
    from apps.accounts.models import InstanceAccess

    shared = make_instance(name="Shared")
    friend = make_user("robin")
    InstanceAccess.objects.create(user=friend, instance=shared, role=roles["friend"])
    return friend, shared


def test_a_friend_may_only_start_and_stop(
    friend_on: Any, client_for: Any, clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    from apps.instances.services import enqueue_power

    friend, shared = friend_on
    client = client_for(friend)
    base = f"/api/instances/{shared.id}"
    refused = [
        {"action": "kill"},
        {"action": "restart"},
        {"action": "stop", "warn_minutes": 1},
        {"action": "stop", "warn_minutes": 1, "warn_message": "free diamonds at evil.example"},
        {"action": "start", "allow_overcommit": True},
    ]
    Instance.objects.filter(pk=shared.pk).update(status="running", desired_state="running")
    for body in refused:
        with django_capture_on_commit_callbacks(execute=False):
            assert client.post(f"{base}/power", body, format="json").status_code == 403, body
    assert not Job.objects.exists()
    with django_capture_on_commit_callbacks(execute=False):
        assert client.post(f"{base}/power", {"action": "stop"}, format="json").status_code == 202
    Job.objects.all().delete()
    # the owner's countdown: a friend can neither cancel it nor see its text
    shared.refresh_from_db()
    enqueue_power(shared, "restart", user=None, warn_minutes=5, warn_message="Restart for the owner's mods")
    assert client.post(f"{base}/power/cancel").status_code == 403
    data = client.get(base).json()
    assert set(data["power_countdown"]) == {"action", "phase", "action_at"}
    # an operator keeps every action
    with django_capture_on_commit_callbacks(execute=False):
        assert clients["owner"].post(f"{base}/power/cancel").status_code == 200


def test_the_short_answer_holds_no_addresses_job_texts_or_agent_errors(friend_on: Any, client_for: Any) -> None:
    from apps.core.models import SystemSetting
    from apps.instances.serializers import LIMITED_FIELDS, LIMITED_PLAYER_FIELDS
    from apps.jobs.runner import enqueue

    friend, shared = friend_on
    SystemSetting.set("lan_address", "192.168.1.89")
    job = enqueue("backup", title="Backup before restoring 'secret-base-coords'", instance=shared, user=None)
    Instance.objects.filter(pk=shared.pk).update(active_job=job, status="failed", status_detail="agent: /mnt/user/appdata/x")
    data = client_for(friend).get(f"/api/instances/{shared.id}").json()
    assert tuple(data) == LIMITED_FIELDS + LIMITED_PLAYER_FIELDS and "connection" not in data
    assert data["active_job"] == {"kind": "backup", "status": "queued", "progress": 0.0}
    assert data["status_detail"] == "The server stopped with an error; the owner can see why in the panel."
    assert "192.168.1.89" not in str(data) and "secret-base" not in str(data) and "/mnt/user" not in str(data)


def test_global_answers_are_not_for_a_friend(
    friend_on: Any, client_for: Any, clients: Any, fake_agent: Any, make_user: Any, roles: dict[str, Any]
) -> None:
    from apps.accounts.models import InstanceAccess

    friend, shared = friend_on
    routes = (
        "/api/system/info",
        "/api/system/ports",
        "/api/nodes",
        "/api/roles",
        "/api/catalog/games",
        "/api/catalog/server-types",
        "/api/catalog/versions",
        f"/api/catalog/games/{shared.game or 'minecraft'}/versions",
        "/api/permissions",
        "/api/modpacks/providers",
        "/api/notifications/events",
    )
    for route in routes:
        assert client_for(friend).get(route).status_code == 403, route
    # what the full UI needs stays: everyone with any other right, or with "View server" on one server
    for who in ("owner", "viewer", "operator"):
        for route in routes[:4]:
            assert clients[who].get(route).status_code == 200, (who, route)
    viewer_here = make_user("vic")
    InstanceAccess.objects.create(user=viewer_here, instance=shared, role=roles["viewer"])
    assert client_for(viewer_here).get("/api/system/ports").status_code == 200
    # a download ticket only for a download the caller may make
    url = f"/api/instances/{shared.id}/files/download?path=server.properties"
    assert client_for(friend).post("/api/auth/download-ticket", {"url": url}, format="json").status_code == 403
    assert clients["owner"].post("/api/auth/download-ticket", {"url": url}, format="json").status_code == 200
    assert clients["viewer"].post("/api/auth/download-ticket", {"url": url}, format="json").status_code == 403


# --- F-15: the bot's service account ----------------------------------------------------------------------------
def test_the_service_account_is_never_a_person(make_instance: Any, clients: Any, make_user: Any) -> None:
    from apps.accounts import reserved
    from apps.accounts.models import User

    # a person called "discord-bot" from before this version is never taken over
    person = make_user("discord-bot")
    bot_user = logic.service_user()
    person.refresh_from_db()
    assert bot_user.pk != person.pk and bot_user.username == "discord-bot-2" and person.is_active
    assert person.has_usable_password() and reserved.is_service_account(bot_user) and not reserved.is_service_account(person)
    assert logic.service_user().pk == bot_user.pk
    # the names are reserved: create, invite, rename
    a = make_instance(name="Alpha")
    taken = {"username": "Discord-Bot-7", "password": "Long-Enough-Password-1", "role": None}
    assert clients["owner"].post("/api/users", taken, format="json").status_code == 400
    invite = {"username": "discord-bot-9", "servers": [str(a.id)]}
    assert clients["owner"].post("/api/users/invite", invite, format="json").status_code == 400
    other = make_user("sam")
    assert clients["owner"].patch(f"/api/users/{other.pk}", {"username": "discord-bot-3"}, format="json").status_code == 400
    assert clients["owner"].patch(f"/api/users/{person.pk}", {"email": "p@example.org"}, format="json").status_code == 200
    # the service account itself cannot be changed or given a sign-in link
    assert clients["owner"].patch(f"/api/users/{bot_user.pk}", {"is_active": True}, format="json").status_code == 403
    assert clients["owner"].post(f"/api/users/{bot_user.pk}/reset-link").status_code == 403
    assert User.objects.get(pk=bot_user.pk).is_active is False


# --- F-17: every reveal of a join password is audited (never the value) ----------------------------------------
def test_join_password_reveals_are_audited(clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any) -> None:
    import json

    from apps.audit.models import AuditEvent
    from apps.core.models import SystemSetting

    from .test_games_six import create

    SystemSetting.set("public_address", "203.0.113.5")
    vh = create(clients, django_capture_on_commit_callbacks, "valheim", secrets={"server_password": "sesame-42"})
    clients["owner"].get(f"/api/instances/{vh.id}/share")
    assert not AuditEvent.objects.filter(action="instance.secret_revealed").exists()  # (no password in it)
    clients["owner"].get(f"/api/instances/{vh.id}/share", {"with_password": "true"})
    event = AuditEvent.objects.get(action="instance.secret_revealed")
    assert event.details == {"key": "join_password", "via": "share_card"} and event.actor_username == "owner"
    Instance.objects.filter(pk=vh.pk).update(discord_available=True, discord_share_password=True)
    assert "sesame-42" in logic.join(FRIEND, vh.name).text
    shown = AuditEvent.objects.filter(action="instance.secret_revealed").order_by("-created_at").first()
    assert shown.details["key"] == "join_password" and shown.details["discord_user_id"] == FRIEND.id
    assert "sesame-42" not in json.dumps([e.details for e in AuditEvent.objects.all()])
