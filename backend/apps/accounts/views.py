from __future__ import annotations

import logging
import os

from django.conf import settings
from django.contrib.auth import authenticate, login, logout, password_validation, update_session_auth_hash
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts import throttle as account_throttle
from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import HasGlobalPermission, PasswordChangeEnforced

from .models import InstanceAccess, PasswordResetToken, Role, User
from .permissions import ALL_CODES, INSTANCE_CODES, OWNER_GRANTED_CODES, PERMISSIONS, SYSTEM_ROLES
from .serializers import (
    LoginSerializer,
    MeSerializer,
    PasswordChangeSerializer,
    PasswordResetSerializer,
    RoleSerializer,
    SetupSerializer,
    UserCreateSerializer,
    UserSerializer,
)

log = logging.getLogger(__name__)


def ensure_system_roles() -> None:
    for key, spec in SYSTEM_ROLES.items():
        Role.objects.update_or_create(
            key=key,
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "permissions": spec["permissions"],
                "is_system": True,
            },
        )


def setup_token() -> str:
    """The optional first-run setup token (MCPANEL_SETUP_TOKEN or MCPANEL_SETUP_TOKEN_FILE); '' = none."""
    value = getattr(settings, "MCPANEL_SETUP_TOKEN", None)
    if value is None:
        value = os.environ.get("MCPANEL_SETUP_TOKEN", "")
        path = os.environ.get("MCPANEL_SETUP_TOKEN_FILE", "")
        if not value and path:
            try:
                with open(path, encoding="utf-8") as fh:
                    value = fh.read()
            except OSError:
                log.error("MCPANEL_SETUP_TOKEN_FILE cannot be read; first-run setup stays locked")
                return "\x00unreadable"  # never matches: a broken token file must not open setup
    return str(value or "").strip()


_open_setup_warned = False


def _warn_open_setup() -> None:
    """Log once per process that the owner account can be claimed by anyone who reaches the panel."""
    global _open_setup_warned
    if not _open_setup_warned:
        _open_setup_warned = True
        log.warning(
            "First-run setup is open: whoever reaches the panel first can create the owner account. "
            "Finish setup now, or set MCPANEL_SETUP_TOKEN to require a token for it."
        )


class AuthStateView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: dict})
    def get(self, request):  # type: ignore[no-untyped-def]
        get_token(request)  # ensure the CSRF cookie is set for the SPA
        setup_required = not User.objects.exists()
        data = {"setup_required": setup_required, "authenticated": request.user.is_authenticated, "user": None}
        if setup_required:
            if setup_token():
                data["setup_token_required"] = True
            else:
                _warn_open_setup()
        if request.user.is_authenticated:
            data["user"] = MeSerializer(request.user).data
        return Response(data)


SETUP_LOCK_KEY = 0x4D43_5345_5455_5000  # arbitrary, stable advisory-lock id for first-run setup


@method_decorator(csrf_protect, name="dispatch")
class SetupView(APIView):
    """Create the owner account. Only possible while no users exist."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "setup"

    @extend_schema(request=SetupSerializer, responses={201: MeSerializer})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = SetupSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        expected = setup_token()
        if expected:
            if not constant_time_compare(ser.validated_data.get("setup_token") or "", expected):
                audit.record("auth.setup_failed", request=request, success=False, details={"reason": "setup_token"})
                raise ApiError(
                    "Enter the setup token configured on the server (MCPANEL_SETUP_TOKEN) to create the owner account",
                    code="setup_token_invalid",
                    status_code=403,
                )
        else:
            _warn_open_setup()
        with transaction.atomic():
            # Row locks cannot guard an empty table, so serialise concurrent first-run setups
            # with a transaction-scoped advisory lock; the check below then sees the winner.
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [SETUP_LOCK_KEY])
            if User.objects.exists():
                raise ApiError("Setup has already been completed", code="setup_complete", status_code=409)
            ensure_system_roles()
            user = User(
                username=ser.validated_data["username"],
                email=ser.validated_data.get("email", ""),
                is_owner=True,
                is_staff=True,
                is_superuser=True,
            )
            user.set_password(ser.validated_data["password"])
            user.save()
        login(request, user)
        audit.record("auth.setup", request=request, actor=user, target=user)
        return Response(MeSerializer(user).data, status=status.HTTP_201_CREATED)


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=LoginSerializer, responses={200: MeSerializer})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        name = ser.validated_data["username"]
        account_throttle.check("login", name)  # 429 after too many failures for this account (D-3)
        user = authenticate(request, username=name, password=ser.validated_data["password"])
        if user is None:
            account_throttle.failed("login", name)
            audit.record("auth.login_failed", request=request, success=False, details={"username": name[:150]})
            raise ApiError("Invalid username or password", code="invalid_credentials", status_code=401)
        login(request, user)
        audit.record("auth.login", request=request, actor=user, target=user)
        return Response(MeSerializer(user).data)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]  # explicit: must stay usable while must_change_password is set

    @extend_schema(request=None, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        audit.record("auth.logout", request=request)
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = MeSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):  # type: ignore[no-untyped-def]
        return self.request.user


def _validate_new_password(password: str, user: User) -> None:
    """Run Django's password validators, reporting failures as a field error (not a 500)."""
    try:
        password_validation.validate_password(password, user)
    except DjangoValidationError as exc:
        raise ValidationError({"new_password": list(exc.messages)}) from exc


class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]  # explicit: this is how must_change_password gets cleared

    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = PasswordChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user
        account_throttle.check("password", str(user.pk))  # 429 after too many wrong current passwords (D-3)
        if not user.check_password(ser.validated_data["old_password"]):
            account_throttle.failed("password", str(user.pk))
            raise ValidationError({"old_password": ["Current password is incorrect"]})
        _validate_new_password(ser.validated_data["new_password"], user)
        user.set_password(ser.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        update_session_auth_hash(request, user)
        audit.record("auth.password_changed", request=request, target=user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@method_decorator(csrf_protect, name="dispatch")
class PasswordResetView(APIView):
    """Complete a reset using a one-time token issued by an administrator."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=PasswordResetSerializer, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        # The token is only ever read from the POST body (never from the URL, which ends up in
        # access logs); it is single-use, expires, and dies with any password change (see model).
        ser = PasswordResetSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            # Row lock: two concurrent requests with the same token cannot both use it.
            token = (
                PasswordResetToken.objects.select_for_update(of=("self",))
                .select_related("user")
                .filter(token_hash=PasswordResetToken.digest(ser.validated_data["token"]))
                .first()
            )
            if token is None or not token.is_valid:
                raise ApiError("This reset link is invalid or has expired", code="invalid_token", status_code=400)
            _validate_new_password(ser.validated_data["new_password"], token.user)
            user = token.user
            user.set_password(ser.validated_data["new_password"])
            user.must_change_password = False
            user.save(update_fields=["password", "must_change_password"])
            PasswordResetToken.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
        audit.record("auth.password_reset", request=request, actor=user, target=user)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _is_root(user: User) -> bool:
    return bool(user.is_owner or user.is_superuser)


def _assignable_perms(user: User) -> frozenset[str]:
    """Permissions a user holds through their account (independent of is_active)."""
    if _is_root(user):
        return ALL_CODES
    return user.role.permission_set if user.role_id else frozenset()


def _outranked(request, target: User) -> str | None:  # type: ignore[no-untyped-def]
    """Why ``request.user`` may not manage ``target`` (None if they may).

    Only the owner may touch the owner; nobody may manage a user who out-ranks them.
    "Out-ranks" covers per-server grants too: a reset link or re-enabling an account
    hands its server access to whoever controls it.
    """
    if target.is_owner and not request.user.is_owner:
        return "Only the owner can modify the owner account"
    if _is_root(request.user):
        return None
    if _is_root(target) or not _assignable_perms(target) <= request.user.global_permissions():
        return "You cannot manage a user who has permissions you do not have"
    for grant in InstanceAccess.objects.filter(user=target).select_related("role"):
        if not (grant.role.permission_set & INSTANCE_CODES) <= request.user.instance_permissions(grant.instance_id):
            return "You cannot manage a user who has server access you do not have"
    return None


def _guard_owner(request, target: User) -> None:  # type: ignore[no-untyped-def]
    reason = _outranked(request, target)
    if reason:
        raise PermissionDenied(reason)


def _guard_permission_grant(request, codes, what: str = "a role") -> None:  # type: ignore[no-untyped-def]
    """Prevent privilege escalation: users can only hand out permissions they hold themselves,
    and only the owner hands out administration rights (OWNER_GRANTED_CODES)."""
    if _is_root(request.user):
        return
    owner_only = sorted(set(codes) & OWNER_GRANTED_CODES)
    if owner_only:
        raise PermissionDenied(f"Only the owner can grant {what} with these permissions: {', '.join(owner_only)}")
    missing = sorted(set(codes) - request.user.global_permissions())
    if missing:
        raise PermissionDenied(f"You cannot grant {what} with permissions you do not have: {', '.join(missing)}")


def _guard_role_change(request, role: Role) -> None:  # type: ignore[no-untyped-def]
    """Changing a role's permissions changes every account holding it, so it needs the same
    standing as managing each of them (and holding everything the role currently grants)."""
    if _is_root(request.user):
        return
    if not role.permission_set <= request.user.global_permissions():
        raise PermissionDenied("You cannot change a role that has permissions you do not have")
    for target in role.users.select_related("role"):
        if _outranked(request, target):
            raise PermissionDenied(
                f"This role is assigned to '{target.username}', who has permissions you do not have; "
                "only an account that out-ranks every holder can change it"
            )


def _guard_role_assignment(request, role: Role | None) -> None:  # type: ignore[no-untyped-def]
    if role is None:
        return
    if role.key == "administrator" and not _is_root(request.user):
        raise PermissionDenied("Only the owner can assign the Administrator role")
    _guard_permission_grant(request, role.permission_set, "a role")


class UserListCreateView(generics.ListCreateAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.users"
    queryset = User.objects.select_related("role").order_by("username")

    def get_serializer_class(self):  # type: ignore[no-untyped-def]
        return UserCreateSerializer if self.request.method == "POST" else UserSerializer

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        role = serializer.validated_data.get("role")
        _guard_role_assignment(self.request, role)
        user = serializer.save()
        audit.record("user.created", request=self.request, target=user, details={"role": role.key if role else None})


class UserDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.users"
    queryset = User.objects.select_related("role")
    serializer_class = UserSerializer

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        target = serializer.instance
        _guard_owner(self.request, target)
        if target == self.request.user and serializer.validated_data.get("is_active") is False:
            raise ValidationError({"is_active": ["You cannot disable your own account"]})
        if target == self.request.user and "role" in serializer.validated_data and not target.is_owner:
            raise ValidationError({"role": ["You cannot change your own role"]})
        if "role" in serializer.validated_data and serializer.validated_data["role"] != target.role:
            _guard_role_assignment(self.request, serializer.validated_data["role"])
        user = serializer.save()
        audit.record(
            "user.updated", request=self.request, target=user, details={k: str(v) for k, v in serializer.validated_data.items()}
        )

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        _guard_owner(self.request, instance)
        if instance == self.request.user:
            raise ValidationError({"detail": ["You cannot delete your own account"]})
        if instance.is_owner:
            raise ValidationError({"detail": ["The owner account cannot be deleted"]})
        audit.record("user.deleted", request=self.request, target=instance, details={"username": instance.username})
        instance.delete()


class UserResetLinkView(APIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.users"

    @extend_schema(request=None, responses={201: dict})
    def post(self, request, pk: int):  # type: ignore[no-untyped-def]
        user = generics.get_object_or_404(User, pk=pk)
        _guard_owner(request, user)
        _token, raw = PasswordResetToken.issue(user, created_by=request.user)
        audit.record("user.reset_link_issued", request=request, target=user)
        # The token travels in the URL fragment: browsers never send it to the server, so it
        # does not end up in the panel's or a reverse proxy's access log.
        return Response({"token": raw, "path": f"/reset-password#token={raw}", "expires_in_hours": 24}, status=201)


class RoleListCreateView(generics.ListCreateAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.users"
    queryset = Role.objects.all()
    serializer_class = RoleSerializer

    def get_permissions(self):  # type: ignore[no-untyped-def]
        if self.request.method == "GET":
            return [IsAuthenticated(), PasswordChangeEnforced()]  # role names are needed to display access grants
        return super().get_permissions()

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        _guard_permission_grant(self.request, serializer.validated_data.get("permissions", []))
        role = serializer.save(is_system=False)
        audit.record("role.created", request=self.request, target=role, details={"permissions": role.permissions})


class RoleDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.users"
    queryset = Role.objects.all()
    serializer_class = RoleSerializer

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        if serializer.instance.is_system:
            raise ValidationError({"detail": ["Built-in roles cannot be modified; create a custom role instead"]})
        if "permissions" in serializer.validated_data:
            _guard_role_change(self.request, serializer.instance)
            _guard_permission_grant(self.request, serializer.validated_data["permissions"])
        role = serializer.save()
        audit.record("role.updated", request=self.request, target=role, details={"permissions": role.permissions})

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        if instance.is_system:
            raise ValidationError({"detail": ["Built-in roles cannot be deleted"]})
        if instance.users.exists() or instance.instance_grants.exists():
            raise ValidationError({"detail": ["This role is still assigned to users or servers"]})
        audit.record("role.deleted", request=self.request, target=instance)
        instance.delete()


class PermissionDefsView(APIView):
    permission_classes = [IsAuthenticated, PasswordChangeEnforced]

    @extend_schema(responses={200: dict})
    def get(self, request):  # type: ignore[no-untyped-def]
        return Response(
            {
                "permissions": [
                    {"code": p.code, "label": p.label, "description": p.description, "scope": p.scope, "group": p.group}
                    for p in PERMISSIONS
                ]
            }
        )


class _Empty(serializers.Serializer):
    pass
