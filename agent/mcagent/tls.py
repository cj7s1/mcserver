"""HTTPS for the agent API, used when the panel reaches this agent over a network.

``AGENT_TLS`` selects the mode:

``off``   (default) plain HTTP. Right for the single-host stack, where the panel talks to the
          agent over the private Compose network.
``auto``  HTTPS with a self-signed certificate generated on first start and kept in
          ``<data_root>/agent/tls/``. The private key never leaves this host. The panel pins the
          certificate by its SHA-256 fingerprint, which ``python -m mcagent fingerprint`` prints
          (it is also logged at startup).
``files`` HTTPS with ``AGENT_TLS_CERT_FILE`` / ``AGENT_TLS_KEY_FILE`` (for example a certificate
          from your own CA). The panel then verifies it against its CA bundle, or pins it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import os
import socket
import ssl
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

CERT_NAME = "agent.crt"
KEY_NAME = "agent.key"
VALID_DAYS = 3650


class TlsConfigError(RuntimeError):
    pass


def tls_dir(settings: Settings) -> Path:
    return settings.state_dir / "tls"


def _san_entries(hostnames: list[str]) -> list[object]:
    from cryptography import x509

    names: list[object] = []
    seen: set[str] = set()
    for raw in [*hostnames, socket.gethostname(), "localhost", "127.0.0.1", "::1"]:
        value = raw.strip()
        if not value or value.lower() in seen:
            continue
        seen.add(value.lower())
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(value)))
        except ValueError:
            names.append(x509.DNSName(value))
    return names


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` with mode 0600 from the first byte (atomic replace)."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def generate_self_signed(directory: Path, hostnames: list[str]) -> tuple[Path, Path]:
    """Create a new key pair and self-signed certificate in ``directory``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "mcpanel-agent"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MCPanel node agent"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(hostnames)), critical=False)  # type: ignore[arg-type]
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = directory / KEY_NAME, directory / CERT_NAME
    _write_private(
        key_path,
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )
    _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)
    return cert_path, key_path


def fingerprint_pem(pem: str | bytes) -> str:
    """SHA-256 fingerprint of a PEM certificate as colon-separated upper-case hex."""
    text = pem.decode() if isinstance(pem, bytes) else pem
    der = ssl.PEM_cert_to_DER_cert(text)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def cert_paths(settings: Settings, *, create: bool = True) -> tuple[Path, Path] | None:
    """Return (cert, key) for the configured mode, generating them in ``auto`` mode."""
    mode = settings.tls
    if mode == "off":
        return None
    if mode == "files":
        if not settings.tls_cert_file or not settings.tls_key_file:
            raise TlsConfigError("AGENT_TLS=files needs AGENT_TLS_CERT_FILE and AGENT_TLS_KEY_FILE")
        for p in (settings.tls_cert_file, settings.tls_key_file):
            if not p.is_file():
                raise TlsConfigError(f"TLS file not found: {p}")
        return settings.tls_cert_file, settings.tls_key_file
    directory = tls_dir(settings)
    cert, key = directory / CERT_NAME, directory / KEY_NAME
    if cert.is_file() and key.is_file():
        return cert, key
    if not create:
        return None
    log.warning("Generating a self-signed TLS certificate in %s", directory)
    return generate_self_signed(directory, settings.tls_hostnames)


def server_fingerprint(settings: Settings) -> str | None:
    paths = cert_paths(settings, create=False)
    if not paths:
        return None
    return fingerprint_pem(paths[0].read_text())


def uvicorn_ssl_kwargs(settings: Settings) -> dict[str, str]:
    paths = cert_paths(settings)
    if not paths:
        return {}
    cert, key = paths
    # Fail at startup (not on the first connection) if the pair does not load.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(str(cert), str(key))
    except (ssl.SSLError, OSError) as exc:
        raise TlsConfigError(f"Cannot load the TLS certificate/key ({cert}, {key}): {exc}") from exc
    return {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
