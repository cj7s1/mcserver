"""Container health check for the node agent (stdlib only).

``/v1/health`` is unauthenticated and answers 200 with ``status: "degraded"``
when the Docker daemon is unreachable. An agent without Docker cannot do any of
its work, so only ``status: "ok"`` counts as healthy. Environment proxies are
ignored so the loopback probe can never be sent to an egress proxy.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8700/v1/health"


def main() -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(URL, timeout=8) as response:
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
