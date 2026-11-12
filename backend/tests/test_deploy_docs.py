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
    # round 2: one network per server; the shared network is named for what it still is
    assert "### One network per server" in read("docs/SECURITY.md") and "`mcpanel_i_<first 12 hex digits" in security
    assert "A server stays on the shared `mcpanel_instances` network" in security
    assert "nothing on your network can take it over first" not in words("docs/UNRAID.md")
    assert "nothing outside the host can take it over first" in words("docs/UNRAID.md")
    games = read("docs/GAMES.md")
    assert "inside the panel's network" not in games and "inside the panel's Docker network" not in words("docs/GAMES.md")
    header = read("deploy/docker-compose.yml").split("name: mcpanel", 1)[0]
    assert "can reach the agent port" not in header and "only the token" not in header
    assert "The agent refuses every request" in header and "one bridge per game server" in header
    assert "reach each other's ports" in header  # what the shared network still allows


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


# --- A-2: the host firewall script ---------------------------------------------------------------------
def test_a2_the_firewall_script_is_documented_as_untested_with_its_checks_and_boot_line():
    unraid = words("docs/UNRAID.md")
    assert "### The game servers' firewall (`instance-firewall.sh`)" in read("docs/UNRAID.md")
    assert "**Not run on a real Unraid host yet.**" in unraid
    assert "instance-firewall.sh install --wait 300" in unraid and "install-hook.sh install" in unraid
    for needle in (
        "curl -m 5 http://<your Unraid IP>/` must fail",
        "curl -m 5 https://example.com/` must work",
        "players must still join",
        "bash instance-firewall.sh remove",
    ):
        assert needle in unraid, needle
    security = words("docs/SECURITY.md")
    assert "**It has not run on an Unraid host yet**" in security and "instance-firewall.sh" in security
    assert "instance-firewall.sh install --wait 300" in words("docs/UNRAID-SHUTDOWN.md")


# --- round 2: the docs of every fix ------------------------------------------------------------------------
def test_security_md_describes_the_fixes_of_both_rounds():
    text = read("docs/SECURITY.md")
    flat = words("docs/SECURITY.md")
    for heading in (
        "## Sessions bound to the tab",
        "### What users without `instance.configure` see",
        "### One network per server",
    ):
        assert heading in text, heading
    for needle in (
        "`X-MCPanel-Session-Token`",
        "401 `session_token_required`",
        "`POST /api/auth/download-ticket`",
        '`{"type": "auth", "token": ...}`',
        "`mcpanel_known_browser`",
        "`manage.py unlock_login <username>`",
        "10 in 10 minutes and 100 a day",
        "empty `setup.lock`",
        "409 `image_digest_mismatch`",
        "`AGENT_REFUSE_BRIDGE_GATEWAYS`",
        "`/v1/openapi.json`, `/docs` and `/redoc` answer 404",
        "**Project Zomboid runs as uid 1000 without capabilities**",
        "`/home/steam/server/rcon.yml`",
        "The quick stop without a save (15 s) needs all of",
        "`node.low_disk_space`",
        "the five upload endpoints",
        "**Operations that need a stopped server**",
        "a zip download holds at most 200,000 files (413 `archive_too_large`)",
    ):
        assert needle in flat, needle
    # statements the fixes made wrong
    for stale in (
        "`setup-code.txt` stays in the server folder",
        "**root**: the image never drops privileges",
        "login throttling (10/min),",
        "only the four upload endpoints",
    ):
        assert stale not in flat, stale


def test_unraid_md_games_md_and_deployment_md_follow_the_fixes():
    unraid = words("docs/UNRAID.md")
    assert "the others run as root with a few capabilities" not in unraid
    assert "Counter-Strike 2 and Project Zomboid run as uid 1000:1000 without any capability" in unraid
    assert "**Low disk space.**" in unraid
    games = words("docs/GAMES.md")
    for needle in (
        "acted on by their id",
        "**World import and reset:**",
        "(the image gets 15 s)",
        "**Copies** (Clone, Restore as a new server)",
        "`image_digest_mismatch`",
    ):
        assert needle in games, needle
    assert "the name of an online player works too" not in games and "an online player's name works too" not in games
    deployment = words("DEPLOYMENT.md")
    for needle in (
        "**After the upgrade to this version everyone signs in once.**",
        "### The upgrade with the security review",
        "`docker compose exec panel python manage.py unlock_login <username>`",
        "`MCPANEL_HSTS_SECONDS`",
    ):
        assert needle in deployment or needle in read("DEPLOYMENT.md"), needle
    assert "everyone signs in once" in words("docs/QUICKSTART-UNRAID.md")


def test_development_status_and_roadmap_record_the_review_and_what_is_open():
    status = words("DEVELOPMENT_STATUS.md")
    assert "## Security and bug review of 2026-11-11: fixes (branch `fix/security-review`" in read("DEVELOPMENT_STATUS.md")
    for needle in ("E-7", "TOTP", "`Instance.env` stored in clear text", "the firewall script (not run on a real host"):
        assert needle in status, needle
    assert "Security and bug review of 2026-11-11" in read("ROADMAP.md")


def test_the_create_wizards_who_can_join_texts_say_what_games_md_says():
    """frontend/src/lib/games.ts (WHO_CAN_JOIN) shows GAMES.md's table in the wizard's review step."""
    games = words("docs/GAMES.md").lower()
    wizard = " ".join((REPO / "frontend" / "src" / "lib" / "games.ts").read_text().split()).lower()
    for phrase in (
        "online mode is on and the whitelist is off",
        "the game password is optional",
        "the server password is empty by default",
        "unless you set a join password",
        "anyone can create an account",
        "its whitelist is off",
        "the panel offers no password or whitelist",
        "rust servers normally appear in the in-game server list",
    ):
        assert phrase in wizard, phrase
        assert phrase.replace("online mode is on and the whitelist is off", "online mode on, whitelist off") in games, phrase
