from django.urls import path

from .consumers import ConsoleConsumer, EventsConsumer

websocket_urlpatterns = [
    path("ws/instances/<uuid:instance_id>/console/", ConsoleConsumer.as_asgi()),
    path("ws/events/", EventsConsumer.as_asgi()),
]
