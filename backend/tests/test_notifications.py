import pytest

from apps.notifications.models import NotificationChannel
from apps.notifications.service import _payload, notify, validate_url


def test_validate_url_rules():
    assert validate_url("discord", "https://discord.com/api/webhooks/1/abc")
    for bad in ["http://discord.com/api/webhooks/1/a", "https://evil.com/api/webhooks/1/a", "https://discord.com/other"]:
        with pytest.raises(ValueError):
            validate_url("discord", bad)
    assert validate_url("webhook", "http://ntfy.lan/topic")
    with pytest.raises(ValueError):
        validate_url("webhook", "ftp://x")


def test_discord_payload_shape():
    ch = NotificationChannel(kind="discord", url="https://discord.com/api/webhooks/1/a")
    p = _payload(ch, "instance.failed", "Srv failed", "boom", {"server": "Srv", "status": "failed", "secret": "x"})
    emb = p["embeds"][0]
    assert emb["title"] == "Srv failed" and emb["color"] == 0xE5484D
    assert {f["name"] for f in emb["fields"]} == {"Server", "Status"}  # unknown keys are not leaked


@pytest.mark.django_db
def test_notify_filters_by_event_and_scope(monkeypatch):
    sent = []
    monkeypatch.setattr("apps.notifications.service.deliver.delay", lambda *a: sent.append(a))
    NotificationChannel.objects.create(name="all", kind="webhook", url="https://x.test/h", events=["backup.failed"])
    NotificationChannel.objects.create(
        name="off", kind="webhook", url="https://x.test/h", events=["backup.failed"], enabled=False
    )
    NotificationChannel.objects.create(name="other", kind="webhook", url="https://x.test/h", events=["instance.running"])
    assert notify("backup.failed", "t", "m") == 1
    assert notify("unknown.event", "t", "m") == 0
    assert len(sent) == 1
