from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterable
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

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
        constraints = [
            # At most one owner, enforced by the database (closes the concurrent first-run setup race).
            models.UniqueConstraint(fields=["is_owner"], condition=models.Q(is_owner=True), name="single_owner"),
        ]

    # -- permission evaluation -------------------------------------------------
    @property
    def is_root(self) -> bool:
        """The owner, or a Django superuser (``manage.py createsuperuser``): every permission.

        The user-management guards already treated superusers as root (apps/accounts/views.py
        ``_is_root``); evaluating their permissions like any other account gave them an empty panel
        that no administrator could manage. Superusers can only be made on the command line.
        """
        return bool(self.is_owner or self.is_superuser)

    def global_permissions(self) -> frozenset[str]:
        if not self.is_active:
            return frozenset()
        if self.is_root:
            return ALL_CODES
        return self.role.permission_set if self.role_id else frozenset()

    def instance_permissions(self, instance_id, grants: Iterable[InstanceAccess] | None = None) -> frozenset[str]:  # type: ignore[no-untyped-def]
        """Instance-level codes on one instance. ``grants`` are this user's InstanceAccess rows for
        that instance (with ``role`` loaded) when the caller prefetched them (list views)."""
        if not self.is_active:
            return frozenset()
        perms = set(self.global_permissions() & INSTANCE_CODES)
        if self.is_root:
            return INSTANCE_CODES
        if grants is None:
            grants = InstanceAccess.objects.filter(user=self, instance_id=instance_id).select_related("role")
        for grant in grants:
            if grant.user_id == self.pk:
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
    """One-time password reset token. Only the SHA-256 digest is stored.

    A token is bound to the password the account had when it was issued
    (``password_fingerprint``): any later password change (by the user, a reset or the CLI)
    invalidates every outstanding token without having to find and revoke them.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    password_fingerprint = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    @staticmethod
    def digest(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def fingerprint_for(user: User) -> str:
        return salted_hmac("mcpanel.password-reset", user.password or "", algorithm="sha256").hexdigest()

    @classmethod
    def issue(cls, user: User, created_by: User | None = None, hours: int = 24) -> tuple[PasswordResetToken, str]:
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            token_hash=cls.digest(raw),
            password_fingerprint=cls.fingerprint_for(user),
            expires_at=timezone.now() + timedelta(hours=hours),
            created_by=created_by,
        )
        return token, raw

    @property
    def is_valid(self) -> bool:
        return (
            self.used_at is None
            and self.expires_at > timezone.now()
            and self.user.is_active
            and bool(self.password_fingerprint)  # tokens issued before the binding existed are void
            and constant_time_compare(self.password_fingerprint, self.fingerprint_for(self.user))
        )
