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
    # the two scripts that run unattended repair it themselves (as root, in the checkout) and say so
    assert "were loose and have been tightened" in security and "check 'git status' there" in security
    for path in ("docs/QUICKSTART-UNRAID.md", "docs/UNRAID.md", "docs/UNRAID-SHUTDOWN.md", "DEPLOYMENT.md"):
        text = words(path)
        assert "themselves" in text and "syslog" in text, path


# --- A-1, A-9: what the network isolates, who can join -------------------------------------------------
def test_a1_no_document_claims_the_game_servers_are_isolated_from_each_other():
    security = words("docs/SECURITY.md")
    assert "only the agent reaches them on the container network" not in security
    assert "**which every game server shares**" in security
    assert "nothing on your network can take it over first" not in words("docs/UNRAID.md")
    assert "nothing outside the host can take it over first" in words("docs/UNRAID.md")
    games = read("docs/GAMES.md")
    assert "inside the panel's network" not in games and "inside the panel's Docker network" not in words("docs/GAMES.md")
    header = read("deploy/docker-compose.yml").split("name: mcpanel", 1)[0]
    assert "can reach the agent port" not in header and "only the token" not in header
    assert "The agent refuses every request" in header and "reach each" in header


def test_a9_who_can_join_and_what_a_compromised_server_reaches_are_documented():
    games = read("docs/GAMES.md")
    table = games.split("## Who can join by default", 1)[1].split("\n## ", 1)[0]
    for game in (
        "Minecraft",
        "Factorio",
        "Satisfactory",
        "Terraria, tModLoader, TShock",
        "Palworld",
        "Valheim",
        "Project Zomboid",
        "HKMP",
        "Counter-Strike 2",
        "Rust",
    ):
        assert f"| {game} |" in table, game
    assert "**anyone can create an account**" in table and "no password or whitelist" in table
    readme = words("README.md")
    assert '"Who can join by default"' in readme and '"What a compromised game server can reach"' in readme
    security = read("docs/SECURITY.md")
    assert "### What a compromised game server can reach" in security
    assert "### Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2, Rust" in security
    flat = words("docs/SECURITY.md")
    for needle in (
        "`-adminpassword`",
        "VPN interfaces (Tailscale, WireGuard) included",
        "Rust's WebRCON (28016)",
        "The Docker host on every address:",
        "The LAN and the internet",
    ):
        assert needle in flat, needle
