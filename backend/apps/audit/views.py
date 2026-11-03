from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import get_instance_for, require

from .models import AuditEvent


class AuditEventSerializer(serializers.ModelSerializer):
    instance_name = serializers.CharField(source="instance.name", read_only=True, default=None)

    class Meta:
        model = AuditEvent
        fields = [
            "id",
            "created_at",
            "actor",
            "actor_username",
            "action",
            "target_type",
            "target_id",
            "instance",
            "instance_name",
            "ip_address",
            "success",
            "details",
        ]


class AuditListView(APIView):
    @extend_schema(responses={200: AuditEventSerializer(many=True)})
    def get(self, request: Request) -> Response:
        qp = request.query_params
        qs = AuditEvent.objects.select_related("instance")
        if inst := qp.get("instance"):
            instance = get_instance_for(request.user, inst)
            if not request.user.has_panel_perm("system.audit"):
                require(request.user, "instance.configure", instance)
            qs = qs.filter(instance=instance)
        else:
            require(request.user, "system.audit")
        if action := qp.get("action"):
            qs = qs.filter(action__startswith=action)
        if actor := qp.get("actor"):
            qs = qs.filter(actor_username=actor)
        if before := qp.get("before"):
            qs = qs.filter(id__lt=before)
        try:
            limit = max(1, min(int(qp.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        return Response(AuditEventSerializer(qs[:limit], many=True).data)
