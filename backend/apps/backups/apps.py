from django.apps import AppConfig


class BackupsConfig(AppConfig):
    name = "apps.backups"
    label = "backups"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import handlers  # noqa: F401
