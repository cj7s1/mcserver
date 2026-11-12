"""What the documentation says about deploying, against the security review of 2026-11-11.

Each test names the finding whose statement it keeps true (security-review/<slice>/FINDINGS.md next
to the checkout).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (REPO / path).read_text()


def words(path: str) -> str:
    """The text with every run of white space as one blank (statements wrap at any word)."""
    return " ".join(read(path).split())


# --- D-2: root runs scripts from the checkout -----------------------------------------------------------
def test_d2_the_checkout_is_cloned_pulled_and_repaired_so_that_only_root_can_change_it():
    for path in ("docs/QUICKSTART-UNRAID.md", "docs/UNRAID.md"):
        text = read(path)
        assert "(umask 022 && git clone" in text and "chmod -R go-w mcpanel-src" in text, path
        assert "cd /mnt/user/appdata/mcpanel-src && (umask 022 && git pull)" in text, path
        assert "&& git pull\n" not in text, path  # every update command pulls with umask 022
    deployment = read("DEPLOYMENT.md")
    assert "(umask 022 && git clone" in deployment and "cd mcserver && (umask 022 && git pull)" in deployment
    assert "chmod -R go-w <checkout>" in deployment
    security = words("docs/SECURITY.md")
    assert "## The source folder" in read("docs/SECURITY.md")
    for needle in ("chmod -R go-w <checkout>", "-c core.fsmonitor= -c core.hooksPath=/dev/null", "(umask 022 && git pull)"):
        assert needle in security, needle
    # what the check cannot cover is said, too
    assert "can rename the whole checkout and put another one in its place" in security
    assert "chmod -R go-w" in read("docs/UNRAID-SHUTDOWN.md")
