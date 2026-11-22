"""The game registry's first load from several request threads at once (2026-11-22, seen on Unraid).

Right after a panel start, the browser's parallel requests made the first lookups at the same time. The
registry was marked loaded before it was filled, so a second thread found it empty and the Share card
failed with "This panel does not know the game 'minecraft'".
"""

from __future__ import annotations

import threading
import time

import pytest

import apps.games as games
from apps.games.minecraft import MinecraftDefinition


@pytest.fixture
def fresh_registry(monkeypatch):
    # an empty registry for the test; monkeypatch puts the real one back afterwards
    monkeypatch.setattr(games, "_registry", {})
    monkeypatch.setattr(games, "_loaded", False)
    monkeypatch.setattr(games, "_loading", False, raising=False)


def test_concurrent_first_lookups_all_find_minecraft(fresh_registry, monkeypatch):
    original_init = MinecraftDefinition.__init__

    def slow_init(self, *args, **kwargs):
        time.sleep(0.3)  # the first thread is still filling the registry when the others look
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(MinecraftDefinition, "__init__", slow_init)
    results: list[object] = []
    errors: list[BaseException] = []

    def lookup():
        try:
            results.append(games.get("minecraft"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=lookup) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert len(results) == 6 and all(r is results[0] for r in results)
    assert set(games.BUILT_IN) <= set(games.ids())


def test_failed_load_is_tried_again(fresh_registry, monkeypatch):
    original_init = MinecraftDefinition.__init__
    calls = {"n": 0}

    def flaky_init(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(MinecraftDefinition, "__init__", flaky_init)
    with pytest.raises(RuntimeError):
        games.get("minecraft")
    assert games.get("minecraft").id == "minecraft"  # not left marked as loaded but empty
