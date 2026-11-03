from __future__ import annotations

from django.contrib.auth import authenticate, login, logout, password_validation, update_session_auth_hash
from django.db import connection, transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import HasGlobalPermission

from .models import InstanceAccess, PasswordResetToken, Role, User
from .permissions import ALL_CODES, INSTANCE_CODES, PERMISSIONS, SYSTEM_ROLES
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


class AuthStateView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(responses={200: dict})
    def get(self, request):  # type: ignore[no-untyped-def]
        get_token(request)  # ensure the CSRF cookie is set for the SPA
        setup_required = not User.objects.exists()
        data = {"setup_required": setup_required, "authenticated": request.user.is_authenticated, "user": None}
        if request.user.is_authenticated:
            data["user"] = MeSerializer(request.user).data
        return Response(data)


SETUP_LOCK_KEY = 0x4D43_5345_5455_5000  # arbitrary, stable advisory-lock id for first-run setup


class SetupView(APIView):
    """Create the owner account. Only possible while no users exist."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "setup"

    @extend_schema(request=SetupSerializer, responses={201: MeSerializer})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = SetupSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
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


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=LoginSerializer, responses={200: MeSerializer})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = authenticate(request, username=ser.validated_data["username"], password=ser.validated_data["password"])
        if user is None:
            audit.record(
                "auth.login_failed", request=request, success=False, details={"username": ser.validated_data["username"][:150]}
            )
            raise ApiError("Invalid username or password", code="invalid_credentials", status_code=401)
        login(request, user)
        audit.record("auth.login", request=request, actor=user, target=user)
        return Response(MeSerializer(user).data)


class LogoutView(APIView):
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


class PasswordChangeView(APIView):
    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = PasswordChangeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(ser.validated_data["old_password"]):
            raise ValidationError({"old_password": ["Current password is incorrect"]})
        password_validation.validate_password(ser.validated_data["new_password"], user)
        user.set_password(ser.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        update_session_auth_hash(request, user)
        audit.record("auth.password_changed", request=request, target=user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetView(APIView):
    """Complete a reset using a one-time token issued by an administrator."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    @extend_schema(request=PasswordResetSerializer, responses={204: None})
    def post(self, request):  # type: ignore[no-untyped-def]
        ser = PasswordResetSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token = (
            PasswordResetToken.objects.select_related("user")
            .filter(token_hash=PasswordResetToken.digest(ser.validated_data["token"]))
            .first()
        )
        if token is None or not token.is_valid:
            raise ApiError("This reset link is invalid or has expired", code="invalid_token", status_code=400)
        password_validation.validate_password(ser.validated_data["new_password"], token.user)
        user = token.user
        user.set_password(ser.validated_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
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


def _guard_owner(request, target: User) -> None:  # type: ignore[no-untyped-def]
    """Only the owner may touch the owner; nobody may manage a user who out-ranks them.

    "Out-ranks" covers per-server grants too: a reset link or re-enabling an account
    hands its server access to whoever controls it.
    """
    if target.is_owner and not request.user.is_owner:
        raise PermissionDenied("Only the owner can modify the owner account")
    if _is_root(request.user):
        return
    if not _assignable_perms(target) <= request.user.global_permissions():
        raise PermissionDenied("You cannot manage a user who has permissions you do not have")
    for grant in InstanceAccess.objects.filter(user=target).select_related("role"):
        if not (grant.role.permission_set & INSTANCE_CODES) <= request.user.instance_permissions(grant.instance_id):
            raise PermissionDenied("You cannot manage a user who has server access you do not have")


def _guard_permission_grant(request, codes, what: str = "a role") -> None:  # type: ignore[no-untyped-def]
    """Prevent privilege escalation: users can only hand out permissions they hold themselves."""
    if _is_root(request.user):
        return
    missing = sorted(set(codes) - request.user.global_permissions())
    if missing:
        raise PermissionDenied(f"You cannot grant {what} with permissions you do not have: {', '.join(missing)}")


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
        return Response({"token": raw, "path": f"/reset-password?token={raw}", "expires_in_hours": 24}, status=201)


class RoleListCreateView(generics.ListCreateAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.users"
    queryset = Role.objects.all()
    serializer_class = RoleSerializer

    def get_permissions(self):  # type: ignore[no-untyped-def]
        if self.request.method == "GET":
            return [IsAuthenticated()]  # role names are needed to display access grants
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
