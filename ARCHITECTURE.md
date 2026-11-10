# Architecture

MCPanel is split into a **panel** (UI + API + worker, unprivileged) and a **node agent**
(the only component that touches Docker and host storage). This is the same split as
Pterodactyl's panel/Wings. One panel can manage several hosts, each with its own agent.

```
 Browser ──HTTPS(reverse proxy, optional)──▶ panel container (UID 10001, no capabilities, no Docker socket)
                                             ├─ web       Django ASGI: REST + WebSockets + SPA
                                             ├─ worker    Celery: jobs
                                             ├─ periodic  Celery: reconcile loop, schedules, watchdog, metrics
                                             ├─ beat      Celery scheduler
                                             └─ redis     cache, Celery broker, Channels (Unix socket only)
                                               │  Postgres container (state)
                                               │  Bearer token, internal network only
                                               ▼
                                agent container (FastAPI) ──▶ Docker Engine ──▶ itzg/minecraft-server containers
                                   │                                      ▲ RCON 25575 (container network only)
                                   └── instance + backup storage (bind-mounted host paths, restic repos)
```

Three containers: `panel`, `agent`, `postgres`. The five processes of the panel container run under
a small supervisor (`backend/container/supervise.py`, standard library only, started by the
entrypoint's `all` mode after the migrations). They used to be five containers; they never were
isolated from each other in any way that mattered (same image, same secrets, same network, same
user), so one container costs no secret or network isolation. What is shared now is the file system
and the process space of the web process, the workers and the scheduler; for that reason the
scheduler keeps its schedule in memory (no pickle file the web process could write), Redis listens
on a Unix socket in a directory of the runtime user only and is started without the panel's
environment. The supervisor restarts a process that exits (capped backoff, never gives up), starts
Redis first and stops it last, restarts the other processes after a Redis restart, prefixes every
log line with the process name, pings both workers and writes a status file that the container
health check and `/api/health` read. The entrypoint still has the one-role modes (`web`, `worker`,
`beat`) for running the roles as separate containers.

On a stop the supervisor works in stages: web, scheduler and job worker first (the job worker
gets 110 seconds to finish its jobs), then the periodic worker, then Redis. Everything is gone
after 116 seconds at the latest, inside the container's `stop_grace_period` of 2 minutes. After a
restart of Redis alone the job worker is not killed: it finishes the job it is running, without
a time limit, and is started again when it has exited.

Health: the container is healthy when the database and the cache answer, every child runs, both
workers answered a ping within 2 minutes and the reconcile task started within the last 3
minutes (`backend/container/healthcheck.py`). The reconcile task records when it starts
(`mcpanel/celery.py`, `tick_age`); `/api/health` reports that as `tick_age_seconds`, next to
`components` (the state of each child, from the supervisor's status file).

Redis keeps nothing on disk. Job state is in Postgres; when the job worker becomes ready it sends
every job that is still queued again (`mcpanel/celery.py`, `apps/jobs/tasks.py`), so a restart of
the panel container or of Redis loses no job. Lost at a restart: webhook deliveries waiting in the
queue and the crash-loop counters kept in the cache. With the Compose profile `external-redis`
Redis runs as its own container and the supervisor has four children.

## Components

| Component | Code | Responsibilities |
|---|---|---|
| Frontend | `frontend/` (React, Vite, Tailwind, shadcn/ui, TanStack Query, xterm.js) | All screens. Talks only to `/api` and `/ws` on the panel. |
| Management API | `backend/` (Django 5.2, DRF, Channels) | Auth, permission matrix, instance records, provider integrations, file/console proxying, audit log, OpenAPI at `/api/docs`. |
| Workers | same image and container: `celery -A mcpanel worker` (jobs), a second worker for the `periodic` queue, and `beat` | Execute **jobs** (install, start/stop/restart, recreate, backup, restore, clone, world replace/reset, delete), the 10 s reconcile loop (status, retiring the containers of stopped servers), the minute tick (cron schedules, starting servers again after a host shutdown) and the job watchdog. |
| Node agent | `agent/` (FastAPI, docker SDK, restic) | Narrow authenticated API: apply container spec, power (graceful stop), retire a stopped server's container and keep its last-run record, status/stats/logs, RCON, confined file ops, hash-verified downloads, restic backup/restore, btrfs/ZFS snapshots, world replace/reset, storage capability detection. `GET /v1/info` lists `features` so that a newer panel can work with an older agent. |
| Runtime | `itzg/minecraft-server` | Installs Vanilla/Paper/Purpur/Fabric/Forge/NeoForge/Quilt and Modrinth/CurseForge/FTB modpacks. We do **not** implement our own installer. |

## Key design decisions (do not revisit without evidence)

1. **Agent holds the Docker socket; the panel never does.** The agent validates every request
   independently: image allow-list (`itzg/minecraft-server` only), no privileged mode, `cap_drop: ALL`
   plus the five capabilities the itzg entrypoint needs, `no-new-privileges`, only the instance
   directory mounted at `/data`, ports within the allowed range, allowed networks only, memory cap.
   The spec schema (`agent/mcagent/schemas.py`) cannot even express extra mounts, devices or caps.
2. **Desired vs observed state.** `Instance.desired_state` is what the user asked for;
   `status` is computed by `backend/apps/instances/state.py` from the container state, the Docker
   health check (`mc-health`, a real Minecraft status ping) and the active job. A running container is
   *not* a ready server: status is `installing` until the first successful health check, then
   `starting`/`running`/`unhealthy`. A stopped server has no container; its status then comes from
   the record the agent kept of its last run (see "Container lifecycle").
3. **Jobs with per-instance locks.** Every state-changing long operation is a `Job`. Exclusive jobs
   atomically claim `Instance.active_job` (`select_for_update`), so conflicting operations get HTTP 409
   `instance_busy`. A job found `running` when a worker picks it up was interrupted and is failed
   rather than blindly re-run (important for restores). A restart/stop that first warns players in
   game (1–15 min) is enqueued **non-exclusive** and takes the lock only for its last 10 seconds and
   the action (`runner.claim_lock`, waiting up to 20 min for another exclusive job), so a long warning
   never blocks backups or other operations; its waits are cancellation-aware (`JobContext.sleep_until`
   raises `JobCancelled` once the job is cancelled or abandoned). See `apps/instances/power_warnings.py`.
   Job messages can be lost (Redis keeps nothing on disk), so queued jobs are sent again: when the
   job worker becomes ready (`jobs.tasks.resend_queued_jobs`, called from `mcpanel/celery.py`), and
   by the watchdog for a job that is still queued after 15 minutes (it fails the job after 2 hours).
   `runner.dispatch` does not fail the request when the broker is unreachable at enqueue time: the
   job stays queued. `run_job` claims a job under a row lock, so a second message for it does
   nothing. The watchdog (every 2 minutes) fails a running job without a heartbeat for 5 minutes as
   `interrupted`, and settles backup rows whose job ended without finishing them
   (`backups.handlers.settle_orphaned_backups`): a snapshot the agent completed anyway is taken
   over, anything else is marked failed.
4. **server.properties is a file, not env.** itzg rewrites properties from env on every start, so the
   panel only sets env for what it owns (port, RCON, type/version, memory, JVM) and edits
   `server.properties` directly for everything else. Initial values are written before first start.
5. **RCON for commands**, password fixed per instance (encrypted in DB) and read by the agent from the
   container config. RCON is never published to the host.
6. **Modpacks are pinned.** We store provider, project id and version/file id and pass them to itzg
   (`MODRINTH_VERSION`, `CF_FILE_ID`, `FTB_MODPACK_VERSION_ID`), so restarts never silently upgrade.
   A periodic check (`apps/modpacks/updates.py`, at most every 6 h per server, staggered) only *records*
   the newest release for the pinned Minecraft version and loader and notifies once per release;
   updating stays an explicit job.
7. **Backups use restic** (one repository per instance, encrypted, deduplicated). Running servers are
   coordinated with `save-off` → `save-all flush` → backup → `save-on` (always re-enabled in `finally`).
   Restores go into a staging directory first and are swapped in only after restic succeeds; a
   pre-restore safety backup is taken by default. Restores and clones keep the folder kind
   (`snapshots.prepare_replacement`): a btrfs subvolume is replaced by a new subvolume built next to it,
   a ZFS dataset (a mount point, which cannot be renamed) is filled in place through an fd-anchored
   hidden staging folder and an entry-by-entry swap. Retention is computed in the panel
   (`apps/backups/retention.py`) so pinned and safety backups are honoured.
8. **File access is fd-based.** `agent/mcagent/safefs.py` walks paths with `O_NOFOLLOW` directory fds,
   rejects `..`, never follows symlinks (even ones created concurrently by a mod) and writes atomically.
9. **One agent per host.** `Node` model + `AgentClient(node)`; the default node comes
   from `MCPANEL_AGENT_URL`/`MCPANEL_AGENT_TOKEN`. Additional nodes are reached over HTTPS with a
   pinned certificate (`apps/nodes/tls.py`). Panel and agents can differ in version for a while:
   the agent reports `features` and `container_format` in `GET /v1/info`, and the panel only
   uses what the agent lists (`AgentClient.supports`).

## Container lifecycle

A server has a container while it runs, and while it is down although it was meant to run. A
stopped server has none.

* **Retire.** After each reconcile pass (every 10 seconds) the panel asks the agent to retire
  the container of a server when all of this holds (`reconcile.retire_blocker`,
  `reconcile.maybe_retire`): the container is `created`, `exited` or `dead`; it has been like
  that for 10 seconds; the server is not archived; `desired_state` is "stopped"; no job is
  queued or running for it; `MCPANEL_RETIRE_STOPPED_CONTAINERS` is on; the agent lists the
  feature `retire`. Nothing else removes the container of a stopped server: Stop, Kill, the
  crash-loop stop and a server created without starting all end here. A server that died while
  `desired_state` is "running" keeps its container until someone presses Start or Stop.
* **On the agent** (`docker_ops.retire`, `POST /v1/instances/{id}/retire`): refuses a running
  container (409 `container_running`) and one that stopped less than the settle time ago (409
  `not_settled`, by the Docker host's clock), writes the last-run record, then removes the
  container. If the record cannot be written the container stays (500 `last_run_failed`).
* **Last-run record** (`agent/mcagent/lastrun.py`, `<data root>/agent/last-run/<id>.json`): exit
  code, out-of-memory flag, restart count, times, health output, image id and the last 2,000
  console lines. Without a container the agent's status and list answers carry it as `last_run`
  (`exists` is false), and the logs and log-stream endpoints serve its console lines; with a new
  container the lines of the previous run are put in front of a short log. `state.compute`
  turns it into "stopped", or "failed" after an out-of-memory kill. The diagnostics read one
  run only: the container's own, or the record when there is no container. The record is
  deleted with the server and by `DELETE /v1/instances/{id}/last-run` when a server moves to
  another node.
* **Per-server lock on the agent** (`docker_ops.instance_lock`): held around every sequence
  that removes, creates or starts a container (apply, start, restart, remove, retire), so a
  retire cannot run between the stop and the start of a restart. Stop and kill take no lock:
  they only act on a running container, which a retire refuses.
* **Start** (`handlers.start_server`): the one way a job starts a server. It starts the
  existing container, or creates one from the current configuration when there is none, also
  when the container disappears between the status request and the start (404 `no_container`).
  The Start job passes `apply_changes`, which also recreates a container whose configuration is
  out of date. Restore, rollback, world replace and world reset use the same helper to start
  the server again. Every start by a job records `desired_state = "running"` (`_started`).
* **Restart required.** `spec.spec_outdated` says that the stored spec hash differs from the
  current one. `spec.restart_required`, what the UI shows, is `spec_outdated` and "the server
  has a container": a stopped server gets a container from the current settings at its next
  start anyway. The hash includes `CONTAINER_FORMAT` (2: the container has Docker's stop
  timeout), taken from what the agent reported for the container it created. A container made
  by an older release or by an older agent therefore reads restart-required until it is
  recreated.

What the agent API offers for this (all under `/v1`, `agent/mcagent/main.py`):

| Endpoint | Purpose |
|---|---|
| `GET /info` | Carries `features` (currently `["retire"]`) and `container_format` (currently 2). The panel caches the feature list per node for 5 minutes, "none" for 1 minute |
| `GET /instances` | `containers` plus `last_runs`: the last-run record without console lines, per server |
| `GET /instances/{id}/status` | Without a container: `exists: false` and `last_run` |
| `POST /instances/{id}/retire` | Save the record, remove the container (body: `settle_seconds`) |
| `DELETE /instances/{id}/last-run` | Delete the record |
| `GET /instances/{id}/logs` | `previous=false` leaves the previous run out (the diagnostics ask like that) |
| `POST /instances/{id}/stop`, `/restart` | The answer carries `stop` (see below) |
| `PUT /instances/{id}/container` | The result of the apply carries `container_format` |
| `POST /instances/{id}/world/reset`, `GET /instances/{id}/world?sizes=false` | World reset (see below) and the folder list without measuring sizes |

## Stopping a server

`docker_ops.graceful_stop` is behind the agent's `/stop` and `/restart` and behind every
removal of a running container:

1. If RCON answers within 3 seconds: `save-all flush`, waiting up to half the stop timeout (at
   least 10, at most 60 seconds). A failure is a warning, never an error.
2. `docker stop` with the requested timeout, always. It tells Docker that the stop is meant, so
   the restart policy does not start the server again.
3. If RCON did not answer in step 1 (the server is still starting), a helper thread tries every
   2 seconds and sends `stop` once the server accepts an RCON login, until the container has
   exited or `docker stop` has returned.

The answer carries `stop` (`flushed`, `stop_sent_via_rcon`, `warnings`, `exit_code`), and the job
log repeats it (`handlers._log_stop`).

Two properties of every container cover stops that do not go through the agent. Docker's own
stop timeout is set at creation to min(the server's stop timeout, `AGENT_SHUTDOWN_STOP_TIMEOUT`),
20 seconds by default (docker-py's high-level `create()` cannot set it, so `create_container`
calls the low-level API with the same arguments). And `spec.build_env` sets
`CREATE_CONSOLE_IN_PIPE=true`, so the itzg runner can hand `stop` to the server console when RCON
is not up yet.

## Host shutdown and resume

`manage.py host_shutdown [--timeout 45] [--node <name>]` is run by the Unraid hook
(`deploy/unraid/stopping_docker`, through `docker exec`) right before Docker stops. For every
running, non-archived server of the node it sets `Instance.resume_after_boot` and
`resume_flagged_at`, then asks the agent to stop them all at once (the graceful stop above, each
with min(its stop timeout, `--timeout`)). It uses the database and the agent only: no job, no
Celery, no Redis. It returns after the timeout plus 8 seconds at the latest. `desired_state`
stays "running", so these containers are not retired.

`scheduling.resume_flagged_servers` runs in the scheduler tick (every minute) and queues at most
one `start` job per run. For each marked server, in this order:

| Case | What happens |
|---|---|
| Archived, or `desired_state` is no longer "running" | Mark dropped, no start |
| Marked while this panel container was already running (the mark is newer than the start of PID 1) | Nothing: the host is still shutting down. After 10 minutes like this the mark is dropped (Docker was never stopped) |
| A job that changes the server's files failed, was marked as failed or was interrupted after the mark | Mark dropped, no start. Failed backups, snapshots, clones, starts and restarts do not count |
| The agent does not answer | Waits for the next run |
| The container runs already (Docker's restart policy, or someone started it) | Mark dropped |
| A job holds the server, or another server was started in this run | Waits for the next run |
| Otherwise | `start` job queued, mark dropped, audit `instance.resumed_after_boot` |

A mark dropped without a start is audited as `instance.not_resumed_after_boot` with the reason. An
explicit start, stop, kill, archive or delete clears the mark, and so does the crash-loop stop.

## World reset

`GET` / `POST /api/instances/<id>/world/reset` (`views.WorldResetView`, job `world_reset`, agent
`POST /v1/instances/{id}/world/reset`). `world.reset_plan` names the folders that hold the world
and the reasons a reset is refused. The view, the job and the agent each compare the folders the
user confirmed with what they find (`world_changed`). The job stops the server (only if the
request allowed it), takes a safety backup without the server's own backup exclusions, refuses
to go on if restic reported warnings or the snapshot does not list the world
(`world.verify_world_in_backup`), writes the seed, and lets the agent delete the folders. The
agent moves them into `.mcpanel-world-old-*` first and moves them back on any failure; its error
code says whether the world was touched (`world.WORLD_RESET_UNTOUCHED`). After a failure that
left the world as it was, the seed is put back and a server that was running is started again.
After anything unclear the server is left stopped.

## Data layout on the host (`AGENT_DATA_ROOT`)

```
<data root>/instances/<instance-uuid>/   -> mounted at /data in the Minecraft container
<backup root>/restic/<instance-uuid>/    -> restic repository (AGENT_BACKUP_ROOT, default <data root>/backups)
<data root>/staging/                     -> restore/clone staging (same filesystem for atomic rename)
<data root>/instances/<id>.mcpanel-{restore,clone,old}-* -> btrfs: new subvolume built next to the server, renamed in
<instance dir>/.mcpanel-tmp-*            -> restore-path and ZFS in-place restore/clone staging, opened by fd
                                           (excluded from backups; removed afterwards)
<instance dir>/.mcpanel-world-{upload,new,old}-* -> world replace: uploaded archive, extracted world, old world
                                           (inside the instance so every swap is a rename; removed afterwards).
                                           World reset: the world folders are moved into .mcpanel-world-old-*
                                           (one sub-folder per role) before they are deleted, so a failure
                                           can move them back; a folder left behind holds the old world
<data root>/snapshots/<instance-uuid>/   -> btrfs: read-only snapshot subvolumes of that server (.deleting-* while one is removed)
<data root>/.mcpanel-probe/              -> btrfs: exists only while the agent tests how it can delete subvolumes
<data root>/trash/                       -> deletes are renamed here first, then removed
<data root>/agent/                       -> restic password (if generated) and cache
<data root>/agent/last-run/<instance-uuid>.json -> record of a stopped server's last run: exit state and the last
                                           2,000 console lines (folder 0700, file 0600; never in a backup)
<data root>/agent/cache/file-hashes/     -> persistent file-digest cache for /files/hashes (AGENT_CACHE_ROOT; rebuildable)
```

## Backend apps

`accounts` (users, roles, per-instance grants, permission matrix) · `audit` · `nodes` (Node + agent
client) · `catalog` (Mojang/Fabric/Quilt/Forge/NeoForge/Paper/Purpur metadata, Java resolution) ·
`instances` (model, spec builder, state, reconcile, handlers, schedules, files/properties/console
proxies) · `jobs` (runner, watchdog) · `modpacks` (Modrinth, CurseForge, FTB adapters) · `content`
(mods/plugins manager) · `backups` · `players` · `console` (WebSocket consumers, event fan-out) · `core`
(errors, permissions helpers, settings, crypto).

Management commands (`docker compose exec panel python manage.py <command>`): `host_shutdown`
and `retire_stopped_containers` (`apps/instances`), `ensure_backup_schedules` (`apps/backups`),
`add_node` (`apps/nodes`), `reencrypt_secrets` (`apps/core`).

Deployment code outside the two images: `deploy/scripts/upgrade.sh` (upgrade, rollback,
database restore; state in `deploy/upgrade-state.env`) and `deploy/unraid/` (the
`stopping_docker` hook and `install-hook.sh`).

## Real-time

* `/ws/instances/<id>/console/` — the panel relays the agent's log stream
  (`/v1/instances/<id>/logs/stream`, follows across restarts) and executes commands via RCON with a
  per-command permission check and audit entry.
* `/ws/events/` — instance status and job progress, filtered by what the user may view; the SPA uses
  it to invalidate cached queries (polling remains as a fallback).
