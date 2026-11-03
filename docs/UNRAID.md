# Running MCPanel on Unraid

> **Validation status:** the Compose stack has been tested on a generic Linux Docker host, but **not
> yet on real Unraid hardware**. The paths and settings below follow Unraid conventions. Please
> report anything that differs.

MCPanel is a multi-container stack (panel, worker, beat, agent, Postgres, Redis), so it runs through
**Docker Compose**, not as a single Community Applications template.

## 1. Install Docker Compose support

Install the **Docker Compose Manager** plugin from Community Applications (Apps → search "Compose").
Alternatively, Unraid 7 includes `docker compose` on the command line.

## 2. Choose storage paths

Minecraft worlds do many small random writes, so put them on a **pool** (SSD cache), not the parity
array:

| What | Recommended path | Notes |
|---|---|---|
| Server data (`MCPANEL_DATA_DIR`) | `/mnt/cache/appdata/mcpanel` | Use the pool path rather than `/mnt/user/...`: it bypasses the FUSE layer (faster) and is required for btrfs snapshots. With Unraid 6.12+ "exclusive shares", `/mnt/user/appdata` is also fine for performance. |
| Backups (`MCPANEL_BACKUP_DIR`) | `/mnt/user/backups/mcpanel` | On the array, on a different disk from the worlds. restic repositories are encrypted and deduplicated. |
| Database (`MCPANEL_POSTGRES_DIR`) | `/mnt/cache/appdata/mcpanel-db` | Keeps the database out of `docker.img`. |

Make sure the share used for `appdata` is set to stay on the pool (Primary storage: cache, no mover to
array), otherwise the mover may relocate live world files.

## 3. Configure

Open a terminal (Unraid web UI → the `>_` icon). If `git --version` fails, install git with a plugin
(for example NerdTools or un-get), or download the repository as a zip from GitHub and unpack it.
If the repository is private, clone with a GitHub personal access token.

```bash
cd /mnt/user/appdata
git clone https://github.com/cj7s1/mcserver.git mcpanel-src
cd mcpanel-src/deploy
./scripts/generate-secrets.sh
nano .env
```

Unraid-specific values:

```ini
MCPANEL_DATA_DIR=/mnt/cache/appdata/mcpanel
MCPANEL_BACKUP_DIR=/mnt/user/backups/mcpanel
MCPANEL_POSTGRES_DIR=/mnt/cache/appdata/mcpanel-db
MC_UID=99                     # nobody
MC_GID=100                    # users
MCPANEL_BIND=192.168.1.10     # your Unraid server's LAN IP
MCPANEL_PORT=8080             # pick a port not used by the Unraid web UI or other containers
MCPANEL_ALLOWED_HOSTS=192.168.1.10,tower.local
MCPANEL_CSRF_TRUSTED_ORIGINS=http://192.168.1.10:8080,http://tower.local:8080
MCPANEL_LAN_ADDRESS=192.168.1.10
TZ=Europe/Berlin
```

**Write down `AGENT_RESTIC_PASSWORD` somewhere other than the server.**

## 4. Start

With Compose Manager: add a new stack, point it at `.../mcpanel-src/deploy/docker-compose.yml`, and
start it. On the command line:

```bash
cd /mnt/user/appdata/mcpanel-src/deploy && docker compose up -d --build
```

Open `http://192.168.1.10:8080`, create the owner account, and create your first server. The Minecraft
containers appear in Unraid's Docker tab as `mcpanel-<id>`. **Manage them from the panel, not from the
Docker tab.** Changes made to them outside the panel are overwritten when the panel recreates the container.

### First modpack server: Better MC

**Create server → Modpack.** Without a CurseForge API key, choose **CurseForge page URL** and paste
`https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4` (Minecraft `1.20.1`), or the
Fabric edition `better-mc-fabric-bmc2`. Give it at least 8 GB of memory. The first install takes a few
minutes (about 300 mods); the overview shows progress, and the server turns **running** only once
Minecraft answers its health check. Avoid the Modrinth editions of Better MC: they need dozens of
files you would have to download by hand (the panel detects this and lists them).

## 5. Ports and players

* Every server gets a host port from `MCPANEL_PORT_RANGE_START`–`END` (default 25565–25665). LAN
  players connect to `192.168.1.10:<port>`.
* For internet players, forward that TCP port on your router to the Unraid IP. The panel never changes
  your router or DNS.
* Voice-chat and map mods need extra ports (for example Simple Voice Chat on UDP 24454). Add them in the
  server's Network tab.

## 6. Snapshots on Unraid

On a **btrfs** pool, MCPanel creates each new server folder as a btrfs subvolume and offers instant
snapshots and rollback alongside restic backups. The agent image already contains `btrfs-progs`; the
agent only needs permission to delete subvolumes. Add this to `deploy/docker-compose.override.yml`:

```yaml
services:
  agent:
    cap_add: [SYS_ADMIN]   # btrfs subvolume delete; the agent already controls Docker (root-equivalent)
```

Snapshots only work when `MCPANEL_DATA_DIR` is a pool path (`/mnt/cache/...`). They can't be used on
`/mnt/user/...` FUSE paths or on XFS. ZFS pools are not supported yet. Snapshots live on the same disk
as the server, so they are **not** backups.

## 7. More than one server box

A second Unraid or Linux machine can run only the agent (`deploy/docker-compose.node.yml`) and be
managed from this panel over HTTPS with a pinned certificate. See DEPLOYMENT.md, "Additional nodes".

## 8. Updating

```bash
cd /mnt/user/appdata/mcpanel-src && git pull && cd deploy && docker compose up -d --build
```

Running Minecraft servers are not restarted by updates.

## 9. Troubleshooting on Unraid

* **Permission denied in server files:** confirm `MC_UID=99`/`MC_GID=100`, then restart the server.
  The runtime fixes ownership of `/data` on start.
* **Slow worlds or lag spikes on save:** make sure the data directory is on the cache pool, not the
  array.
* **Panel unreachable:** confirm `MCPANEL_BIND` is the Unraid LAN IP and the port isn't already used.
* **Container shows "update available" in the Docker tab:** ignore it for `mcpanel-*` server
  containers. Runtime image updates are managed per server (System settings → runtime image channel).
