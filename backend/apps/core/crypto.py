"""Symmetric encryption for secrets stored in the database (RCON passwords, API keys,
node tokens, webhook URLs).

Keys are derived from MCPANEL_SECRET_KEY. To rotate it, move the old key to
MCPANEL_SECRET_KEY_FALLBACKS (Django's SECRET_KEY_FALLBACKS): values encrypted with a
fallback key stay readable, new values are encrypted with the current key, and
``manage.py reencrypt_secrets`` rewrites every stored secret with the current key so the
fallback can be dropped afterwards (docs/SECURITY.md, "Rotating the secret key").

A value that no key can decrypt is never silently replaced: it is logged once per field
at ERROR, read as an :class:`UnreadableSecret` (an empty string that remembers the
stored ciphertext, so saving the row again does not destroy it) and can be detected with
:func:`is_unreadable` to tell the user to re-enter it.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import models

log = logging.getLogger(__name__)
_reported: set[str] = set()


class UnreadableSecret(str):
    """A stored secret that could not be decrypted with any configured key.

    It compares and behaves like ``""`` (so callers treat the secret as missing), but keeps
    the original ciphertext: :meth:`EncryptedTextField.get_prep_value` writes it back
    unchanged, so an unrelated ``save()`` does not wipe a secret that becomes readable again
    once the old key is restored in MCPANEL_SECRET_KEY_FALLBACKS.
    """

    ciphertext: str

    def __new__(cls, ciphertext: str = "") -> UnreadableSecret:
        obj = super().__new__(cls, "")
        obj.ciphertext = ciphertext
        return obj


def is_unreadable(value: object) -> bool:
    return isinstance(value, UnreadableSecret)


def _derive(secret: str) -> Fernet:
    digest = hashlib.sha256(("mcpanel-field-encryption:" + secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> MultiFernet:
    keys = [settings.SECRET_KEY, *(getattr(settings, "SECRET_KEY_FALLBACKS", None) or [])]
    # the first key encrypts; all of them are tried for decryption
    return MultiFernet([_derive(k) for k in keys if k])


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt ``token``; raises ``InvalidToken`` if no configured key can decrypt it."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise InvalidToken from exc


def needs_reencryption(token: str) -> bool:
    """True if ``token`` is readable but was not encrypted with the current key."""
    try:
        _derive(settings.SECRET_KEY).decrypt(token.encode())
        return False
    except (InvalidToken, ValueError):
        try:
            decrypt(token)
        except InvalidToken:
            return False  # unreadable: re-encrypting is impossible
        return True


def _report_unreadable(where: str) -> None:
    if where in _reported:
        return
    _reported.add(where)
    log.error(
        "A stored secret (%s) cannot be decrypted with MCPANEL_SECRET_KEY or MCPANEL_SECRET_KEY_FALLBACKS. "
        "It is treated as missing: re-enter it, or put the previous secret key into "
        "MCPANEL_SECRET_KEY_FALLBACKS (see docs/SECURITY.md, 'Rotating the secret key').",
        where,
    )


class EncryptedTextField(models.TextField):
    """TextField transparently encrypted at rest. Values are never logged by the app."""

    def from_db_value(self, value, expression, connection):  # type: ignore[no-untyped-def]
        if value is None or value == "":
            return value
        try:
            return decrypt(value)
        except InvalidToken:
            model = getattr(self, "model", None)
            _report_unreadable(f"{model._meta.label}.{self.name}" if model is not None else str(self.name))
            return UnreadableSecret(value)

    def get_prep_value(self, value):  # type: ignore[no-untyped-def]
        if isinstance(value, UnreadableSecret):
            # keep the stored ciphertext as it is (see UnreadableSecret)
            return value.ciphertext or ""
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt(value)
