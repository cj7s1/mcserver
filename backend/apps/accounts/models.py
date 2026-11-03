from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from .permissions import ALL_CODES, INSTANCE_CODES


class Role(models.Model):
    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    permissions = models.JSONField(default=list)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def permission_set(self) -> frozenset[str]:
        return frozenset(self.permissions) & ALL_CODES


class User(AbstractUser):
    """Panel user. Minecraft players are *not* panel users."""

    is_owner = models.BooleanField(default=False)
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="users")
    must_change_password = models.BooleanField(default=False)
    preferences = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["username"]

    # -- permission evaluation -------------------------------------------------
    def global_permissions(self) -> frozenset[str]:
        if not self.is_active:
            return frozenset()
        if self.is_owner:
            return ALL_CODES
        return self.role.permission_set if self.role_id else frozenset()

    def instance_permissions(self, instance_id) -> frozenset[str]:  # type: ignore[no-untyped-def]
        if not self.is_active:
            return frozenset()
        perms = set(self.global_permissions() & INSTANCE_CODES)
        if self.is_owner:
            return INSTANCE_CODES
        grants = InstanceAccess.objects.filter(user=self, instance_id=instance_id).select_related("role")
        for grant in grants:
            perms |= grant.role.permission_set & INSTANCE_CODES
        return frozenset(perms)

    def has_panel_perm(self, code: str, instance=None) -> bool:  # type: ignore[no-untyped-def]
        if code in INSTANCE_CODES and instance is not None:
            instance_id = getattr(instance, "pk", instance)
            return code in self.instance_permissions(instance_id)
        return code in self.global_permissions()


class InstanceAccess(models.Model):
    """Grants a role's instance-level permissions on a single instance."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="instance_access")
    instance = models.ForeignKey("instances.Instance", on_delete=models.CASCADE, related_name="access_grants")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="instance_grants")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "instance"], name="unique_instance_access")]


class PasswordResetToken(models.Model):
    """One-time password reset token. Only the SHA-256 digest is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    @staticmethod
    def digest(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user: User, created_by: User | None = None, hours: int = 24) -> tuple[PasswordResetToken, str]:
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            token_hash=cls.digest(raw),
            expires_at=timezone.now() + timedelta(hours=hours),
            created_by=created_by,
        )
        return token, raw

    @property
    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now() and self.user.is_active
