from typing import Any

from rest_framework.authentication import SessionAuthentication

from . import session_binding
from .errors import ApiError


class SessionTokenRequired(ApiError):
    status_code = 401
    default_code = "session_token_required"
    default_detail = "Sign in again: this browser tab has no valid session token."


class CsrfEnforcedSessionAuthentication(SessionAuthentication):
    """Session authentication that always enforces CSRF for unsafe methods, and (``MCPANEL_SESSION_BINDING``,
    D-1/I-7) a session bound to the tab that signed in: a signed-in session must send its token in
    ``X-MCPanel-Session-Token`` on every request, safe methods included, or present a download ticket on a GET
    of a download route; else 401 ``session_token_required``. The cookie alone is not a credential.
    Exempt: ``session_binding.EXEMPT_PATHS`` (state, login, setup, logout, reset, health).
    """

    def authenticate(self, request: Any) -> Any:
        result = super().authenticate(request)  # (user, None) or None; CSRF for unsafe methods
        if result is None or not session_binding.enabled() or session_binding.exempt(request.path):
            return result
        if session_binding.header_ok(request):
            return result
        view = (getattr(request, "parser_context", None) or {}).get("view")
        if getattr(view, "download_ticket", False) and session_binding.ticket_ok(request):
            return result
        raise SessionTokenRequired()


try:  # describe the session + CSRF scheme in the OpenAPI document
    from drf_spectacular.authentication import SessionScheme

    class CsrfEnforcedSessionScheme(SessionScheme):
        target_class = "apps.core.auth.CsrfEnforcedSessionAuthentication"
        name = "sessionAuth"
except ImportError:  # pragma: no cover - drf-spectacular is a hard dependency
    pass
