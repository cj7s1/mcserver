"""SpigotMC resources through Spiget (api.spiget.org), the public read-only SpigotMC mirror API.

Verified with real requests on 2026-11-06 (details in ``docs/reference/spiget-api.md``):

* ``GET /search/resources/{query}?field=name&size=&page=&sort=-downloads&fields=...`` returns a list of
  resources; **404 with ``[]``** when nothing matches; ``X-Page-Count`` header, pages start at 1.
  ``GET /resources/free?sort=-downloads`` lists popular free resources (used for an empty query).
* ``GET /resources/{id}``: ``description`` is **base64-encoded HTML**, ``tag`` is a one-line summary,
  ``premium``/``price``, ``external`` + ``file{type, size, sizeUnit, url, externalUrl}``,
  ``testedVersions`` (author-declared, resource level, e.g. ``"1.21"``), ``version{id}``, ``author{id}``.
* ``GET /resources/{id}/versions`` lists ``{id, name, releaseDate}`` only: no files, no hashes and no
  per-version Minecraft versions. ``GET /authors/{id}`` gives the author's name.
* ``GET /resources/{id}/download`` answers ``302`` to ``https://cdn.spiget.org/file/spiget-resources/{id}.jar``
  (``X-Spiget-File-Source: cdn``) for files hosted on SpigotMC, or to the author's site for external
  resources. ``GET /resources/{id}/versions/{vid}/download`` redirects to ``www.spigotmc.org``, which
  answers automated clients with a Cloudflare challenge (403), so **only Spiget's mirror of the latest
  file can be installed automatically**, and older versions only by hand.
* ``cdn.spiget.org`` is Backblaze B2 behind Cloudflare. It returns ``x-bz-content-sha1`` (the SHA-1 B2
  computed when Spiget stored the file) and ``x-bz-upload-timestamp`` (ms). The mirror **can lag**:
  Chunky's mirror held 1.5.3 while SpigotMC's latest was 1.5.66. Spiget publishes **no hash of
  SpigotMC's own file**, so nothing here proves a jar is what the author uploaded.

Everything returned here is plain text. Descriptions are converted from HTML to text in the panel and
never passed on as HTML.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import IO, Any
from urllib.parse import quote, urlparse

import httpx
from django.conf import settings
from django.core.cache import cache

from apps.core.errors import ApiError, UpstreamError

from .providers import _cached

SPIGET_API = "https://api.spiget.org/v2"
SPIGOT_SITE = "https://www.spigotmc.org"
CDN_HOST = "cdn.spiget.org"
PROVIDER = "SpigotMC (Spiget)"
PAGE_SIZE = 20
MAX_RESOURCE_ID = 10**9
DESCRIPTION_LIMIT = 4000
SEARCH_FIELDS = (
    "id,name,tag,external,premium,price,currency,file,icon.url,downloads,likes,rating,testedVersions,updateDate,"
    "releaseDate,version,author"
)
_CDN_PATH = re.compile(r"^/file/spiget-resources/(\d{1,10})\.jar$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_ICON_PATH = re.compile(r"^data/resource_icons/\d+/\d+\.(?:jpg|jpeg|png|gif|webp)(?:\?\d+)?$", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


# =====================================================================================
# HTTP
# =====================================================================================
def _headers() -> dict[str, str]:
    return {"User-Agent": settings.MCPANEL_USER_AGENT, "Accept": "application/json"}


def _get(path: str, params: dict[str, Any] | None = None, *, empty_on_404: bool = False) -> tuple[Any, httpx.Headers]:
    try:
        resp = httpx.get(f"{SPIGET_API}{path}", params=params, headers=_headers(), timeout=20, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not reach {PROVIDER}: {exc}", code="provider_unreachable") from exc
    if resp.status_code == 404:
        if empty_on_404:
            return [], resp.headers
        raise ApiError(f"Not found on {PROVIDER}", code="provider_not_found", status_code=404)
    if resp.status_code == 429:
        raise UpstreamError(f"{PROVIDER} rate limit reached; try again shortly", code="provider_rate_limited")
    if resp.status_code >= 400:
        raise UpstreamError(f"{PROVIDER} returned HTTP {resp.status_code}", code="provider_error")
    try:
        return resp.json(), resp.headers
    except ValueError as exc:
        raise UpstreamError(f"{PROVIDER} returned an unreadable response", code="provider_error") from exc


def validate_resource_id(value: Any) -> int:
    try:
        rid = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ApiError("Invalid SpigotMC resource ID", code="invalid_resource_id") from exc
    if not 0 < rid < MAX_RESOURCE_ID:
        raise ApiError("Invalid SpigotMC resource ID", code="invalid_resource_id")
    return rid


# =====================================================================================
# Plain text
# =====================================================================================
class _TextExtractor(HTMLParser):
    BLOCK = frozenset({"br", "p", "div", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})
    SKIP = frozenset({"script", "style", "noscript", "iframe", "object", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            # as in a browser, line breaks in the markup are just spaces; only tags break lines
            self.parts.append(data.replace("\n", " "))


def html_to_text(markup: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """Render untrusted HTML as plain text: tags dropped, entities decoded, whitespace tidied."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup must never break the page; fall back to stripping tags
        return clean_text(re.sub(r"<[^>]*>", " ", markup), limit)
    return clean_text("".join(parser.parts), limit)


def clean_text(text: str, limit: int) -> str:
    text = _CONTROL.sub("", text.replace("\r", "").replace("\xa0", " "))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(out) > limit:
        out = out[:limit].rstrip() + " …"
    return out


def description_text(encoded: Any) -> str:
    """Spiget sends descriptions as base64 HTML; anything else is treated as HTML too."""
    if not isinstance(encoded, str) or not encoded:
        return ""
    try:
        markup = base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        markup = encoded
    return html_to_text(markup)


def _short(value: Any, limit: int = 300) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return clean_text(html.unescape(value), limit) or None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    return value.strip() if parsed.scheme in ("http", "https") and parsed.hostname else None


def _iso(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# =====================================================================================
# Resources
# =====================================================================================
def resource_url(resource_id: int) -> str:
    return f"{SPIGOT_SITE}/resources/{resource_id}/"


def version_url(resource_id: int, version_id: int) -> str:
    return f"{SPIGOT_SITE}/resources/{resource_id}/download?version={version_id}"


def _resource(r: dict[str, Any]) -> dict[str, Any]:
    rid = int(r.get("id") or 0)
    f = r.get("file") or {}
    icon = (r.get("icon") or {}).get("url")
    rating = r.get("rating") or {}
    size, unit = f.get("size"), f.get("sizeUnit")
    tested = [str(v) for v in r.get("testedVersions") or [] if isinstance(v, (str, int, float))]
    return {
        "provider": "spigot",
        "id": rid,
        "title": _short(r.get("name"), 200) or f"Resource {rid}",
        "description": _short(r.get("tag")),
        "icon_url": f"{SPIGOT_SITE}/{icon}" if isinstance(icon, str) and _ICON_PATH.match(icon) else None,
        "downloads": r.get("downloads"),
        "likes": r.get("likes"),
        "rating": {"average": rating.get("average"), "count": rating.get("count")} if rating else None,
        "tested_versions": tested,
        "updated": _iso(r.get("updateDate")),
        "released": _iso(r.get("releaseDate")),
        # Spiget omits ``premium`` on some free resources; a price also marks a paid resource
        "premium": bool(r.get("premium")) or bool(r.get("price")),
        "price": r.get("price") or None,
        "currency": (str(r["currency"]).upper()[:8] if r.get("currency") else None),
        "external": bool(r.get("external")) or f.get("type") == "external",
        "file_type": str(f.get("type") or "")[:16] or None,
        "file_size": f"{size} {unit}".strip() if size and unit else None,
        "external_url": _http_url(f.get("externalUrl")),
        "latest_version_id": (r.get("version") or {}).get("id"),
        "author_id": (r.get("author") or {}).get("id"),
        "source_url": _http_url(r.get("sourceCodeLink")),
        "url": resource_url(rid),
    }


def availability(res: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(reason_code, message)`` when the resource cannot be installed automatically, else ``(None, None)``."""
    if res.get("premium"):
        return (
            "premium",
            "This is a premium (paid) resource. Buy and download it on SpigotMC, then add the jar in the Upload tab.",
        )
    if res.get("external"):
        host = urlparse(res.get("external_url") or "").hostname
        where = f" ({host})" if host else ""
        return (
            "external",
            f"The author hosts this resource outside SpigotMC{where}. Download it there and add the jar in the Upload tab.",
        )
    if (res.get("file_type") or "").lower() != ".jar":
        kind = f"a {res['file_type']}" if res.get("file_type") else "an unknown type of"
        return (
            "not_a_jar",
            f"This resource is published as {kind} file, not a plugin jar. Download it from SpigotMC and upload the jar.",
        )
    return None, None


def search(query: str, *, page: int = 1, size: int = PAGE_SIZE) -> dict[str, Any]:
    """Name search sorted by downloads; an empty query lists the most downloaded free resources."""
    query = re.sub(r"[\x00-\x1f\x7f/\\?#%]+", " ", query or "").strip()[:100]
    params = {"size": size, "page": max(1, page), "sort": "-downloads", "fields": SEARCH_FIELDS}
    if query:
        # one path segment; dots are encoded too so ".." can never become a dot-segment
        path = f"/search/resources/{quote(query, safe='').replace('.', '%2E')}"
        params["field"] = "name"
    else:
        path = "/resources/free"
    key = f"spiget:search:{hashlib.sha1(f'{path}|{sorted(params.items())}'.encode()).hexdigest()}"  # noqa: S324 - cache key

    def load() -> dict[str, Any]:
        data, headers = _get(path, params, empty_on_404=True)
        try:
            pages = max(1, int(headers.get("x-page-count") or 1))
        except ValueError:
            pages = 1
        rows = data if isinstance(data, list) else []
        return {"pages": pages if rows else 0, "results": [_resource(r) for r in rows if isinstance(r, dict)]}

    data = _cached(key, 120, load)
    return {"query": query, "page": max(1, page), "size": size, **data}


def resource(resource_id: int) -> dict[str, Any]:
    rid = validate_resource_id(resource_id)

    def load() -> dict[str, Any]:
        data, _ = _get(f"/resources/{rid}")
        if not isinstance(data, dict):
            raise UpstreamError(f"{PROVIDER} returned an unexpected response", code="provider_error")
        return {**_resource(data), "description_text": description_text(data.get("description"))}

    return _cached(f"spiget:resource:{rid}", 300, load)


def versions(resource_id: int, size: int = 15) -> list[dict[str, Any]]:
    rid = validate_resource_id(resource_id)

    def load() -> list[dict[str, Any]]:
        data, _ = _get(
            f"/resources/{rid}/versions", {"size": size, "sort": "-releaseDate", "fields": "id,name,releaseDate,downloads"}
        )
        out = []
        for v in data if isinstance(data, list) else []:
            if not isinstance(v, dict) or not isinstance(v.get("id"), int):
                continue
            out.append(
                {
                    "id": v["id"],
                    "name": _short(v.get("name"), 100) or str(v["id"]),
                    "released": _iso(v.get("releaseDate")),
                    "released_ts": v.get("releaseDate") if isinstance(v.get("releaseDate"), int) else None,
                    "downloads": v.get("downloads"),
                    "url": version_url(rid, v["id"]),
                }
            )
        return out

    return _cached(f"spiget:versions:{rid}:{size}", 300, load)


def author_name(author_id: Any) -> str | None:
    if not isinstance(author_id, int) or author_id <= 0:
        return None
    key = f"spiget:author:{author_id}"
    hit = cache.get(key)
    if hit is not None:
        return hit or None
    try:
        data, _ = _get(f"/authors/{author_id}", {"fields": "id,name"})
    except ApiError:
        return None  # the name is cosmetic; a failed lookup is not cached
    name = (_short(data.get("name"), 100) or "") if isinstance(data, dict) else ""
    cache.set(key, name, 86400)
    return name or None


def tested_for(minecraft_version: str | None, tested: list[str]) -> bool | None:
    """Whether the author lists the server's version ("1.21" covers "1.21.4"). None when unknown."""
    if not minecraft_version or not tested:
        return None
    return any(minecraft_version == t or minecraft_version.startswith(f"{t}.") for t in tested)


# =====================================================================================
# Spiget's file mirror (cdn.spiget.org)
# =====================================================================================
def mirror_url(resource_id: int) -> str:
    """Resolve Spiget's download redirect and accept it only if it points at its own CDN."""
    rid = validate_resource_id(resource_id)

    def load() -> str:
        try:
            resp = httpx.get(f"{SPIGET_API}/resources/{rid}/download", headers=_headers(), timeout=20, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Could not reach {PROVIDER}: {exc}", code="provider_unreachable") from exc
        location = resp.headers.get("location") or ""
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            raise UpstreamError(
                f"{PROVIDER} did not offer a download for this resource (HTTP {resp.status_code})", code="not_mirrored"
            )
        return check_mirror_url(location, rid)

    return _cached(f"spiget:mirror-url:{rid}", 3600, load)


def check_mirror_url(url: str, resource_id: int) -> str:
    parsed = urlparse(url)
    match = _CDN_PATH.match(parsed.path or "")
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != CDN_HOST
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.query
        or not match
        or int(match.group(1)) != resource_id
    ):
        raise ApiError(
            "Spiget does not host a copy of this file; download it from SpigotMC and add it in the Upload tab",
            code="not_mirrored",
            status_code=409,
        )
    return f"https://{CDN_HOST}{parsed.path}"


@dataclass
class MirrorInfo:
    url: str
    size: int | None
    sha1: str | None
    uploaded_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "sha1": self.sha1,
            "uploaded_at": self.uploaded_at.isoformat().replace("+00:00", "Z") if self.uploaded_at else None,
        }


def _mirror_info(url: str, headers: httpx.Headers) -> MirrorInfo:
    sha1 = (headers.get("x-bz-content-sha1") or "").strip().lower()
    try:
        uploaded = datetime.fromtimestamp(int(headers.get("x-bz-upload-timestamp") or "") / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        uploaded = None
    try:
        # a compressed transfer has a different length than the file
        encoded = (headers.get("content-encoding") or "identity").lower() != "identity"
        size = None if encoded else int(headers.get("content-length") or "")
    except ValueError:
        size = None
    return MirrorInfo(url=url, size=size, sha1=sha1 if _SHA1.match(sha1) else None, uploaded_at=uploaded)


def mirror_head(resource_id: int) -> MirrorInfo | None:
    """What Spiget's mirror holds right now (size, SHA-1, when it was stored); None if it holds nothing."""
    rid = validate_resource_id(resource_id)
    url = mirror_url(rid)

    def load() -> dict[str, Any]:
        try:
            resp = httpx.head(url, headers={**_headers(), "Accept-Encoding": "identity"}, timeout=20, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Could not reach {CDN_HOST}: {exc}", code="provider_unreachable") from exc
        if resp.status_code == 404:
            return {"missing": True}
        if resp.status_code != 200:
            raise UpstreamError(f"{CDN_HOST} returned HTTP {resp.status_code}", code="provider_error")
        info = _mirror_info(url, resp.headers)
        return {
            "missing": False,
            "size": info.size,
            "sha1": info.sha1,
            "uploaded_ts": info.uploaded_at.timestamp() if info.uploaded_at else None,
        }

    data = _cached(f"spiget:mirror-head:{rid}", 120, load)
    if data.get("missing"):
        return None
    ts = data.get("uploaded_ts")
    return MirrorInfo(
        url=url, size=data.get("size"), sha1=data.get("sha1"), uploaded_at=datetime.fromtimestamp(ts, tz=UTC) if ts else None
    )


def freshness(info: MirrorInfo | None, latest: dict[str, Any] | None) -> str:
    """``current`` when the mirror was stored after SpigotMC's latest release, ``outdated`` when before."""
    if info is None or info.uploaded_at is None or not latest or not latest.get("released_ts"):
        return "unknown"
    return "current" if info.uploaded_at.timestamp() >= latest["released_ts"] else "outdated"


@dataclass
class MirrorDownload:
    info: MirrorInfo
    file: IO[bytes]
    size: int
    sha1: str
    sha256: str
    warnings: list[str] = field(default_factory=list)

    def chunks(self, size: int = 256 * 1024) -> Iterator[bytes]:
        self.file.seek(0)
        while chunk := self.file.read(size):
            yield chunk

    def close(self) -> None:
        self.file.close()


class MirrorStream:
    """An open response from the mirror: headers are known before any of the body is read, so the
    caller can refuse (and close) before downloading."""

    def __init__(self, url: str, response: httpx.Response) -> None:
        self.url = url
        self.response = response
        self.info = _mirror_info(url, response.headers)

    def download(self, max_bytes: int) -> MirrorDownload:
        if self.info.size is not None and self.info.size > max_bytes:
            raise ApiError(f"The file is larger than {max_bytes // (1024 * 1024)} MB", code="file_too_large", status_code=413)
        h1 = hashlib.sha1()  # noqa: S324 - compared with the SHA-1 the mirror's storage reports
        h256 = hashlib.sha256()
        spool = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # closed by the caller (MirrorDownload.close)
        size = 0
        try:
            for chunk in self.response.iter_bytes(256 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(
                        f"The file is larger than {max_bytes // (1024 * 1024)} MB", code="file_too_large", status_code=413
                    )
                h1.update(chunk)
                h256.update(chunk)
                spool.write(chunk)
        except httpx.HTTPError as exc:
            spool.close()
            raise UpstreamError(f"Download from {CDN_HOST} failed: {exc}", code="download_failed") from exc
        except BaseException:
            spool.close()
            raise
        if not size or (self.info.size is not None and size != self.info.size):
            spool.close()
            raise UpstreamError(f"Download from {CDN_HOST} was incomplete", code="download_failed")
        if self.info.sha1 and h1.hexdigest() != self.info.sha1:
            spool.close()
            raise UpstreamError(f"The file from {CDN_HOST} does not match the SHA-1 its storage reports", code="hash_mismatch")
        warnings = (
            [] if self.info.sha1 else [f"{CDN_HOST} did not report a SHA-1 for this file, so the transfer was not checked."]
        )
        return MirrorDownload(self.info, spool, size, h1.hexdigest(), h256.hexdigest(), warnings)


@contextmanager
def open_mirror(resource_id: int) -> Iterator[MirrorStream]:
    """Stream Spiget's copy of the resource's file. Redirects are never followed."""
    rid = validate_resource_id(resource_id)
    url = mirror_url(rid)
    try:
        with httpx.stream(
            "GET",
            url,
            headers={**_headers(), "Accept": "application/java-archive, application/octet-stream", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(30, read=60),
            follow_redirects=False,
        ) as resp:
            if resp.status_code == 404:
                raise ApiError(
                    "Spiget does not hold a copy of this file yet; download it from SpigotMC and add it in the Upload tab",
                    code="not_mirrored",
                    status_code=409,
                )
            if resp.status_code != 200:
                raise UpstreamError(f"{CDN_HOST} returned HTTP {resp.status_code}", code="provider_error")
            yield MirrorStream(url, resp)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Could not download from {CDN_HOST}: {exc}", code="download_failed") from exc
