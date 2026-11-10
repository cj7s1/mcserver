"""The upload spool folder (review of 2026-11-10, real-stack finding 23): importing the settings
creates nothing (the image build imports them as root), the web process makes the folder as its
own user, and /api/health fails while it is not writable."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from django.test.utils import override_settings

from .test_security_audit_http import _run_entrypoint

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def _import_in_subprocess(statement: str, upload_dir: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "MCPANEL_PROCESS_ROLE"}
    env.update(MCPANEL_TESTING="1", MCPANEL_UPLOAD_TMP=str(upload_dir), DJANGO_SETTINGS_MODULE="mcpanel.settings")
    code = f"import django\n{statement}\n"
    subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, check=True)


def test_importing_the_settings_creates_nothing(tmp_path):
    target = tmp_path / "uploads"
    # what collectstatic does during the image build (as root)
    _import_in_subprocess("django.setup()\nfrom django.conf import settings\nassert settings.FILE_UPLOAD_TEMP_DIR", target)
    assert not target.exists()


def test_the_web_process_makes_the_folder_as_its_own_user(tmp_path):
    target = tmp_path / "uploads"
    _import_in_subprocess("import mcpanel.asgi", target)
    info = target.stat()
    assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.getuid()


@pytest.mark.django_db
def test_health_fails_while_the_upload_folder_is_not_usable(client, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    with override_settings(FILE_UPLOAD_TEMP_DIR=str(blocker / "uploads")):  # cannot exist, also not for root
        from apps.core.uploads import ensure_upload_dir

        assert "is not writable" in (ensure_upload_dir() or "")
        resp = client.get("/api/health")
        assert resp.status_code == 503 and resp.json()["checks"]["uploads"] == "error"
    resp = client.get("/api/health")
    assert resp.json()["checks"]["uploads"] == "ok"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
# Every mode, not only the web server: `manage migrate --plan` (upgrade.sh) and `migrate` run
# Django's system checks, which refuse a missing FILE_UPLOAD_TEMP_DIR (files.E001).
@pytest.mark.parametrize("mode", [("web",), ("serve",), ("migrate",), ("manage", "check"), ("worker",), ("beat",)])
def test_the_entrypoint_makes_the_folder_and_says_when_it_cannot(tmp_path, mode):
    target = tmp_path / "spool" / "uploads"
    out = _run_entrypoint(tmp_path, *mode, MCPANEL_UPLOAD_TMP=str(target))
    assert out.returncode == 0, out.stderr
    assert target.is_dir() and stat.S_IMODE(target.stat().st_mode) == 0o700
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    out = _run_entrypoint(tmp_path, *mode, MCPANEL_UPLOAD_TMP=str(blocker / "uploads"))
    assert "is not writable" in out.stderr


def test_the_image_build_refuses_a_spool_folder_made_as_root():
    dockerfile = (BACKEND / "Dockerfile").read_text()
    assert "python manage.py collectstatic" in dockerfile
    assert 'if [ -e /tmp/mcpanel-uploads ]; then echo "the build created /tmp/mcpanel-uploads as root"' in dockerfile


def test_the_acceptance_script_uploads_more_than_the_memory_limit():
    script = (REPO / ".github" / "scripts" / "panel-acceptance.sh").read_text()
    assert "upload of 10 MB" in script
