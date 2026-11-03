"""Entrypoint: ``python -m mcagent [serve|fingerprint]``.

``serve`` (default) starts the API with uvicorn, over HTTPS when ``AGENT_TLS`` is ``auto`` or
``files``. ``fingerprint`` prints the SHA-256 fingerprint of the agent's certificate (creating
the self-signed certificate in ``auto`` mode) so it can be pasted into the panel's node form.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from . import tls
from .config import get_settings


def _fingerprint() -> int:
    settings = get_settings()
    if settings.tls == "off":
        print(
            "TLS is off (set AGENT_TLS=auto to serve HTTPS with a self-signed certificate)", file=sys.stderr
        )
        return 1
    try:
        paths = tls.cert_paths(settings)
    except tls.TlsConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    assert paths is not None
    print(tls.fingerprint_pem(paths[0].read_text()))
    return 0


def _serve() -> int:
    import uvicorn

    level = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    try:
        ssl_kwargs = tls.uvicorn_ssl_kwargs(settings)
    except tls.TlsConfigError as exc:
        logging.getLogger("mcagent").error("%s", exc)
        return 2
    if ssl_kwargs:
        fp = tls.fingerprint_pem(Path(ssl_kwargs["ssl_certfile"]).read_text())
        logging.getLogger("mcagent").warning("Serving HTTPS. Certificate SHA-256 fingerprint: %s", fp)
    uvicorn.run(
        "mcagent.main:app",
        host=settings.listen_host,
        port=settings.listen_port,
        proxy_headers=False,
        server_header=False,
        log_level=level.lower(),
        **ssl_kwargs,  # type: ignore[arg-type]
    )
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "serve"
    if command == "serve":
        return _serve()
    if command == "fingerprint":
        return _fingerprint()
    print("usage: python -m mcagent [serve|fingerprint]", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
