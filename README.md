# MCPanel

> **New here?** Follow the illustrated [Unraid quickstart](docs/QUICKSTART-UNRAID.md): install, then a
> Better MC modpack server your friends can join.


A self-hosted Minecraft **Java Edition** server manager for a Docker host (more hosts can be added as
nodes). It's designed for Unraid first and also runs on any Linux Docker host. It combines
Prism-Launcher-style instance management with game-panel administration, and it is built around
**easy modpack servers**: search for *Better MC*, pick a release, choose RAM and a port, and deploy.

> Status: active development. See [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) for exactly what has
> been verified and how, and [ROADMAP.md](ROADMAP.md) for what's next.

## Features

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
- **Files:** a web file manager and editor that is confined to the server folder and never follows
  symlinks, with uploads/downloads, search, and a typed `server.properties` editor.
- **Backups:** restic-based (encrypted, deduplicated) with coordinated `save-off` / `save-all` while
  running. Includes schedules, retention, integrity checks, restore in place (with a safety backup),
  restore as a new server, and a backup browser for single-file restores. New servers get a nightly
  backup schedule unless you switch it off, and a server without a recent backup says so ("No
  backup yet", "Last backup N days ago").
- **Snapshots:** instant snapshots and rollback when the data directory is on a btrfs pool, with the
  default stack (no extra privileges). ZFS is implemented and has not run on a real pool yet.
- **Mods/plugins:** a manager for Modrinth, Hangar and SpigotMC (through Spiget) with hash
  identification, update checks, compatible search and JAR upload.
- **Players:** whitelist, operators, bans, kick, and online and historical player lists.
- **Multi-user:** a permission matrix with built-in and custom roles, per-server access grants, and an
  audit log.
- **Automation:** cron schedules (restart/stop/backup/snapshot/command), in-game countdown warnings
  before restarts and stops (manual or scheduled, cancellable), Discord/webhook notifications, and
  resource history.
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
on a Linux desktop with real Docker and real Minecraft servers and have not run on Unraid yet.

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
git clone https://github.com/cj7s1/mcserver.git && cd mcserver/deploy
./scripts/generate-secrets.sh          # creates .env with random secrets
nano .env                              # set MCPANEL_DATA_DIR, MCPANEL_BIND (LAN IP), allowed hosts
docker compose up -d
```

The first start builds the two images (it needs BuildKit: `docker buildx version`). Then open
`http://<MCPANEL_BIND>:8080`, create the owner account, and create your first server.
For Unraid specifics (paths, UID/GID 99:100, the Docker tab, pools vs user shares), see
[docs/UNRAID.md](docs/UNRAID.md). For general deployment, upgrades and reverse proxies, see
[DEPLOYMENT.md](DEPLOYMENT.md).

To upgrade later, in `deploy/`: `./scripts/upgrade.sh dry-run`, then the `run` command it prints.

> Modded servers run third-party code. Docker raises the bar but is not a strong sandbox, so only install
> content you trust. Never expose the panel to the internet without HTTPS, and preferably use a VPN.

## Development

See [CLAUDE.md](CLAUDE.md) for repository layout and commands, and [TESTING.md](TESTING.md) for the test
suites.

## License

Not yet chosen by the repository owner. Minecraft is a trademark of Mojang/Microsoft; this project isn't
affiliated with them. Running a server requires accepting the [Minecraft EULA](https://aka.ms/MinecraftEULA).
