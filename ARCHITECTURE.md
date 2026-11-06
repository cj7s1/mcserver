# Architecture

MCPanel is split into a **panel** (UI + API + worker, unprivileged) and a **node agent**
(the only component that touches Docker and host storage). This is the same split as
Pterodactyl's panel/Wings and is what will let us add remote nodes later.

```
 Browser ──HTTPS(reverse proxy, optional)──▶ panel (Django ASGI: REST + WebSockets + SPA)
                                               │  Postgres (state)   Redis (cache, Celery broker, Channels)
                                               │
                                worker (Celery: jobs, reconcile loop, schedules)
                                               │  Bearer token, internal network only
                                               ▼
                                agent (FastAPI) ──▶ Docker Engine ──▶ itzg/minecraft-server containers
                                   │                                      ▲ RCON 25575 (container network only)
                                   └── instance + backup storage (bind-mounted host paths, restic repos)
```

## Components

| Component | Code | Responsibilities |
|---|---|---|
| Frontend | `frontend/` (React, Vite, Tailwind, shadcn/ui, TanStack Query, xterm.js) | All screens. Talks only to `/api` and `/ws` on the panel. |
| Management API | `backend/` (Django 5.2, DRF, Channels) | Auth, permission matrix, instance records, provider integrations, file/console proxying, audit log, OpenAPI at `/api/docs`. |
| Worker | same image, `celery -A mcpanel worker` + `beat` | Executes **jobs** (install, start/stop/restart, recreate, backup, restore, clone, delete), the 10 s reconcile loop and cron schedules. |
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
   rather than blindly re-run (important for restores).
4. **server.properties is a file, not env.** itzg rewrites properties from env on every start, so the
   panel only sets env for what it owns (port, RCON, type/version, memory, JVM) and edits
   `server.properties` directly for everything else. Initial values are written before first start.
5. **RCON for commands**, password fixed per instance (encrypted in DB) and read by the agent from the
   container config. RCON is never published to the host.
6. **Modpacks are pinned.** We store provider, project id and version/file id and pass them to itzg
   (`MODRINTH_VERSION`, `CF_FILE_ID`, `FTB_MODPACK_VERSION_ID`), so restarts never silently upgrade.
7. **Backups use restic** (one repository per instance, encrypted, deduplicated). Running servers are
   coordinated with `save-off` → `save-all flush` → backup → `save-on` (always re-enabled in `finally`).
   Restores go into a staging directory first and are swapped in only after restic succeeds; a
   pre-restore safety backup is taken by default. Retention is computed in the panel
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
<instance dir>/.mcpanel-world-{upload,new,old}-* -> world replace: uploaded archive, extracted world, old world
                                           (inside the instance so every swap is a rename; removed afterwards)
<data root>/trash/                       -> deletes are renamed here first, then removed
<data root>/agent/                       -> restic password (if generated) and cache
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
