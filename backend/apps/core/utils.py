from __future__ import annotations

import secrets
import string


def random_secret(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def client_ip(request) -> str | None:  # type: ignore[no-untyped-def]
    return request.META.get("REMOTE_ADDR")


def content_disposition(filename: str) -> str:
    """RFC 6266 attachment header that is safe for any file name (no header injection)."""
    import re
    from urllib.parse import quote

    cleaned = re.sub(r"[\x00-\x1f\x7f\"\\\\/]", "", filename).strip() or "download"
    ascii_name = cleaned.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name[:150]}\"; filename*=UTF-8''{quote(cleaned[:150], safe='')}"
