"""Symmetric encryption for secrets stored in the database (RCON passwords, API keys).

Keys are derived from MCPANEL_SECRET_KEY. Rotating the secret key makes stored
secrets unreadable; the application treats undecryptable values as empty and
regenerates RCON passwords on the next container recreation.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models


def _fernet() -> Fernet:
    digest = hashlib.sha256(("mcpanel-field-encryption:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


class EncryptedTextField(models.TextField):
    """TextField transparently encrypted at rest. Values are never logged by the app."""

    def from_db_value(self, value, expression, connection):  # type: ignore[no-untyped-def]
        if value is None or value == "":
            return value
        return decrypt(value)

    def get_prep_value(self, value):  # type: ignore[no-untyped-def]
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        return encrypt(value)
