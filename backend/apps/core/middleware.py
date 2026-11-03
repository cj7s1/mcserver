from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .logging import request_id_var

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Assigns a request id (honouring a sane incoming X-Request-ID) for log correlation."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get("X-Request-ID", "")
        rid = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        try:
            request.request_id = rid  # type: ignore[attr-defined]
            response = self.get_response(request)
            response["X-Request-ID"] = rid
            return response
        finally:
            request_id_var.reset(token)


class UploadLimitMiddleware:
    """Reject oversized request bodies before Django reads/spools them (HTTP 413).

    The limit is MCPANEL_MAX_UPLOAD_BYTES (default 2 GiB); chunked uploads without a
    Content-Length are refused for the upload endpoints.
    """

    UPLOAD_HINTS = ("/files/upload", "/import", "/content/upload")

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.conf import settings
        from django.http import JsonResponse

        limit = settings.MCPANEL_MAX_UPLOAD_BYTES
        if request.method in ("POST", "PUT", "PATCH") and limit:
            raw = request.META.get("CONTENT_LENGTH") or ""
            try:
                length = int(raw) if raw else None
            except ValueError:
                length = None
            is_upload = any(h in request.path for h in self.UPLOAD_HINTS)
            if (length is not None and length > limit) or (length is None and is_upload):
                return JsonResponse(
                    {
                        "error": {
                            "code": "file_too_large" if length else "length_required",
                            "message": f"Uploads are limited to {limit // (1024 * 1024)} MB"
                            if length
                            else "Uploads must declare a Content-Length",
                            "details": {},
                        }
                    },
                    status=413 if length else 411,
                )
        return self.get_response(request)
