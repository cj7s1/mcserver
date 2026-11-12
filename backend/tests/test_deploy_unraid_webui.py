"""The panel in Unraid's Docker tab: a WebUI entry and an icon (deploy/docker-compose.yml labels).

Unraid's Docker tab reads `net.unraid.docker.webui` and `net.unraid.docker.icon` from any container
(dynamix.docker.manager, DockerClient.php) and fills in `[IP]` and `[PORT:<container port>]`. The icon
is fetched by the Unraid host itself, so the panel serves it: `frontend/public/unraid-icon.png`, a file of
the build, without a login. `net.unraid.docker.managed` must never be set: Unraid decides which containers
are its own by searching `docker ps` for `net.unraid.docker.managed=` and stops those itself at array stop,
before the shutdown hook (docs/UNRAID-SHUTDOWN.md, docs/SECURITY.md).
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
import yaml
from django.test import Client, override_settings

REPO = Path(__file__).resolve().parents[2]
ICON = REPO / "frontend" / "public" / "unraid-icon.png"


def compose() -> dict:
    return yaml.safe_load((REPO / "deploy" / "docker-compose.yml").read_text())


def test_the_panel_carries_the_webui_and_icon_labels_and_nothing_is_managed_by_unraid():
    services = compose()["services"]
    labels = services["panel"]["labels"]
    assert labels == {
        "net.unraid.docker.webui": "http://[IP]:[PORT:8000]/",
        "net.unraid.docker.icon": "http://${MCPANEL_BIND:-127.0.0.1}:${MCPANEL_PORT:-8080}/unraid-icon.png",
    }
    # [PORT:8000] is the container port the panel publishes
    assert any(str(port).endswith(":8000") for port in services["panel"]["ports"])
    for name, service in services.items():
        text = " ".join(f"{key}={value}" for key, value in (service.get("labels") or {}).items())
        assert "net.unraid.docker.managed" not in text, name
        if name != "panel":
            assert "net.unraid.docker" not in text, name  # the other platform containers get no labels
    node = yaml.safe_load((REPO / "deploy" / "docker-compose.node.yml").read_text())
    assert "net.unraid.docker.managed" not in str(node)


def test_the_icon_is_a_square_png_made_from_the_favicon():
    data = ICON.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height and 128 <= width <= 512
    assert len(data) < 200_000


@pytest.mark.django_db
def test_the_panel_serves_the_icon_without_a_login(tmp_path):
    """WhiteNoise serves the files of the frontend build (Vite copies `public/` to its root)."""
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "unraid-icon.png").write_bytes(ICON.read_bytes())
    with override_settings(WHITENOISE_ROOT=tmp_path, FRONTEND_DIST=tmp_path):
        answer = Client().get("/unraid-icon.png")
        assert answer.status_code == 200
        assert answer["Content-Type"] == "image/png"
        assert b"".join(answer.streaming_content if answer.streaming else [answer.content]) == ICON.read_bytes()


def test_the_docs_say_where_the_webui_entry_is():
    unraid = " ".join((REPO / "docs" / "UNRAID.md").read_text().split())
    assert "clicking it offers **WebUI**" in unraid and "`net.unraid.docker.managed`" in unraid
    quickstart = " ".join((REPO / "docs" / "QUICKSTART-UNRAID.md").read_text().split())
    assert "click the `mcpanel-panel-1` container and choose **WebUI**" in quickstart
