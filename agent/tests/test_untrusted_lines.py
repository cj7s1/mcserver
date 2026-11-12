"""C-3 of the security review (2026-11-11): "the save is finished" is the game's own log line, never a chat
line that repeats its words. Adopted from the reviewers' PoC (audit-2026-11-09/security-review/
c-untrusted-data/poc/agent/test_c_agent.py), turned around. (C-1: tests/test_ping.py; C-6:
tests/test_terraria.py.)"""

from __future__ import annotations

import time

import pytest

from mcagent import docker_ops
from mcagent.profiles import factorio, terraria, zomboid


class FakeContainer:
    """Only what docker_ops._log_matches needs: .logs() answers the container log."""

    id = "c0ffee"
    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text.encode()

    def logs(self, **_kwargs):
        return self._text


@pytest.mark.parametrize(
    ("pattern", "chat_line"),
    [
        # Factorio prints chat into the container log as "<date> [CHAT] <name>: <text>"
        (factorio.SAVE_DONE_PATTERN, "2026-11-11 20:15:03 [CHAT] mallory: Saving finished"),
        (factorio.SAVE_DONE_PATTERN, " 600.707 Info AppManagerStates.cpp:2131: [CHAT] m: Saving finished"),
        # Terraria (vanilla / tModLoader; TShock when REST is down) prints chat as "<name> text"
        (terraria.SAVE_DONE_LOG, "<mallory> Backing up world file"),
        (terraria.SAVE_DONE_LOG, ": <mallory> Backing up world file"),
        # Project Zomboid
        (zomboid.SAVE_DONE_PATTERN, "[11-11-26 20:15:03.123] mallory: Saving finish"),
        (zomboid.SAVE_DONE_PATTERN, "LOG  : General      f:0 st:1,234> mallory: Saving finish"),
        (zomboid.SAVE_DONE_PATTERN, "LOG  : General      f:0 st:1,234> chat x> Saving finish"),
    ],
)
def test_a_chat_line_does_not_count_as_save_finished(pattern: str, chat_line: str) -> None:
    c = FakeContainer(chat_line + "\n")
    assert docker_ops.wait_for_log(c, pattern, time.time() - 0.2, 0.3) is False


@pytest.mark.parametrize(
    ("pattern", "line"),
    [
        # the lines the games print (end-to-end runs, audit-2026-11-09/more-games and the probes)
        (factorio.SAVE_DONE_PATTERN, " 600.707 Info AppManagerStates.cpp:2131: Saving finished"),
        (factorio.SAVE_DONE_PATTERN, "  12345.678 Info AppManagerStates.cpp:2131: Saving finished\r"),
        (terraria.SAVE_DONE_LOG, ": Backing up world file"),
        (terraria.SAVE_DONE_LOG, "Backing up world file"),
        (zomboid.SAVE_DONE_PATTERN, "LOG  : General      f:0 st:395,038,282> Saving finish"),
        (zomboid.SAVE_DONE_PATTERN, "LOG  : General     , 1731316503123> 18,734,105> Saving finish"),
    ],
)
def test_the_games_own_line_counts(pattern: str, line: str) -> None:
    c = FakeContainer("other output\n" + line + "\nmore\n")
    assert docker_ops.wait_for_log(c, pattern, time.time() - 0.2, 0.3) is True
