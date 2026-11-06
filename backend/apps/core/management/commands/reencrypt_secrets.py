"""Re-encrypt every stored secret with the current MCPANEL_SECRET_KEY.

    docker compose exec panel python manage.py reencrypt_secrets [--check]

Run it after moving the previous key to MCPANEL_SECRET_KEY_FALLBACKS; afterwards the
fallback can be removed. Secrets that no configured key can decrypt are counted and left
untouched (re-enter them in the panel). Values are never printed.
"""

from __future__ import annotations

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.crypto import EncryptedTextField, is_unreadable


class Command(BaseCommand):
    help = "Re-encrypt stored secrets with the current secret key (after a key rotation)"

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--check", action="store_true", help="only report, change nothing")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        readable = unreadable = 0
        for model in apps.get_models():
            fields = [f for f in model._meta.concrete_fields if isinstance(f, EncryptedTextField)]
            for field in fields:
                with transaction.atomic():
                    for pk, value in model._default_manager.select_for_update().values_list("pk", field.attname):
                        if value in (None, "") and not is_unreadable(value):
                            continue
                        if is_unreadable(value):
                            unreadable += 1
                            self.stderr.write(f"unreadable: {model._meta.label}.{field.name} (pk={pk})")
                            continue
                        readable += 1
                        if not opts["check"]:
                            # update() runs get_prep_value, i.e. encrypts with the current key
                            model._default_manager.filter(pk=pk).update(**{field.attname: value})
        verb = "Readable" if opts["check"] else "Re-encrypted"
        self.stdout.write(f"{verb}: {readable} secret(s). Unreadable: {unreadable}.")
        if unreadable:
            self.stdout.write(
                self.style.WARNING(
                    "Unreadable secrets were left as they are. Add the key they were encrypted with to "
                    "MCPANEL_SECRET_KEY_FALLBACKS, or re-enter them in the panel."
                )
            )
