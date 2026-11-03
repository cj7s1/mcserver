from rest_framework.authentication import SessionAuthentication


class CsrfEnforcedSessionAuthentication(SessionAuthentication):
    """Session authentication that always enforces CSRF for unsafe methods.

    DRF's SessionAuthentication already enforces CSRF; this subclass exists so
    the policy is explicit and centrally configurable.
    """


try:  # describe the session + CSRF scheme in the OpenAPI document
    from drf_spectacular.authentication import SessionScheme

    class CsrfEnforcedSessionScheme(SessionScheme):
        target_class = "apps.core.auth.CsrfEnforcedSessionAuthentication"
        name = "sessionAuth"
except ImportError:  # pragma: no cover - drf-spectacular is a hard dependency
    pass
