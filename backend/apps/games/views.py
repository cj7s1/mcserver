from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from . import MEMORY_MODELS, catalog


class GameVariantSerializer(serializers.Serializer):
    id = serializers.CharField()
    label = serializers.CharField()


class GameCatalogEntrySerializer(serializers.Serializer):
    id = serializers.CharField()
    label = serializers.CharField()
    variants = GameVariantSerializer(many=True)
    capabilities = serializers.ListField(child=serializers.CharField(), help_text="Every capability the game can have")
    memory_model = serializers.ChoiceField(choices=list(MEMORY_MODELS))
    default_memory_mb = serializers.IntegerField()
    notes = serializers.ListField(child=serializers.CharField())


class GameCatalogView(APIView):
    """The games servers can be created for (phase 1: Minecraft only). Any signed-in user."""

    @extend_schema(operation_id="catalog_games", responses={200: GameCatalogEntrySerializer(many=True)})
    def get(self, request: Request) -> Response:
        return Response(catalog())
