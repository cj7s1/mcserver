"""Bounded requests to the metadata services the panel reads (catalog versions, modpack and mod providers).

C-10 of the security review of 2026-11-11: those requests followed redirects to any host and read answers of
any size, so a compromised or spoofed upstream could send the panel's GET into the internal network or exhaust
its memory. :func:`fetch` follows a redirect only over HTTPS to the host it started at or to one of
``allowed_hosts``, and reads at most ``max_bytes`` of the answer. (File downloads go through the node agent,
which has its own host allow-list and hash check.)
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

MAX_UPSTREAM_BYTES = 32 * 1024 * 1024  # the largest answers read (a big project's version list) are a few MB
MAX_REDIRECTS = 5
_DROPPED_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class RedirectRefused(httpx.HTTPError):
    """The upstream redirected to a host it is not allowed to send the panel to."""


class ResponseTooLarge(httpx.HTTPError):
    """The upstream's answer is larger than the panel reads."""


def fetch(
    method: str,
    url: str,
    *,
    allowed_hosts: Iterable[str] = (),
    max_bytes: int = MAX_UPSTREAM_BYTES,
    timeout: float = 20,
    **kwargs: Any,
) -> httpx.Response:
    """``httpx.request`` with the two bounds above; the returned response is fully read (``content``, ``text``,
    ``json()``). Raises an ``httpx.HTTPError`` (the callers' "could not reach" path) for a refused redirect,
    too many redirects or an answer above ``max_bytes``."""
    current = httpx.URL(url)
    hosts = {current.host.lower(), *(h.lower() for h in allowed_hosts)}
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            request = client.build_request(method, current, **kwargs)
            resp = client.send(request, stream=True)
            try:
                if resp.has_redirect_location:
                    target = current.join(resp.headers["location"])
                    if target.scheme != "https" or (target.host or "").lower() not in hosts:
                        raise RedirectRefused(f"{current.host} redirected to another host ({target.host})")
                    current = target
                    # the query and body belong to the first request (a 303 is a GET, as httpx does it)
                    kwargs.pop("params", None)
                    if resp.status_code == 303:
                        method = "GET"
                        for key in ("content", "data", "files", "json"):
                            kwargs.pop(key, None)
                    continue
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise ResponseTooLarge(f"{current.host} sent more than {max_bytes // (1024 * 1024)} MB")
                headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _DROPPED_HEADERS]
                return httpx.Response(resp.status_code, headers=headers, content=bytes(body), request=request)
            finally:
                resp.close()
    raise httpx.TooManyRedirects(f"{httpx.URL(url).host}: too many redirects", request=request)
