from __future__ import annotations

import secrets
import string


def random_secret(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def client_ip(request) -> str | None:  # type: ignore[no-untyped-def]
    return request.META.get("REMOTE_ADDR")
