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
