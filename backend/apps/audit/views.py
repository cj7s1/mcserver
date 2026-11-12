from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import ApiError
from apps.core.permissions import get_instance_for, require
from apps.core.utils import parse_uuid

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
            if parse_uuid(inst) is None:
                raise ApiError("instance must be a server id", code="invalid_instance")  # (D-12: was a 500)
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
            if not before.isdigit() or len(before) > 18:
                raise ApiError("before must be an event id", code="invalid_cursor")  # (D-12: was a 500)
            qs = qs.filter(id__lt=int(before))
        try:
            limit = max(1, min(int(qp.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        rows = AuditEventSerializer(qs[:limit], many=True).data
        if not request.user.has_panel_perm("system.audit"):
            # a server's trail for its configurers: who did what, not from which address (D-13)
            for row in rows:
                row["ip_address"] = None
        return Response(rows)
