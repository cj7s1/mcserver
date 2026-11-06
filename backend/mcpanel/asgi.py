"""ASGI entry point: HTTP (Django) + WebSockets (Channels)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcpanel.settings")
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.console.routing import websocket_urlpatterns  # noqa: E402
from apps.core.middleware import WebSocketOriginValidator  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # Origin must match the panel's scheme://host:port (or CSRF_TRUSTED_ORIGINS), like
        # Django's CSRF check; Channels' AllowedHostsOriginValidator ignores scheme and port.
        "websocket": WebSocketOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
    }
)
