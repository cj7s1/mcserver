"""Download provider files (mods/plugins/packs) into an instance with hash verification.

Only HTTPS URLs on allow-listed provider CDNs are fetched; the agent never
fetches arbitrary URLs supplied by users.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from typing import Any
from urllib.parse import urlparse

from .docker_ops import AgentError
from .safefs import SafeRoot

DEFAULT_ALLOWED_HOSTS = {
    "cdn.modrinth.com",
    "cdn-raw.modrinth.com",
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
    "media.forgecdn.net",
}
MAX_FETCH_BYTES = int(os.environ.get("AGENT_MAX_FETCH_BYTES", str(1024 * 1024 * 1024)))
USER_AGENT = "mcpanel-agent/0.1 (+https://github.com/cj7s1/mcserver)"


def allowed_hosts() -> set[str]:
    extra = {
        h.strip().lower() for h in os.environ.get("AGENT_FETCH_ALLOWED_HOSTS", "").split(",") if h.strip()
    }
    return DEFAULT_ALLOWED_HOSTS | extra


def fetch_into(root: SafeRoot, body: dict[str, Any]) -> dict[str, Any]:
    url = str(body.get("url", ""))
    path = str(body.get("path", ""))
    sha1 = body.get("sha1")
    sha512 = body.get("sha512")
    overwrite = bool(body.get("overwrite", False))
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts():
        raise AgentError("Download URL is not on an allowed provider host", "url_not_allowed", 403)
    if not (sha1 or sha512):
        raise AgentError("A sha1 or sha512 hash is required to verify downloads", "hash_required", 400)
    if not path:
        raise AgentError("Destination path is required", "invalid_path", 400)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - scheme/host checked above
    h1 = hashlib.sha1()  # noqa: S324 - provider-published hash, integrity check only
    h512 = hashlib.sha512()
    writer = root.open_writer(path, overwrite=overwrite, max_bytes=MAX_FETCH_BYTES)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            final = urlparse(resp.geturl())
            if final.scheme != "https" or (final.hostname or "").lower() not in allowed_hosts():
                raise AgentError("Download redirected to a non-allowed host", "url_not_allowed", 403)
            while chunk := resp.read(256 * 1024):
                h1.update(chunk)
                h512.update(chunk)
                writer.write(chunk)
        if sha1 and h1.hexdigest() != str(sha1).lower():
            raise AgentError("Downloaded file failed SHA-1 verification", "hash_mismatch", 502)
        if sha512 and h512.hexdigest() != str(sha512).lower():
            raise AgentError("Downloaded file failed SHA-512 verification", "hash_mismatch", 502)
        size = writer.commit()
    except AgentError:
        writer.abort()
        raise
    except OSError as exc:
        writer.abort()
        raise AgentError(f"Download failed: {exc}", "download_failed", 502) from exc
    except BaseException:
        writer.abort()
        raise
    return {"path": path, "size": size, "sha1": h1.hexdigest(), "sha512": h512.hexdigest()}
