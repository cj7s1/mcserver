"""The agent's error type (re-exported by ``docker_ops``, where it used to live).

``AgentError(message, code, status)``: the API answers ``{"error": {"code", "message"}}`` with
``status``. Codes added with game profiles (multi-game phases 1-3):

* ``unknown_game`` (400): the spec names a game/variant this agent has no runtime profile for;
* ``image_not_allowed`` (403) / ``invalid_image`` (400): the image is not in the profile's
  allow-list, or its tag does not match the profile's ``tag_pattern`` (Factorio: exact
  ``X.Y.Z-rootless`` tags);
* ``env_not_allowed`` (400): a variable the profile refuses, or a value outside its
  ``env_values`` pattern;
* ``port_not_allowed`` (403): a container port the profile does not publish (or never publishes);
* ``command_unsupported`` / ``save_unsupported`` / ``broadcast_unsupported`` /
  ``ping_unavailable`` (409): the game's control driver has no such operation;
* ``rcon_unavailable`` (409): no RCON password (variable or secret file) or address;
* ``save_unconfirmed`` (409): the game did not print its "save finished" line in time (a
  consistent backup with ``consistency=require`` then fails with ``consistency_failed``).
"""

from __future__ import annotations


class AgentError(Exception):
    def __init__(self, message: str, code: str = "agent_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
