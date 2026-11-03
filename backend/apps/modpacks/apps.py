from django.apps import AppConfig


class ModpacksConfig(AppConfig):
    name = "apps.modpacks"
    label = "modpacks"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import handlers  # noqa: F401  (registers modpack_update / modpack_rollback jobs)
