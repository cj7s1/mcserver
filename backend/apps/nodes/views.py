from __future__ import annotations

from urllib.parse import urlparse

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import HasGlobalPermission, PasswordChangeEnforced, SeesMoreThanStatus

from . import tls
from .client import AgentClient
from .models import Node


class NodeSerializer(serializers.ModelSerializer):
    agent_token = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=500)
    tls_fingerprint = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=200,
        help_text="SHA-256 fingerprint of the agent certificate (python -m mcagent fingerprint) to pin it",
    )
    instance_count = serializers.SerializerMethodField()
    transport = serializers.SerializerMethodField(help_text="plain, tls_pinned or tls_ca")
    tls_pinned = serializers.SerializerMethodField()
    security_warning = serializers.SerializerMethodField()

    class Meta:
        model = Node
        fields = [
            "id",
            "name",
            "agent_url",
            "agent_token",
            "use_settings_token",
            "is_default",
            "public_address",
            "lan_address",
            "tls_fingerprint",
            "transport",
            "tls_pinned",
            "security_warning",
            "last_seen_at",
            "last_error",
            "instance_count",
            "created_at",
        ]
        # use_settings_token is never user-writable: MCPANEL_AGENT_TOKEN (root on the panel host's
        # agent) is only ever sent to the settings-managed default node, at MCPANEL_AGENT_URL.
        read_only_fields = ["last_seen_at", "last_error", "created_at", "is_default", "use_settings_token"]

    # Fields that decide where a stored token is sent; a settings-managed node takes them from
    # MCPANEL_AGENT_URL / MCPANEL_AGENT_TLS_FINGERPRINT / MCPANEL_AGENT_TOKEN instead.
    MANAGED_FIELDS = ("name", "agent_url", "tls_fingerprint", "agent_token")

    def get_instance_count(self, obj: Node) -> int:
        annotated = getattr(obj, "_instance_count", None)
        return annotated if annotated is not None else obj.instances.count()

    def get_transport(self, obj: Node) -> str:
        return tls.transport(obj)

    def get_tls_pinned(self, obj: Node) -> bool:
        return bool(obj.tls_fingerprint and obj.tls_cert_pem)

    def get_security_warning(self, obj: Node) -> str | None:
        if tls.transport(obj) == "plain" and not obj.is_default:
            return (
                "This node is reached over plain HTTP: its token and console traffic can be read on the network. "
                "Set AGENT_TLS=auto on the node, change the URL to https:// and pin its fingerprint."
            )
        return None

    def to_representation(self, instance: Node) -> dict:
        data = super().to_representation(instance)
        data["tls_fingerprint"] = tls.format_fingerprint(instance.tls_fingerprint) if instance.tls_fingerprint else ""
        return data

    def validate_tls_fingerprint(self, value: str) -> str:
        if not value.strip():
            return ""
        try:
            return tls.normalize_fingerprint(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_agent_url(self, value: str) -> str:
        value = value.strip()
        p = urlparse(value)
        try:
            p.port  # noqa: B018 - raises ValueError for a malformed or out-of-range port
        except ValueError:
            p = None  # type: ignore[assignment]
        if (
            p is None
            or p.scheme not in ("http", "https")
            or not p.hostname
            or p.path not in ("", "/")
            or p.params
            or p.query
            or "#" in value  # also an empty fragment: everything after '#' never reaches the agent
            or "@" in p.netloc  # userinfo (http://trusted@evil) hides the real host
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise serializers.ValidationError("Use the agent base URL, e.g. https://node2.lan:8700")
        return value.rstrip("/")

    def create(self, validated_data: dict) -> Node:
        validated_data["use_settings_token"] = False  # new nodes always carry their own token
        return super().create(validated_data)

    def validate(self, attrs: dict) -> dict:
        current: Node | None = self.instance
        token = attrs.get("agent_token")
        if current is not None and current.use_settings_token:
            self._validate_managed(current, attrs)
            if not token:
                if not current.is_default:  # the default node's TLS settings come from the environment
                    self._validate_tls(attrs)
                return attrs
            attrs["use_settings_token"] = False  # a legacy remote node switching to its own token
        if token is not None and token and len(token) < 24:
            raise serializers.ValidationError({"agent_token": ["Agent tokens must be at least 24 characters"]})
        if not token and not getattr(current, "agent_token", ""):
            raise serializers.ValidationError({"agent_token": ["Enter the token configured as AGENT_TOKEN on that node"]})
        self._validate_tls(attrs)  # a TLS handshake only; nothing is sent to the agent
        if current is not None and "agent_url" in attrs and attrs["agent_url"] != current.agent_url and not token:
            # Otherwise the stored token would follow whatever URL was entered (any listener,
            # e.g. a game port served by a third-party mod, would receive it). A pinned
            # fingerprint does not help here: whoever enters the URL also chooses the pin.
            raise serializers.ValidationError(
                {"agent_token": ["Re-enter the node's AGENT_TOKEN when changing its URL (the stored token is not reused)"]}
            )
        return attrs

    def _validate_managed(self, current: Node, attrs: dict) -> None:
        """Nodes using MCPANEL_AGENT_TOKEN: the default node cannot be repointed through the API."""
        if not current.is_default:
            # legacy remote node that shares the settings token: it may switch to its own token,
            # but never move the settings token to a different URL
            if "agent_url" in attrs and attrs["agent_url"] != current.agent_url and not attrs.get("agent_token"):
                raise serializers.ValidationError({"agent_token": ["Enter this node's own AGENT_TOKEN to change its URL"]})
            return
        errors = {}
        for field in self.MANAGED_FIELDS:
            if field not in attrs:
                continue
            value = attrs[field]
            unchanged = (not value) if field == "agent_token" else value == getattr(current, field)
            if not unchanged:
                errors[field] = [
                    "The default node follows MCPANEL_AGENT_URL, MCPANEL_AGENT_TOKEN and MCPANEL_AGENT_TLS_FINGERPRINT; "
                    "change those settings and restart the panel instead"
                ]
        if errors:
            raise serializers.ValidationError(errors)

    def _validate_tls(self, attrs: dict) -> None:
        current: Node | None = self.instance
        url = attrs.get("agent_url", getattr(current, "agent_url", ""))
        fp = attrs.get("tls_fingerprint", getattr(current, "tls_fingerprint", ""))
        if not fp:
            attrs["tls_cert_pem"] = ""
            return
        if not url.startswith("https://"):
            raise serializers.ValidationError({"tls_fingerprint": ["Certificate pinning needs an https:// agent URL"]})
        pem = getattr(current, "tls_cert_pem", "")
        unchanged = current is not None and url == current.agent_url and fp == current.tls_fingerprint
        if unchanged and pem and tls.fingerprint_of_pem(pem) == fp:
            return
        try:
            attrs["tls_cert_pem"] = tls.pin(url, fp)
        except tls.AgentTlsError as exc:
            if exc.error_code == "agent_tls_mismatch":
                raise serializers.ValidationError({"tls_fingerprint": [str(exc.detail)]}) from exc
            # Agent not reachable right now: keep the fingerprint; the certificate is read and
            # checked on the first connection (nothing is sent before it matches).
            attrs["tls_cert_pem"] = ""


class NodeSummarySerializer(serializers.ModelSerializer):
    """What every signed-in user may know about a node (to pick one when creating a server)."""

    class Meta:
        model = Node
        fields = ["id", "name", "is_default"]
        read_only_fields = fields


class NodeListView(generics.ListCreateAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.nodes"
    serializer_class = NodeSerializer

    def get_queryset(self):  # type: ignore[no-untyped-def]
        Node.default()  # make sure the local node exists
        qs = Node.objects.all()
        if self.request.method == "GET" and self.get_serializer_class() is NodeSerializer:
            qs = qs.annotate(_instance_count=Count("instances"))
        return qs

    def get_serializer_class(self):  # type: ignore[no-untyped-def]
        if getattr(self, "swagger_fake_view", False):  # OpenAPI generation documents the full shape
            return NodeSerializer
        user = self.request.user
        if self.request.method == "GET" and not (user.is_authenticated and user.has_panel_perm("system.nodes")):
            return NodeSummarySerializer  # no agent URLs, errors or per-node server counts
        return NodeSerializer

    def get_permissions(self):  # type: ignore[no-untyped-def]
        if self.request.method == "GET":
            # names are needed by the create wizard; everything else needs system.nodes; not for a Friend (F-11)
            return [IsAuthenticated(), PasswordChangeEnforced(), SeesMoreThanStatus()]
        return super().get_permissions()

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        node = serializer.save()
        audit.record("node.created", request=self.request, target=node, details={"url": node.agent_url})


class NodeDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.nodes"
    serializer_class = NodeSerializer
    queryset = Node.objects.all()

    def perform_update(self, serializer):  # type: ignore[no-untyped-def]
        node = serializer.save()
        audit.record("node.updated", request=self.request, target=node, details={"url": node.agent_url})

    def perform_destroy(self, instance: Node) -> None:
        if instance.is_default:
            raise ApiError("The default node cannot be removed", code="default_node", status_code=409)
        if instance.instances.exists():
            raise ApiError("Move or delete this node's servers first", code="node_in_use", status_code=409)
        audit.record("node.deleted", request=self.request, target=instance)
        instance.delete()


class NodeCheckView(APIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.nodes"

    @extend_schema(request=None, responses={200: dict})
    def post(self, request: Request, pk: int) -> Response:
        node = generics.get_object_or_404(Node, pk=pk)
        try:
            info = AgentClient(node, timeout=10).info()
        except ApiError as exc:
            node.last_error = str(exc.detail)[:1000]
            node.save(update_fields=["last_error"])
            return Response({"ok": False, "error": node.last_error, "code": exc.error_code})
        from django.utils import timezone

        node.last_seen_at = timezone.now()
        node.last_error = ""
        node.info = {k: info.get(k) for k in ("version", "docker", "restic", "policy", "tls")}
        node.save(update_fields=["last_seen_at", "last_error", "info"])
        return Response({"ok": True, "info": info})


class TlsProbeSerializer(serializers.Serializer):
    agent_url = serializers.URLField(max_length=300)


class NodeTlsProbeView(APIView):
    """Read the certificate an https:// agent presents so its fingerprint can be compared.

    Only a TLS handshake is performed (no HTTP request, no token). The administrator must
    compare the result with ``python -m mcagent fingerprint`` on the node before pinning it.
    """

    permission_classes = [HasGlobalPermission]
    required_permission = "system.nodes"

    @extend_schema(request=TlsProbeSerializer, responses={200: dict})
    def post(self, request: Request) -> Response:
        ser = TlsProbeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        url = NodeSerializer().validate_agent_url(ser.validated_data["agent_url"])
        if not url.startswith("https://"):
            raise ApiError("Enter an https:// agent URL", code="not_https")
        pem = tls.fetch_certificate(url)
        details = tls.describe_certificate(pem)
        return Response({"fingerprint": tls.format_fingerprint(tls.fingerprint_of_pem(pem)), **details})
