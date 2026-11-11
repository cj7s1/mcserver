from __future__ import annotations

from django.db import models


class MetricSample(models.Model):
    """One resource/player sample of a running server (taken by the worker every minute)."""

    instance = models.ForeignKey("instances.Instance", on_delete=models.CASCADE, related_name="metric_samples")
    taken_at = models.DateTimeField(db_index=True)
    cpu_percent = models.FloatField(null=True)
    memory_bytes = models.BigIntegerField(null=True)
    memory_limit_bytes = models.BigIntegerField(null=True)
    network_rx_bytes = models.BigIntegerField(null=True)
    network_tx_bytes = models.BigIntegerField(null=True)
    players_online = models.IntegerField(null=True)
    players_max = models.IntegerField(null=True)

    class Meta:
        ordering = ["taken_at"]
        indexes = [models.Index(fields=["instance", "taken_at"])]


class PlayerSeen(models.Model):
    """Players observed online (via RCON `list`), aggregated per server."""

    instance = models.ForeignKey("instances.Instance", on_delete=models.CASCADE, related_name="players_seen")
    # Minecraft names have up to 16 characters, Factorio names up to 60
    name = models.CharField(max_length=64)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    minutes_online = models.PositiveIntegerField(default=0, help_text="Approximate, from 1-minute sampling")
    sessions = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-last_seen"]
        constraints = [models.UniqueConstraint(fields=["instance", "name"], name="unique_player_seen")]
