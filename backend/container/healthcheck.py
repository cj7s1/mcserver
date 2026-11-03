"""Container health check for the panel web process (stdlib only).

Requests ``/api/health`` (database + cache check) on the local uvicorn listener.
Django validates the Host header against ``MCPANEL_ALLOWED_HOSTS``, so the probe
sends a host name taken from that list instead of ``127.0.0.1``, which a
production configuration may not allow. Proxies from the environment are ignored
so an egress proxy setting can never capture the loopback probe.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8000/api/health"


def probe_host() -> str:
    hosts = [h.strip() for h in os.environ.get("MCPANEL_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return "localhost"  # settings.py default: localhost,127.0.0.1
    if "localhost" in hosts or "*" in hosts:
        return "localhost"
    if "127.0.0.1" in hosts:
        return "127.0.0.1"
    # A leading dot (".example.com") also matches the bare domain.
    return hosts[0].lstrip(".") or "localhost"


def main() -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(URL, headers={"Host": probe_host(), "Accept": "application/json"})
    try:
        with opener.open(request, timeout=8) as response:
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
