from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import service as audit
from apps.core.permissions import HasGlobalPermission

from .models import EVENTS, NotificationChannel
from .service import deliver, validate_url


class ChannelSerializer(serializers.ModelSerializer):
    url = serializers.CharField(write_only=True, max_length=1000)
    url_hint = serializers.SerializerMethodField()

    class Meta:
        model = NotificationChannel
        fields = [
            "id",
            "name",
            "kind",
            "url",
            "url_hint",
            "events",
            "instance",
            "enabled",
            "last_sent_at",
            "last_error",
            "created_at",
        ]
        read_only_fields = ["last_sent_at", "last_error", "created_at"]

    def get_url_hint(self, obj: NotificationChannel) -> str:
        from urllib.parse import urlparse

        p = urlparse(obj.url)
        return f"{p.scheme}://{p.hostname}/…" if p.hostname else ""

    def validate_events(self, value: list) -> list:
        if not isinstance(value, list) or not value or any(e not in EVENTS for e in value):
            raise serializers.ValidationError(f"Choose events from: {', '.join(EVENTS)}")
        return sorted(set(value))

    def validate(self, attrs: dict) -> dict:
        kind = attrs.get("kind") or getattr(self.instance, "kind", "webhook")
        if "url" in attrs:
            try:
                attrs["url"] = validate_url(kind, attrs["url"])
            except ValueError as exc:
                raise serializers.ValidationError({"url": [str(exc)]}) from exc
        return attrs


class ChannelListView(generics.ListCreateAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.settings"
    queryset = NotificationChannel.objects.all()
    serializer_class = ChannelSerializer

    def perform_create(self, serializer):  # type: ignore[no-untyped-def]
        ch = serializer.save()
        audit.record("notifications.channel_created", request=self.request, target=ch, details={"events": ch.events})


class ChannelDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.settings"
    queryset = NotificationChannel.objects.all()
    serializer_class = ChannelSerializer

    def perform_destroy(self, instance):  # type: ignore[no-untyped-def]
        audit.record("notifications.channel_deleted", request=self.request, target=instance)
        instance.delete()


class ChannelTestView(APIView):
    permission_classes = [HasGlobalPermission]
    required_permission = "system.settings"

    @extend_schema(request=None, responses={202: dict})
    def post(self, request: Request, pk: int) -> Response:
        ch = generics.get_object_or_404(NotificationChannel, pk=pk)
        deliver.delay(
            ch.pk,
            "job.failed",
            "MCPanel test notification",
            "If you can read this, notifications from MCPanel work.",
            {"status": "test"},
        )
        return Response({"queued": True}, status=202)


class EventsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        return Response({"events": [{"id": k, "label": v} for k, v in EVENTS.items()]})
