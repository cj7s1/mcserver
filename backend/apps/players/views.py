from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps import games
from apps.audit import service as audit
from apps.core.permissions import get_instance_for

from . import service


class PlayerActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["whitelist_add", "whitelist_remove", "op", "deop", "ban", "pardon", "kick"])
    name = serializers.CharField(max_length=16)
    reason = serializers.CharField(max_length=200, required=False, allow_blank=True)


class PlayersView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.view")
        games.for_instance(instance).require(instance, "players")
        return Response(service.lists(instance))

    @extend_schema(request=PlayerActionSerializer, responses={200: dict})
    def post(self, request: Request, pk: str) -> Response:
        instance = get_instance_for(request.user, pk, "instance.players")
        games.for_instance(instance).require(instance, "player_actions")
        ser = PlayerActionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        result = service.mutate(instance, d["action"], d["name"], d.get("reason", ""))
        audit.record(
            f"players.{d['action']}",
            request=request,
            instance=instance,
            target=instance,
            details={"player": d["name"], "via": result["via"]},
        )
        return Response(result)
