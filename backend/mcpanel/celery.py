import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcpanel.settings")

app = Celery("mcpanel")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks(["apps.instances", "apps.jobs", "apps.backups", "apps.content", "apps.metrics"])
app.conf.imports = ("apps.jobs.runner", "apps.notifications.service")
