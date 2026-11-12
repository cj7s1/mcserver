"""deploy/scripts/generate-secrets.sh writes the first-run setup token (security review 2026-11-11, D-9).

Without MCPANEL_SETUP_TOKEN whoever reaches the panel first becomes its owner, and the script left
it empty. It now fills it in for the panel's .env (not for a node's) and says where it is, without
printing it.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "generate-secrets.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None or shutil.which("od") is None, reason="needs sh and od")


def generate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, check=False)


def values(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))


def test_the_panel_env_gets_a_setup_token_and_the_script_says_where_it_is(tmp_path):
    target = tmp_path / ".env"
    done = generate(str(target))
    assert done.returncode == 0, done.stderr
    token = values(target)["MCPANEL_SETUP_TOKEN"]
    assert re.fullmatch(r"[0-9a-f]{48}", token)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # where it is, and how to see it; the value itself is never printed
    assert f"grep '^MCPANEL_SETUP_TOKEN=' {target}" in done.stdout
    assert token not in done.stdout + done.stderr
    for key in ("MCPANEL_SECRET_KEY", "MCPANEL_AGENT_TOKEN", "POSTGRES_PASSWORD", "AGENT_RESTIC_PASSWORD"):
        assert values(target)[key] not in done.stdout + done.stderr, key
    # a second file gets another token
    other = tmp_path / "other.env"
    assert generate(str(other)).returncode == 0
    assert values(other)["MCPANEL_SETUP_TOKEN"] != token


def test_a_node_env_has_no_setup_token(tmp_path):
    target = tmp_path / "node.env"
    done = generate("--node", str(target))
    assert done.returncode == 0, done.stderr
    assert "MCPANEL_SETUP_TOKEN" not in target.read_text() and "MCPANEL_SETUP_TOKEN" not in done.stdout


def test_the_template_and_the_docs_agree():
    example = (REPO / "deploy" / ".env.example").read_text()
    assert example.count("\nMCPANEL_SETUP_TOKEN=\n") == 1  # the line the script fills in
    assert "generate-secrets.sh fills it in" in example
    quickstart = (REPO / "docs" / "QUICKSTART-UNRAID.md").read_text()
    assert "REPLACE-with-the-output-of-openssl" not in quickstart  # no hand-made token any more
    for path in ("docs/QUICKSTART-UNRAID.md", "DEPLOYMENT.md", "docs/UNRAID.md", "README.md"):
        assert "grep '^MCPANEL_SETUP_TOKEN=' .env" in (REPO / path).read_text(), path
