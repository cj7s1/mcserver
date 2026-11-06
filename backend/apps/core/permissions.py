"""DRF permission helpers backed by the panel permission matrix."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import BasePermission

from apps.accounts.permissions import INSTANCE_CODES

from .errors import ApiError

MUST_CHANGE_PASSWORD = "must_change_password"


class PasswordChangeRequired(ApiError):
    status_code = 403
    default_code = MUST_CHANGE_PASSWORD
    default_detail = "You must set a new password before you can continue."


def ensure_password_current(user: Any) -> None:
    """Refuse every request of an account flagged ``must_change_password`` (403 must_change_password).

    Only the account endpoints under ``/api/auth/`` (state, me, logout, password, ...) stay usable;
    they do not call this. Called by every permission helper below, so instance and system
    endpoints are covered even where a view does not use :class:`PasswordChangeEnforced`.
    """
    if user is not None and getattr(user, "is_authenticated", False) and getattr(user, "must_change_password", False):
        raise PasswordChangeRequired()


def require(user: Any, code: str, instance: Any = None) -> None:
    ensure_password_current(user)
    if not user or not user.is_authenticated or not user.has_panel_perm(code, instance):
        raise PermissionDenied(f"You do not have the '{code}' permission" + (" for this server" if instance else ""))


def visible_instances(user: Any, qs: QuerySet | None = None) -> QuerySet:
    from apps.instances.models import Instance

    qs = qs if qs is not None else Instance.objects.all()
    if not user.is_authenticated or not user.is_active:
        return qs.none()
    ensure_password_current(user)
    if "instance.view" in user.global_permissions():
        return qs
    return qs.filter(Q(access_grants__user=user, access_grants__role__permissions__contains=["instance.view"])).distinct()


def get_instance_for(user: Any, pk: Any, code: str = "instance.view") -> Any:
    """Fetch an instance the user may see (404 otherwise) and require ``code`` on it."""
    from apps.instances.models import Instance

    instance = visible_instances(user, Instance.objects.select_related("node")).filter(pk=pk).first()
    if instance is None:
        raise NotFound("Server not found")
    if code != "instance.view":
        require(user, code, instance)
    return instance


class PasswordChangeEnforced(BasePermission):
    """Authenticated users flagged ``must_change_password`` get 403 must_change_password.

    Meant for ``DEFAULT_PERMISSION_CLASSES`` (next to IsAuthenticated); anonymous requests
    pass through so the other permission classes decide about them.
    """

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        ensure_password_current(request.user)
        return True


class HasGlobalPermission(BasePermission):
    """Use with ``required_permission = 'system.users'`` on the view."""

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        code = getattr(view, "required_permission", None)
        if not request.user or not request.user.is_authenticated:
            return False
        ensure_password_current(request.user)
        if code is None:
            return True
        if code in INSTANCE_CODES:
            return True  # checked per object
        return request.user.has_panel_perm(code)
