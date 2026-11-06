"""Outbound notifications (Discord / generic webhook). Delivery runs in the worker.

Webhook URLs are entered by admins, but the worker sits on the panel's internal network, so a
URL must not turn it into a proxy for internal services (the root-equivalent node agent,
Postgres, Redis) or cloud metadata endpoints, nor into a port scanner whose error messages tell
what answered. ``check_destination`` resolves the host and refuses loopback, link-local
(169.254.0.0/16 incl. metadata, fe80::/10), unspecified/multicast/reserved addresses, known
metadata addresses, MCPanel's own services (agent/postgres/redis/... and every node agent's
address:port) and, unless MCPANEL_WEBHOOK_ALLOW_LAN is on (the default, because Home Assistant,
ntfy or Gotify on the LAN are the common targets), private/LAN ranges. The request then goes to
the checked IP address (no second DNS lookup: no DNS rebinding), redirects are never followed and
errors are reported generically.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.core.crypto import is_unreadable

from .models import EVENTS, NotificationChannel

log = logging.getLogger(__name__)
COLORS = {"failed": 0xE5484D, "crash": 0xE5484D, "succeeded": 0x30A46C, "running": 0x30A46C, "stopped": 0x8B8D98}

# Compose service names of the stack: never a webhook target, whatever they resolve to.
INTERNAL_SERVICE_NAMES = ("agent", "postgres", "redis", "panel", "worker", "beat")
BLOCKED_HOSTNAMES = {
    *INTERNAL_SERVICE_NAMES,
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
}
# Cloud metadata services outside the link-local range (AWS IPv6, Alibaba).
METADATA_ADDRESSES = {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
NOT_ALLOWED = "Destination not allowed (loopback, link-local, metadata and MCPanel's own services are blocked)"
LAN_NOT_ALLOWED = "Destination not allowed (private/LAN addresses are disabled by MCPANEL_WEBHOOK_ALLOW_LAN)"
DELIVERY_FAILED = "Delivery failed (connection error or timeout)"
UNREADABLE_URL = "The webhook URL cannot be decrypted (the panel's secret key changed); enter it again"

# indirection for tests
_getaddrinfo = socket.getaddrinfo
_INTERNAL_TTL = 60.0
_internal_cache: tuple[float, frozenset[Any], frozenset[tuple[Any, int]]] | None = None


class BlockedDestination(ValueError):
    """The webhook target is not allowed (message is safe to show)."""


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str  # ASCII host name or literal IP, without brackets
    port: int
    address: ipaddress.IPv4Address | ipaddress.IPv6Address


def _host_blocked(host: str) -> bool:
    name = host.rstrip(".").lower()
    return name in BLOCKED_HOSTNAMES or name.endswith(".localhost")


def _embedded_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def address_problem(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_lan: bool | None = None) -> str | None:
    """Why ``ip`` must not be a webhook target, or None."""
    if allow_lan is None:
        allow_lan = settings.MCPANEL_WEBHOOK_ALLOW_LAN
    ip = _embedded_ipv4(ip)
    if (
        ip in METADATA_ADDRESSES
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local)
        or (isinstance(ip, ipaddress.IPv4Address) and ip in _THIS_NETWORK)
    ):
        return NOT_ALLOWED
    if ip.is_global:
        return None
    return None if allow_lan else LAN_NOT_ALLOWED


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if ip not in out:
            out.append(ip)
    return out


def _url_endpoint(url: str, default_port: int | None = None) -> tuple[str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    return parts.hostname, port or default_port or (443 if parts.scheme in ("https", "wss", "rediss") else 80)


def _internal_targets() -> tuple[frozenset[Any], frozenset[tuple[Any, int]]]:
    """Addresses of MCPanel's own services (whole IPs) and agent/database endpoints (IP, port)."""
    global _internal_cache
    now = time.monotonic()
    if _internal_cache is not None and now - _internal_cache[0] < _INTERNAL_TTL:
        return _internal_cache[1], _internal_cache[2]
    hosts: set[Any] = set()
    for name in INTERNAL_SERVICE_NAMES:
        hosts.update(_resolve(name, 80))
    endpoints: set[tuple[str, int]] = set()
    for url, port in (
        (settings.MCPANEL_AGENT_URL, None),
        (getattr(settings, "REDIS_URL", ""), 6379),
        (getattr(settings, "CELERY_BROKER_URL", "") or "", 6379),
    ):
        endpoint = _url_endpoint(url or "", port)
        if endpoint:
            endpoints.add(endpoint)
    db = settings.DATABASES.get("default", {})
    if db.get("HOST"):
        endpoints.add((str(db["HOST"]), int(db.get("PORT") or 5432)))
    from apps.nodes.models import Node

    for agent_url in Node.objects.values_list("agent_url", flat=True):
        endpoint = _url_endpoint(agent_url)
        if endpoint:
            endpoints.add(endpoint)
    pinned = frozenset((ip, port) for host, port in endpoints for ip in _resolve(host, port))
    _internal_cache = (now, frozenset(hosts), pinned)
    return frozenset(hosts), pinned


def check_destination(url: str) -> Destination:
    """Resolve and vet a webhook URL; raises BlockedDestination with a generic message."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise BlockedDestination("Invalid webhook URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.raw_host:
        raise BlockedDestination("Invalid webhook URL")
    host = parsed.raw_host.decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if _host_blocked(host):
        raise BlockedDestination(NOT_ALLOWED)
    addresses = _resolve(host, port)
    if not addresses:
        raise BlockedDestination("Could not resolve the webhook host")
    internal_hosts, internal_endpoints = _internal_targets()
    for ip in addresses:
        problem = address_problem(ip)
        if problem:
            raise BlockedDestination(problem)
        if ip in internal_hosts or (ip, port) in internal_endpoints:
            raise BlockedDestination(NOT_ALLOWED)
    return Destination(parsed.scheme, host, port, addresses[0])


def validate_url(kind: str, url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError("Enter a full http(s) URL")
    if kind == "discord":
        if parsed.scheme != "https" or parsed.hostname not in (
            "discord.com",
            "discordapp.com",
            "ptb.discord.com",
            "canary.discord.com",
        ):
            raise ValueError("Discord webhooks look like https://discord.com/api/webhooks/...")
        if not parsed.path.startswith("/api/webhooks/"):
            raise ValueError("Discord webhooks look like https://discord.com/api/webhooks/...")
    # Static checks only (no DNS lookup here); delivery re-checks the resolved address.
    if _host_blocked(parsed.hostname):
        raise ValueError(NOT_ALLOWED)
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal = None
    if literal is not None and (problem := address_problem(literal)):
        raise ValueError(problem)
    return url.strip()


def _payload(channel: NotificationChannel, event: str, title: str, message: str, data: dict[str, Any]) -> dict[str, Any]:
    if channel.kind == "discord":
        color = next((c for k, c in COLORS.items() if k in event), 0x5B5BD6)
        fields = [
            {"name": k.replace("_", " ").title(), "value": str(v)[:1000], "inline": True}
            for k, v in data.items()
            if v not in (None, "") and k in ("server", "job", "status", "detail", "error")
        ]
        return {
            "username": "MCPanel",
            "embeds": [
                {
                    "title": title[:250],
                    "description": message[:3500],
                    "color": color,
                    "fields": fields[:10],
                    "timestamp": timezone.now().isoformat(),
                }
            ],
        }
    return {"event": event, "title": title, "message": message, "data": data, "sent_at": timezone.now().isoformat()}


def _env_proxy(scheme: str, host: str) -> str | None:
    """The egress proxy httpx would use for this URL (HTTPS_PROXY & co.), if any."""
    proxies = urllib.request.getproxies()
    proxy = proxies.get(scheme) or proxies.get("all")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    return proxy


def _client(*, direct: bool) -> httpx.Client:
    if direct:
        # Straight to the checked address: no environment proxy (its NO_PROXY rules would see the
        # IP, not the host name), but the CA settings (SSL_CERT_FILE/SSL_CERT_DIR) still apply.
        return httpx.Client(timeout=10, follow_redirects=False, trust_env=False, verify=httpx.create_ssl_context(trust_env=True))
    return httpx.Client(timeout=10, follow_redirects=False)


def post_webhook(url: str, payload: dict[str, Any]) -> httpx.Response:
    """POST ``payload`` to a vetted webhook URL, connecting to the address that was checked."""
    dest = check_destination(url)
    headers = {"User-Agent": settings.MCPANEL_USER_AGENT}
    if _env_proxy(dest.scheme, dest.host):
        # An egress proxy connects (and resolves) for us; the check above is what we can do.
        with _client(direct=False) as client:
            return client.post(url, json=payload, headers=headers)
    parsed = httpx.URL(url)
    headers["Host"] = parsed.netloc.decode("ascii")
    extensions = {"sni_hostname": dest.host} if dest.scheme == "https" else {}
    with _client(direct=True) as client:
        return client.post(parsed.copy_with(host=str(dest.address)), json=payload, headers=headers, extensions=extensions)


@shared_task(
    name="apps.notifications.deliver",
    ignore_result=True,
    autoretry_for=(httpx.TransportError,),
    retry_backoff=True,
    max_retries=3,
)
def deliver(channel_id: int, event: str, title: str, message: str, data: dict[str, Any]) -> None:
    channel = NotificationChannel.objects.filter(pk=channel_id, enabled=True).first()
    if channel is None:
        return
    if is_unreadable(channel.url) or not channel.url:
        NotificationChannel.objects.filter(pk=channel_id).update(last_error=UNREADABLE_URL)
        return
    try:
        resp = post_webhook(channel.url, _payload(channel, event, title, message, data))
    except BlockedDestination as exc:
        NotificationChannel.objects.filter(pk=channel_id).update(last_error=str(exc)[:500])
        return
    except httpx.HTTPError:
        # no exception text: it would say what (if anything) listens at the address
        NotificationChannel.objects.filter(pk=channel_id).update(last_error=DELIVERY_FAILED)
        raise
    if resp.is_redirect:
        error = f"HTTP {resp.status_code} (redirects are not followed)"
    else:
        error = "" if resp.status_code < 400 else f"HTTP {resp.status_code}"
    NotificationChannel.objects.filter(pk=channel_id).update(last_sent_at=timezone.now(), last_error=error[:500])


def notify(event: str, title: str, message: str, *, instance: Any = None, data: dict[str, Any] | None = None) -> int:
    """Queue a notification to every matching channel. Never raises."""
    if event not in EVENTS:
        return 0
    try:
        qs = NotificationChannel.objects.filter(enabled=True)
        if instance is not None:
            qs = qs.filter(instance__isnull=True) | qs.filter(instance=instance)
        else:
            qs = qs.filter(instance__isnull=True)
        payload = dict(data or {})
        if instance is not None:
            payload.setdefault("server", instance.name)
        count = 0
        for ch in qs:
            if event in (ch.events or []):
                deliver.delay(ch.pk, event, title, message, payload)
                count += 1
        return count
    except Exception as exc:  # notifications must never break the caller
        log.warning("notify(%s) failed: %s", event, exc)
        return 0
