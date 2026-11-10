# Running MCPanel on Unraid

> Step-by-step with screenshots: [QUICKSTART-UNRAID.md](QUICKSTART-UNRAID.md). This page is the reference.

> **Validation status:** MCPanel has been running on the owner's Unraid server (Unraid 7.3.4,
> Docker 29.8.2, btrfs cache pool) since 2026-11-07, in the version with seven platform
> containers. What this page says about the three-container layout, the upgrade script, stopped
> servers without a container, the shutdown hook and btrfs snapshots without `CAP_SYS_ADMIN` was
> tested on a Linux desktop with real Docker and real Minecraft servers. **None of that has run on
> Unraid yet.** DEVELOPMENT_STATUS.md lists what was verified where.

MCPanel is a stack of three containers (`panel`, `agent`, `postgres`), so it runs through
**Docker Compose**, not as a single Community Applications template. The web server, the job
workers, the scheduler and Redis are processes inside the `panel` container (DEPLOYMENT.md, "What
runs"). The Docker tab shows three rows for the platform plus one per **running** Minecraft
server; a stopped server has no container (section 4).

## 1. Docker Compose

MCPanel needs the `docker compose` command and BuildKit (it builds the two images). Open a
terminal (Unraid web UI → the `>_` icon) and check:

```bash
docker compose version
docker buildx version
```

Both must print a version. A server that has built the MCPanel images before has both.

If `docker compose` is missing, a Compose plugin from Community Applications installs it. The
current one is **Compose Manager Plus**. The older **Docker Compose Manager** is deprecated and
hidden in Community Applications by default. No Compose plugin has been tested with MCPanel.

Start, upgrade and roll back MCPanel from the command line as described below, not as a stack of
a Compose plugin: `upgrade.sh` and the shutdown hook were only tested that way.

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

```bash
cd /mnt/user/appdata/mcpanel-src/deploy && docker compose up -d --build
```

Open `http://192.168.1.10:8080`, create the owner account (enter `MCPANEL_SETUP_TOKEN` if you set
it), and create your first server.

### First modpack server: Better MC

**Create server → Modpack.** Without a CurseForge API key, choose **CurseForge page URL** and paste
`https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4` (Minecraft `1.20.1`), or the
Fabric edition `better-mc-fabric-bmc2`. Give it at least 8 GB of memory and leave **Nightly backup**
switched on. The first install takes a few minutes (about 300 mods); the overview shows progress, and
the server turns **running** only once Minecraft answers its health check. Avoid the Modrinth editions
of Better MC: they need dozens of files you would have to download by hand (the panel detects this and
lists them).

### What Unraid's Docker tab shows

* Three rows for the platform: `mcpanel-panel-1`, `mcpanel-agent-1`, `mcpanel-postgres-1`.
* One row `mcpanel-<12 characters of the server id>` for every **running** Minecraft server.
* No row for a stopped server. The panel removes the container 10 to 20 seconds after a stop
  and creates a new one at the next start. The world is in the server folder and is not touched.
  The last 2,000 console lines of the run are kept and shown on the server's Console page.
* A server that crashed, or stopped by itself, while it was meant to run keeps its row until you
  press Start or Stop on its page in the panel.

What follows from that:

* **A stopped server can only be started from the panel.** Start All and folder actions in the
  Docker tab cannot start it.
* **Manage running servers from the panel too.** Stop in the Docker tab gives a server Unraid's
  10 seconds and no save first, and the panel then shows it as "stopped itself" or as failed.
  Changes made to a container outside the panel are lost when the panel recreates it.
* After an upgrade from a version that kept exited containers, the rows of stopped servers
  disappear within seconds of the first start of the new panel (section 8).
* To keep the old behaviour, set `MCPANEL_RETIRE_STOPPED_CONTAINERS=false` in `.env`
  (DEPLOYMENT.md, "Minecraft server containers").

### "Orphan image" rows: which ones to keep

In its advanced view the Docker tab lists every image that no container uses as "(orphan
image)" and lets you remove it. Two kinds belong to MCPanel:

| Image | Why no container uses it | If you remove it |
|---|---|---|
| `itzg/minecraft-server:<tag>`, for example `stable-java21` | Every server that uses this tag is stopped, and a stopped server has no container. | The next start of such a server downloads the image again. That needs internet access, takes a while and can bring a newer build of the tag. Keep it while stopped servers use that tag. |
| `mcpanel-rollback/...:<time>` | They are the images of the version before the last upgrade. | `./scripts/upgrade.sh rollback` is no longer possible (`./scripts/upgrade.sh status` shows `IS GONE`). Keep them as long as you want to be able to go back. The next successful upgrade removes them itself. |

`docker image prune -a` and `docker system prune -a` remove both kinds as well.

### Stopping or restarting the `panel` row

Unraid's Docker tab gives a container 10 seconds to stop (Settings → Docker).

* With no job running that is enough: a stop took about 5 seconds in a test.
* A job that is running at that moment (install, backup, restore) is cut off after the 10
  seconds. The panel marks it as failed ("interrupted") only about 6 minutes later (measured:
  6 min 9 s). Until then its server stays locked: no start, stop or backup. **Mark as failed** on
  the Activity page releases it sooner. Check the server before you retry the job.
* When the Activity page is not empty, use
  `cd /mnt/user/appdata/mcpanel-src/deploy && docker compose restart panel` instead: it gives jobs
  up to 110 seconds to finish.
* A platform container that was stopped from the Docker tab, or by a backup plugin that stops
  containers, is not started again by Docker, also not after a reboot. Start it from the Docker
  tab or with `docker compose up -d` in `deploy/`.

The Minecraft servers keep running in all of these cases.

### Array stop, reboot and shutdown

Unraid does not stop the Minecraft containers itself. Docker stops them on its way down, with
up to 20 seconds each and no save first. Install the shutdown hook so that the panel saves and
stops them before Docker stops, and starts them again after boot:
[UNRAID-SHUTDOWN.md](UNRAID-SHUTDOWN.md).

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
backups. On a btrfs pool they work with the default stack (an override only makes deleting them faster).
On a ZFS pool the default stack lacks the privileges below, so snapshots show as unavailable until you add
the override; everything else works without it.

### btrfs pools

Each new server folder is created as a btrfs subvolume, and a snapshot is a read-only subvolume in
`<data dir>/snapshots/<server id>/`. The agent image contains `btrfs-progs`.

> **Status:** deleting without `CAP_SYS_ADMIN` was tested on a real btrfs filesystem in a QEMU
> guest (kernel 7.2.14, btrfs-progs 6.14) with exactly the capabilities the agent container has.
> It has not run on an Unraid pool yet (Unraid 7.3.4 uses kernel 6.12).

**Default stack (no override).** Taking a snapshot and rolling back need no extra privilege. Deleting is
different: the kernel lets only a process with `CAP_SYS_ADMIN` run `btrfs subvolume delete`, and the agent
container has no such capability. The agent then deletes the slow way: it clears the snapshot's read-only
flag, removes its files one by one and removes the emptied subvolume (the kernel allows that to the owner
since Linux 4.18). It does the same for the previous server folder after a rollback and for a server's
snapshots when the server is deleted. What that means:

* Deleting takes as long as deleting that many files. In a test with 100,000 small files plus 1 GiB of
  world data, one snapshot took about 2 seconds this way and no measurable time with the override. That
  was in the test guest, whose disk is an image file on NVMe with write caching; a real pool is slower,
  and a hard-disk pool, or a server with millions of files such as map-renderer tiles, takes much
  longer. Scheduled snapshots are pruned the same way.
* The panel waits up to 30 minutes for a single snapshot delete (other agent requests get
  `MCPANEL_AGENT_TIMEOUT`, 30 seconds by default). If a delete takes even longer, the job reports that the
  agent did not respond in time; the agent finishes the delete anyway.
* A snapshot being deleted leaves the list at once (its folder is renamed to `.deleting-<name>` first),
  so a half-removed snapshot can never be chosen for a rollback. If the agent is restarted in the middle,
  the rest is removed with the next snapshot it deletes for that server, or when the server is deleted.
* The agent finds out which way works by trying, not by guessing: at its first snapshot or storage request
  after a start it creates a tiny subvolume and a read-only snapshot of it in
  `<data dir>/.mcpanel-probe`, deletes both and removes the folder. The result is in the agent log
  (`subvolumes are deleted in fallback mode`) and in the snapshot capability the panel gets from the
  agent (`delete_mode`: `native` or `fallback`, with a `note` in fallback mode).
* If neither way works, snapshots show as **unavailable** with the reason, and the agent refuses to take
  snapshots or to convert a folder, so you never end up with snapshots that can't be deleted. A failed
  check is repeated when the panel asks again, at most once a minute.

**With the override: instant deletes.** Add this to `deploy/docker-compose.override.yml` and recreate the
agent (`docker compose up -d`); `delete_mode` then reads `native`:

```yaml
services:
  agent:
    cap_add: [SYS_ADMIN]   # btrfs subvolume delete; the agent already controls Docker (root-equivalent)
```

A pool mounted with the btrfs option `user_subvol_rm_allowed` gives instant deletes without the
capability: the kernel then accepts `btrfs subvolume delete` from the owner once a snapshot's read-only
flag is cleared, and the agent clears it. `delete_mode` reads `native` there too.

Whichever way is in use:

* A rollback is done once the snapshot's files are in the server folder. If the previous folder can't be
  removed afterwards, the job still succeeds (and starts the server again) with a warning that names the
  folder, `instances/<server id>.mcpanel-old-<time>`; the agent tries again at the next rollback of that
  server and when the server is deleted.
* Deleting a server deletes its snapshots too. A snapshot that can't be deleted does not stop the
  server from being deleted: the job's log names what was left in `<data dir>/snapshots/<server id>/`,
  and `btrfs subvolume delete <path>` on the Unraid command line removes it.

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
cd /mnt/user/appdata/mcpanel-src && git pull
cd deploy
./scripts/upgrade.sh dry-run     # shows what will happen; changes nothing
./scripts/upgrade.sh run         # or the exact command the dry run ended with
```

Use the command the dry run prints in its last line. **The first upgrade from the version with
seven platform containers is `./scripts/upgrade.sh run --with-migrations`**: the new version
changes the database schema, and a plain `run` refuses before it changes anything.

Building takes several minutes. If the terminal window is closed during the build, nothing was
changed: run the command again. Once the switch has begun (`==> Switching over`), a closed window
no longer stops it: the script finishes on its own and keeps what it printed in
`deploy/upgrade-log-<time>.log`. Open a terminal again and run `./scripts/upgrade.sh status`: it
names that log and says whether the run is still at work (then wait for it) or how it ended, and
what to run next if it did not finish.

The script only needs what the server already has (bash, docker, git). It refuses while a job is
running, saves the database and the current images first, rebuilds on fresh base images, removes
containers the new version no longer has, waits until everything is healthy and prints what
changed. `./scripts/upgrade.sh status` shows what is running. Running Minecraft servers are not
restarted by updates. Details of every step: DEPLOYMENT.md, "Upgrading".

What to expect at the first upgrade to the three-container layout:

* The Docker tab loses four platform rows: the two worker containers, `mcpanel-beat-1` and
  `mcpanel-redis-1`.
* The exited containers of stopped servers are removed within seconds (the dry run says how
  many). To look first, add `MCPANEL_RETIRE_STOPPED_CONTAINERS=false` to `.env` before the run
  (DEPLOYMENT.md, "Stopped servers at the first upgrade").
* Every server that is running shows "restart required", although none of its settings changed.
  Restart it when it suits you; until then Docker gives it 10 instead of 20 seconds at an array
  stop without the hook.

Going back: `./scripts/upgrade.sh rollback` returns to the version and the images from before the
last upgrade and undoes its database changes. A closed terminal window does not stop it either.
Afterwards the source folder is on the old revision (a detached HEAD), and
`deploy/scripts/upgrade.sh` is gone from disk if that revision did not have it.
`git checkout <your branch>` brings it back. From then on the folder holds the new version while
the old one runs: do not run `docker compose up -d` there by hand (the new compose file does not
fit the old images); use `./scripts/upgrade.sh`, whose `status` says which version is deployed
and which checkout fits it. A rollback needs the `mcpanel-rollback/...` images: do not remove
their "orphan image" rows (section 4).

The script keeps its files in `deploy/`: `upgrade-state.env`, per upgrade
`upgrade-backup-<time>.sql` (the database dump) with `upgrade-backup-<time>.env` (a copy of
`.env`; the newest three are kept), and `upgrade-log-<time>.log` (what a `run`, `rollback` or
`restore-db` printed; the newest ten are kept). They are readable by root only, and the dumps and
`.env` copies hold password hashes and the panel's secrets: keep the source folder on the cache
pool or the array, not in RAM, and do not export it over SMB or NFS.

## 9. Troubleshooting on Unraid

* **Permission denied in server files:** confirm `MC_UID=99`/`MC_GID=100`, then restart the server.
  The runtime fixes ownership of `/data` on start.
* **Slow worlds or lag spikes on save:** make sure the data directory is on the cache pool, not the
  array.
* **Panel unreachable:** confirm `MCPANEL_BIND` is the Unraid LAN IP and the port isn't already used.
* **Container shows "update available" in the Docker tab:** ignore it for `mcpanel-*` server
  containers. Runtime image updates are managed per server (System settings → runtime image channel).
* **A stopped server is missing in the Docker tab:** expected. A stopped server has no container;
  start it in the panel (section 4).
* **A server shows "restart required" after an update of MCPanel:** its container was made by the
  older version. Restart it when it suits you; the world is kept.
* **The panel is down after a backup plugin ran, or after Stop in the Docker tab:** Docker does not
  start containers again that were stopped that way. Run `docker compose up -d` in `deploy/`, and
  exclude the `mcpanel-*` containers in plugins that stop containers.
* **A server stays busy after the `panel` row was stopped during a job:** the job is marked as
  failed about 6 minutes later, or at once with **Mark as failed** on the Activity page.
* **Servers did not come back after a reboot:** see UNRAID-SHUTDOWN.md, "How the servers come back".
