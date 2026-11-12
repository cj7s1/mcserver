"""The PHP of the Unraid plugin (deploy/unraid/plugin/src): `php -l` with the PHP of Unraid 7.3 (8.4) and the
page's endpoint against a stand-in for bin/mcpanel (tests/unraid_plugin_php/endpoint_test.php), and
mcpanel.plg read the way Unraid's plugin manager reads it (tests/unraid_plugin_php/plg_test.php).

Runs with a local PHP 8.4, or else in a throw-away container of the image php:8.4-cli-alpine when that image
is on this host (the test does not pull it: `docker pull php:8.4-cli-alpine` once). CI runs the same in its
docker job.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = "deploy/unraid/plugin/src"
IMAGE = "php:8.4-cli-alpine"  # Unraid 7.3.4 runs PHP 8.4.27
CONTAINER = "fixdeploy-php"
SCRIPT = f"""set -e
php -v | head -n 1
for f in {SRC}/include/common.php {SRC}/include/action.php backend/tests/unraid_plugin_php/*.php; do php -l "$f"; done
sed '1,/^---$/d' {SRC}/MCPanel.page >/tmp/MCPanel.page.php && php -l /tmp/MCPanel.page.php
php backend/tests/unraid_plugin_php/endpoint_test.php {SRC}
php backend/tests/unraid_plugin_php/plg_test.php deploy/unraid/plugin
"""


def php_runner() -> list[str] | None:
    php = shutil.which("php")
    if php:
        version = subprocess.run([php, "-r", "echo PHP_MAJOR_VERSION.'.'.PHP_MINOR_VERSION;"], capture_output=True, text=True)
        if version.stdout == "8.4":
            return ["sh", "-c", SCRIPT]
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "image", "inspect", IMAGE], capture_output=True).returncode == 0:
        return [
            docker,
            "run",
            "--rm",
            "--name",
            CONTAINER,
            "--network",
            "none",
            "-v",
            f"{REPO}:/repo:ro",
            "-w",
            "/repo",
            IMAGE,
            "sh",
            "-c",
            SCRIPT,
        ]
    return None


def test_the_plugins_php_lints_its_endpoint_checks_everything_and_the_plg_reads_as_the_sources():
    command = php_runner()
    if command is None:
        pytest.skip(f"needs PHP 8.4 or the image {IMAGE} (docker pull {IMAGE})")
    done = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PHP 8.4." in done.stdout
    assert done.stdout.count("No syntax errors detected") == 5
    assert "endpoint: " in done.stdout and " checks ok" in done.stdout
    assert "plg: 7 files as the sources" in done.stdout
