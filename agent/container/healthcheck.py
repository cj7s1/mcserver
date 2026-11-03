"""Container health check for the node agent (stdlib only).

``/v1/health`` is unauthenticated and answers 200 with ``status: "degraded"``
when the Docker daemon is unreachable. An agent without Docker cannot do any of
its work, so only ``status: "ok"`` counts as healthy. Environment proxies are
ignored so the loopback probe can never be sent to an egress proxy.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request


def _target() -> tuple[str, ssl.SSLContext | None]:
    port = int(os.environ.get("AGENT_LISTEN_PORT", "8700"))
    if os.environ.get("AGENT_TLS", "off").strip().lower() in ("", "off"):
        return f"http://127.0.0.1:{port}/v1/health", None
    # Loopback liveness probe of our own listener: no credentials are sent and nothing
    # but the status is read, so the certificate is not verified here.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return f"https://127.0.0.1:{port}/v1/health", ctx


def main() -> int:
    url, ctx = _target()
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=8) as response:
            body = json.loads(response.read(65536) or b"{}")
    except urllib.error.HTTPError as exc:
        print(f"unhealthy: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    if body.get("status") != "ok":
        print(f"unhealthy: {body}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
