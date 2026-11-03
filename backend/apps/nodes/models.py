from __future__ import annotations

from django.db import models

from apps.core.crypto import EncryptedTextField


class Node(models.Model):
    """A Docker host managed through an MCPanel agent.

    The first release targets a single host; the default node is created from
    MCPANEL_AGENT_URL / MCPANEL_AGENT_TOKEN. The model exists so additional
    remote agents can be added later without schema redesign.
    """

    name = models.CharField(max_length=100, unique=True)
    agent_url = models.URLField(max_length=300)
    agent_token = EncryptedTextField(blank=True, default="")
    use_settings_token = models.BooleanField(default=True, help_text="Use MCPANEL_AGENT_TOKEN instead of agent_token")
    is_default = models.BooleanField(default=False)
    public_address = models.CharField(max_length=255, blank=True, help_text="Hostname/IP players use to connect")
    lan_address = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    info = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def token(self) -> str:
        from django.conf import settings

        return settings.MCPANEL_AGENT_TOKEN if self.use_settings_token else self.agent_token

    @classmethod
    def default(cls) -> Node:
        from django.conf import settings

        node = cls.objects.filter(is_default=True).first()
        if node is None:
            node, _ = cls.objects.get_or_create(
                name="local",
                defaults={
                    "agent_url": settings.MCPANEL_AGENT_URL,
                    "is_default": True,
                    "use_settings_token": True,
                    "lan_address": settings.MCPANEL_LAN_ADDRESS or "",
                    "public_address": settings.MCPANEL_PUBLIC_HOSTNAME or "",
                },
            )
        elif node.use_settings_token and node.agent_url != settings.MCPANEL_AGENT_URL and node.name == "local":
            node.agent_url = settings.MCPANEL_AGENT_URL
            node.save(update_fields=["agent_url"])
        return node
