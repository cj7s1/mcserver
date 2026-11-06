from __future__ import annotations

from django.contrib.auth import password_validation
from rest_framework import serializers

from .models import InstanceAccess, Role, User
from .permissions import validate_codes


class RoleSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ["id", "key", "name", "description", "permissions", "is_system", "user_count", "created_at", "updated_at"]
        read_only_fields = ["is_system", "created_at", "updated_at"]

    def get_user_count(self, obj: Role) -> int:
        return obj.users.count()

    def validate_permissions(self, value: list[str]) -> list[str]:
        try:
            return validate_codes(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class UserSerializer(serializers.ModelSerializer):
    role = serializers.PrimaryKeyRelatedField(queryset=Role.objects.all(), allow_null=True, required=False)
    role_name = serializers.CharField(source="role.name", read_only=True, default=None)
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_owner",
            "role",
            "role_name",
            "must_change_password",
            "last_login",
            "date_joined",
            "permissions",
        ]
        read_only_fields = ["is_owner", "last_login", "date_joined", "must_change_password"]

    def get_permissions(self, obj: User) -> list[str]:
        return sorted(obj.global_permissions())


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=10, max_length=256)
    role = serializers.PrimaryKeyRelatedField(queryset=Role.objects.all(), allow_null=True, required=False)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "password", "role", "must_change_password"]

    def validate(self, attrs: dict) -> dict:
        user = User(username=attrs.get("username"), email=attrs.get("email", ""))
        password_validation.validate_password(attrs["password"], user)
        return attrs

    def create(self, validated: dict) -> User:
        password = validated.pop("password")
        user = User(**validated)
        user.set_password(password)
        user.save()
        return user


class MeSerializer(serializers.ModelSerializer):
    permissions = serializers.SerializerMethodField()
    role_name = serializers.CharField(source="role.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "is_owner",
            "role_name",
            "permissions",
            "must_change_password",
            "preferences",
        ]
        read_only_fields = ["username", "is_owner", "role_name", "permissions", "must_change_password"]

    def get_permissions(self, obj: User) -> list[str]:
        return sorted(obj.global_permissions())


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=256, trim_whitespace=False)


class SetupSerializer(serializers.Serializer):
    username = serializers.RegexField(r"^[\w.@+-]{3,150}$", max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(min_length=10, max_length=256, trim_whitespace=False)
    setup_token = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=512,
        write_only=True,
        help_text="Required when the server sets MCPANEL_SETUP_TOKEN (see /auth/state setup_token_required)",
    )

    def validate(self, attrs: dict) -> dict:
        password_validation.validate_password(attrs["password"], User(username=attrs["username"]))
        return attrs


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(max_length=256, trim_whitespace=False)
    new_password = serializers.CharField(min_length=10, max_length=256, trim_whitespace=False)


class PasswordResetSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=200)
    new_password = serializers.CharField(min_length=10, max_length=256, trim_whitespace=False)


class InstanceAccessSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)

    class Meta:
        model = InstanceAccess
        fields = ["id", "user", "username", "role", "role_name", "created_at"]
        read_only_fields = ["created_at"]
