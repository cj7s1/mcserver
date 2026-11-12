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

Codes of games run through an HTTP API (``drivers.http_api``, Satisfactory):

* ``action_unsupported`` (409): ``POST /game/{action}`` names an operation the game's driver lacks;
* ``api_unavailable`` (409) / ``api_timeout`` (504): the game's API does not answer (yet), or not in
  time; ``api_tls_changed`` (409): its certificate changed during the run (request not sent);
* ``api_error`` (502, or 409 for the game's state errors such as ``server_claimed``),
  ``api_unauthorized`` / ``api_forbidden`` (409): the game answered with an error (its own
  ``errorCode`` is in the message);
* ``server_unclaimed`` (409): an apply, start or restart would run a Satisfactory server with
  published ports that nobody claimed (no claim file ``saved/ServerSettings.7777.sav``, no API
  token, or the running server still accepts an InitialAdmin login); ``claim_unverified`` (409):
  the claim of the running server could not be verified (its API did not answer or answered
  unexpectedly); ``claimed_elsewhere`` (409): it was claimed with another admin password;
* ``session_exists`` / ``save_exists`` (409): a new game or an imported save would reuse the name
  of a session or save the server has.

Codes of the container checks:

* ``network_not_supported`` (400): the profile runs on the agent's instance network only;
* ``port_collision`` (400): a port a variable names collides with a profile port, or a host or
  container port (per protocol) is published twice (profiles with ``env_ports``);
* ``image_volume_uncovered`` (409): the image declares a VOLUME the profile does not mount (Docker
  would create an anonymous volume);
* ``volume_source_invalid`` (409): a folder of the server folder the container mounts is no longer
  a real folder (checked before every start);
* ``console_timeout`` (504): a console exec did not finish in time.
"""

from __future__ import annotations

import re


class AgentError(Exception):
    def __init__(self, message: str, code: str = "agent_error", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --- text that comes from a game (B-9) ---------------------------------------------------------------
GAME_TEXT_MAX = 300
# control characters (C0 incl. newline/tab, DEL, C1) and the Unicode line/paragraph separators
_GAME_TEXT_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def game_text(value: object, limit: int = GAME_TEXT_MAX) -> str:
    """Text a game supplied (an error message of its API, a reply), fit for an error message or an
    agent log line: one line (control characters become a blank) and at most ``limit`` characters
    (cut with "..."). Never raises."""
    try:
        text = str(value)
    except Exception:
        return "?"
    text = _GAME_TEXT_CONTROL.sub(" ", text[: limit * 4]).strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."
