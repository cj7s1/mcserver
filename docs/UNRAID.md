# Running MCPanel on Unraid

> Step-by-step with screenshots: [QUICKSTART-UNRAID.md](QUICKSTART-UNRAID.md). This page is the reference.

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

MCPanel offers instant snapshots and rollback alongside restic backups when `MCPANEL_DATA_DIR` is a
**pool path** (`/mnt/cache/...`, `/mnt/<pool>/...`) on a **btrfs** or **ZFS** pool. They can't be used on
`/mnt/user/...` FUSE paths or on XFS. Snapshots live on the same disk as the server, so they are **not**
backups. The default stack grants none of the privileges below, so snapshots show as unavailable until
you add an override; everything else works without it.

### btrfs pools

Each new server folder is created as a btrfs subvolume. The agent image contains `btrfs-progs`; the agent
only needs permission to delete subvolumes. Add this to `deploy/docker-compose.override.yml`:

```yaml
services:
  agent:
    cap_add: [SYS_ADMIN]   # btrfs subvolume delete; the agent already controls Docker (root-equivalent)
```

### ZFS pools (Unraid 6.12+ / 7)

> **Status:** implemented and unit-tested against a simulated `zfs` command only. It has **not** been run
> on a real ZFS pool yet. Try it on a test server first and report what you see.

How it works:

* Each server folder becomes its **own dataset**, `<parent>/mcpanel-<server id>`, where `<parent>` is the
  dataset that holds the instances folder (for `/mnt/cache/appdata/mcpanel` on Unraid usually
  `cache/appdata`). The dataset gets an explicit `mountpoint` (the server folder) and the user property
  `mcpanel:instance`. New servers get one automatically; existing servers show **Enable snapshots**
  (server stopped: a dataset is created next to the folder, the files are copied in, then it is mounted in
  the folder's place; this needs free space for one copy unless the pool supports block cloning).
* Snapshots are `<dataset>@<UTC timestamp>-<label>`, with the label and type in the user properties
  `mcpanel:label`, `mcpanel:kind` and `mcpanel:consistent`. Snapshots made by other tools are not shown
  and not touched.
* **Rollback never destroys snapshots.** `zfs rollback` can only go back to the newest snapshot; going
  further needs `zfs rollback -r`, which deletes every newer snapshot, including the safety snapshot the
  panel takes right before a rollback. MCPanel therefore clones the chosen snapshot, unmounts the current
  dataset and renames it to `<dataset>-old-<epoch>` (a *history* dataset: never mounted, it keeps the
  newer snapshots), gives the clone the dataset's name and mountpoint and promotes it. Snapshots on
  history datasets appear in the list as "Kept from before a rollback" and work like any other. A history
  dataset is destroyed when its last snapshot is deleted. A snapshot that snapshots from before a rollback
  are based on can't be deleted until those are; the error names them. If a step fails, the previous steps
  are undone in reverse order; if even that fails, the error says which dataset holds the previous files.
* Deleting a server destroys its dataset, its history datasets and their snapshots (never with
  `zfs destroy -R`: a clone you made yourself from one of its snapshots stops the delete instead).

Requirements: the agent image contains the ZFS userland (Debian `zfsutils-linux` 2.3.x from `contrib`;
no kernel module, no `zed`). The agent container additionally needs:

* the device `/dev/zfs`,
* `CAP_SYS_ADMIN` (ZFS mounts datasets itself),
* the data directory bind-mounted with **shared propagation** (`:rshared`). A dataset the agent mounts
  exists only in the agent container's mount namespace otherwise: Docker would hand the empty directory
  underneath to the Minecraft container and the world would end up outside the dataset. The agent checks
  this and reports *"not bind-mounted … with shared propagation"* instead of creating datasets. In
  Unraid's own Docker templates this access mode is called *RW/Shared*.

`deploy/docker-compose.override.yml` (Compose merges volumes by container path, so this entry replaces
the data directory mount of `docker-compose.yml`; keep host path and container path identical).
**`MCPANEL_BACKUP_DIR` must point outside `MCPANEL_DATA_DIR`** (the Unraid default above,
`/mnt/user/backups/mcpanel`, does): with `:rshared`, every other bind mount Docker places *inside* the
data directory (the default backups directory `${MCPANEL_DATA_DIR}/backups` is one) is copied back onto
the host at each agent start, and the copies pile up (1, 3, 7, ... mounts; reproduced with Docker 29).
That can keep the pool busy when the array stops. The agent refuses ZFS snapshots and says so while
such a mount exists. If you already have them, `findmnt -R ${MCPANEL_DATA_DIR}` on the host lists them;
stop the stack and run `umount` on the extra entries (or reboot) after moving the backups directory.

```yaml
services:
  agent:
    cap_add: [SYS_ADMIN]
    devices: ["/dev/zfs:/dev/zfs"]
    volumes:
      - ${MCPANEL_DATA_DIR}:${MCPANEL_DATA_DIR}:rshared
```

Notes and limits:

* Docker refuses `:rshared` when the host mount is not shared (`... is not a shared mount`). Check with
  `findmnt -o TARGET,PROPAGATION /mnt/cache` on the host. If it says `private`, `mount --make-shared
  /mnt/cache` after the pool is mounted and before the stack starts fixes it until the next reboot (on
  Unraid for example with a User Scripts "At Startup of Array" script). Not yet checked on real Unraid.
* `AGENT_HOST_DATA_ROOT` must not differ from `AGENT_DATA_ROOT` (the default stack keeps them equal):
  the mountpoint ZFS stores is a host path.
* Hosts that enforce AppArmor (Ubuntu, not Unraid) block `mount` in containers with the default profile;
  there you would also need `security_opt: ["apparmor:unconfined"]` for the agent. Weigh that before
  using it.
* The agent's `zfs` tools (2.3.x) talk to the host's ZFS kernel module. Basic operations are compatible
  across 2.2/2.3, but if the panel shows *"The zfs command cannot use the ZFS kernel module"*, compare
  `zfs version` on the host with the version shown in the snapshot capability (`zfs_version`).
* Restic restores and clones keep a dataset in place (they swap the folder's contents inside it), so a
  server stays snapshot-capable after a backup restore.
* Each dataset is a separate mount, so tools on the host that walk `/mnt/cache/appdata` (backup plugins,
  the mover) see one more filesystem per server. Keep the share on the pool (no mover to the array).
* **Other containers** that bind-mount the whole appdata share (backup containers such as Duplicati or
  Kopia) get a private copy of the mounts that existed when *they* started: they don't see a server
  dataset created later (only the empty folder underneath), and after a rollback they keep the old
  dataset mounted until they are restarted, which also stops MCPanel from destroying it ("dataset is
  busy"). Restart them after creating servers or rolling back, or give them the share with
  `:rslave` propagation (Unraid template access mode *RW/Slave*).
* Verified here only with a stand-in: in a Docker 29 sandbox, a mount the agent container made below its
  `:rshared` data mount (tmpfs instead of a dataset) appeared on the host and in a Minecraft-like
  container started afterwards, an unmount + new mount (what a rollback does) reached both, and a
  restarted agent saw it. Real ZFS and real Unraid are still unverified.

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
