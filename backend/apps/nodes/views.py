from __future__ import annotations

from urllib.parse import urlparse

from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.errors import ApiError
from apps.core.permissions import HasGlobalPermission

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
        read_only_fields = ["last_seen_at", "last_error", "created_at", "is_default"]

    def get_instance_count(self, obj: Node) -> int:
        return obj.instances.count()

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
        p = urlparse(value)
        if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/") or p.query:
            raise serializers.ValidationError("Use the agent base URL, e.g. https://node2.lan:8700")
        return value.rstrip("/")

    def validate(self, attrs: dict) -> dict:
        use_settings = attrs.get("use_settings_token", getattr(self.instance, "use_settings_token", False))
        token = attrs.get("agent_token")
        if not use_settings and not token and not getattr(self.instance, "agent_token", ""):
            raise serializers.ValidationError({"agent_token": ["Enter the token configured as AGENT_TOKEN on that node"]})
        if token is not None and token and len(token) < 24:
            raise serializers.ValidationError({"agent_token": ["Agent tokens must be at least 24 characters"]})
        self._validate_tls(attrs)
        return attrs

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


class NodeListView(generics.ListCreateAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.nodes"
    serializer_class = NodeSerializer

    def get_queryset(self):  # type: ignore[no-untyped-def]
        Node.default()  # make sure the local node exists
        return Node.objects.all()

    def get_permissions(self):  # type: ignore[no-untyped-def]
        if self.request.method == "GET":
            return [IsAuthenticated()]  # names are needed by the create wizard; tokens are write-only
        return super().get_permissions()

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        node = serializer.save(use_settings_token=serializer.validated_data.get("use_settings_token", False))
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
