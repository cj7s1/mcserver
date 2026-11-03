from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "apps.audit"
    label = "audit"
    default_auto_field = "django.db.models.BigAutoField"
