"""DRF permission helpers backed by the panel permission matrix."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import BasePermission

from apps.accounts.permissions import INSTANCE_CODES, with_implied

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


def visible_instances(user: Any, qs: QuerySet | None = None, code: str = "instance.view") -> QuerySet:
    """The servers ``user`` holds ``code`` on: "instance.view" (the default), or "instance.overview" (also the
    servers granted to a Friend, who sees their status but not their configuration)."""
    from apps.instances.models import Instance

    qs = qs if qs is not None else Instance.objects.all()
    if not user.is_authenticated or not user.is_active:
        return qs.none()
    ensure_password_current(user)
    if code in with_implied(user.global_permissions()):
        return qs
    holds = Q(access_grants__role__permissions__contains=["instance.view"])
    if code == "instance.overview":
        holds |= Q(access_grants__role__permissions__contains=["instance.overview"])
    return qs.filter(Q(access_grants__user=user) & holds).distinct()


def sees_more_than_status(user: Any) -> bool:
    """Whether ``user`` sees more than the status of a few servers (a Friend does not): the owner, any global
    permission beyond start/stop, or "instance.view" on at least one server. The global answers that describe the
    host (system info, ports, nodes, roles) are for these accounts only (review F-11)."""
    from apps.accounts.models import InstanceAccess
    from apps.accounts.permissions import LIMITED_CODES

    if not user or not user.is_authenticated or not user.is_active:
        return False
    if getattr(user, "is_root", False):
        return True
    if user.global_permissions() - LIMITED_CODES - {"instance.control"}:
        return True
    return InstanceAccess.objects.filter(user=user, role__permissions__contains=["instance.view"]).exists()


class SeesMoreThanStatus(BasePermission):
    """DRF permission: ``sees_more_than_status`` (403 for a Friend)."""

    message = "Your account only sees the status of the servers shared with you"

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        ensure_password_current(request.user)
        return sees_more_than_status(request.user)


def not_for_friends() -> list[type[BasePermission]]:
    """The default permission classes plus SeesMoreThanStatus: for the global answers a Friend has no use for."""
    from rest_framework.permissions import IsAuthenticated

    return [IsAuthenticated, PasswordChangeEnforced, SeesMoreThanStatus]


def require_more_than_status(user: Any) -> None:
    ensure_password_current(user)
    if not sees_more_than_status(user):
        raise PermissionDenied(SeesMoreThanStatus.message)


def get_instance_for(user: Any, pk: Any, code: str = "instance.view") -> Any:
    """Fetch an instance the user may see (404 otherwise) and require ``code`` on it."""
    from apps.instances.models import Instance

    from .utils import parse_uuid

    if parse_uuid(pk) is None:  # a malformed id from a query string or body: not found (D-12; was a 500)
        raise NotFound("Server not found")
    instance = visible_instances(user, Instance.objects.select_related("node"), "instance.overview").filter(pk=pk).first()
    if instance is None:
        raise NotFound("Server not found")
    # (a Friend finds the server but holds only "instance.overview" and "instance.control" on it)
    if code != "instance.overview" and not (code == "instance.view" and code in user.global_permissions()):
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
