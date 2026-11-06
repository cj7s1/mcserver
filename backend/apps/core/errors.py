"""Centralised API error handling.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "details": {...}}}

so the frontend can render actionable messages consistently.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class ApiError(exceptions.APIException):
    """An application error with a stable machine-readable code."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "error"
    default_detail = "The request could not be completed."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail=message or self.default_detail, code=code or self.default_code)
        if status_code is not None:
            self.status_code = status_code
        self.error_code = code or self.default_code
        self.details = details or {}


class ConflictError(ApiError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "The resource is busy or in a conflicting state."


class UpstreamError(ApiError):
    """A dependency (agent, provider API) failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "upstream_error"
    default_detail = "A dependent service failed."


class ServiceUnavailable(ApiError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "service_unavailable"
    default_detail = "A required service is unavailable."


def _code_for(exc: exceptions.APIException) -> str:
    if isinstance(exc, ApiError):
        return exc.error_code
    codes = exc.get_codes()
    if isinstance(codes, str):
        return codes
    if isinstance(exc, exceptions.ValidationError):
        return "validation_error"
    return getattr(exc, "default_code", "error")


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    # Imported here: rest_framework.views resolves DEFAULT_PERMISSION_CLASSES on import, and
    # apps.core.permissions.PasswordChangeEnforced (listed there) imports this module.
    from rest_framework.views import exception_handler as drf_exception_handler

    if isinstance(exc, Http404):
        exc = exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API exception", exc_info=exc)
        return Response(
            {"error": {"code": "internal_error", "message": "An unexpected server error occurred.", "details": {}}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    assert isinstance(exc, exceptions.APIException)
    if isinstance(exc, exceptions.ValidationError):
        message = "Some fields are invalid."
        details: dict[str, Any] = {"fields": exc.detail} if isinstance(exc.detail, dict) else {"errors": exc.detail}
        if isinstance(exc.detail, list) and exc.detail:
            message = str(exc.detail[0])
        elif isinstance(exc.detail, dict) and "non_field_errors" in exc.detail:
            message = str(exc.detail["non_field_errors"][0])
    else:
        message = str(exc.detail) if not isinstance(exc.detail, (dict, list)) else exc.default_detail
        details = getattr(exc, "details", {}) or {}
    response.data = {"error": {"code": _code_for(exc), "message": message, "details": details}}
    return response
