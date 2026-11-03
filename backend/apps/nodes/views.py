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

from .client import AgentClient
from .models import Node


class NodeSerializer(serializers.ModelSerializer):
    agent_token = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=500)
    instance_count = serializers.SerializerMethodField()

    class Meta:
        model = Node
        fields = ["id", "name", "agent_url", "agent_token", "use_settings_token", "is_default", "public_address",
                  "lan_address", "last_seen_at", "last_error", "instance_count", "created_at"]
        read_only_fields = ["last_seen_at", "last_error", "created_at", "is_default"]

    def get_instance_count(self, obj: Node) -> int:
        return obj.instances.count()

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
        return attrs


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
        node.info = {k: info.get(k) for k in ("version", "docker", "restic", "policy")}
        node.save(update_fields=["last_seen_at", "last_error", "info"])
        return Response({"ok": True, "info": info})
