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

**Settings → Docker → "Preserve user defined networks" must be on** (advanced view; change it while
Docker is stopped). The stack's own Compose networks and every game server's network
(`zz_mcpanel_i_...`) are user-defined networks, which Unraid otherwise removes whenever Docker
restarts.

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

If that share is exported over SMB, set it to **Private** or
**Secure**, not **Public**: the server folders hold the RCON passwords (`.rcon-cli.env`). The agent
makes the data directory and its `instances` folder mode 0750, owned by root, at every start, so a
guest login over SMB can no longer read the server folders; the share setting is the second line.

**This also ends access to the data folder for every other user, on purpose.** From this release
on, an SMB login (also a named, non-guest user), the `nobody` user of Unraid's file manager or of
other containers (Krusader, a backup container), and a non-root shell on the host can no longer
open `appdata/mcpanel` or the server folders in it: only root (the Unraid terminal, the panel's
Files page and its backups) can. The agent sets it again at every start, so a `chmod` by hand does
not last. To edit server files, use the panel's Files page, or the Unraid terminal as root. To copy
worlds off the box, use a backup or the panel's download (zip) of a folder. A backup container that
must read the folder has to run as root (or read the restic backups in `MCPANEL_BACKUP_DIR`
instead).

## 3. Configure

Open a terminal (Unraid web UI → the `>_` icon). If `git --version` fails, install git with a plugin
(for example NerdTools or un-get), or download the repository as a zip from GitHub and unpack it.
If the repository is private, clone with a GitHub personal access token.

```bash
cd /mnt/user/appdata
(umask 022 && git clone https://github.com/cj7s1/mcserver.git mcpanel-src)
chmod -R go-w mcpanel-src
cd mcpanel-src/deploy
./scripts/generate-secrets.sh
nano .env
```

Root runs `upgrade.sh`, `install-hook.sh` and `backup-db.sh` from this folder, so nobody but root
may be able to change its files. While group or others can write them, `upgrade.sh` refuses and
prints the repair (`chmod -R go-w <the folder>`); the hook installer and the database copy, which
run unattended, run that repair themselves, warn in the syslog and go on. docs/SECURITY.md, "The
source folder", has the details and what this check cannot cover.

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

**Write down `AGENT_RESTIC_PASSWORD` somewhere other than the server** (a password manager), and set
up the daily copy of the panel database (DEPLOYMENT.md, "Backing up the panel itself"): the worlds,
the database and `.env` are all on the cache pool, and without the database and the password the
backups on the array cannot be used from the panel (section 10).

## 4. Start

```bash
cd /mnt/user/appdata/mcpanel-src/deploy && docker compose up -d --build
```

Open `http://192.168.1.10:8080`, create the owner account (enter the setup token that
`generate-secrets.sh` wrote: `grep '^MCPANEL_SETUP_TOKEN=' .env`), and create your first server.

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
| `itzg/minecraft-server:<tag>`, for example `stable-java21` | Every server that uses this tag is stopped, and a stopped server has no container. | The next start of such a server must download the image again, which needs internet access and takes a while. With the image on the host a start checks for a newer build anyway, but starts on the image it has when the host is offline. Keep it while stopped servers use that tag. |
| `mcpanel-rollback/...:<time>` | They are the images of the version before the last upgrade. | `./scripts/upgrade.sh rollback` is no longer possible (`./scripts/upgrade.sh status` shows `IS GONE`). Keep them as long as you want to be able to go back. The next successful upgrade removes them itself. |

`docker image prune -a` and `docker system prune -a` remove both kinds as well.

### Stopping or restarting the `panel` row

Unraid's Docker tab gives a container 10 seconds to stop (Settings → Docker).

* With no job running that is enough: a stop took about 5 seconds in a test.
* A job that is running at that moment (install, backup, restore) is cut off. A job that is
  waiting for the agent at that moment, which is most of the time of such a job, is marked as
  failed ("interrupted") at once and its server is unlocked; the agent finishes what it had
  started. A job cut off in other work is marked as failed about 6 minutes later (measured before
  this change: 6 min 9 s), and its server stays locked until then: no start, stop or backup.
  **Mark as failed** on the Activity page releases it sooner. A server the job had stopped stays
  stopped. Check the server before you retry the job.
* When the Activity page is not empty, use
  `cd /mnt/user/appdata/mcpanel-src/deploy && docker compose restart panel` instead: it gives jobs
  up to 110 seconds to finish.
* A platform container that was stopped from the Docker tab, on a folder, or by a backup plugin
  that stops containers stays stopped until Docker starts again: the three platform containers
  have the restart policy `always`, so the next reboot or array start brings them back. To start
  them before that: the Docker tab, or `docker compose up -d` in `deploy/`.

The Minecraft servers keep running in all of these cases.

### Array stop, reboot and shutdown

Unraid does not stop the Minecraft containers itself. Docker stops them on its way down, with
up to 20 seconds each and no save first. Install the shutdown hook so that the panel saves and
stops them before Docker stops, and starts them again after boot:
[UNRAID-SHUTDOWN.md](UNRAID-SHUTDOWN.md). The same User Scripts entry ("At Startup of Array") also
puts the game servers' firewall back after every boot (section 5, "The game servers' firewall"):

```bash
#!/bin/bash
bash /mnt/user/appdata/mcpanel-src/deploy/unraid/install-hook.sh install
bash /mnt/user/appdata/mcpanel-src/deploy/unraid/instance-firewall.sh install --wait 300
```

## 5. Ports and players

* Every server gets a host port from `MCPANEL_PORT_RANGE_START`–`END` (default 25565–25665). LAN
  players connect to `192.168.1.10:<port>`.
* For internet players, forward that TCP port on your router to the Unraid IP. The panel never changes
  your router or DNS.
* Voice-chat and map mods need extra ports (for example Simple Voice Chat on UDP 24454). Add them in the
  server's Network tab.
* A Factorio server uses **UDP** 34197 by default (the next free UDP port when that one is taken),
  outside the Minecraft range. Forward that UDP port, not TCP. It runs as `MC_UID`/`MC_GID` (99:100 on
  Unraid) without any capability, so its files in the server folder belong to `nobody:users` like the
  Minecraft servers' files. Each Factorio version is an image of about 1 GB on the cache drive.
* A Satisfactory server uses the same number on **UDP and TCP** (7777 by default: the game and the
  server's API) plus a **TCP** messaging port (8888 by default); forward all three. Its image runs
  as root with four capabilities and drops to `MC_UID`/`MC_GID` (99:100), so its files belong to
  `nobody:users` too. The game itself (2.8 GB) is downloaded into the server folder on the first
  start and left out of backups; give it at least 8 GB of RAM (12 GB by default). The panel claims
  the server before its ports are published, so nothing outside the host can take it over first.
  On its own network (the default) nothing but the agent reaches it before that; a server that is
  still on the shared network could be claimed first by a compromised neighbour there
  (docs/SECURITY.md): the setup then stops with "claimed with another admin password" and the server
  is never published.
* A Terraria server uses **TCP** 7777 by default (the next free TCP port when that one is taken, for
  example by a Satisfactory server); forward that TCP port. Vanilla and tModLoader run as root with
  five capabilities and drop to `MC_UID`; because the image cannot reuse the existing group 100
  (`users`), their files belong to `nobody` with group 99 instead of `nobody:users` (the panel and
  backups do not mind; set the group with Unraid's tools if you browse them over SMB as another
  user). TShock runs as `MC_UID`/`MC_GID` (99:100) with no capability. tModLoader downloads .NET from
  Microsoft whenever its container is recreated, so it needs internet access then; give it 3 GB of
  RAM. Each Terraria image is 0.3-0.9 GB on the cache drive.
* **Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2 and Rust** use UDP ports, published with
  the same number inside the container (the game advertises it): Palworld 8211, Valheim 2456 + 2457 (and
  2458 with crossplay), Project Zomboid 16261 + 16262, HKMP 2222, Counter-Strike 2 27015 (+ 27020 for
  CSTV; never forward its TCP port: that is RCON), Rust 28015 + 28017 (+ TCP 28082 for Rust+). Except
  HKMP, their images download the game into the server folder on the first start (Palworld 4.6 GB,
  Valheim 4 GB, Project Zomboid 7 GB, Rust 10 GB, Counter-Strike 2 about **60 GB**) and leave it out of
  backups; check the free space of the share first (the panel warns about low disk space, below).
  Counter-Strike 2 and Project Zomboid run as uid 1000:1000 without any capability (their files
  belong to 1000:1000, not `nobody:users`; on Unraid uid 1000 is normally the first user account you
  made); the other four start as root with a few capabilities and drop to `MC_UID`/`MC_GID`. A
  Project Zomboid server made before this version runs as root until its next start. Per-game notes
  (memory, passwords, tokens): [GAMES.md](GAMES.md).
* **Low disk space.** Every 15 minutes the panel checks the free space of each node's server and
  backup storage and sends the notification "Low disk space on a node" once when it drops below the
  level in System settings (20 GB by default, 0 = off), again when it went above and below it once
  more, or a day later. Subscribe a notification channel to it (System -> Notifications). Nothing
  limits the disk space of one server.

### The game servers' firewall (`instance-firewall.sh`)

> **Not run on a real Unraid host yet.** The rule set was loaded into a real kernel (iptables-nft, in a
> container's own network namespace: install, status, a second install, remove) and the script is
> tested against stand-ins, but it has never run against Unraid's own firewall. Check it on your
> server as described below before you rely on it.

A game server runs code you do not control. If it is compromised (a bad mod, a bug in the game
reached through its forwarded port), Docker lets it open connections to the Unraid host on every
address (the web UI, SMB, every port another container publishes, the panel) and to every device on
your LAN. `deploy/unraid/instance-firewall.sh` adds host firewall rules (chains `MCPANEL-FWD`, jumped
to from Docker's `DOCKER-USER`, and `MCPANEL-IN`, from `INPUT`) that drop **new** connections from the
game containers' addresses to the host itself and to private address ranges (10/8, 172.16/12,
192.168/16, 169.254/16, and 100.64/10, Tailscale's). What keeps working: replies (players join from
the LAN and the internet as before), a server and the agent on the same network, DNS to the LAN
resolvers of `/etc/resolv.conf`, and the internet (Mojang's account check, the Steam downloads). The
game containers' addresses are the per-server pool (`AGENT_INSTANCE_POOL`, default `10.213.0.0/16`, read
from `deploy/.env`) and the subnet of the shared `mcpanel_instances` network, asked from Docker.

```bash
cd /mnt/user/appdata/mcpanel-src/deploy/unraid
bash instance-firewall.sh dry-run    # what it would do: the exact rules and commands
bash instance-firewall.sh install
bash instance-firewall.sh status     # installed / missing / differs
```

Unraid forgets firewall rules at every boot: run `install` at every array start, in the User Scripts
entry of the shutdown hook (section 4, "Array stop, reboot and shutdown"). `--wait 300` there waits up
to 5 minutes for Docker and the stack's network. Run `install` again after Docker was switched off and
on (Settings -> Docker) and after an upgrade; `status` says "differs" when the rules no longer fit (a new
pool, a recreated network, other DNS servers).

**Check it after installing**, from a game server's container (Docker tab -> the server's icon ->
Console, or `docker exec -it <container> sh`; images without `curl` have `wget`):

* `curl -m 5 http://<your Unraid IP>/` must fail (time out), and so must any other LAN device;
* `curl -m 5 https://example.com/` must work;
* players must still join, from your LAN and from the internet.

If something a server needs stops working, `bash instance-firewall.sh remove` takes everything out at
once. A server that must reach something on your LAN (a database, a proxy): `install --allow
192.168.1.20:5432` (an address or range, optionally one port; stored in `deploy/instance-firewall.env`
and repeated by every later `install`; `--forget 192.168.1.20:5432` drops it). A DNS server that is not
in `/etc/resolv.conf`: `--allow-dns <ip>`. On an additional node: `--env /path/to/deploy/node.env`.
The agent itself cannot open new connections to LAN addresses either while the rules are installed (its
default route is the shared instance network, one of the rules' sources); it needs only the internet. A
LAN target it must reach (an HTTPS proxy, a restic server) goes in with `--allow` too, which opens it to
the game servers as well.

Not covered: IPv6 (`status` and `install` say so when one of these networks has IPv6), servers on host
networking or `br0`, and traffic between servers that are both still on the shared `mcpanel_instances`
network (a server gets its own network at its next start: docs/SECURITY.md, "One network per server").
Like the other scripts of this folder it refuses to run (or, as root in the checkout, tightens the
checkout first) when group or others can write it, its folder, `deploy/.env` or its state file.

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
cd /mnt/user/appdata/mcpanel-src && (umask 022 && git pull)
cd deploy
./scripts/upgrade.sh dry-run     # shows what will happen; changes nothing
./scripts/upgrade.sh run         # or the exact command the dry run ended with
```

`umask 022` keeps the files `git pull` rewrites writable by root only; when group or others can
write a file of the folder, the dry run warns and `run` refuses with the repair
(`chmod -R go-w /mnt/user/appdata/mcpanel-src`). Use the command the dry run prints in its last line. **The first upgrade from the version with
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
  containers. Each start of a server that creates its container checks for a newer build of its
  image (System settings → runtime image channel says which tag); a stop and start in the panel
  updates it.
* **A stopped server is missing in the Docker tab:** expected. A stopped server has no container;
  start it in the panel (section 4).
* **A server shows "restart required" after an update of MCPanel:** its container was made by the
  older version. Restart it when it suits you; the world is kept.
* **The panel is down after a backup plugin ran, or after Stop in the Docker tab:** Docker starts
  the platform containers again at its next start (restart policy `always`), not at once. Run
  `docker compose up -d` in `deploy/`, and exclude the `mcpanel-*` containers in plugins that stop
  containers. A stack installed before this policy gets it with the next `./scripts/upgrade.sh run`.
* **A start says "Not enough memory":** the running servers plus this one need more than the
  host's RAM minus the reserve (System settings → Memory kept free for the host, default 4096 MB).
  Stop another server, lower the memory, or use **Start anyway** (DEPLOYMENT.md, "Memory").
* **A server stays busy after the `panel` row was stopped during a job:** the job is marked as
  failed about 6 minutes later, or at once with **Mark as failed** on the Activity page.
* **Servers did not come back after a reboot:** see UNRAID-SHUTDOWN.md, "How the servers come back".
* **The `agent` container does not start, with "network ... not found":** a game server's network was
  removed while Docker was down (check "Preserve user defined networks", section 1). In `deploy/`:
  `docker compose up -d --force-recreate agent`; the agent then makes the missing networks again.
  `./scripts/upgrade.sh status` names this command when the agent is not running.
* **A server shows "failed" after the host started (exit code 128):** Docker could not start its
  container because its network was missing. Press **Start**: the agent makes the network again and
  starts it.

## 10. When the cache drive died

The worlds (`MCPANEL_DATA_DIR`), the panel database (`MCPANEL_POSTGRES_DIR`), the source folder with
`deploy/.env` and a generated restic password all live on the cache pool. What survives on the
array: the restic repositories (`MCPANEL_BACKUP_DIR`, one per server under `restic/<server id>`)
and, if you set it up, the daily database dump of `backup-db.sh` (DEPLOYMENT.md, "Backing up the
panel itself") with its copy of `.env`.

> **Not tested:** this procedure has been run in parts only (the scripts and the command have unit
> tests); it has not been run end to end on a real stack or on Unraid.

What you need:

* **`AGENT_RESTIC_PASSWORD`.** Without it the repositories cannot be opened by anyone. It is in
  `deploy/.env`, and so in every `mcpanel-db-<time>.env` that `backup-db.sh` wrote. Keep one more
  copy outside the server (a password manager). If `.env` has no `AGENT_RESTIC_PASSWORD`, the agent
  generated one in `<MCPANEL_DATA_DIR>/agent/restic-password`, which was on the lost drive: copy
  that file somewhere else while you still have it.
* **A database dump** from `backup-db.sh` (`mcpanel-db-<time>.sql.gz`) and the `.env` copy of the
  same time (`mcpanel-db-<time>.env`): its `MCPANEL_SECRET_KEY` decrypts what the dump holds.

Steps, in the Unraid terminal, once the pool is back:

1. Get the source again (section 3: `git clone` into `/mnt/user/appdata/mcpanel-src`), then put
   the `.env` copy in place and check its paths:

   ```bash
   cd /mnt/user/appdata/mcpanel-src/deploy
   cp /mnt/user/Backups/mcpanel-db/mcpanel-db-<time>.env .env && chmod 600 .env
   nano .env     # MCPANEL_DATA_DIR / MCPANEL_POSTGRES_DIR on the new pool, MCPANEL_BACKUP_DIR on the array
   ```

2. Start the database alone, unpack the dump and load it (`restore-db` reads plain SQL; without
   `--yes` it only says what it would do):

   ```bash
   docker compose up -d postgres
   gunzip -k /mnt/user/Backups/mcpanel-db/mcpanel-db-<time>.sql.gz
   ./scripts/upgrade.sh restore-db /mnt/user/Backups/mcpanel-db/mcpanel-db-<time>.sql
   ./scripts/upgrade.sh restore-db /mnt/user/Backups/mcpanel-db/mcpanel-db-<time>.sql --yes
   ```

   It loads the dump into a new database and swaps it in; the empty database from the first start
   is kept under another name, and the command prints how to remove it. It refuses while other
   services of the stack run. Remove the unpacked `.sql` afterwards (`rm` it; the `.gz` stays).

3. Start the stack: `docker compose up -d --build`. Everybody logs in again (the dump holds no
   login sessions). The panel knows every server of the dump, but their folders are empty.

4. Take over the backups that were made after the dump was taken (the database has no record of
   them), first as a dry run:

   ```bash
   docker compose exec panel python manage.py adopt_backups --all --dry-run
   docker compose exec panel python manage.py adopt_backups --all
   ```

   `--instance <server id>` does one server. Each snapshot of a server's repository that no backup
   row points to becomes a backup named "Adopted: <date time>", with a warning that it is not known
   whether world saving was paused for it. Running it again adds nothing.

5. Restore each server from its newest backup on its **Backups** page, then start it.

Limits: `adopt_backups` only reads the repositories of servers that the restored database knows.
A server created after the dump was taken is not in it; its repository is still on the array
under `restic/<server id>` and can be restored with restic by hand (`docs/reference/backup-snapshots.md`
has the restic notes), but not from the panel. Adopted backups carry no record of the server's software,
so restoring one does not change the server's version.
