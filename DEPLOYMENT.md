# Deployment

MCPanel runs as a Docker Compose stack on one Linux Docker host. For Unraid specifics, read
[docs/UNRAID.md](docs/UNRAID.md) as well, and
[docs/UNRAID-SHUTDOWN.md](docs/UNRAID-SHUTDOWN.md) for what happens to running servers at array
stop, reboot and shutdown.

## Requirements

* Docker Engine 24+ with the Compose v2 plugin (`docker compose version`).
* BuildKit (`docker buildx version` must print a version). The panel and agent images are built on
  your host, and their Dockerfiles use `RUN --mount` and `COPY --chmod`, which only BuildKit
  understands. A host that has built these images before has it.
* `bash` and `git` (the upgrade script uses both).
* x86_64 or arm64. Large modpacks need 8–12 GB RAM per server plus about 2 GB for the stack.
* Outbound HTTPS to Mojang, Modrinth, CurseForge, FTB, the itzg mirrors and Docker Hub.

## Install

```bash
(umask 022 && git clone https://github.com/cj7s1/mcserver.git)
cd mcserver/deploy
./scripts/generate-secrets.sh        # writes .env with random secrets; refuses to overwrite
```

Root (or a member of the `docker` group, which amounts to the same) runs the scripts of this
checkout and builds the images from it, so only its owner may be able to change it.
`scripts/upgrade.sh`, `unraid/install-hook.sh` and `unraid/backup-db.sh` refuse to run while group
or others can write the files they depend on, and print the repair (`chmod -R go-w <checkout>`).
Where the default umask is 002 (distributions that give every user a group of their own), clone
and pull with `umask 022` as above. docs/SECURITY.md, "The source folder", has the details.

Edit `.env` (every variable is documented in `deploy/.env.example`):

| Setting | Why |
|---|---|
| `MCPANEL_DATA_DIR` | Absolute host path for all server data. It is mounted at the **same path** inside the agent. |
| `MCPANEL_BACKUP_DIR` | Optional separate disk for restic repositories. |
| `MC_UID` / `MC_GID` | Owner of server files. Use `99`/`100` on Unraid. |
| `MCPANEL_BIND` | Defaults to `127.0.0.1`. Set your LAN IP to reach the panel from other machines. |
| `MCPANEL_ALLOWED_HOSTS`, `MCPANEL_CSRF_TRUSTED_ORIGINS` | Must contain the host/origin you type into the browser. |
| `MCPANEL_LAN_ADDRESS`, `MCPANEL_PUBLIC_HOSTNAME` | Shown to users as connection addresses (informational only). |
| `AGENT_RESTIC_PASSWORD` | **Store a copy somewhere safe.** Without it, backups can't be restored. |
| `MCPANEL_SETUP_TOKEN` | Set by `generate-secrets.sh` (by hand: `openssl rand -hex 24`, before the first start): the setup page asks for it before it creates the owner account, so nobody else who reaches the panel first can claim it. `grep '^MCPANEL_SETUP_TOKEN=' .env` shows it. Empty = no token asked for. |

Start the stack:

```bash
docker compose up -d --build
docker compose ps          # postgres, agent and panel: all three healthy after about a minute
```

Open `http://<MCPANEL_BIND>:<MCPANEL_PORT>/`, create the owner account (enter the setup token:
`grep '^MCPANEL_SETUP_TOKEN=' .env`), and create a server.

## What runs

Three containers:

| Service | Image | Privileges |
|---|---|---|
| `panel` | `mcpanel/panel` (`all`) | UID 10001, no capabilities, no Docker socket. Applies database migrations on start, then runs everything except the database and the agent as processes of one container (below). |
| `agent` | `mcpanel/agent` | Root, Docker socket, data directory. Never published. It refuses requests coming from the Minecraft network. |
| `postgres` | upstream | Internal network only. |

All three have the restart policy `restart: always`, not `unless-stopped`: Docker starts them
again at its next start also after they were stopped through its API (Stop in Unraid's Docker
tab or on a folder, a backup plugin that stops all containers), so the panel does not stay down
across a reboot. `docker compose stop` still stops them until the next `docker compose up` or the
next start of the Docker daemon. The agent of an additional node (`docker-compose.node.yml`) and
the optional `redis` container still use `unless-stopped`.

Inside `panel`, a small supervisor (`backend/container/supervise.py`) starts and watches five
processes. Every line of `docker compose logs panel` begins with the name of the process that
wrote it:

| Process | What it does |
|---|---|
| `[redis]` | Job queue, live events and cache. A Unix socket inside the container only (no network port), nothing on disk. Started first, stopped last. |
| `[web]` | API, WebSockets and UI (uvicorn). |
| `[worker]` | Celery jobs: installs, backups, restores, updates (and periodic tasks when it has a free slot). `MCPANEL_WORKER_CONCURRENCY` jobs at a time. |
| `[periodic]` | Celery, periodic tasks only (reconcile loop, schedules, job watchdog, metrics, modpack update checks), so they keep running while long jobs fill every `[worker]` slot. Always 2 slots. Never runs jobs. |
| `[beat]` | Scheduler: queues the periodic tasks above. |

```bash
docker compose exec panel python /usr/local/bin/mcpanel-supervise.py status   # state of each process
docker compose logs --tail 200 panel | grep -F '[worker]'                     # one process's lines
```

What to expect from it:

* A process that exits is started again after 1, 2, 4 ... at most 30 seconds, for as long as it
  takes; the others keep running. While it is down the health check fails and says which one:
  `docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' mcpanel-panel-1`, and
  `components` in `/api/health`. After three failed checks (about 90 seconds) Docker shows the
  container as **unhealthy**. If Redis dies it is started again and the other four are restarted
  after it.
* **Healthy** means: the database and the cache answer, all five processes run, both workers
  answered a ping in the last 2 minutes and the reconcile task ran in the last 3 minutes.
* `docker compose stop panel` / `restart panel` / `down` give running jobs up to 110 seconds to
  finish. A job that is waiting at that moment (for the agent, for a countdown or for a server's
  lock, which is most of a backup, restore or install) does not wait for them: it is marked as
  failed ("interrupted") at once and its server is unlocked; the agent finishes an operation it
  had already started. A job that is cut off in the middle of other work is marked as failed
  after 5 minutes without a sign of life, checked every 2 minutes, so about 6 minutes later
  (measured before this change: 6 min 9 s). Until then its server stays locked; **Mark as
  failed** on the Activity page releases it sooner, and the job stops within about 2 seconds.
  A server the job had stopped for its work is left stopped and says so in the job's error.
  Check that server before you retry. So restart the panel when the Activity page is empty.
* Restarting the panel container empties Redis. Jobs that were still waiting are sent again at the
  start. Lost: webhook notifications that were waiting for a retry or were queued in the last
  seconds before the stop, and the crash-loop counters (they start counting again).
* The panel log is capped at 5 x 20 MB. The scheduler and the periodic worker only log warnings
  and errors and the 10-second ticks are not logged, so that is months of requests and jobs.
* `/api/health` (no login needed) reports the database and cache checks, `components` (the state
  of each of the five processes) and `tick_age_seconds` (how long ago the last 10-second
  reconcile run started).

### Minecraft server containers

The agent creates the Minecraft containers itself (named `mcpanel-<first 12 characters of the
server id>`, label `io.mcpanel.managed=true`) on the `mcpanel_instances` network. Because Compose
doesn't manage them, `docker compose down` does **not** stop running servers. Stop them in the
panel first if you want everything down.

A stopped server has no container:

* **Stop** (or Kill) in the panel stops the server. 10 to 20 seconds later the panel removes the
  exited container. Before the container goes, the agent saves a record of that run: exit code,
  out-of-memory flag, restart count, start and end time and the last 2,000 console lines. The
  Console page of a stopped server shows those lines. World data is in the server folder and is
  not touched.
* **Start** creates a new container from the current settings and starts it. `docker ps -a`
  (and Unraid's Docker tab) therefore has no entry for a stopped server, and a stopped server
  can only be started from the panel.
* A server that died or stopped by itself while it was meant to run keeps its container, so
  that you can see what happened. Its page then offers **Start** and **Stop**; Stop marks it as
  stopped, and the container is removed like any other.
* If the record cannot be saved, the container stays and the audit log gets
  `instance.container_retire_failed` (at most once an hour per server).

Settings and commands:

| What | How |
|---|---|
| Keep exited containers, as before this feature | `MCPANEL_RETIRE_STOPPED_CONTAINERS=false` in `.env`, then `./scripts/upgrade.sh run --no-build`. Empty or `true` is the default (containers are removed). |
| List what would be removed, without removing anything | `docker compose exec panel python manage.py retire_stopped_containers --dry-run` |
| Remove the containers of all stopped servers once, also while the setting is `false` | `docker compose exec panel python manage.py retire_stopped_containers` (`--node <name>` for one node) |

**Runtime images are refreshed at each start that creates a container**, which is nearly every
start, because a stopped server has no container. The agent checks for a newer build of the
server's `itzg/minecraft-server` tag, for at most 3 minutes. When the host is offline or the
registry does not answer, the server starts on the image it has and the job log says that no
newer build could be checked. Only a tag that is not on the host at all must be downloaded
before the start. The job log names the build that runs and the one it replaced, and the agent
removes the replaced image when it has no tag left and no container uses it. The runtime image
channel in System settings (stable, latest, ...) applies to existing servers too: a running
server shows **restart required** after a change and gets the new image at its next start.

### Factorio servers

The create wizard asks for the game first (Minecraft, Factorio, Satisfactory or Terraria). A Factorio server:

* runs `factoriotools/factorio:<exact version>-rootless` (about 1 GB per version on disk), as
  `MC_UID`/`MC_GID` with no capabilities. Clients must run the same version: the version is pinned
  and changed in Settings -> Software (with a pre-update backup; going back needs a confirmation).
  The version list comes from Docker Hub's tags and factorio.com, so creating a server needs
  internet access from the panel.
* gets one **UDP** port, 34197 when it is free (else the next free one above it). Forward that UDP
  port on your router for players outside your network; the Network page lists it with its
  protocol. RCON (27015/tcp) is never published.
* has a memory limit of its own (no Java heap; default 2 GB, at least 1 GB). Large maps and Space
  Age games need more.
* asks once whether **Space Age** is on (off by default: players need to own the expansion to
  join). The choice cannot be changed later, because switching it under an existing save converts
  the save.
* is not listed publicly unless you turn it on in Settings -> Game settings (which also needs a
  factorio.com username and token). A game password can be set there and in the wizard.
* keeps its map in `saves/world.zip`. Backups -> Saves downloads it, imports a save (`.zip`) or
  creates a new map with a chosen seed; both move the previous saves into `saves-replaced/` in
  the server folder after a safety backup.
* needs an agent of this release: an older agent does not have the Factorio profile, and the panel
  refuses to create or start a Factorio server on it (`agent_lacks_game`). `./scripts/upgrade.sh run`
  updates both.

A registry mirror for the Factorio image is set in System settings (`runtime_images`, for example
`{"factorio": {"repository": "registry.example/factorio"}}`); the agent must allow it too
(`AGENT_GAME_FACTORIO_IMAGES` in the agent's environment, comma-separated).

### Satisfactory servers

A Satisfactory server:

* runs `wolveix/satisfactory-server` at the tag this panel release pins (`v1.9.11`). The image
  downloads the game (about 2.9 GB, 2.8 GB on disk in `gamefiles/`) with SteamCMD on the first start
  and checks it at every start (about 12 s on an NVMe drive). Clients and server must run the same
  build, so the server follows Steam's stable branch: there is no version to choose and no
  software update; **Update now** on the Overview is a restart. SteamCMD fails now and then
  ("Timed out", exit 5; "Missing configuration", exit 8): the panel's jobs start it again (4 starts
  in all) at the first start and at every start they make (Start, Restart, the publish step, the
  jobs that start a server again after their work), and wait until it is ready.
* needs at least **8 GB** of memory (12 GB by default; 16 GB for big factories or more than 4
  players) and an x86-64 CPU that is not a generic VM CPU model.
* runs as root inside the container with exactly CHOWN, DAC_OVERRIDE, SETUID and SETGID: the image
  changes its `steam` user to `MC_UID`/`MC_GID` and drops to it (it cannot run unprivileged as any
  uid but 1000). The files in the server folder belong to `MC_UID`/`MC_GID`. When the runtime group
  (`MC_GID`, 100 on Unraid) already exists in the image, the image keeps its own group (1000) for
  files the game writes while it runs, so the agent gives the server folder the runtime owner
  (`MC_UID`:`MC_GID`) again after every stop and before every backup; while the server runs, newly
  written files can show group 1000 on the share.
* gets three host ports: the **game port on UDP and the same number on TCP** (the server's HTTPS API,
  which the game's Server Manager uses), 7777 by default, and the **reliable messaging port** on
  TCP, 8888 by default. Forward all three. Inside the container the game port stays 7777 (the
  server's claim is stored per game port); the messaging port is set to the host port, because the
  server tells players that number.
* is **claimed by the panel before any port is published**: whoever reaches an unclaimed server
  first owns it. The install starts the server with no published port; the setup steps (Overview:
  "Setup needed") then claim it through the agent on the internal network with the server's control
  secret as admin password, store an API token for the panel, set the join password (or confirm an
  open server: the wizard makes you choose), load or create the game, have the agent verify the
  claim once more and record it in `.mcpanel/claim-verified`, and only then recreate the container
  with its ports published. A server created stopped is set up at its first start.
* **starts only through the panel while its ports are published.** The container's entrypoint is
  the agent's start wrapper (`.mcpanel/start-guard.sh`): a `docker start`, a `docker restart` or a
  restart of the Docker daemon of a published server whose claim was not verified since its
  settings file changed exits at once with code **78** and the log line `MCPanel start guard: ...`
  (Unraid's Docker page shows it stopped). Start it from the panel: it recreates the container
  without ports, verifies the claim (running the setup again: claim, join password, game; a game is
  loaded from the newest save of the server's session when none is loaded), and publishes again. The
  restart policy is `no`, so Docker never starts it by itself, also not after a reboot of the host
  (the Unraid shutdown hook's resume starts the servers that ran before a host shutdown). After an
  upgrade from a release without the start wrapper, the first start of an existing Satisfactory
  server runs this setup once (its container is recreated anyway: the restart policy changed).
* has its settings in Settings -> Game settings: launch settings (max players, autosaves, tick
  rate, ...; applied at the next start) and, while it runs, the server options read from and
  written to its API (autosave interval, pause when empty, ...). The join password is in the same
  tab under Secrets; changing it applies it to the running server.
* keeps its saves in `saved/server/*.sav`. Backups -> Saves lists them by session, downloads the
  current game (saved first), imports a `.sav` and starts a new game; import and new game go through
  the running server's API after a safety backup (a stopped server is started for it and stopped
  again), and the previous sessions stay. Backups leave out `gamefiles/`, and a restore keeps the
  game files that are on disk.
* is stopped by saving through its API and calling its `Shutdown` function (it never saves on a
  signal; nothing is saved while no game is loaded); its container uses the restart policy `no`.
* closes its ports again whenever its claim may be lost: a restore, a file change under `saved/`
  (upload, edit, rename, delete, extract, import), a start the start wrapper refused, the agent's own
  check. An admin in the game who changes a server setting (a client password) makes the settings file
  change outside the panel: the server keeps running, and its next start goes through the setup once.
* needs an agent of this release (`agent_lacks_game` otherwise).

A registry mirror is set like Factorio's (`runtime_images`: `{"satisfactory": {"repository": "..."}}`,
`AGENT_GAME_SATISFACTORY_IMAGES` on the agent).

### Terraria servers

The wizard asks for the **variant** first; it cannot be changed later.

| Variant | Image (exact tags only) | Container | Memory (default) |
|---|---|---|---|
| Vanilla (the official server) | `passivelemon/terraria-docker:terraria-<version>` | root with CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID; drops to `MC_UID` | 1.5 GB |
| tModLoader | `passivelemon/terraria-docker:tmodloader-<version>` | the same | 3 GB |
| TShock | `ryshe/terraria:tshock-<terraria>-<tshock>` | `MC_UID:MC_GID`, **no capability** | 1.5 GB |

* Clients must run the same game version: the version is pinned and changed in Settings -> Software
  (pre-update backup; going back needs a confirmation). The lists come from Docker Hub's tags (the
  panel needs internet access). **tModLoader is built on Terraria 1.4.4.9**: its players need the
  tModLoader client, and vanilla clients on newer versions cannot join it. TShock is offered from
  TShock 6 on.
* tModLoader downloads the .NET runtime from Microsoft every time its container is created (the
  first start after a create, an update or a settings change that recreates it): it needs internet
  access then, and that start takes 10-20 s longer. Mods: `.tmod` files and `enabled.json` in
  `ModPacks/mcpanel/Mods/` (file manager; a mod manager comes later).
* One **TCP** port, 7777 when it is free (else the next free one above it); forward it on your
  router. TShock's REST API (7878) is never published.
* The panel writes `serverconfig.txt` (Settings -> Game settings: world size, difficulty and seed for
  a new world, max players, password, message of the day) and owns the keys `world`, `worldpath`,
  `worldname`, `port`, `banlist` (and tModLoader's `modpack`/`modpath`), which it puts back before
  every start. TShock also has `config.json` (server name, password, max players, login, whitelist,
  autosave, its own backups); the panel owns its REST settings and the game port there.
* The world is `Worlds/<world name>.wld` (tModLoader adds `.twld`); the name is chosen in the
  wizard. Backups -> Saves downloads it (the running server saves first), imports a `.wld` (checked:
  a Terraria world file) or creates a new world with size, difficulty and seed; both move the
  previous world files into `Worlds-replaced/` after a safety backup.
* **TShock owner account:** enter an owner password in the wizard (or later in Settings -> Game
  settings -> Secrets); the setup step creates the account (default name `owner`) through TShock's
  REST API once the server is ready. Log in in game with `/login <account> <password>`. TShock's
  one-time `/setup` code is then not needed (the console shows it masked; TShock switches it off at
  its next start).
* `exit` (and TShock's `off`) in the console run the panel's Stop instead. The stop saves:
  PassiveLemon's image on SIGTERM (5 s warning in game, then save; exit code 143, a clean stop for
  this image), TShock on SIGINT (exit 0).
* On Unraid (`MC_UID`/`MC_GID` 99:100) the vanilla and tModLoader files belong to 99:**99**: the
  image creates its group from the gid and fails when that gid already exists in the image (100 is
  `users` there), so the agent uses the uid as the group id in that case. TShock's files belong to
  99:100.
* Needs an agent of this release with the variant's profile (`agent_lacks_game` otherwise).

A registry mirror for PassiveLemon's image is set in System settings (`runtime_images`:
`{"terraria": {"repository": "..."}}`; TShock keeps `ryshe/terraria`). The agent allows images per
variant with `AGENT_GAME_TERRARIA_<VARIANT>_IMAGES` (e.g. `AGENT_GAME_TERRARIA_TSHOCK_IMAGES`), else
`AGENT_GAME_TERRARIA_IMAGES`, else the profile's own repository.

### Memory

Before a server is started, the panel checks that it fits into the host's RAM: the memory limits
of the servers that run on that host plus the new one must not exceed the RAM Docker reports
minus a reserve for everything else on the host. The reserve is **Memory kept free for the host
(MB)** in System settings (`memory_reserve_mb`, default 4096, 0 to 65536). A start that does not fit is
refused with the numbers, and the panel offers **Start anyway**. Scheduled starts and restarts
wait (see "Schedules"), and servers started again after a host shutdown wait for their turn. A
stop is never refused, nor the restart of a server whose container already runs with its
settings. The create wizard and the Resources tab show how much host memory the running servers
reserve already; that bar compares with the whole RAM, not with RAM minus the reserve.

### Schedules

* A schedule that comes due while another job holds its server (a backup due while a snapshot
  runs, two templates at the same minute) waits and is tried every minute for up to an hour; the
  schedule list says "Waiting: ...". After an hour it is recorded as failed and a notification is
  sent. Backups run before snapshots, snapshots before restarts, stops and commands.
* After the panel was down (host off, upgrade), a restart, start, stop or command more than 10
  minutes late is not run late ("Missed: ..."). A missed backup or snapshot runs once, when its
  server has been ready for 2 minutes or the panel has been up for 5.
* A date that never occurs (`0 0 31 2 *`) cannot be saved. Daylight saving time: on the
  fall-back night a schedule at a fixed hour runs once; schedules with `*` or a step in the hour
  field run through the repeated hour as on any other night. A time in the hour skipped in
  spring runs when the gap ends.
* Schedules use the time zone of System settings (the one the servers get), not the panel
  process's.

### Uploads

* One upload may be at most `MCPANEL_MAX_UPLOAD_BYTES` (default 2 GiB); every other request at
  most 25 MB. Both limits are checked before the panel reads the body: an upload that is too
  large is refused at once, and without a signed-in session no request may send more than 25 MB.
* The file manager uploads any number of files, in batches of 100; each file is stored on its
  own and the answer lists the files that were uploaded and those that failed (an existing file,
  for example).
* While an upload is received it is kept in `MCPANEL_UPLOAD_TMP` inside the panel container
  (default `/tmp/mcpanel-uploads`, in the container's writable layer, which on Unraid is
  `docker.img`). It is held there once, until it has been passed on to the agent. The compose
  file does not pass this variable; to move the folder (for example to a volume on the cache
  pool with room for the largest upload), set it and mount the volume in a
  `docker-compose.override.yml` for the `panel` service, writable by UID 10001.
* The panel's web process creates that folder (mode 0700) when it starts, as its own user (UID
  10001); the image contains no such folder. If it cannot write there, every request body above
  5 MB fails (uploads, imports, world replace, mod uploads): the container log says so at start
  and `/api/health` reports `"uploads": "error"`, so the container turns unhealthy.


### Stopping servers

* **Stop** and **Restart** in the panel: the agent first asks the server to save
  (`save-all flush`), then runs `docker stop` with the server's stop timeout (server settings,
  default 90 s). The job log says whether the world was saved.
* A stop during start-up, before the server accepts commands, still arrives: the agent
  delivers `stop` as soon as the server accepts an RCON login, and every server runs with
  `CREATE_CONSOLE_IN_PIPE=true`, so the runtime can also hand the stop to the server's console.
* When Docker stops a server on its own (host shutdown, restart of the Docker service, a plain
  `docker stop` or `docker restart` without a time), the server gets
  min(its stop timeout, `AGENT_SHUTDOWN_STOP_TIMEOUT`) seconds, 20 by default. The agent is not
  involved then, so there is no `save-all flush` first. Keep 20 on Unraid
  (docs/UNRAID-SHUTDOWN.md explains why).
* A container created before this behaviour existed has no such timeout (Docker's default of
  10 s applies). The panel shows **restart required** for it; its next start or restart recreates
  the container. World data is not touched.

On Unraid, install the shutdown hook so that servers are saved and stopped through the panel
before Docker stops: [docs/UNRAID-SHUTDOWN.md](docs/UNRAID-SHUTDOWN.md).

### Backups are not automatic unless a schedule exists

* The create wizard has **Nightly backup** switched on: it adds a backup schedule that runs
  once a day between 03:01 and 04:59 panel time, at a minute derived from the server's id, also
  while the server is stopped.
* A server without that schedule (made by an older version, or with the switch turned off) is
  only backed up by hand. The dashboard, the server list and the server's Overview and Backups
  pages show **No backup yet**, or **Last backup N days ago** once the newest backup is older
  than 7 days.
* `docker compose exec panel python manage.py ensure_backup_schedules --dry-run` lists the
  servers without an enabled backup schedule; without `--dry-run` it adds the nightly schedule
  to each of them. Archived servers are skipped. Running it twice changes nothing.

### Console, files and mods

* Typing `stop` in the console stops the server the way the Stop button does and the console
  says so. Sent to the server itself, `stop` would only restart it (Docker's restart policy
  starts it again). It needs the permission to start and stop the server.
* Text files that are not UTF-8 open read-only with a **Download** button, so that saving cannot
  damage them. `server.properties` and other `*.properties` files are read and written as
  ISO-8859-1, as Java does; characters outside ASCII are written as `\uXXXX`.
* A file whose name is not valid UTF-8 is listed (with a replacement character) but cannot be
  renamed, moved or deleted from the panel; do that on the host.
* Deleting or renaming a very large folder can take longer than the panel waits: it then says
  that the node is still working on it. Refresh the list a minute later.
* Mods: jars larger than 2 GiB are not hashed and stay unidentified; while the agent is still
  reading a large mods folder, files are shown as pending. On modpack servers the jars that came
  with the pack are marked as such. Disabling or deleting one is remembered and the modpack's
  re-sync at the next start leaves it out, until the modpack is updated to another release.

### Redis as its own container (optional)

Redis can run as a fourth container again, as it did before it moved into the panel container.
Use this if Redis inside the panel container ever misbehaves. Put both lines into `.env`:

```
COMPOSE_PROFILES=external-redis
MCPANEL_REDIS_URL=redis://redis:6379/0
```

and run `./scripts/upgrade.sh run --no-build`. The panel then starts no Redis of its own
(`mcpanel-supervise.py status` lists four processes). To go back, remove the two lines and run
`./scripts/upgrade.sh run --no-build` again: it also removes the `redis` container, which a plain
`docker compose up -d --remove-orphans` leaves running. `--no-build` only applies the change (no
pull, no build, no database dump) and leaves the rollback point of the last upgrade as it is.

## HTTPS / remote access

The panel speaks plain HTTP. For access from outside your LAN, put an HTTPS reverse proxy in front of it
(any of Nginx Proxy Manager, SWAG, Caddy, Traefik) and set:

```
MCPANEL_BEHIND_TLS_PROXY=true
MCPANEL_SECURE_COOKIES=true
MCPANEL_ALLOWED_HOSTS=panel.example.com
MCPANEL_CSRF_TRUSTED_ORIGINS=https://panel.example.com
MCPANEL_FORWARDED_ALLOW_IPS=192.168.1.10      # the proxy's address as the panel sees it
```

`MCPANEL_FORWARDED_ALLOW_IPS` lists the proxies whose `X-Forwarded-For` header the panel believes.
It defaults to `127.0.0.1,::1` only: Docker and LAN ranges are not trusted automatically, because the
Minecraft containers (third-party mod code) and other machines on your network live there and could
otherwise pretend to be any address (bypassing the login rate limit, faking audit log entries). Until
you set it, every request appears to come from the proxy and all users share one login rate limit;
the panel logs a warning at start. To find the right value, open the panel through the proxy once and
run `docker compose logs --tail 20 panel`: the access log lines start with the address requests came
from. That is the proxy's LAN IP for a proxy on another machine, or the proxy container's Docker
address (for example `172.17.0.5`) for a proxy container on a bridge network. Use that exact address
(a comma-separated list is allowed), never a whole private range and never `*`. Make sure the address
belongs to the proxy alone: a Docker network gateway (`172.x.0.1`, what the panel sees when the proxy
connects to `127.0.0.1` on the host) also stands for every other local process that connects that way.

The proxy must forward WebSockets for `/ws/` (Upgrade/Connection headers). Raise its body-size limit
and its read/send timeouts. Uploads (worlds, archives) can be several GB, and installing a mod with
dependencies or downloading a world zip can take several minutes. For nginx, for example:
`client_max_body_size 4g; proxy_read_timeout 900s; proxy_send_timeout 900s; proxy_request_buffering off;`.
A VPN such as WireGuard or Tailscale is safer than exposing the panel publicly.

Game ports are separate. The panel publishes each server's port on the Docker host only. Players outside
your LAN need a router port forward for that TCP port, which you configure yourself.

## Additional nodes (more Docker hosts)

One panel can manage Minecraft servers on several Docker hosts. Every extra host ("node") runs only
the agent, from `deploy/docker-compose.node.yml`. The panel reaches it over **HTTPS with a pinned
certificate**: the agent generates a self-signed certificate on first start (`AGENT_TLS=auto`, kept in
`<MCPANEL_DATA_DIR>/agent/tls/`), and the panel trusts exactly that certificate. The token is sent only
after the TLS handshake has matched the pin.

On the new host:

```bash
git clone https://github.com/cj7s1/mcserver.git && cd mcserver/deploy
./scripts/generate-secrets.sh --node        # writes node.env (AGENT_TOKEN, AGENT_RESTIC_PASSWORD), mode 600
# edit node.env: MCPANEL_NODE_BIND (required: this host's LAN/VPN IP), MCPANEL_DATA_DIR, MC_UID/MC_GID, port
docker compose -f docker-compose.node.yml --env-file node.env up -d --build
docker compose -f docker-compose.node.yml --env-file node.env exec agent python -m mcagent fingerprint
```

Then register it. In the panel: **System → Nodes → Add node** (name, agent URL, agent token,
certificate fingerprint). Or on the **panel host** (the API `POST /api/nodes` does the same):

```bash
cd mcserver/deploy
read -rs MCPANEL_NODE_TOKEN && export MCPANEL_NODE_TOKEN   # paste AGENT_TOKEN from node.env, press Enter
docker compose exec -e MCPANEL_NODE_TOKEN panel \
  python manage.py add_node --name node2 --url https://<node address>:8700 \
  --fingerprint <the AB:CD:… line printed above> --lan-address <node LAN IP>
unset MCPANEL_NODE_TOKEN
```

`read -s` keeps the token off the screen and out of your shell history (bash, the Unraid default);
`-e MCPANEL_NODE_TOKEN` passes it by name, so it does not appear in the command line either. Don't
use `--token` on the command line for the same reason.

The panel reads the certificate the agent presents and refuses to save the node if it does not match
the fingerprint, then checks that the agent answers. Servers can be created on the node from the create wizard and moved between nodes
(**Settings → Danger zone → Move to another node**). Each node keeps its own restic backups.

Notes:

* The agent port must be reachable from the panel host only. `MCPANEL_NODE_BIND` has no default:
  set it to the LAN or VPN address of the node (the stack refuses to start without it), and do not
  forward the port on your router. A WireGuard/Tailscale link between the hosts is a good fit. Use
  `0.0.0.0` only if a firewall lets nothing but the panel host reach the port.
* If the node's certificate is regenerated (deleting `agent/tls/`), the panel stops talking to it with
  an `agent_tls` error until you paste the new fingerprint into the node.
* Using your own certificate instead: set `AGENT_TLS=files`, `AGENT_TLS_CERT_FILE` and
  `AGENT_TLS_KEY_FILE` in a compose override. Pin it the same way, or leave the fingerprint empty and
  let the panel verify it against public CAs plus `MCPANEL_AGENT_CA_BUNDLE` (a PEM file mounted into
  the panel container), in which case the URL's host name must match the certificate.
* A plain `http://` node still works but the API flags it with a warning: its token and console
  traffic cross the network unencrypted.
* If the **default** node is remote (`MCPANEL_AGENT_URL=https://…`), pin it with
  `MCPANEL_AGENT_TLS_FINGERPRINT` in `.env`.

## Upgrading

```bash
cd mcserver && (umask 022 && git pull)
cd deploy
./scripts/upgrade.sh dry-run     # shows what will happen; changes nothing
./scripts/upgrade.sh run         # or the exact command the dry run ended with
```

`git pull` writes the files it changes with the umask of your shell; `umask 022` keeps them
writable by their owner only. Before anything else, `run`, `rollback` and `restore-db --yes` refuse
(exit 2) while group or others can write a file or folder git tracks in the checkout, the
checkout's folder, `.git`, `.git/config`, `.git/hooks` or `deploy/.env`; `status` and the dry runs
only warn. The message lists them and names the repair, `chmod -R go-w <checkout>`. The script runs
git without fsmonitor and hooks (`-c core.fsmonitor= -c core.hooksPath=/dev/null`) and under
`umask 022`.

The dry run ends with `Nothing was changed. To do it: ...`. Use the command it prints. When the new
version changes the database schema, that command is `./scripts/upgrade.sh run --with-migrations`,
and a plain `run` refuses before it changes anything. **The version that brought the single panel
container changes the schema** (one migration), so the first upgrade to it needs
`--with-migrations`.

Building the images takes several minutes. A terminal session that is closed during the build
ends the run, and nothing was changed: run it again. From the switch on (step 6 below) the script
does not need the terminal any more: it ignores the hangup, starts `docker compose` out of its
reach, finishes the switch and ends as it would have. `rollback` and `restore-db` do the same
from their first change on. What such a command printed is in `deploy/upgrade-log-<time>.log`;
after you are back, `./scripts/upgrade.sh status` names that log and says whether the command is
still at work and how it ended. While it is at work, another `run`, `rollback` or `restore-db`
refuses. Ctrl-C and a kill still end the script ("A run that did not finish" below).

`upgrade.sh` needs only bash, docker with the compose plugin and git. Running Minecraft servers
are not touched by an upgrade or a rollback: they are not part of the Compose project. The panel
is away for under a minute.

| Command | What it does |
|---|---|
| `./scripts/upgrade.sh status` | Changes nothing. Shows the revision of the source tree and the revision that is deployed (read from the panel container that runs; without one it says that the panel is not running), a warning when the two differ, whether the git remote has newer commits, every container of the stack with state, health and image, the processes inside the panel container, queued and running jobs, the log of the last `run`, `rollback` or `restore-db` with how it ended (or `STILL AT WORK`), and the rollback point (with `IS GONE` next to a saved image that was deleted). |
| `./scripts/upgrade.sh dry-run` | Changes nothing. Shows what `run` would do: from which revision to which, the containers that would be stopped and removed, new database migrations, the notice about stopped servers (below), whether jobs would make `run` refuse, and the output of `docker compose --dry-run up`. Ends with the exact `run` command. |
| `./scripts/upgrade.sh run` | Builds the version of the source tree and switches to it (steps below). |
| `./scripts/upgrade.sh rollback` | Goes back to the revision and the images from before the last `run`. `rollback --dry-run` only shows what it would do. |
| `./scripts/upgrade.sh restore-db <file>` | Puts a database dump of an earlier `run` back. Only for the case that `rollback` cannot undo a migration. It ends with the commands that start the stack again. |

### What `run` does

It stops with a message if a step fails. Until step 6 nothing is switched: a run that ends before
it points the image names back at the old images, and the old version keeps running.

1. Refuses a version that changes the database schema unless `--with-migrations` was given. It
   compares the migration files of the two revisions, before anything is recorded, dumped, pulled
   or built. Then it refuses while a job is queued or running (it asks the database; `--force`
   skips this check).
2. Records the rollback point: the git revision that is deployed, the image of every container
   of the stack (each tagged `mcpanel-rollback/<name>:<time>`) and the state of the database
   migrations.
3. Dumps the database without login sessions and copies `.env` next to the dump.
4. Pulls the upstream images (`postgres`) and rebuilds panel and agent on fresh base images
   (`docker compose build --pull`), so security fixes of the base images and of Redis arrive.
5. Asks the new image whether it changes the database schema, and refuses here if it does
   although step 1 saw no new migration file (unless `--with-migrations`).
6. Checks for jobs once more. Stops the containers of services the new version no longer has
   (up to 2 minutes, all at once) and removes them, then `docker compose up -d --remove-orphans`.
   Before the `up` it removes what an earlier `up` that was cut off left half replaced (below).
   If `up` ends without an error and a service still has no started container, it runs `up` once
   more and then stops with a message that names the service, instead of waiting for it.
7. Waits until every service is running and healthy (300 seconds, `--timeout` changes it), checks
   that no container of an older layout is left and that exactly one scheduler runs, and prints
   what changed: per image name the image of the rollback point and the image of the container
   that runs now. The rollback tags of the upgrade before this one are dropped.

Options:

| Option | For | Meaning |
|---|---|---|
| `--with-migrations` | `run` | The new version may change the database schema. |
| `--force` | `run`, `rollback` | Do not refuse because of queued or running jobs. Running jobs get 2 minutes and are marked failed after that. |
| `--resume` | `run` | Continue a run that did not finish, with its rollback point. |
| `--from <rev>` | `run`, `dry-run` | The git revision that is deployed now, if the script cannot tell (below). |
| `--no-pull` | `run` | Build with the base images already on this host (no internet needed). |
| `--no-build` | `run`, `dry-run` | Only apply a change of `.env` or of the compose files: no pull, no build, no database dump, and the rollback point stays as it is. |
| `--no-backup` | `run`, `rollback` | Skip the database dump (of `run`: step 3; of `rollback`: the dump it takes first). |
| `--timeout <seconds>` | `run`, `rollback` | How long to wait for healthy (default 300). |
| `--dry-run` | `rollback` | Only show what it would do. |
| `--archive-other-games` | `rollback` | Going back to a release from before multi-game: archive the servers of games other than Minecraft first ("Rollback with servers of other games" below). |
| `--yes` | `restore-db` | Do it. Without it the command only says what it would do. |

### Files the script writes

All in `deploy/`, mode 600 and ignored by git. Keep the folder on persistent storage and do not
share it.

| File | Content |
|---|---|
| `upgrade-state.env` | The rollback point and the state of the last run. Do not edit it. |
| `upgrade-backup-<time>.sql` | The database dump of step 3 of `run`, or the one `rollback` takes first: users (password hashes), server definitions, schedules, backup metadata and the encrypted secrets. Login sessions are left out. |
| `upgrade-backup-<time>.env` | A copy of `.env` from the same moment. Its `MCPANEL_SECRET_KEY` decrypts the secrets in the dump, so the two files together are as sensitive as the running panel. |
| `upgrade-log-<time>.log` | Everything a `run`, `rollback` or `restore-db` printed, the output of `docker compose` included, with the exit status in its last line. It is written from the first change of that command on (with what was printed before); a command that refuses or only shows something leaves none. |

The newest three dumps are kept (and always the two the state file names: `BACKUP_SQL` of the last
`run`, `ROLLBACK_SQL` of the last `rollback`); older ones are deleted with their `.env` copy. To write the dumps
somewhere else, start the script with `MCPANEL_UPGRADE_BACKUP_DIR=<folder>` set. The newest ten
logs are kept.

The states in `upgrade-state.env` (`status` shows the current one):

| State | Meaning | What to do |
|---|---|---|
| `started` | The rollback point is recorded and nothing was switched. The run ended between step 2 and step 6 (a failed build, a job that started meanwhile, a closed terminal). The old version is running. | `run` again. It goes on with the recorded rollback point and makes no new one. |
| `switching` | The switch began and did not finish (`docker compose up` failed, the new version did not become healthy, the script was killed). The stack may be half old, half new. If `status` says `STILL AT WORK`, the run is going on without its terminal: wait for it. | `rollback` to go back, or `run --resume` to go on. |
| `done` | The last run finished. | Nothing. `rollback` is possible. |
| `rolling-back` | A rollback began and did not finish. | `rollback` again to finish it, or `run --resume` to go forward again. |
| `rolled-back` | The last run was rolled back. The old version runs; the source tree is at its revision (detached HEAD) until you check out your branch again. | `git checkout <your branch>`, then `run` to upgrade again. In the branch checkout do not run `docker compose up -d` by hand ("Rollback" below). |

### A run that did not finish

Run `./scripts/upgrade.sh status`. It names the state and what to run next. If the run was ended
by Ctrl-C, a kill or, before the switch, a closed terminal, it also says when and during which
step, and the log it names ends with the exit status. A plain `run` never makes a new rollback
point on top of an unfinished run, so the way back to the version from before the upgrade is
kept. In the states `switching` and `rolling-back` a plain `run` refuses until you chose
`rollback` or `run --resume`.

A `docker compose up` that is cut off while it replaces a container (a `kill -9`, a host that
goes down) leaves the replacement behind under the temporary name Compose gives it
(`<12 hex digits>_mcpanel-panel-1`, state `created`) next to the stopped container it was to
replace. `rollback` and `run --resume` remove such a leftover before their own
`docker compose up`, so each of them works at the first attempt. The script itself can no longer
cause this through a closed terminal or Ctrl-C: from the switch on `docker compose` runs in a
session of its own (`setsid`), out of reach of both, and the script acts on Ctrl-C or a kill only
when the `docker compose` command that is running has ended. (On a host without `setsid` it uses
`nohup`, which keeps the hangup away but not Ctrl-C.)

### The deployed revision (`--from`)

The rollback point needs the git revision that is running now. Images built by this script carry
it as the label `io.mcpanel.revision`. The first time (images built by hand, no label) the script
takes the revision the checkout was at before your last `git pull`, checks that its compose file
has exactly the services that have containers now, and says so (`from: ... [assumed ...]` in the
dry run). Check that line. If it is wrong, or the script says it cannot tell (after two pulls in
a row, for example), pass the revision: `./scripts/upgrade.sh run --from <revision>` plus the
other options the dry run named. To find it:

```bash
# an image built by this script carries the revision; an image built by hand prints nothing useful
docker inspect -f '{{index .Config.Labels "io.mcpanel.revision"}}' mcpanel-panel-1
# otherwise: the commit the checkout was at before you pulled
git reflog -n 20     # lines "pull: Fast-forward"; the entry below the first pull since the last deployment
```

### Rollback

`./scripts/upgrade.sh rollback` takes the last `run` back. In this order it:

1. checks that the saved images still exist and that the source tree has no local changes
   (commit or stash them first);
2. asks the upgraded version whether every database migration of the upgrade can be undone, and
   stops nothing if one cannot (it then prints the `restore-db` commands). When the rollback
   goes back to a release from before multi-game it asks about the servers of other games too
   and refuses while there are any (next section). It also refuses while jobs are queued or
   running (`--force` skips that);
3. dumps the database as it is now (like step 3 of `run`, mode 600; `--no-backup` skips it and
   says what that means). From here on every failure names the way back with that dump,
   `./scripts/upgrade.sh restore-db <that dump> --yes`, which loses nothing the panel wrote since
   the upgrade; the dump of `run` is older;
4. stops everything but the database (up to 2 minutes) and undoes those migrations. The data
   stays; only what was written into columns the old version does not have is lost;
5. checks out the recorded revision (the checkout is then on a detached HEAD);
6. points the image names back at the saved images and brings the old layout up, without
   building (with the same care as step 6 of `run`: leftovers of an `up` that was cut off are
   removed first, and a service that was not started is a failure at once).

From step 3 on a closed terminal does not stop the rollback; its log is
`deploy/upgrade-log-<time>.log`.

Not undone: `.env` (the copy next to the dump shows what it was), what the panel wrote to the
database since the upgrade, and the containers of stopped servers that the new version removed
(the old version creates one at the next Start).

After the checkout the script itself may be gone from disk, because older revisions do not have
it. It finishes from memory and tells you. `git checkout <your branch>` brings it back; to
upgrade again later run the command the rollback printed at its end (`git checkout <your branch>`,
then `./scripts/upgrade.sh run`, with `--with-migrations` if the first run needed it).

Which checkout you are on matters after a rollback, and the rollback says so in its last lines:

* Right after it the source tree is at the old revision (a detached HEAD). It fits what runs:
  `docker compose up -d` and the other `docker compose` commands are safe there.
* After `git checkout <your branch>` the tree is the new version while the old one runs. Do
  **not** run `docker compose up -d` in that checkout: it would start the new compose file on the
  old images, and the panel would not start. Use `./scripts/upgrade.sh` there. `status` prints a
  warning whenever the source tree and the deployed version differ and names the checkout that
  fits what runs (`git checkout --detach <revision>`).

If you use the Unraid shutdown hook, remove it before you stay on a version without it
(docs/UNRAID-SHUTDOWN.md).

### Rollback with servers of other games

A release from before multi-game knows only Minecraft servers: its database has no `game` column.
Undoing the migrations of multi-game would drop what a Factorio, Satisfactory or Terraria server
is (its game and variant, named ports, pinned settings, secrets, setup state). The old panel
would then take such a server for a Minecraft server and start the Minecraft image in its folder.
The old database also allows each game port number only once per node, so a UDP and a TCP server
that share a number (allowed since multi-game) make undoing the migrations fail. So `rollback`
(and `rollback --dry-run`) asks the upgraded version first, before it stops anything, and refuses
while such servers exist. It lists them (id, name, game) and the port numbers that servers share,
and names the three ways on:

* **Keep the new version.** Nothing to do.
* **`./scripts/upgrade.sh rollback --archive-other-games`.** With the stack still running, for each
  server of another game the panel runs its own Stop job (the agent's graceful stop: the game
  saves first) and its Archive job, disables its schedules and copies its row (the stored
  ciphertext of its secrets included) into the table `mcpanel_rollback_other_games`. Django's
  migrations do not manage that table, so the rollback leaves it alone and every later database
  dump has it. A server that shares its port number with another server of the node gets a
  number above 65535 while the old version runs (never a real port: the old version cannot
  publish it); the listing says which. Player-history names longer than 16 characters (the old
  limit) are put aside in `mcpanel_rollback_player_names`. Only then is the rest of the stack
  stopped and the migrations undone, after the upgraded version was asked once more whether they
  can be undone now. A job that holds such a server makes the rollback refuse
  (also with `--force`); a stop or archive that fails ends the rollback with nothing stopped and no
  migration undone: the servers archived so far stay archived, and `rollback --archive-other-games`
  goes on with the rest once the problem is fixed.
* **The database dump from before the upgrade** (`restore-db`, below). The servers of other games
  are not in it. The refusal prints its commands under this choice, or says that it is no choice when
  that upgrade took no dump (`--no-backup`, or the database was down).

The database migrations guard this themselves: undoing `instances 0006` or `0005` refuses while a
server of another game exists that the archive step has not recorded (also when they are undone
by hand with `manage.py migrate`), and `manage.py migrate --plan` shows
`IRREVERSIBLE (other games)` for them meanwhile, so `rollback` stops nothing.

When the rollback undoes `instances 0007` (right before 0006 and 0005), every recorded server of
another game is moved to a node the rollback creates, **"parked by upgrade.sh rollback (other
games)"** (agent URL `http://127.0.0.1:1/parked-by-upgrade-rollback-other-games`: nothing listens
there, no token is sent; not the default node). The old version took a Terraria server for a
Minecraft server, and its Unarchive + Start ran the Minecraft image in that folder (real-stack run of
2026-11-10). On the parked node, Start, Recreate, Clone, Move to another node, Delete and file access
of such a server fail at once with "Cannot reach the node agent at
http://127.0.0.1:1/parked-by-upgrade-rollback-other-games: ..." and change nothing; Unarchive and the
listing work. Delete such servers after the next upgrade, not in the old version. Leave the parked
node alone in Settings -> Nodes: it shows that error and cannot be deleted while servers are on it.
Parked servers that share a game port number get numbers from 65536 up while the old version runs,
and the port numbers of parked servers are not reserved on their own node meanwhile (check ports
after the next upgrade).

The next `./scripts/upgrade.sh run --with-migrations` puts everything back: the database migration
`instances 0007` restores each recorded server that still exists (game, variant, ports, settings,
secrets, setup state, control secret, port number, its own node, and the fields the old version may
have changed for a Minecraft server), enables the schedules the archive step disabled, puts the long
player names back, drops both tables and deletes the parked node once no server is on it (a server
whose node was deleted meanwhile stays parked). The servers stay archived; unarchive them in the
panel (check their ports first if you created servers while the old version ran).

### Rollback images: do not prune them

The rollback point is only usable while its images exist. They have no container, so:

* a plain `docker image prune` keeps them (they are tagged);
* `docker image prune -a`, `docker system prune -a` and Unraid's removal of "orphan image" rows
  delete them. `status` then shows `IS GONE (rollback is not possible)`.

The script keeps the images of one rollback point and drops the tags of the one before it after
a successful `run`. Images that no name refers to any more show up as `<none>` in `docker images`
and go away with `docker image prune`.

### `restore-db`

`./scripts/upgrade.sh restore-db upgrade-backup-<time>.sql --yes` loads a dump written by step 3
of `run` into a new, empty database and, when all of it went in, swaps the two. The
database as it was is kept under the name `<database>_before_restore_<time>`, and the command
prints how to remove it. Everything but the database container must be stopped first; the command
names the `docker compose stop ...` line. Everything the panel wrote after the dump was taken is
not in it, and everybody has to log in again.

The stack is still stopped then. `restore-db` ends with the commands that start it again, and
they depend on where the last run ended (the state in `upgrade-state.env`):

| State | What `restore-db` prints as the next step |
|---|---|
| `done` (the upgrade is deployed) | `./scripts/upgrade.sh rollback`, to finish going back. `docker compose up -d` instead keeps the upgraded version: the panel applies the migrations of the upgrade to the restored database when it starts. |
| `switching`, `rolling-back` | `./scripts/upgrade.sh rollback` or `./scripts/upgrade.sh run --resume`. Not `docker compose up -d`: the stack is half old, half new. |
| `rolled-back`, `started` (the version from before the upgrade is deployed) | If the source tree is at that revision: `docker compose up -d`. Otherwise (the usual case, because the script only exists in the newer tree): `cd <source> && git checkout --detach <old revision> && cd deploy && docker compose up -d`. A plain `docker compose up -d` in the newer tree would start the new compose file on the old images. |
| no state file | `docker compose up -d` |

### Stopped servers at the first upgrade

A stopped server needs no container any more ("Minecraft server containers" above). On a host
that still has exited server containers, `dry-run` and `run` say how many there are. With the
default setting the new panel removes them within seconds of its first start; world data is not
touched and the last 2,000 console lines of each are kept. To look first, add
`MCPANEL_RETIRE_STOPPED_CONTAINERS=false` to `.env` **before** the run. After the upgrade
`docker compose exec panel python manage.py retire_stopped_containers --dry-run` lists what would
be removed. Remove the line again and run `./scripts/upgrade.sh run --no-build` to turn the
automatic removal on.

Every server that is running during the upgrade shows **restart required** afterwards, although
none of its settings changed: its container was made by the older version ("Stopping servers"
above). Restart it when it suits you.

### The data folder is root-only after the upgrade

The agent of this release makes the data directory (`MCPANEL_DATA_DIR`) and its `instances` folder
`root:root 0750` at every start: the server folders hold RCON passwords. Afterwards only root can
open them on the host. **An SMB share of that folder, Unraid's file manager, other containers that
run as `nobody` or another user (Krusader, backup containers) and non-root shells lose access to
it**, also for named SMB users. A `chmod` by hand is undone at the next agent start. Use the
panel's Files page (or a root shell) to edit files, and backups or the panel's zip download to copy
worlds off the host; a backup tool that reads the folder directly must run as root. See
docs/UNRAID.md, section 2.

### Other things to know

* Run `./scripts/upgrade.sh run` at least monthly even without new commits: it rebuilds on current
  base images.
* Additional nodes: `docker compose -f docker-compose.node.yml --env-file node.env build --pull`,
  then `... up -d`. The certificate (and so the pin) survives upgrades because it lives in the
  data directory. Update the agents together with the panel. While a node's agent is older than
  the panel, the containers of stopped servers on that node are kept and its servers keep showing
  "restart required". This mix of versions is handled in the code and has not been run.
* Do not upgrade with a plain `docker compose up -d --build`: it never removes containers of
  services that no longer exist. Coming from the layout with separate `worker` and `beat`
  containers, the new panel container notices them, logs how to fix it and refuses to start;
  `./scripts/upgrade.sh run` (or `docker compose up -d --remove-orphans`) removes them.
* `docker compose pull` only fetches `postgres`: the panel and agent images are built on your host
  and are never pulled from a registry.
* Migrations run automatically when `panel` starts, before its processes do.
* The upgrade that brings `restart: always` recreates the three platform containers (their
  configuration changed); `upgrade.sh run` does that. The Minecraft servers are not touched.

**Coming from the 7-container layout** (`panel`, `worker`, `worker-periodic`, `beat`, `agent`,
`postgres`, `redis`): the same commands, with `--with-migrations`. `run` removes the four
containers that are no longer needed; `rollback` leads back to the 7 containers. If you want to
take it in two steps, put the two lines from "Redis as its own container" into `.env` before the
first `run` (4 containers: `redis` stays), and remove them a week or so later and run
`./scripts/upgrade.sh run --no-build` (3 containers). `./scripts/upgrade.sh rollback` still leads
back to the 7 containers after that, because `--no-build` keeps the rollback point. The old Redis
volume (`mcpanel_redis_data`) is kept; remove it when you are sure you will not roll back:
`docker volume rm mcpanel_redis_data`.

The compose file uses a tag for the database (`postgres:16-alpine`), which moves to new patch
releases when it is pulled. To pin an exact build instead, put a digest in a
`docker-compose.override.yml`, e.g. `services: {postgres: {image: "postgres:16-alpine@sha256:<digest>"}}`,
taking the digest from `docker image inspect --format '{{index .RepoDigests 0}}' postgres:16-alpine`
after a pull. Update the digest yourself on every upgrade; a pinned image never gets security fixes
on its own. Don't change the Postgres major version (16) without a dump and restore. The panel
and agent images are built from `python:3.13-slim-trixie`, the panel also from `redis:7.4`: a
rebuild brings their fixes, never a new Debian release or Redis major by surprise.

## Backing up the panel itself

* **Database:** it holds users (password hashes), server definitions, schedules, backup metadata and
  the encrypted secrets. `./scripts/upgrade.sh run` dumps it into `deploy/` at every upgrade. To
  take a dump at any other time, write it into a private directory, not a network share:

  ```bash
  cd mcserver/deploy
  (umask 077 && mkdir -p ~/mcpanel-db-backups && \
   docker compose exec -T postgres pg_dump -U mcpanel --exclude-table-data=django_session mcpanel \
     > ~/mcpanel-db-backups/mcpanel-db-$(date +%F).sql)
  ```

  `umask 077` makes the file readable by you only. `--exclude-table-data=django_session` leaves out
  the login sessions: with them, anyone holding the dump could act as a logged-in user until the
  sessions expire. The dump plus `.env` (whose `MCPANEL_SECRET_KEY` decrypts the stored secrets) is
  everything needed to impersonate the panel, so store both like passwords.
* **Daily, off the cache drive: `deploy/unraid/backup-db.sh`.** It runs `pg_dump` in the postgres
  container of the Compose project (without the login sessions), writes
  `mcpanel-db-<date>-<time>.sql.gz` and next to it `mcpanel-db-<date>-<time>.env`, a copy of
  `deploy/.env`, into its destination folder, and keeps the newest 14 of each pair. The folder is
  made 0700 and the files 0600. A dump that `pg_dump` did not finish is never kept. Root runs it,
  so it ends with `FAILED`, writing nothing, while group or others can write the script, its folder
  or the `.env` it copies; the message names the repair (`chmod -R go-w <checkout>`).

  ```
  backup-db.sh [--project NAME] [--env FILE] [--keep N] [DEST_DIR]
  ```

  | Argument | Default |
  |---|---|
  | `DEST_DIR` (absolute path) | `/mnt/user/Backups/mcpanel-db` |
  | `--project` | `mcpanel` (the Compose project, `docker compose ls`) |
  | `--env` | found through the postgres container's Compose label (the project folder), else `../.env` next to the script |
  | `--keep` | 14 |

  On Unraid, with the **User Scripts** plugin: Settings → User Scripts → **Add New Script**, name
  it `mcpanel-db-backup`, edit it to contain

  ```bash
  #!/bin/bash
  bash /mnt/user/appdata/mcpanel-src/deploy/unraid/backup-db.sh /mnt/user/Backups/mcpanel-db
  ```

  (adjust the path to your checkout; the destination should be an array share, not the cache
  pool that holds the database) and set its schedule to **Scheduled Daily**. Run it once with
  **Run Script** and read the last line: `mcpanel-db backup: OK ...` (exit status 0) or
  `mcpanel-db backup: FAILED ...` (exit status 1, nothing half-written left behind). The `.env`
  copy holds every secret of the stack in clear text, including `MCPANEL_SECRET_KEY` and
  `AGENT_RESTIC_PASSWORD`: do not export that share over SMB or NFS without a password, or
  choose a share that is not exported. Restoring after a lost cache drive: docs/UNRAID.md,
  "When the cache drive died".
* **`MCPANEL_DATA_DIR`:** server files. These are best protected with in-panel backups plus your normal NAS backup.
* **`MCPANEL_BACKUP_DIR`:** restic repositories, which are encrypted with `AGENT_RESTIC_PASSWORD`.
  Keep a copy of that password outside the host (a password manager): without it no backup can
  be restored. `backup-db.sh` copies it with `.env`; a generated password
  (`<MCPANEL_DATA_DIR>/agent/restic-password`) is not in `.env` and must be copied separately.
* **`deploy/.env`:** your secrets (mode 600, git-ignored like `deploy/node.env`).

## Troubleshooting

| Symptom | Check |
|---|---|
| Browser shows "Bad Request (400)" | Add the host you typed to `MCPANEL_ALLOWED_HOSTS`. |
| Login works but actions fail with CSRF errors | Add the exact origin (`http://ip:port`) to `MCPANEL_CSRF_TRUSTED_ORIGINS`. |
| "Node agent unreachable" | `docker compose logs agent`. Check that the token in `.env` is the same for panel and agent (both read `MCPANEL_AGENT_TOKEN`). |
| Server stuck in "Installing" | Open the console. The first start downloads the server and mods, which takes minutes for big packs. Crash loops are detected automatically and shown as *Failed* with a diagnosis. |
| Server "Failed: crash loop" | Overview → Diagnostics lists the cause (missing mods, Java version, memory…). |
| Port conflict on start | Another container or program uses the port. Change it in the server's Network tab. |
| Files owned by the wrong user | Set `MC_UID`/`MC_GID` and restart the server (the runtime fixes ownership on start). |
| Jobs never run | `docker compose exec panel python /usr/local/bin/mcpanel-supervise.py status`: `worker` and `beat` must be running; `docker compose logs --tail 200 panel \| grep -F '[worker]'`. Stuck jobs can be marked failed in Activity. |
| `panel` is "unhealthy" | `docker inspect -f '{{range .State.Health.Log}}{{.Output}}{{end}}' mcpanel-panel-1` names the process and the reason. |
| `panel` restarts in a loop after an upgrade, log says "containers of the previous layout are still running" | `./scripts/upgrade.sh run` (or `docker compose up -d --remove-orphans`). |
| A stopped server has no entry in `docker ps -a` | Expected: a stopped server has no container ("Minecraft server containers" above). Start it in the panel. |
| A server shows "restart required" although nothing was changed | Its container was made by an older MCPanel version. The next start or restart recreates it; world data is kept. |
| A server stays "busy" after the panel container was stopped during a job | The job is marked as failed about 6 minutes later, or at once with **Mark as failed** on the Activity page. |
| A start says "Not enough memory on node ..." | The running servers plus this one exceed the host's RAM minus the reserve ("Memory" above). Stop another server, lower this one's memory, use **Start anyway**, or lower **Memory kept free for the host** in System settings. |
| A schedule says "Waiting: ..." or "Missed: ..." | "Waiting": its server was busy; it is tried every minute for an hour. "Missed": a power action or command whose time passed while the panel was down ("Schedules" above). |
| A schedule is "Paused: the cron expression ... never matches a date" | It was saved by an older version with a date that does not exist. Correct the expression and resume it. |
| A server restarted after `stop` was typed into it from another tool | Docker's restart policy starts a server that exits by itself. Stop it in the panel (the panel's console turns `stop` into a stop). |
| `./scripts/upgrade.sh run` says the last run did not finish | `./scripts/upgrade.sh status` names the state and the two commands to choose from ("A run that did not finish" above). |

Logs: `docker compose logs -f panel agent`. Each server's own log is in its console and in
`logs/` in the file manager.
