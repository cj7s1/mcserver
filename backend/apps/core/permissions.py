"""DRF permission helpers backed by the panel permission matrix."""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import BasePermission

from apps.accounts.permissions import INSTANCE_CODES


def require(user: Any, code: str, instance: Any = None) -> None:
    if not user or not user.is_authenticated or not user.has_panel_perm(code, instance):
        raise PermissionDenied(f"You do not have the '{code}' permission" + (" for this server" if instance else ""))


def visible_instances(user: Any, qs: QuerySet | None = None) -> QuerySet:
    from apps.instances.models import Instance

    qs = qs if qs is not None else Instance.objects.all()
    if not user.is_authenticated or not user.is_active:
        return qs.none()
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


class HasGlobalPermission(BasePermission):
    """Use with ``required_permission = 'system.users'`` on the view."""

    def has_permission(self, request, view) -> bool:  # type: ignore[no-untyped-def]
        code = getattr(view, "required_permission", None)
        if not request.user or not request.user.is_authenticated:
            return False
        if code is None:
            return True
        if code in INSTANCE_CODES:
            return True  # checked per object
        return request.user.has_panel_perm(code)
