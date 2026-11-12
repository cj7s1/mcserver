from __future__ import annotations

import secrets
import string


def random_secret(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse_uuid(value: object):  # type: ignore[no-untyped-def]
    """``value`` as a UUID, or None when it is not one (a malformed id from a query string or body, D-12)."""
    import uuid

    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def client_ip(request) -> str | None:  # type: ignore[no-untyped-def]
    return request.META.get("REMOTE_ADDR")


# A file from a server folder is never content of the panel's origin (C-4): no script, style, frame or plugin
# may run from it, whatever its name (``evil.js`` loaded by a <script src> under the SPA's script-src 'self')
DOWNLOAD_CSP = "default-src 'none'; sandbox"


def as_attachment(response, filename: str):  # type: ignore[no-untyped-def]
    """Mark a download answer: attachment with a safe file name, ``nosniff`` and :data:`DOWNLOAD_CSP`."""
    response["Content-Disposition"] = content_disposition(filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = DOWNLOAD_CSP
    return response


def content_disposition(filename: str) -> str:
    """RFC 6266 attachment header that is safe for any file name (no header injection)."""
    import re
    from urllib.parse import quote

    cleaned = re.sub(r"[\x00-\x1f\x7f\"\\\\/]", "", filename).strip() or "download"
    ascii_name = cleaned.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name[:150]}\"; filename*=UTF-8''{quote(cleaned[:150], safe='')}"
