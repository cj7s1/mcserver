# MCPanel

> **New here?** Follow the illustrated [Unraid quickstart](docs/QUICKSTART-UNRAID.md): install, then a
> Better MC modpack server your friends can join.


A self-hosted Minecraft **Java Edition** server manager for a single Docker host. It's designed for
Unraid first and also runs on any Linux Docker host. It combines Prism-Launcher-style instance management
with game-panel administration, and it is built around **easy modpack servers**: search for *Better MC*,
pick a release, choose RAM and a port, and deploy.

> Status: active development. See [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) for exactly what has
> been verified and how, and [ROADMAP.md](ROADMAP.md) for what's next.

## Features

- **Servers:** Vanilla, Paper, Purpur, Folia, Fabric, Quilt, Forge, NeoForge. Versions come from the
  official metadata services, and Java is picked automatically from Mojang's requirements.
- **Modpacks:** Modrinth, CurseForge (with or without an API key) and Feed The Beast. Releases are
  **pinned**: restarts never upgrade silently. The panel checks for newer compatible releases every 6 h
  ("Update available" badge, optional notification); updates are explicit, take a pre-update backup, and
  can be rolled back.
- **Lifecycle:** install, start, graceful stop, restart, kill, recreate (keeps world data), clone, import
  an existing server from an archive, archive, reset the world (the server generates a new one; a safety
  backup is taken and checked first), and delete with typed confirmation. Status comes from real
  health checks. Crash loops are detected and stopped, and a diagnosis names the likely cause (missing
  mods, wrong Java version, out of memory and so on).
- **Console:** live log streaming and RCON commands over WebSockets, with history and filtering.
- **Files:** a web file manager and editor that is confined to the server folder and never follows
  symlinks, with uploads/downloads, search, and a typed `server.properties` editor.
- **Backups:** restic-based (encrypted, deduplicated) with coordinated `save-off` / `save-all` while
  running. Includes schedules, retention, integrity checks, restore in place (with a safety backup),
  restore as a new server, and a backup browser for single-file restores.
- **Mods/plugins:** a Modrinth-backed manager with hash identification, update checks, compatible search
  and JAR upload.
- **Players:** whitelist, operators, bans, kick, and online and historical player lists.
- **Multi-user:** a permission matrix with built-in and custom roles, per-server access grants, and an
  audit log.
- **Automation:** cron schedules (restart/stop/backup/snapshot/command), in-game countdown warnings
  before restarts and stops (manual or scheduled, cancellable), Discord/webhook notifications, and
  resource history.
- **Keyboard:** a command palette (<kbd>Ctrl</kbd>+<kbd>K</kbd> / <kbd>⌘</kbd>+<kbd>K</kbd>) to jump to any
  page or server and start/stop/restart servers (stop and restart always ask first), plus shortcuts such as
  `g s` (servers) and `c` (create server); press `?` for the list.

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

Then open `http://<MCPANEL_BIND>:8080`, create the owner account, and create your first server.
For Unraid specifics (paths, UID/GID 99:100, Compose Manager, pools vs user shares), see
[docs/UNRAID.md](docs/UNRAID.md). For general deployment, upgrades and reverse proxies, see
[DEPLOYMENT.md](DEPLOYMENT.md).

> Modded servers run third-party code. Docker raises the bar but is not a strong sandbox, so only install
> content you trust. Never expose the panel to the internet without HTTPS, and preferably use a VPN.

## Development

See [CLAUDE.md](CLAUDE.md) for repository layout and commands, and [TESTING.md](TESTING.md) for the test
suites.

## License

Not yet chosen by the repository owner. Minecraft is a trademark of Mojang/Microsoft; this project isn't
affiliated with them. Running a server requires accepting the [Minecraft EULA](https://aka.ms/MinecraftEULA).
