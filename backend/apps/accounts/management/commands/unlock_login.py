"""Lift the per-account limit on failed sign-ins (D-3) for one account, at once.

    docker compose exec panel python manage.py unlock_login <username>

After too many wrong passwords an account cannot sign in for a while (10 failures in 10 minutes, 100 in a day),
except from a browser that has signed in to it before. This clears the account's counters of failed sign-ins
and of wrong current passwords at a password change. The per-address limit (10 a minute) is not touched; it ends
by itself within a minute. The unlock is recorded in the audit log.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from apps.accounts import throttle
from apps.accounts.models import User


class Command(BaseCommand):
    help = "Clear the failed sign-in counters of one account (the per-account limit of the login)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("username", help="the user name as typed at the login (the limit ignores letter case)")

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.audit import service as audit

        name = str(options["username"])
        cleared = throttle.unlock("login", name)
        user = User.objects.filter(username=name).first()
        if user is not None:
            cleared += throttle.unlock("password", str(user.pk))
        audit.record("auth.login_unlocked", target=user, details={"username": name[:150]})
        if user is None:
            self.stdout.write(f"No account is called {name!r}; its failed sign-ins were cleared anyway.")
        self.stdout.write(f"Unlocked {name!r} ({cleared} limit counter{'' if cleared == 1 else 's'} cleared).")
