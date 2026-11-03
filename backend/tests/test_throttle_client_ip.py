"""The login throttle identifies clients by REMOTE_ADDR, never by a client-sent X-Forwarded-For.

Behind a reverse proxy uvicorn (--proxy-headers with a trusted peer list, see
backend/container/entrypoint.sh) rewrites REMOTE_ADDR; DRF itself must not parse the
header again (settings.REST_FRAMEWORK["NUM_PROXIES"] = 0), or a client could pick a new
address per request and bypass the rate limit.
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db


def test_spoofed_x_forwarded_for_does_not_bypass_login_throttle(users):
    client = APIClient()
    codes = [
        client.post(
            "/api/auth/login",
            {"username": "admin", "password": "nope-nope-nope"},
            HTTP_X_FORWARDED_FOR=f"203.0.113.{i}",
        ).status_code
        for i in range(11)
    ]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_throttle_is_per_remote_addr(users):
    for _ in range(10):
        APIClient(REMOTE_ADDR="198.51.100.1").post("/api/auth/login", {"username": "admin", "password": "nope-nope-nope"})
    blocked = APIClient(REMOTE_ADDR="198.51.100.1").post("/api/auth/login", {"username": "admin", "password": "nope-nope-nope"})
    other = APIClient(REMOTE_ADDR="198.51.100.2").post("/api/auth/login", {"username": "admin", "password": "nope-nope-nope"})
    assert blocked.status_code == 429
    assert other.status_code == 401
