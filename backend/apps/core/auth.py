from rest_framework.authentication import SessionAuthentication


class CsrfEnforcedSessionAuthentication(SessionAuthentication):
    """Session authentication that always enforces CSRF for unsafe methods.

    DRF's SessionAuthentication already enforces CSRF; this subclass exists so
    the policy is explicit and centrally configurable.
    """
