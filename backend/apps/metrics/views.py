from __future__ import annotations

from datetime import timedelta

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import ApiError
from apps.core.permissions import get_instance_for

from .models import MetricSample, PlayerSeen

RANGES = {
    "1h": (timedelta(hours=1), 1),
    "6h": (timedelta(hours=6), 5),
    "24h": (timedelta(hours=24), 10),
    "7d": (timedelta(days=7), 60),
}


class MetricsView(APIView):
    """Stored samples, downsampled to fixed buckets (minutes) for the requested range."""

    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        from django.utils import timezone

        instance = get_instance_for(request.user, pk, "instance.view")
        rng = request.query_params.get("range", "24h")
        if rng not in RANGES:
            raise ApiError(f"range must be one of {', '.join(RANGES)}", code="invalid_range")
        span, bucket_minutes = RANGES[rng]
        since = timezone.now() - span
        samples = MetricSample.objects.filter(instance=instance, taken_at__gte=since).values(
            "taken_at",
            "cpu_percent",
            "memory_bytes",
            "memory_limit_bytes",
            "network_rx_bytes",
            "network_tx_bytes",
            "players_online",
        )
        buckets: dict[int, list[dict]] = {}
        for s in samples:
            key = int(s["taken_at"].timestamp() // (bucket_minutes * 60))
            buckets.setdefault(key, []).append(s)
        points = []
        prev = None
        for key in sorted(buckets):
            group = buckets[key]

            def avg(field: str, group: list[dict] = group) -> float | None:
                vals = [g[field] for g in group if g[field] is not None]
                return round(sum(vals) / len(vals), 2) if vals else None

            last = group[-1]
            rx_rate = tx_rate = None
            if prev and last["network_rx_bytes"] is not None and prev["network_rx_bytes"] is not None:
                dt = (last["taken_at"] - prev["taken_at"]).total_seconds() or 1
                rx_rate = max(0.0, (last["network_rx_bytes"] - prev["network_rx_bytes"]) / dt)
                tx_rate = max(0.0, (last["network_tx_bytes"] - prev["network_tx_bytes"]) / dt)
            prev = last
            points.append(
                {
                    "t": key * bucket_minutes * 60,
                    "cpu_percent": avg("cpu_percent"),
                    "memory_bytes": avg("memory_bytes"),
                    "memory_limit_bytes": last["memory_limit_bytes"],
                    "players_online": max((g["players_online"] for g in group if g["players_online"] is not None), default=None),
                    "network_rx_bps": rx_rate,
                    "network_tx_bps": tx_rate,
                }
            )
        return Response(
            {
                "range": rng,
                "bucket_minutes": bucket_minutes,
                "points": points,
                "note": "Sampled every minute while the server runs; gaps mean the server was offline.",
            }
        )


class PlayerHistoryView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        rows = PlayerSeen.objects.filter(instance=instance)[:500]
        return Response(
            {
                "players": [
                    {
                        "name": r.name,
                        "first_seen": r.first_seen,
                        "last_seen": r.last_seen,
                        "minutes_online": r.minutes_online,
                        "sessions": r.sessions,
                    }
                    for r in rows
                ],
                "note": "Approximate: based on the online player list sampled every minute.",
            }
        )
