from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.errors import ApiError

from . import providers


class ServerTypesView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        return Response({"server_types": providers.SERVER_TYPES})


class GameVersionsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        st = request.query_params.get("server_type", "VANILLA").upper()
        snapshots = request.query_params.get("snapshots") == "true"
        versions = providers.game_versions(st, include_snapshots=snapshots)
        latest = providers.latest_release()
        return Response({"server_type": st, "latest_release": latest, "versions": versions})


class LoaderVersionsView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        st = request.query_params.get("server_type", "").upper()
        mc = request.query_params.get("minecraft_version", "")
        if not st or not mc:
            raise ApiError("server_type and minecraft_version are required", code="missing_params")
        return Response({"server_type": st, "minecraft_version": mc, "versions": providers.loader_versions(st, mc)})


class JavaView(APIView):
    @extend_schema(responses={200: dict})
    def get(self, request: Request) -> Response:
        mc = request.query_params.get("minecraft_version", "")
        st = request.query_params.get("server_type", "").upper()
        if not mc:
            raise ApiError("minecraft_version is required", code="missing_params")
        return Response(providers.resolve_java(mc, st))
