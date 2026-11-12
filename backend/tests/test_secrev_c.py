"""Fixes of the security and bug review of 2026-11-11, slice C (audit-2026-11-09/security-review/c-untrusted-data).

The reviewers' proofs (``c-untrusted-data/poc/backend/test_c_backend.py``) passed while each weakness was present;
the tests here assert the fixed behaviour.

* C-8: TShock's one-time ``/setup`` code is switched off (``setup.lock``) and its file is a secret file.
"""

from __future__ import annotations

from typing import Any

from apps import games
from apps.games import terraria
from apps.instances import secret_files
from apps.nodes.client import AgentClient

from .test_terraria import make_tr


# --- C-8 ------------------------------------------------------------------------------------------------------
def test_c8_a_new_tshock_server_has_the_setup_lock(make_instance: Any) -> None:
    inst = make_tr(make_instance, "tshock")
    files = games.for_instance(inst).initial_files(inst, {})
    assert files[terraria.SETUP_LOCK] == ""
    vanilla = make_tr(make_instance, "vanilla", game_port=7778)
    assert terraria.SETUP_LOCK not in games.for_instance(vanilla).initial_files(vanilla, {})


def test_c8_every_apply_writes_the_lock_and_deletes_a_setup_code(make_instance: Any, fake_agent: Any) -> None:
    """A server from before the fix (or a restored / imported folder): no lock, TShock's code file in place."""
    inst = make_tr(make_instance, "tshock")
    fake_agent.put_file(inst.id, "setup-code.txt", "4815162")
    games.for_instance(inst).before_apply(inst, AgentClient(inst.node))
    assert fake_agent.text(inst.id, "setup.lock") == ""
    assert fake_agent.get_file(inst.id, "setup-code.txt") is None
    # again: nothing to do (an existing lock is kept as it is)
    fake_agent.put_file(inst.id, "setup.lock", "kept")
    games.for_instance(inst).before_apply(inst, AgentClient(inst.node))
    assert fake_agent.text(inst.id, "setup.lock") == "kept"


def test_c8_vanilla_gets_no_lock(make_instance: Any, fake_agent: Any) -> None:
    inst = make_tr(make_instance, "vanilla")
    fake_agent.prepare(inst.id)
    games.for_instance(inst).before_apply(inst, AgentClient(inst.node))
    assert fake_agent.get_file(inst.id, "setup.lock") is None


def test_c8_the_setup_code_file_is_a_secret_file(make_instance: Any, fake_agent: Any, clients: Any) -> None:
    """The PoC's operator read ``setup-code.txt`` in clear through the file manager."""
    inst = make_tr(make_instance, "tshock")
    fake_agent.put_file(inst.id, "setup-code.txt", "4815162")
    assert secret_files.is_secret(inst, "setup-code.txt")
    base = f"/api/instances/{inst.id}/files"
    for client in (clients["operator"], clients["owner"]):
        shown = client.get(f"{base}/text", {"path": "setup-code.txt"}).json()
        assert shown["redacted"] is True and "4815162" not in shown["content"]
        assert client.get(f"{base}/download", {"path": "setup-code.txt"}).status_code == 403
    # only TShock has one
    assert not secret_files.is_secret(make_tr(make_instance, "vanilla", game_port=7779), "setup-code.txt")
