"""The session bound to the browser tab that signed in (D-1 of the security review of 2026-11-11, interface I-7).

A cookie belongs to a host name, not to a port: a browser signed in at ``http://tower:8080`` sends the panel's
cookies to every other port of ``tower`` too (game servers, other web UIs on the NAS), and whoever gets them held
the whole panel. Now the cookie alone is not enough:

* at sign-in (login, first-run setup) the answer carries a random ``session_token`` (32 bytes, shown once); the
  session keeps only its SHA-256;
* every ``/api/`` request of a signed-in session must carry it in ``X-MCPanel-Session-Token`` (constant-time
  compare), safe methods included; else 401 ``session_token_required``. A session without a token (from before
  this release) has to sign in again. Exempt: :data:`EXEMPT_PATHS`;
* a GET of a download route (a view with ``download_ticket = True``) may carry a ``ticket`` query parameter
  instead: a signed value bound to the session, the exact path and query, valid :data:`TICKET_SECONDS`
  (``POST /api/auth/download-ticket``), because a browser following a link sends no header;
* WebSockets: the first client frame must be ``{"type": "auth", "token": ...}`` (``apps.console.consumers``).

The setting ``MCPANEL_SESSION_BINDING`` (default true) switches the checks off; tokens are still issued.
"""

from __future__ import annotations

import hashlib
import secrets
from importlib import import_module
from typing import Any
from urllib.parse import parse_qsl

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare

HEADER = "X-MCPanel-Session-Token"
META_KEY = "HTTP_X_MCPANEL_SESSION_TOKEN"
SESSION_DIGEST_KEY = "mcpanel_session_token_sha256"
TICKET_PARAM = "ticket"
TICKET_SECONDS = 60
_TICKET_SALT = "mcpanel.core.download-ticket"
# routes a session without its token may still call (signing in, the state, the health probe)
EXEMPT_PATHS = frozenset({"/api/auth/state", "/api/auth/login", "/api/auth/setup", "/api/auth/reset", "/api/health"})
# the logout needs the token too when the session has one (R-13: a stolen cookie must not sign the owner out);
# a session without one (from before this release) may sign out without it
LOGOUT_PATH = "/api/auth/logout"


def enabled() -> bool:
    return bool(getattr(settings, "MCPANEL_SESSION_BINDING", True))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(session: Any) -> str:
    """A new token for this (just signed-in) session; only its digest is kept in the session."""
    token = secrets.token_urlsafe(32)
    session[SESSION_DIGEST_KEY] = _digest(token)
    return token


def token_matches(session_data: Any, token: Any) -> bool:
    """Whether ``token`` is the token of the session whose data is ``session_data`` (a session or a dict)."""
    stored = session_data.get(SESSION_DIGEST_KEY) if session_data is not None else None
    if not stored or not isinstance(token, str) or not token or len(token) > 200:
        return False
    return constant_time_compare(stored, _digest(token))


def header_ok(request: Any) -> bool:
    return token_matches(getattr(request, "session", None), request.META.get(META_KEY, ""))


def exempt(path: str, session: Any = None) -> bool:
    clean = path.rstrip("/")
    if clean == LOGOUT_PATH:
        return session is None or not session.get(SESSION_DIGEST_KEY)
    return clean in EXEMPT_PATHS


def session_token_ok(session_key: str | None, token: Any) -> bool:
    """For a WebSocket: whether ``token`` belongs to the stored session ``session_key``."""
    if not session_key:
        return False
    store = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    try:
        data = store.load()
    except Exception:
        return False
    return token_matches(data, token)


# --- download tickets --------------------------------------------------------------------------------------
def _binding(session: Any) -> str:
    """What a ticket is bound to: derived from the session's token digest (so a ticket dies with the session)."""
    stored = session.get(SESSION_DIGEST_KEY) if session is not None else None
    return hashlib.sha256(f"ticket:{stored}".encode()).hexdigest()[:32] if stored else ""


def _query_items(query: str) -> list[tuple[str, str]]:
    return sorted((k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != TICKET_PARAM)


def make_ticket(session: Any, path: str, query: str) -> str:
    return signing.dumps({"b": _binding(session), "p": path, "q": query}, salt=_TICKET_SALT, compress=True)


def ticket_ok(request: Any) -> bool:
    """A GET whose ``ticket`` was issued to this session for exactly this path and query, less than
    TICKET_SECONDS ago."""
    raw = request.GET.get(TICKET_PARAM) if request.method == "GET" else None
    binding = _binding(getattr(request, "session", None))
    if not raw or not binding:
        return False
    try:
        data = signing.loads(raw, salt=_TICKET_SALT, max_age=TICKET_SECONDS)
    except signing.BadSignature:  # (also an expired one)
        return False
    if not isinstance(data, dict) or not constant_time_compare(str(data.get("b") or ""), binding):
        return False
    if data.get("p") != request.path:
        return False
    return _query_items(request.META.get("QUERY_STRING", "")) == _query_items(str(data.get("q") or ""))
