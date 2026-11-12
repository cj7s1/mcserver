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
    assert "### One network per server" in read("docs/SECURITY.md") and "`zz_mcpanel_i_<first 12 hex digits" in security
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
    # put back at every array start by array-start.sh, only where the owner installed it
    assert "deploy/unraid/array-start.sh" in unraid and "only where you had" in unraid
    for needle in (
        "curl -m 5 http://<your Unraid IP>/` must fail",
        "curl -m 5 https://example.com/` must work",
        "players must still join",
        "bash instance-firewall.sh remove",
    ):
        assert needle in unraid, needle
    security = words("docs/SECURITY.md")
    assert "**It has not run on an Unraid host yet**" in security and "instance-firewall.sh" in security
    assert "bash /mnt/user/appdata/mcpanel-src/deploy/unraid/array-start.sh" in words("docs/UNRAID-SHUTDOWN.md")


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
        "a zip download is built while it streams after its size and depth were checked",
        "500 `clone_incomplete`",
        "only when no pinned image exists does creating it fail with 409 `image_digest_mismatch`",
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
    for needle in (
        "E-7 / R-6: a first-start wait holds a job worker process",
        "CS2's and Rust's readiness probes have not met a real server yet",
        "Read-only root filesystems",
        "TOTP / a second factor",
        "`Instance.env` (custom variables) is stored in clear text",
        "has not run on Unraid",
        "Verified on a real container (2026-11-11, branch `legacy41`): a Docker-initiated stop of Project Zomboid",
    ):
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


# --- the end-to-end run of 2026-11-12 (FX-1, FX-2, FX-5) --------------------------------------------------
def test_preserve_user_defined_networks_is_required_and_the_recoveries_are_named():
    for path in ("docs/QUICKSTART-UNRAID.md", "docs/UNRAID.md", "docs/SECURITY.md"):
        text = words(path)
        assert "Preserve user defined networks" in text, path
        assert 'unless "Preserve user defined networks" is on)' not in text, path
    assert '**"Preserve user defined networks" (Settings -> Docker) must be on**' in words("docs/SECURITY.md")
    for path in ("docs/UNRAID.md", "DEPLOYMENT.md"):
        text = words(path)
        assert "docker compose up -d --force-recreate agent" in text and "network ... not found" in text, path
        assert "exit code 128" in text and "Start" in text, path


def test_the_firewall_docs_say_the_agent_reaches_no_lan_address_either():
    security = words("docs/SECURITY.md")
    assert "**The agent itself cannot open new connections to LAN addresses either**" in security
    assert "`mcpanel_instances` sorts before `mcpanel_internal`" in security
    assert "The server networks never take that route (the agent joins them with gateway priority -1000)" in security
    assert "The agent's own traffic is not affected" not in security
    assert "The agent itself cannot open new connections to LAN addresses either" in words("docs/UNRAID.md")


def test_fx1_the_agent_is_on_a_server_network_only_while_the_server_runs():
    security = words("docs/SECURITY.md")
    assert "but only while that server runs" in security and "at its own shutdown" in security
    assert "Only a hard kill of the agent while servers run leaves such a network" in security
    header = read("deploy/docker-compose.yml").split("name: mcpanel", 1)[0]
    assert "the agent joins each while its server" in header and "leaves it when the server stops" in header
    for path in ("docs/UNRAID.md", "DEPLOYMENT.md"):
        assert "killed hard while servers ran" in words(path), path
    assert "only while its server runs" in read("CLAUDE.md")


# --- boot-network repair: the agent repairs containers that lost their network ---------------------------
def test_the_agents_network_repair_is_documented_with_its_scope_and_limits():
    security = words("docs/SECURITY.md")
    assert "### The agent repairs containers that lost their network" in read("docs/SECURITY.md")
    for needle in (
        "`AGENT_REPAIR_PLATFORM_NETWORKS`, default `true`",
        "never a container outside the project",
        "one attempt per container per 5 minutes",
        "after 3 failed or undone repairs it gives up",
        "`deploy/unraid/array-start.sh` (Unraid, at every array start) is the fallback",
    ):
        assert needle in security, needle
    unraid = words("docs/UNRAID.md")
    assert "the script's repair is the fallback for when the agent does not run" in unraid
    assert "a game server that lost its network is repaired at its next start" in unraid
    assert "`docker compose up -d --force-recreate agent`" in unraid
    assert "platformnet.py" in read("CLAUDE.md") and "AGENT_REPAIR_PLATFORM_NETWORKS" in words("DEVELOPMENT_STATUS.md")
    assert "AGENT_REPAIR_PLATFORM_NETWORKS" in read("deploy/unraid/array-start.sh")


# --- the Unraid plugin (feature plan 2026-11-12, section A) ---------------------------------------------------
PLUGIN_URL_FORM = "https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/unraid/plugin/mcpanel.plg"


def test_the_plugin_is_the_recommended_way_on_unraid_and_says_it_is_untested():
    unraid = words("docs/UNRAID.md")
    assert "### The Unraid plugin (recommended)" in read("docs/UNRAID.md")
    assert "### Without the plugin: the User Script `array-start.sh`" in read("docs/UNRAID.md")
    for needle in (
        "**The plugin has not been run on Unraid yet.**",
        f"Plugins → Install Plugin**, and paste the raw address of `deploy/unraid/plugin/mcpanel.plg` of the repository you cloned, `{PLUGIN_URL_FORM}`",
        "**Every button that changes something first shows the exact commands it will run** and asks",
        "**First-run checklist** (what only a real Unraid server can show)",
        "**and read before you press anything**",
        "**After the next reboot:**",
        "`plugin: boot tasks finished: ok`",
        "whether Unraid fires the Docker events when Docker is switched off and on",
        "**Removing it:** Plugins → Installed Plugins → MCPanel → Remove",
        "**Plugins → Check for updates**",
        "never reads `deploy/.env`",
        "**v5.7.0**",
    ):
        assert needle in unraid, needle
    quick = words("docs/QUICKSTART-UNRAID.md")
    assert "### Install the plugin (recommended)" in read("docs/QUICKSTART-UNRAID.md")
    for needle in (
        PLUGIN_URL_FORM,
        "**It has not been run on Unraid yet.**",
        "**read it before you press anything**",
        "`plugin: boot tasks finished: ok`",
        "**Plugins → Installed Plugins → MCPanel → Remove**",
        '[UNRAID.md](UNRAID.md), "The Unraid plugin"',
    ):
        assert needle in quick, needle
    # the repository's own address is only in the build script and the built file
    for path in ("docs/UNRAID.md", "docs/QUICKSTART-UNRAID.md", "DEPLOYMENT.md", "docs/SECURITY.md"):
        assert "raw.githubusercontent.com/cj7s1" not in read(path), path


def test_the_plugin_is_in_security_deployment_status_roadmap_and_the_map():
    security = words("docs/SECURITY.md")
    assert "## The Unraid plugin" in read("docs/SECURITY.md")
    for needle in (
        "Unraid's `local_prepend.php` ends every POST without the session's CSRF token",
        "never through a shell",
        "Unraid's `StartCommand.php` runs any file there with the arguments of a POST",
        "within 12 hours, once",
        "checked against the pin again before every use",
        "every file of the plugin has a `Mode`",
    ):
        assert needle in security, needle
    deployment = words("DEPLOYMENT.md")
    assert "On Unraid the MCPanel plugin's page does the same" in deployment
    assert "the MCPanel plugin's **Install Compose**" in deployment
    status = words("DEVELOPMENT_STATUS.md")
    assert "## Unraid plugin `mcpanel` (branch `feature/unraid-and-friends`" in read("DEVELOPMENT_STATUS.md")
    assert "**Not run on Unraid yet.**" in status
    assert "Unraid plugin `mcpanel`" in words("ROADMAP.md") and "[ ] Run on Unraid" in words("ROADMAP.md")
    claude = read("CLAUDE.md")
    assert "deploy/unraid/plugin/src/" in claude and "tests/test_deploy_unraid_plugin.py" in claude
    assert "The MCPanel plugin (recommended)" in words("docs/UNRAID-SHUTDOWN.md")
