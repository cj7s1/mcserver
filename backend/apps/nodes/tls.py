"""TLS trust for node agents.

An agent reached over ``https://`` is trusted in one of two ways:

* **Pinned** (``Node.tls_fingerprint`` set): the panel trusts exactly the certificate whose
  SHA-256 fingerprint the administrator entered (printed by ``python -m mcagent fingerprint``
  on the node). The certificate is read once with a bare TLS handshake (no request, no
  token), checked against the fingerprint and stored in ``Node.tls_cert_pem``; afterwards
  every connection requires the agent to present that certificate (host names are not
  checked: the pin replaces them). This is how self-signed agents (``AGENT_TLS=auto``) work.
* **CA-verified** (no fingerprint): normal certificate and host name verification against
  certifi's bundle plus ``MCPANEL_AGENT_CA_BUNDLE`` (for a private CA).

The bearer token is only ever sent after the handshake has verified the agent.
"""

from __future__ import annotations

import hashlib
import re
import socket
import ssl
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from django.conf import settings

from apps.core.errors import ServiceUnavailable

if TYPE_CHECKING:
    from .models import Node

_HEX = re.compile(r"^[0-9a-f]{64}$")
_lock = threading.Lock()
_contexts: dict[str, ssl.SSLContext] = {}


class AgentTlsError(ServiceUnavailable):
    default_code = "agent_tls"
    default_detail = "The node agent's TLS certificate could not be verified."


def normalize_fingerprint(value: str) -> str:
    """Accept ``AB:CD:…``, ``ab cd …``, ``SHA256 Fingerprint=AB:…`` or bare hex; return lower hex."""
    text = value.strip()
    if "=" in text:
        text = text.split("=", 1)[1]
    if text.lower().startswith("sha256:"):
        text = text[7:]
    hexed = re.sub(r"[\s:]", "", text).lower()
    if not _HEX.match(hexed):
        raise ValueError("Enter the SHA-256 fingerprint: 64 hex digits, optionally separated by colons")
    return hexed


def format_fingerprint(hexed: str) -> str:
    up = hexed.upper()
    return ":".join(up[i : i + 2] for i in range(0, len(up), 2))


def fingerprint_of_pem(pem: str) -> str:
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def _host_port(url: str) -> tuple[str, int]:
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Certificate pinning needs an https:// agent URL")
    return p.hostname, p.port or 443


def fetch_certificate(url: str, timeout: float = 10.0) -> str:
    """Read the certificate the agent presents (handshake only: nothing is sent)."""
    host, port = _host_port(url)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we only read the certificate; it is compared to the pin below
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            server_hostname = None if _is_ip(host) else host
            with ctx.wrap_socket(raw, server_hostname=server_hostname) as tls_sock:
                der = tls_sock.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise AgentTlsError(f"Cannot read the TLS certificate of {host}:{port}: {exc}", code="agent_unreachable") from exc
    if not der:
        raise AgentTlsError(f"{host}:{port} presented no certificate")
    return ssl.DER_cert_to_PEM_cert(der)


def _is_ip(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def pin(url: str, fingerprint: str, timeout: float = 10.0) -> str:
    """Fetch the agent certificate and return its PEM if it matches ``fingerprint``."""
    pem = fetch_certificate(url, timeout=timeout)
    presented = fingerprint_of_pem(pem)
    if presented != fingerprint:
        raise AgentTlsError(
            "The certificate presented by the agent does not match the fingerprint. "
            f"Presented: {format_fingerprint(presented)}. Compare it with the output of "
            "`python -m mcagent fingerprint` on that node before trusting it.",
            code="agent_tls_mismatch",
            details={"presented": format_fingerprint(presented)},
        )
    return pem


def _pinned_context(pem: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False  # the pinned certificate replaces name checks
    ctx.verify_mode = ssl.CERT_REQUIRED
    # trust the pinned certificate itself as an anchor, even if it is a leaf from a private CA
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_verify_locations(cadata=pem)
    return ctx


def _ca_context() -> ssl.SSLContext:
    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    extra = getattr(settings, "MCPANEL_AGENT_CA_BUNDLE", "")
    if extra:
        ctx.load_verify_locations(cafile=extra)
    return ctx


def _cached(key: str, factory) -> ssl.SSLContext:  # type: ignore[no-untyped-def]
    with _lock:
        ctx = _contexts.get(key)
        if ctx is None:
            ctx = _contexts[key] = factory()
        return ctx


def transport(node: Node) -> str:
    if not node.agent_url.startswith("https://"):
        return "plain"
    return "tls_pinned" if node.tls_fingerprint else "tls_ca"


def ensure_pinned(node: Node) -> None:
    """Store the agent certificate for a pinned node if it is missing or stale."""
    if transport(node) != "tls_pinned":
        return
    if node.tls_cert_pem and fingerprint_of_pem(node.tls_cert_pem) == node.tls_fingerprint:
        return
    node.tls_cert_pem = pin(node.agent_url, node.tls_fingerprint)
    if node.pk:
        type(node).objects.filter(pk=node.pk).update(tls_cert_pem=node.tls_cert_pem)


def ssl_context(node: Node) -> ssl.SSLContext | None:
    """The context to connect to ``node`` with, or ``None`` for plain HTTP."""
    mode = transport(node)
    if mode == "plain":
        return None
    if mode == "tls_ca":
        return _cached("ca:" + getattr(settings, "MCPANEL_AGENT_CA_BUNDLE", ""), _ca_context)
    ensure_pinned(node)
    pem = node.tls_cert_pem
    return _cached("pin:" + hashlib.sha256(pem.encode()).hexdigest(), lambda: _pinned_context(pem))


def is_verification_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cur):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def describe_certificate(pem: str) -> dict[str, object]:
    """Subject, validity and names of a certificate (for display only)."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(pem.encode())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + [
            str(v) for v in san.get_values_for_type(x509.IPAddress)
        ]
    except x509.ExtensionNotFound:
        names = []
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "self_signed": cert.subject == cert.issuer,
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "names": names,
    }
