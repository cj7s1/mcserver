"""ASGI entry point: HTTP (Django) + WebSockets (Channels).

Imported only by the web server (uvicorn), so this is also where the process says that it is the
web process (``MCPANEL_PROCESS_ROLE``; settings.py turns persistent database connections off for
it). HTTP requests pass the request body limits (apps.core.asgi.RequestBodyLimit) before
Django reads anything.
"""

import os

import django
from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcpanel.settings")
if not settings.configured:  # not when a test (or a shell) imports this module after loading the settings
    os.environ.setdefault("MCPANEL_PROCESS_ROLE", "web")

django.setup(set_prefix=False)

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.console.routing import websocket_urlpatterns  # noqa: E402
from apps.core.asgi import PanelASGIHandler, RequestBodyLimit  # noqa: E402
from apps.core.middleware import WebSocketOriginValidator  # noqa: E402
from apps.core.uploads import ensure_upload_dir  # noqa: E402

# the spool folder of request bodies and uploads, made by this process's user (never at import of
# the settings, which the image build does as root); /api/health reports it while it is unusable
ensure_upload_dir()

django_asgi_app = PanelASGIHandler()

application = ProtocolTypeRouter(
    {
        "http": RequestBodyLimit(django_asgi_app),
        # Origin must match the panel's scheme://host:port (or CSRF_TRUSTED_ORIGINS), like
        # Django's CSRF check; Channels' AllowedHostsOriginValidator ignores scheme and port.
        "websocket": WebSocketOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    }
)
