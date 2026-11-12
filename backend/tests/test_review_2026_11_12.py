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
