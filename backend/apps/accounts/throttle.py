"""Per-account limits on failed password checks (D-3 of the security review of 2026-11-11).

The login's scoped throttle counts per source address only (10 a minute), so a client with many addresses could
try passwords against one account without bound, and the password change checked ``old_password`` with no limit
at all. These counters are per account, from any address, and count failures only:

* ``login``: keyed by the lower-cased user name typed (unknown names alike, so the answer tells nothing);
* ``password``: keyed by the signed-in user (a stolen session cannot test the account's password either).

When a counter is full, the check is refused before the password is looked at (429 ``throttled`` with
``details.wait``), also for the right password: otherwise the limit would not limit guessing. The counters live
in the cache (Redis in the panel container), in fixed windows.
"""

from __future__ import annotations

import hashlib
import time

from django.core.cache import cache
from rest_framework.exceptions import Throttled

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
            exc = Throttled(wait=wait, detail="Too many failed attempts for this account.")
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
