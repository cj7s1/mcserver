"""Fixes of the independent review of the branch fix/security-review (audit-2026-11-09/fixes/review/).

* R-8 (A-6): the games whose control port listens on the network run only on the agent's own network: the panel
  refuses another network for them instead of letting the next create fail at the agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps import games

from .test_games_phase2 import factorio  # noqa: F401  (fixture)
from .test_games_six import create
from .test_terraria import make_tr

pytestmark = pytest.mark.django_db


# --- R-8 ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("game", ["palworld", "project-zomboid", "cs2", "rust"])
def test_r8_no_custom_network_for_games_with_a_network_control_port(
    game: str, clients: Any, fake_agent: Any, django_capture_on_commit_callbacks: Any
) -> None:
    """Adopted from fixes/review/agent-net/test_r_custom_network_mismatch.py (the agent refuses them: 400
    network_not_supported)."""
    inst = create(clients, django_capture_on_commit_callbacks, game)
    assert games.get(game).custom_network_for(inst) is False
    resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"network": "br0"}, format="json")
    assert resp.status_code == 400 and "network" in resp.json()["error"]["details"]["fields"]
    assert "node agent's own network" in str(resp.json()["error"]["details"]["fields"]["network"])
    detail = clients["owner"].get(f"/api/instances/{inst.id}").json()
    assert detail["custom_network"] is False and detail["network"] in ("", None)
    # clearing it stays possible
    assert clients["owner"].patch(f"/api/instances/{inst.id}", {"network": ""}, format="json").status_code == 200


def test_r8_factorio_and_tshock_neither_but_vanilla_terraria_and_minecraft_may(
    make_instance: Any,
    fake_agent: Any,
    clients: Any,
    factorio: Any,  # noqa: F811
) -> None:
    tshock = make_tr(make_instance, "tshock")
    vanilla = make_tr(make_instance, "vanilla", game_port=7778)
    minecraft = make_instance()
    for inst, allowed in ((factorio, False), (tshock, False), (vanilla, True), (minecraft, True)):
        assert clients["owner"].get(f"/api/instances/{inst.id}").json()["custom_network"] is allowed
        resp = clients["owner"].patch(f"/api/instances/{inst.id}", {"network": "br0"}, format="json")
        assert (resp.status_code == 400) is not allowed, (inst.game, inst.server_type, resp.content)
