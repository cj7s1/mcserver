# Architecture

MCPanel is split into a **panel** (UI + API + worker, unprivileged) and a **node agent**
(the only component that touches Docker and host storage). This is the same split as
Pterodactyl's panel/Wings and is what will let us add remote nodes later.

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

Redis keeps nothing on disk. Job state is in Postgres; when the job worker becomes ready it sends
every job that is still queued again (`mcpanel/celery.py`, `apps/jobs/tasks.py`), so a restart of
the panel container or of Redis loses no job. Lost at a restart: webhook deliveries waiting in the
queue and the crash-loop counters kept in the cache.

## Components

| Component | Code | Responsibilities |
|---|---|---|
| Frontend | `frontend/` (React, Vite, Tailwind, shadcn/ui, TanStack Query, xterm.js) | All screens. Talks only to `/api` and `/ws` on the panel. |
| Management API | `backend/` (Django 5.2, DRF, Channels) | Auth, permission matrix, instance records, provider integrations, file/console proxying, audit log, OpenAPI at `/api/docs`. |
| Workers | same image and container: `celery -A mcpanel worker` (jobs), a second worker for the `periodic` queue, and `beat` | Execute **jobs** (install, start/stop/restart, recreate, backup, restore, clone, delete), the 10 s reconcile loop and cron schedules. |
| Node agent | `agent/` (FastAPI, docker SDK, restic) | Narrow authenticated API: apply container spec, power, status/stats/logs, RCON, confined file ops, hash-verified downloads, restic backup/restore, storage capability detection. |
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
   `starting`/`running`/`unhealthy`.
3. **Jobs with per-instance locks.** Every state-changing long operation is a `Job`. Exclusive jobs
   atomically claim `Instance.active_job` (`select_for_update`), so conflicting operations get HTTP 409
   `instance_busy`. A job found `running` when a worker picks it up was interrupted and is failed
   rather than blindly re-run (important for restores). A restart/stop that first warns players in
   game (1–15 min) is enqueued **non-exclusive** and takes the lock only for its last 10 seconds and
   the action (`runner.claim_lock`, waiting up to 20 min for another exclusive job), so a long warning
   never blocks backups or other operations; its waits are cancellation-aware (`JobContext.sleep_until`
   raises `JobCancelled` once the job is cancelled or abandoned). See `apps/instances/power_warnings.py`.
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
9. **Single node now, multi-node ready.** `Node` model + `AgentClient(node)`; the default node comes
   from `MCPANEL_AGENT_URL`/`MCPANEL_AGENT_TOKEN`.

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
<data root>/trash/                       -> deletes are renamed here first, then removed
<data root>/agent/                       -> restic password (if generated) and cache
<data root>/agent/cache/file-hashes/     -> persistent file-digest cache for /files/hashes (AGENT_CACHE_ROOT; rebuildable)
```

## Backend apps

`accounts` (users, roles, per-instance grants, permission matrix) · `audit` · `nodes` (Node + agent
client) · `catalog` (Mojang/Fabric/Quilt/Forge/NeoForge/Paper/Purpur metadata, Java resolution) ·
`instances` (model, spec builder, state, reconcile, handlers, schedules, files/properties/console
proxies) · `jobs` (runner, watchdog) · `modpacks` (Modrinth, CurseForge, FTB adapters) · `content`
(mods/plugins manager) · `backups` · `players` · `console` (WebSocket consumers, event fan-out) · `core`
(errors, permissions helpers, settings, crypto).

## Real-time

* `/ws/instances/<id>/console/` — the panel relays the agent's log stream
  (`/v1/instances/<id>/logs/stream`, follows across restarts) and executes commands via RCON with a
  per-command permission check and audit entry.
* `/ws/events/` — instance status and job progress, filtered by what the user may view; the SPA uses
  it to invalidate cached queries (polling remains as a fallback).
