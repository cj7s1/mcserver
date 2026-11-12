# MCPanel

> **New here?** Follow the illustrated [Unraid quickstart](docs/QUICKSTART-UNRAID.md): install, then a
> Better MC modpack server your friends can join.


A self-hosted Minecraft **Java Edition** server manager for a Docker host (more hosts can be added as
nodes), which also runs **Factorio**, **Satisfactory**, **Terraria**, **Palworld**, **Valheim**, **Project Zomboid**,
**Hollow Knight (HKMP)**, **Counter-Strike 2** and **Rust** servers. It's designed for Unraid first and also runs on any Linux Docker host. It combines
Prism-Launcher-style instance management with game-panel administration, and it is built around
**easy modpack servers**: search for *Better MC*, pick a release, choose RAM and a port, and deploy.

> Status: active development. See [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) for exactly what has
> been verified and how, and [ROADMAP.md](ROADMAP.md) for what's next.

## Features

- **Games:** Minecraft (Java edition) and Factorio (headless, pinned exact versions, Space Age
  chosen once, UDP port, game settings form, saves: download, import, new map with a seed). Each
  game is a reviewed definition in the panel plus a runtime profile in the agent; the UI shows only
  what a game supports.
- **Satisfactory:** dedicated server on the stable branch (the game is downloaded on the first start
  and updated at every start), claimed by the panel through its API before any port is published,
  an explicit choice between a join password and an open server, player count, saves through the
  server's API (new game, import and download of `.sav` files), server options and launch settings,
  backups without the 2.8 GB game files. 8 GB of memory at least.
- **Terraria:** three variants, each pinned to an exact image tag: the official server (vanilla),
  tModLoader (built on Terraria 1.4.4.9; its players need the tModLoader client) and TShock
  (accounts and permissions; it runs with no Linux capability, and the panel creates its in-game
  owner account over TShock's REST API, so no `/setup` code is needed). TCP port 7777 by default,
  console commands, online players (kick; ban and unban by IP for vanilla/tModLoader), consistent
  backups, `serverconfig.txt` (and TShock's `config.json`) as a form, worlds: download, import of a
  `.wld`, new world with size, difficulty and seed.
- **Palworld, Valheim, Project Zomboid, Hollow Knight (HKMP), Counter-Strike 2, Rust:** dedicated servers
  on pinned community images that download the game from Steam and follow its public branch (no version
  to pick; "update" means restart), settings as forms, ports published with the number the game
  advertises, online players and the actions each game has, worlds as folders (download, import of a
  zip, reset; Rust: a map wipe with a new seed). See [docs/GAMES.md](docs/GAMES.md) for ports, disk,
  memory and the rules of each game (Valheim's password, CS2's Game Server Login Token, PZ's admin login).
- **Servers:** Vanilla, Paper, Purpur, Folia, Fabric, Quilt, Forge, NeoForge. Versions come from the
  official metadata services, and Java is picked automatically from Mojang's requirements.
- **Modpacks:** Modrinth, CurseForge (with or without an API key) and Feed The Beast. Releases are
  **pinned**: restarts never upgrade silently. The panel checks for newer compatible releases every 6 h
  ("Update available" badge, optional notification); updates are explicit, take a pre-update backup, and
  can be rolled back.
- **Lifecycle:** install, start, stop, restart, kill, recreate (keeps world data), clone, import
  an existing server from an archive, archive, and delete with typed confirmation. Status comes from real
  health checks. Crash loops are detected and stopped, and a diagnosis names the likely cause (missing
  mods, wrong Java version, out of memory and so on).
- **Stopping safely:** Stop and Restart save the world first (`save-all flush`) and then stop the
  server with its own timeout. A stop during start-up still arrives. A stopped server has no
  container: the panel removes it after the stop, keeps the last 2,000 console lines, and creates a
  new one at the next start.
- **Worlds:** replace the world from an archive or export it as a zip; reset the world so that the
  server generates a new one (a safety backup is taken and checked first).
- **Console:** live log streaming and RCON commands over WebSockets, with history and filtering.
  Typing `stop` stops the server the way the Stop button does.
- **Files:** a web file manager and editor that is confined to the server folder and never follows
  symlinks, with uploads/downloads, search, and a typed `server.properties` editor.
- **Backups:** restic-based (encrypted, deduplicated) with coordinated `save-off` / `save-all` while
  running. Includes schedules, retention, integrity checks, restore in place (with a safety backup),
  restore as a new server, and a backup browser for single-file restores. New servers get a nightly
  backup schedule unless you switch it off, and a server without a recent backup says so ("No
  backup yet", "Last backup N days ago"). On Unraid a User Scripts job copies the panel database off
  the cache drive every day (`deploy/unraid/backup-db.sh`), and `manage.py adopt_backups` takes the
  backups on the array over after a restore of that database.
- **Snapshots:** instant snapshots and rollback when the data directory is on a btrfs pool, with the
  default stack (no extra privileges). ZFS is implemented and has not run on a real pool yet.
- **Mods/plugins:** a manager for Modrinth, Hangar and SpigotMC (through Spiget) with hash
  identification, update checks, compatible search and JAR upload.
- **Players:** whitelist, operators, bans, kick, and online and historical player lists.
- **Multi-user:** a permission matrix with built-in and custom roles, per-server access grants, and an
  audit log.
- **Friends:** a Share card per server (the address, the ports, the join steps of that game, a ready
  invite text), stop when empty, a Discord bot your friends start and stop servers with (outbound only),
  and an optional Friend role with invite links: [docs/FRIENDS.md](docs/FRIENDS.md). New and not tried
  with real players or a real Discord server yet.
- **Automation:** cron schedules (restart/stop/backup/snapshot/command) that wait for a busy server
  instead of being dropped and do not fire late power actions after downtime, in-game countdown
  warnings before restarts and stops (manual or scheduled, cancellable), Discord/webhook
  notifications, and resource history.
- **Memory check:** a start that would commit more than the host's RAM minus a reserve is refused
  with the numbers (an administrator can start it anyway).
- **Keyboard:** a command palette (<kbd>Ctrl</kbd>+<kbd>K</kbd> / <kbd>⌘</kbd>+<kbd>K</kbd>) to jump to any
  page or server and start/stop/restart servers (stop and restart always ask first), plus shortcuts such as
  `g s` (servers) and `c` (create server); press `?` for the list.
- **Operations:** three containers (panel, agent, database). `deploy/scripts/upgrade.sh` upgrades
  the stack with a database dump and a rollback point, and rolls it back. On Unraid a hook stops
  the servers through the panel before Docker stops and the panel starts them again after boot,
  one per minute ([docs/UNRAID-SHUTDOWN.md](docs/UNRAID-SHUTDOWN.md)).
- **More hosts:** additional Docker hosts run only the agent and are managed from the same panel
  over HTTPS with a pinned certificate.

What has run where: the panel has been in use on the owner's Unraid server since 2026-11-07 in an
earlier version. The three-container layout, the upgrade script, stopped servers without a
container, the stop safety, the shutdown hook, the backup safeguards and Reset world were tested
on a Linux desktop with real Docker and real Minecraft servers and have not run on Unraid yet. The
fixes of the audit round of 2026-11-09 (memory check, schedules, database backup, uploads and
more) are unit-tested only.

## Architecture in one paragraph

A Django/Channels **panel** (API + WebSockets + React SPA) and its Celery **workers** never touch Docker;
together with the scheduler and Redis they are the processes of one unprivileged `panel` container. A
separate **node agent** (FastAPI) is the only component with the Docker socket, and PostgreSQL is the
third container. The agent is reachable only
on the internal network, requires a bearer token, and independently enforces a strict container policy
(an image allow-list, no privileged mode, dropped capabilities, only the server folder mounted). Minecraft
runs in [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server) containers. Details are
in [ARCHITECTURE.md](ARCHITECTURE.md); the security model is in [docs/SECURITY.md](docs/SECURITY.md).

## Quick start (Docker Compose)

```bash
(umask 022 && git clone https://github.com/cj7s1/mcserver.git) && cd mcserver/deploy
./scripts/generate-secrets.sh          # creates .env with random secrets
nano .env                              # set MCPANEL_DATA_DIR, MCPANEL_BIND (LAN IP), allowed hosts
docker compose up -d
```

The first start builds the two images (it needs BuildKit: `docker buildx version`). Then open
`http://<MCPANEL_BIND>:8080`, create the owner account (the setup page asks for the setup token that
`generate-secrets.sh` wrote: `grep '^MCPANEL_SETUP_TOKEN=' .env`), and create your first server.
For Unraid specifics (paths, UID/GID 99:100, the Docker tab, pools vs user shares), see
[docs/UNRAID.md](docs/UNRAID.md). For general deployment, upgrades and reverse proxies, see
[DEPLOYMENT.md](DEPLOYMENT.md).

To upgrade later: `(umask 022 && git pull)`, then in `deploy/`: `./scripts/upgrade.sh dry-run`, then the
`run` command it prints. It refuses a checkout that group or others can write and prints the repair
(`chmod -R go-w <checkout>`).

> Modded servers run third-party code. Docker raises the bar but is not a strong sandbox, so only install
> content you trust. Never expose the panel to the internet without HTTPS, and preferably use a VPN.

> **Sharing a server with friends:** [docs/FRIENDS.md](docs/FRIENDS.md). **Before you forward a game's port**, check who can join: Terraria, Palworld, HKMP and
> Counter-Strike 2 let anyone in until you set a password, Rust has no password or whitelist in the
> panel, and Project Zomboid lets anyone create an account
> ([docs/GAMES.md](docs/GAMES.md), "Who can join by default"). Every server has a network of its
> own, but a compromised one can still reach the host and your LAN until you install
> `deploy/unraid/instance-firewall.sh` ([docs/UNRAID.md](docs/UNRAID.md), "The game servers' firewall";
> [docs/SECURITY.md](docs/SECURITY.md), "What a compromised game server can reach").

## Development

See [CLAUDE.md](CLAUDE.md) for repository layout and commands, and [TESTING.md](TESTING.md) for the test
suites.

## License

Not yet chosen by the repository owner. Minecraft is a trademark of Mojang/Microsoft; this project isn't
affiliated with them. Running a server requires accepting the [Minecraft EULA](https://aka.ms/MinecraftEULA).
