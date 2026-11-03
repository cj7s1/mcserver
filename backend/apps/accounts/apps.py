from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        def _seed(sender, **kwargs):  # type: ignore[no-untyped-def]
            from .views import ensure_system_roles

            ensure_system_roles()

        post_migrate.connect(_seed, sender=self, weak=False)
