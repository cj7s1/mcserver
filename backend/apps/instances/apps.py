from django.apps import AppConfig


class InstancesConfig(AppConfig):
    name = "apps.instances"
    label = "instances"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from . import game_handlers, handlers  # noqa: F401  (registers job handlers)
