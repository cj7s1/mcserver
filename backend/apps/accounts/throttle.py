"""Per-account limits on failed password checks (D-3 of the security review of 2026-11-11).

The login's scoped throttle counts per source address only (10 a minute), so a client with many addresses could
try passwords against one account without bound, and the password change checked ``old_password`` with no limit
at all. These counters are per account, from any address, and count failures only:

* ``login``: keyed by the lower-cased user name typed (unknown names alike, so the answer tells nothing);
* ``password``: keyed by the signed-in user (a stolen session cannot test the account's password either).

When a counter is full, the check is refused before the password is looked at (429 ``throttled`` with
``details.wait``), also for the right password: otherwise the limit would not limit guessing. The counters live
in the cache (Redis in the panel container), in fixed windows.

A **known browser** is not held back by these limits: anyone who can reach the panel could otherwise keep the
owner out of his own panel with a few wrong passwords. A successful sign-in sets a signed cookie (HttpOnly,
SameSite=Lax, Secure like the session cookie, a year, one per route: the login and the password change), bound to
the user and new at every sign-in; a request that carries a valid one for the account it is about skips the
per-account limit (the per-address limit still applies). ``manage.py unlock_login <username>`` clears the counters.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from rest_framework.exceptions import Throttled

# the cookie of each route that checks a password (a cookie's path keeps it to that route)
KNOWN_BROWSER_COOKIES = {
    "login": ("mcpanel_known_browser", "/api/auth/login"),
    "password": ("mcpanel_known_browser_pw", "/api/auth/password"),
}
KNOWN_BROWSER_AGE = 365 * 24 * 3600
_KNOWN_BROWSER_SALT = "mcpanel.accounts.known-browser"
LOCKED_HINT = (
    "Too many failed attempts for this account. Wait until the limit ends, sign in from a browser that has signed "
    "in to this account before, or run `manage.py unlock_login <username>` in the panel container."
)

# (window in seconds, failures allowed in it)
WINDOWS: tuple[tuple[int, int], ...] = ((600, 10), (86400, 100))


def _keys(scope: str, account: str, now: float) -> list[tuple[str, int, int, float]]:
    digest = hashlib.sha256(account.strip().lower().encode("utf-8")).hexdigest()[:32]
    out = []
    for window, limit in WINDOWS:
        slot = int(now // window)
        out.append((f"auth-fail:{scope}:{window}:{slot}:{digest}", window, limit, (slot + 1) * window - now))
    return out


def check(scope: str, account: str) -> None:
    """Refuse (429 ``throttled``) while this account has used up a window's failures."""
    now = time.time()
    for key, _window, limit, remaining in _keys(scope, account, now):
        if int(cache.get(key) or 0) >= limit:
            wait = max(1, int(remaining) + 1)
            # (DRF adds "Expected available in N seconds.")
            exc = Throttled(wait=wait, detail=LOCKED_HINT)
            exc.details = {"wait": wait}  # type: ignore[attr-defined]
            raise exc


def failed(scope: str, account: str) -> None:
    """Count one failure of this account."""
    now = time.time()
    for key, window, _limit, _remaining in _keys(scope, account, now):
        cache.add(key, 0, timeout=window + 60)
        try:
            cache.incr(key)
        except ValueError:  # expired between the two calls
            cache.set(key, 1, timeout=window + 60)


def unlock(scope: str, account: str) -> int:
    """Clear this account's counters (``manage.py unlock_login``); returns how many were set."""
    keys = [key for key, *_rest in _keys(scope, account, time.time())]
    found = sum(1 for key in keys if cache.get(key))
    cache.delete_many(keys)
    return found


# --- known browsers ---------------------------------------------------------------------------------------
def set_known_browser(response: Any, user: Any) -> None:
    """After a successful sign-in: a new known-browser cookie for each password route (rotated at every sign-in)."""
    value = signing.dumps({"u": user.pk, "n": secrets.token_urlsafe(12)}, salt=_KNOWN_BROWSER_SALT)
    for name, path in KNOWN_BROWSER_COOKIES.values():
        response.set_cookie(
            name,
            value,
            max_age=KNOWN_BROWSER_AGE,
            path=path,
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="Lax",
        )


def known_browser_user_id(request: Any, scope: str) -> Any:
    """The user id a valid, unexpired known-browser cookie of this route names, else None (forged, expired, none)."""
    name, _path = KNOWN_BROWSER_COOKIES[scope]
    raw = request.COOKIES.get(name)
    if not raw:
        return None
    try:
        data = signing.loads(raw, salt=_KNOWN_BROWSER_SALT, max_age=KNOWN_BROWSER_AGE)
    except signing.BadSignature:  # (SignatureExpired is one)
        return None
    uid = data.get("u") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) and not isinstance(uid, bool) else None
