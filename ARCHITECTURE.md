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

Three containers: `panel`, `agent`, `postgres`. The five processes of the panel container (six with the
Discord bot, `discord`, which exits at once and stays idle while it is off) run under
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
after 116 seconds at the latest, inside the container's `stop_grace_period` of 2 minutes. A job
that is only waiting (for an agent operation, a countdown or a server's lock) does not use those
110 seconds: when the job worker gets its stop signal it leaves a flag file, the pool process
running the job sees it in `JobContext.check_cancelled` and the guard of `wait_operation`, and the
job is failed as `interrupted` at once (`apps/jobs/shutdown.py`, `worker_shutting_down` in
`mcpanel/celery.py`). An operation the agent has already started goes on there. After a
restart of Redis alone the job worker is not killed: it finishes the job it is running, without
a time limit, and is started again when it has exited (a flag only counts while the supervisor's
status file says it is stopping).

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
   `runner.finish()` only ends a job that is still active: a job already marked failed (abandoned,
   cancelled, interrupted) stays failed when its handler returns. **Mark as failed** is refused
   (409 `not_active`) for a job that has ended; for a running one the handler notices within about
   2 seconds (`operation_guard` in `AgentClient.wait_operation`, `check_cancelled` before every
   destructive step), so an abandoned restore never replaces the files afterwards.
   A job that stops a running server for its work (restart with changes, recreate, software or
   modpack update and rollback, restore, single-path restore, snapshot rollback and conversion,
   migration, world replace and reset) tracks how far it got (`Progress`). When it fails and the
   files are provably unchanged it starts the server again (`put_back`); after anything unclear
   it sets `desired_state` to "stopped" and says so in the job log. A job cut off by a worker stop
   or found by the watchdog never restarts the server: `settle_interrupted` records it stopped,
   drops its resume mark and adds that to the job's error.
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
   (`apps/backups/retention.py`) so pinned and safety backups are honoured; snapshots whose row
   is failed or deleted are pruned with it. Before `save-off` the agent writes a marker
   (`<data root>/agent/saving-paused/<id>`): if the agent ends inside the pause, `save-on` is sent
   at its shutdown, at its next start and every 30 seconds for marked running servers. restic
   never removes a stale lock itself, so the agent runs `restic unlock --remove-all` on every
   repository at start (nothing of its own runs yet), and a command that finds its repository
   locked (exit 11) unlocks stale locks and retries once; restic is stopped with SIGINT first so
   that it removes its own lock. Every backup records the server's software (`meta.software`);
   restoring a "Before update" backup puts that software back and recreates the container.
   `manage.py adopt_backups` creates rows for snapshots of a server's repository that the
   database does not know (after a database restore from an older dump).
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
  Every start that creates a container asks the agent for `pull="newer"` (feature `pull_newer`;
  older agents get `missing`): the agent tries a pull bounded to 180 seconds and falls back to
  the image on the host with a warning in the job log; afterwards it removes superseded runtime
  images that are untagged and unused. The image comes from `spec.current_image`, so a change of
  the runtime image channel reaches existing servers at their next start (and shows as restart
  required while they run). An apply creates the new container under a temporary name first and
  removes the old one only when that worked, so a spec Docker refuses (or a CPU limit above the
  host's cores) leaves the old container as it was.
* **Memory admission** (`apps/instances/capacity.py`). Before a start that creates or starts a
  container, the limits (`container_memory_mb`) of the servers whose containers run on the node
  plus the new one must fit into the node's RAM (`docker.memory_bytes` of `GET /v1/info`, cached
  5 minutes) minus the system setting `memory_reserve_mb` (default 4096, 0 to 65536, editable in
  System settings). A start a user asked for fails with 409 `insufficient_memory` and the
  numbers; the UI offers **Start anyway**, which resends with `allow_overcommit`. Scheduled starts
  and the resume step after a host shutdown wait instead. A container that already runs with its
  current configuration, stops, and the restart of a server a job stopped for its own work are
  never refused. When the RAM or the containers cannot be read the start goes ahead.
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
| `GET /info` | Carries `features` (currently `retire`, `pull_newer`, `ping`, `clone_level_name`), `container_format` (currently 2) and `docker.memory_bytes`. The panel caches the feature list per node for 5 minutes, "none" for 1 minute |
| `GET /instances` | `containers` plus `last_runs`: the last-run record without console lines, per server |
| `GET /instances/{id}/status` | Without a container: `exists: false` and `last_run` |
| `POST /instances/{id}/retire` | Save the record, remove the container (body: `settle_seconds`) |
| `DELETE /instances/{id}/last-run` | Delete the record |
| `GET /instances/{id}/logs` | `previous=false` leaves the previous run out (the diagnostics ask like that) |
| `POST /instances/{id}/stop`, `/restart` | The answer carries `stop` (see below) |
| `PUT /instances/{id}/container` | The result of the apply carries `container_format`, the image ids before and after, `pulled`, `images_removed` and `warnings` |
| `GET /instances/{id}/ping` | Minecraft server list ping (`slp.py`): online count, a sample of names, version. The panel counts players with it every minute; RCON `list` only for agents without `ping`, so the console gets no RCON connect lines |
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
| It does not fit into the node's memory next to the running servers (`capacity`) | Waits for the next run; once the panel has been up for 10 minutes the mark is dropped (reason `insufficient_memory`, with the numbers) |
| Otherwise | `start` job queued, mark dropped, audit `instance.resumed_after_boot` |

A mark dropped without a start is audited as `instance.not_resumed_after_boot` with the reason. An
explicit start, stop, kill, archive or delete clears the mark, and so does the crash-loop stop.

## Schedules

`scheduling.run_due_schedules` runs in the minute tick. Schedules are matched in the servers'
time zone (system setting `timezone`, else the panel's `TIME_ZONE`). Celery beat itself runs in
UTC (`CELERY_TIMEZONE`): its interval entries are added to the last run as wall-clock time, and
in a zone with daylight saving time the tick that crossed the fall-back hour used to land an hour
late, so there was no reconcile, scheduler or watchdog tick for that hour.

* **One schedule cannot stop the others.** Each is handled in its own `try`. An expression that
  never matches a date (`0 0 31 2 *`) is refused when it is saved (`validate_cron`, 400 on
  `cron`; the frontend checks it too); a row saved by an older release is paused with the reason
  in `last_result`. Any other error is recorded on the row and the loop goes on.
* **Order within a tick:** backups, then snapshots, then power actions and commands; on one
  server the first takes the lock.
* **Deferral.** A run that finds its server locked by another job (`instance_busy`) or without
  room in the node's memory (`insufficient_memory`) is put off, not dropped: `Schedule.deferred_at`
  records when, `next_run_at` stays, and it is tried on every tick ("Waiting: ..." in
  `last_result`). After 60 minutes it is recorded as failed, the schedule moves to its next time
  and `job.failed` is notified (also `backup.failed` for a backup). Other refusals ("Skipped: ...")
  move it on at once.
* **Misfire limit.** A restart, start, stop or command more than 10 minutes late (the panel was
  down) is not run late: "Missed: ..." and the next time. A missed backup or snapshot runs once,
  but only when its server has been ready for 2 minutes or the panel has been up for 5 (so not
  on servers that are still starting after a boot); several missed times collapse into that one
  run. A run that was already deferred is not counted as missed.
* **Daylight saving time.** On the fall-back night, a time that occurs twice runs once (at its
  first occurrence) for a schedule whose hour field names fixed hours (`30 1 * * *`).
  Interval-style schedules (hour field `*` or a step such as `*/2`) follow real elapsed time and
  also run through the repeated hour. A time inside the hour skipped in spring runs at the end of
  the gap (03:00 for a 02:30 schedule); several times inside the gap run once there.

## Request limits and file transfers

* **Body limits before Django** (`apps/core/asgi.RequestBodyLimit`, wired in `mcpanel/asgi.py`).
  Django's ASGI handler reads the whole body before any middleware or login check, so the limits
  are enforced in front of it: a declared `Content-Length` above the limit is answered 413 without
  reading the body, a chunked body is cut off as it passes the limit. Every request may carry at
  most 25 MB (`MCPANEL_MAX_REQUEST_BYTES`). The upload endpoints (`files/upload`, `import`,
  `content/upload`, `world/replace`) take up to `MCPANEL_MAX_UPLOAD_BYTES` (default 2 GiB), must
  declare their length (411 otherwise) and need a signed-in session for anything above 25 MB.
  `UploadLimitMiddleware` stays as the fallback for other servers and the test client.
* **One copy on disk.** Bodies are spooled to `FILE_UPLOAD_TEMP_DIR` (`MCPANEL_UPLOAD_TMP`, default
  `/tmp/mcpanel-uploads`, created 0700), and `apps/core/uploads.ReleaseRequestBodyHandler` empties
  the body spool once the multipart parser has copied the files out (raw single-file uploads
  through DRF's `FileUploadParser` still keep both copies until the request ends).
* **Many files.** A multipart request may carry up to 1000 files (`DATA_UPLOAD_MAX_NUMBER_FILES`;
  more is 413 `too_many_files`); the frontend sends batches of 100. The file manager stores each
  file on its own and answers `{"uploaded": [...], "failed": [{name, path, code, message}]}`;
  only when no file could be stored is the first error raised.
* **Downloads stream** (`apps/core/streaming.AgentStreamingResponse`): one agent chunk is read and
  sent before the next, and the agent's response is closed when the browser goes away. File
  downloads, folder and world zips and backup file downloads use it.
* **Database connections.** The web process has `CONN_MAX_AGE=0` (under ASGI each request runs in
  its own thread, so a kept connection was never reused); workers and `manage.py` keep
  `POSTGRES_CONN_MAX_AGE`.
* **Long file operations.** Rename and delete (up to 500 paths) wait up to 15 minutes for the agent
  (`FILE_OPERATION_TIMEOUT`); a read time-out answers 202 `{"still_running": true}` and is audited
  as such, because the agent keeps working.
* **Text encodings.** The agent decodes text strictly: a file that is not UTF-8 is answered 415
  `not_utf8`, except `*.properties`, which it reads and writes as ISO-8859-1 (Java's encoding for
  them) and reports `encoding`. The editor opens such files read-only with a download. Everything
  the panel writes into `server.properties` is ASCII: characters above `~` become `\uXXXX`. A file
  name that is not valid UTF-8 is listed with U+FFFD and `name_valid: false`; it cannot be opened,
  renamed, moved or deleted from the panel.

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

## Games: definitions, runtime profiles, control drivers

The design is docs/design/GAME-DEFINITIONS.md. Built: Minecraft (phase 1, unchanged behaviour),
Factorio (phases 2 and 3), Satisfactory and Terraria (their own sections below), and six games on a
shared base: Palworld, Valheim, Project Zomboid, Hollow Knight (HKMP), Counter-Strike 2 and Rust
("Six Steam-era games" below). A game has two halves that must agree:

* **Panel: the game definition** (`backend/apps/games/<game>.py`, a `GameDefinition` subclass,
  registered in `apps/games/__init__.py` `_load`). Everything game-specific on the panel side is
  asked from `games.for_instance(instance)`: capabilities (which features and UI sections exist),
  the memory model, the agent spec (`build_spec`; the base class builds the generic spec of a game
  with named ports from `image()`, `game_env()` and `ports`), the config files and their form
  schema (`config_files()`), pinned options (`options`, stored in `Instance.game_options`), secrets
  (`secret_specs`, `Instance.secrets`), first-start steps (`setup_steps`, `run_setup_step`,
  `Instance.setup_state`), the version catalog (`versions`, `validate_version`), the console rules
  (`normalise_command`, `stop_words`, `refuse_stop_commands`, `keeps_command_slash`), players
  (`online_players`, `player_lists`, `player_action`), broadcast, the saves model (`saves`) and the
  software record of backups (`software_state`, `set_software`, `describe_software`). Minecraft's
  definition wraps the code that existed before and never adds keys to its spec.
* **Agent: the runtime profile** (`agent/mcagent/profiles/<game>.py`, registered in
  `profiles/__init__.py`). Data the panel cannot choose: allowed image repositories and tag pattern,
  the container user, capabilities, mounts, injected and reserved environment and the environment
  allow-list (with value patterns for the variables the panel sets), publishable container ports and
  the never-published ones, the readiness probe, stop signal, TTY/stdin, log redaction, default
  backup excludes and the **control driver** (`agent/mcagent/drivers/`: `minecraft-rcon`, the
  generic `rcon`, `http-api` with `satisfactory-api` and `palworld-api`, `console`, `tshock-rest`,
  `webrcon`, `none`) with its options. A spec names only `game`/`variant`; an agent without the
  profile answers 400 `unknown_game`, and the panel refuses to create or start such a server on it
  (409 `agent_lacks_game`, from `GET /v1/info` `games`).

What the generic pieces do:

* **Named ports** (`Instance.ports`, `services.assign_named_ports`, `ports_in_use_by_protocol`):
  conflicts are per protocol (TCP 7777 and UDP 7777 are different ports); a new server gets the
  game's well-known port when it is free, else the next free one above it, else one of the panel's
  range. `game_port` of such a server is a copy of its first named port (the unique constraint on
  `game_port` holds for Minecraft rows only). The agent publishes only the profile's container ports
  and never its `never_publish` ones. The connection info lists every named port with its protocol.
  A port can share another one's number (`same_port_as`, Satisfactory) or derive from it
  (`offset_of` + `offset`, Valheim's query port = game + 1): such a port is never asked for, the game
  port is chosen so that the derived numbers are free as well and they move with it (409
  `port_conflict` names the game port). A port can be published 1:1 (`one_to_one`: container port =
  host port, for images that take the port from a variable and advertise it) and only while a setting
  is on (`when` = (config id, key, value): Valheim's crossplay port, CS2's CSTV port, Rust+); its number
  stays reserved either way. The catalog shows these as `same_port_as`, `derived_from`, `one_to_one`, `when`.
* **Memory model `plain`**: `memory_mb` is the container limit (no JVM overhead); the memory check
  and the API use `Instance.container_memory_mb`, which asks the definition.
* **Config files** (`apps/games/codecs.py`: `json` with dotted keys, `ini` with `section.key`, `kv`,
  `java-properties`): `GET /api/instances/<id>/config` and `PATCH .../config/<file id>` read and
  change only the keys of the schema; comments, order and unknown keys stay; secret fields are
  masked (`********` sent back means unchanged); managed keys are refused. The frontend renders the
  schema (`components/ConfigFieldsForm.tsx`, Settings -> Game settings, capability `config_files`).
* **Secrets and setup steps**: `Instance.secrets` is encrypted JSON (never returned, never in audit
  details: keys only). A secret may carry the value `pattern` of the agent's profile (checked at create
  and in Settings; `pattern_help` says it in words), be `generate`d when the create request has none
  (it can then not be cleared; nor can a `required` one) and be `reveal`able: `POST
  /api/instances/<id>/secrets/<key>/reveal` (`instance.configure`, `Cache-Control: no-store`, audited as
  `instance.secret_revealed` with the key only) for passwords players type in game (Palworld's and
  Project Zomboid's admin passwords). `definition.secret_error` adds rules across settings (Valheim: the
  join password must not contain the server name). String config fields may carry a `pattern` too
  (values that reach an environment; Counter-Strike 2's image `eval`s its start line). Steps of kind `auto` run in the `setup` job (enqueued by the install job,
  non-exclusive until it takes the lock); a `secret` step runs once its secret is set; the
  Overview shows "Setup needed". Factorio has none; the mechanism is tested with a test game.
* **Readiness**: besides Docker health checks (`cmd`, the image's own), the agent runs `tcp`
  (connect to the container), `udp-query` (a datagram, a reply matching a pattern), `driver` (the
  control driver's `ready()`) and `log:<regex>` (a line since the container started) probes itself
  and reports them as the container's `health` (`starting` / `healthy` / `unhealthy` after the
  profile's `start_period_s`), so `state.compute` reads them like Docker's. Factorio uses the log
  probe (`Starting RCON interface at`): an RCON health check would print a line every interval.
* **Consistent backups**: the agent's driver saves (`/server-save`) and waits for the completion line
  (`Saving finished`) before restic copies; the same save precedes a stop. Default excludes come from
  the profile (agent, absolute: Factorio's `temp/`) and the definition (panel: `*.tmp.zip`).
* **Image VOLUMEs**: for profiles with `mount_image_volumes` the agent inspects the image and mounts
  every declared VOLUME that is not a profile mount into `<server folder>/.volumes/...` (or a tmpfs
  the profile names), so Docker never creates an anonymous volume outside the server folder. For every
  profile but Minecraft's a declared VOLUME without its own bind or tmpfs is refused at create
  (`image_volume_uncovered`), and before every start and restart every bind source inside the server
  folder (`.volumes/*` included) must still be a real directory reached without symbolic links
  (`volume_source_invalid`): a start re-resolves bind sources, a file user could have replaced them.
* **First start of images that download the game**: a definition can name `retryable_install_exits`
  (SteamCMD's exit 5/8); the install job then waits for the first start and starts the container
  again up to `install_attempts` times (`game_handlers.wait_first_start`). A first start that is not
  ready after 30 minutes (the definition's `first_start_timeout`: CS2 4 h, Rust 2 h) fails the install
  job (error code `first_start_timeout`; the server goes on starting and
  the setup job is queued to go on once it answers). Satisfactory's restart policy is "no", so every
  start a job makes (`handlers.start_server`, the restart job, the publish step) waits the same way
  (`handlers.watch_start`, at most 10 minutes, then the job ends with a note) and also handles the
  start wrapper's refusal (exit 78: `mark_unclaimed`, started again unpublished, the setup follows).
* **Rollback to a revision without games** (`upgrade.sh rollback`, see DEPLOYMENT.md): refused while
  servers of other games exist, unless `--archive-other-games` (`manage.py rollback_other_games`) stops
  and archives them through the panel's jobs and keeps their rows in a side table outside Django's
  migrations; migration `instances/0007_restore_other_games` puts them back (archived) on the next upgrade.
  The reverse of 0007 parks the recorded servers on a node it creates ("parked by upgrade.sh rollback
  (other games)", agent URL `http://127.0.0.1:1/...`: a loopback address, so the refusal is immediate
  and no DNS lookup happens; no token): the 082b949 panel took a Terraria server for Minecraft and its
  Unarchive + Start ran itzg in that folder (real-stack run of 2026-11-10), while on the parked node
  every start, clone, move or delete fails with "node unreachable" (verified against the 082b949 code).
  Parking happens in the undo of 0007, not in the archive step, so a rollback that fails after archiving
  leaves startable servers in the new version. The original node comes from the side table's
  `data->>'node_id'`; 0007 moves each server back and deletes the parked node once it is empty.
* **Minecraft's world endpoints** (`/world`, `/world/replace`, `/world/reset`) refuse other games (409
  `unsupported_by_game`): they share the capabilities with the saves endpoints. A clone "without the
  worlds" of another game deletes the definition's save folders from the copy (`world_folders`).
* **Backups' software record** keeps each definition's `software_keys` (Factorio: version, Space Age;
  Terraria: variant, version; Satisfactory: none), so a "Before update" backup sets the version back. The
  backup job's log says what "consistent" means for the game (`consistency_note`: saved right before the
  copy, not paused), as it happened: the agent reports `saving` (`stopped`: the server was not running,
  nothing saved; `no_game`; `saved`; `paused`), and the log line follows it.
* **Agent support**: `games.require_agent_support` asks the agent again (no cache) before it refuses, and
  a migration checks the target node's agent before anything is stopped.
* **Exits from inside the game**: Factorio's `/quit` exits 0, which Docker's restart policy would
  restart. The console turns stop words into the panel's Stop; `POST /command` and schedule commands
  refuse them for games with `refuse_stop_commands` (400 `stop_via_power`). Minecraft too since the
  real-stack run of 2026-11-11: `stop` over RCON exits the server and Docker started it again (a
  deliberate change; a `stop` command schedule saved earlier is skipped with a note). For Minecraft a
  line also counts as `stop` when its final `run` target is stop: it starts with `execute` or `return`
  (also as `minecraft:` names) and ends with `run stop` (nesting, any case, slashes on the line and the
  target; `MinecraftDefinition.is_stop_command`); `minecraft:stop` counts too (Paper/Spigot run it).
* **Variants with their own data** (Terraria): one agent profile per variant (`(game, variant)`), a
  spec names `variant`; `/v1/info` lists each variant's images and control under
  `variant_profiles`, and the panel checks the variant too (`require_agent_support(..., variant)`).
  On the panel the definition narrows per server: `config_files_for`, `secret_specs_for`,
  `setup_status`, `capabilities(instance)`; the catalog's variants carry their memory, notes,
  capabilities and stop words, and options, config files and secrets name their `variants`, which
  the wizard uses to show only the chosen variant's fields. Versions per variant:
  `GET /api/catalog/games/<id>/versions?variant=`.
* **Console input** (`agent/mcagent/drivers/console_io.py`, `console.py`): for games that take
  commands on their console only. The profile chooses the transport: `exec`, a fixed argv template
  with one `{line}` element run through Docker's exec API as the server's uid:gid (the only exec the
  agent runs), or `stdin`, the line written through Docker's attach API to a container created with
  `stdin_open`. Every line is sanitised (printable, length-capped; with the `tmux` guard no key names,
  no leading `-`, no trailing `;`) and written under a per-server console lock; the reply is the log
  read after the write (bounded), so a command answers with what the server printed meanwhile. The exec
  is bounded by `console_io.EXEC_TIMEOUT` (504 `console_timeout`; the console lock is released).
* **The container's process** can be fixed by a profile (`entrypoint`, `command`, `working_dir`;
  fixed environment through `injected_env`), never by a spec: TShock runs its server binary directly
  as the runtime uid:gid instead of the image's root bootstrap script.
* **Panel-owned keys inside game files**: `definition.before_apply(instance, client)` runs before every
  apply (after the control secret file) and puts back what the panel owns (Terraria's
  `serverconfig.txt` world/port/paths, TShock's REST token); `masked_text` hides a secret the game keeps
  inside a file in the text editor.
* **Clean stops that do not exit 0**: `definition.clean_exit_codes` (Terraria's image: 143 after its
  saving SIGTERM handler) are neither a crash in `state.compute` nor a warning in the stop log.
* **Runtime images per game**: system setting `runtime_images` (`{game: {"repository"}}`); the tag
  follows the game's rule (Factorio: `<exact version>-rootless`). Minecraft keeps
  `runtime_image_repository` / `runtime_image_channel`. The agent must allow the repository
  (`AGENT_GAME_<ID>_IMAGES`, default: the profile's list).
* **Secret files** (`apps/instances/secret_files.py`; `definition.secret_paths` /
  `secret_bearing_paths`): files with a control secret (Factorio `config/rconpw`, Satisfactory
  `.mcpanel/api-token` and its claim `saved/ServerSettings.7777.sav`) are never downloaded (also not
  from the backup browser, not in a zip: the agent leaves them out, feature `archive_excludes`; an
  older agent's zip of a folder with one is refused), never written, overwritten, renamed or deleted
  through the file API, and no folder holding one is renamed or deleted. Extract and import (job
  `extract_archive`) never write them either: the panel sends `protected` =
  `secret_files.extract_protected` (the secret paths; the whole `.mcpanel/` folder when a secret lives
  there), and the agent (feature `extract_protected`) skips every entry whose final target (after its
  normalisation, the strip of a common folder and the destination; compared without case) is one of
  them or inside one, and a file entry that would replace a folder above one (`protected_count` /
  `protected` in the result, a line in the job log); a destination inside a protected path is refused
  (403 `managed_file`), an agent without the feature is refused for games with secret files (409
  `agent_outdated`). TShock's `config.json` is extracted like a file-API write. Files with a secret inside
  (TShock's `config.json`) are masked like the editor in downloads and search results, and left out of
  zips for users without `instance.configure`. Backups keep them (restic is encrypted).
* **Files changed outside the game** (`definition.files_touched`): restores, restore-path, snapshot
  restores, imports of a server archive and the file manager's uploads, edits, renames, deletes and
  extracts tell the definition
  which paths changed (Satisfactory: anything under `saved/` closes the ports, see below).
* **Saves** of games other than Minecraft (`definition.saves`; Factorio `FactorioSaves`):
  `GET /saves`, `POST /saves/reset` (job `saves_reset`), `POST /saves/import` (job `saves_import`),
  `GET /saves/export`. Reset and import stop the server, take a safety backup, move every save into
  `saves-replaced/<UTC stamp>/` in the server folder, then write the map settings (the server creates
  the new map at its start) or put the uploaded zip in place, and start it again if it was running.
  Worlds that are folders use one generic model (`apps/games/foldersaves.py` `FolderSaves`, catalog
  `world.kind = "folder"`), configured per definition with the folder, the file names that make a world
  (`markers`) and hooks (`world_paths` per purpose reset/import/export, `world_target`, `place_world`,
  `after_import`, `prepare_world_reset`, `save_now`): the list shows the current world (size from a
  bounded walk, `size_complete`) and the set-aside copies; an import is a zip of the world folder that
  the panel checks before uploading it (the markers at the top or one folder below, no links, no
  absolute or `..` paths, bounded size and entry count; `root` goes to the job), which the job extracts
  into a staging folder `.mcpanel-import-<hex>.d` and moves into place; an export is a zip of the world
  folder built by the agent (`files/archive`, minus the game's own rolling copies), after a save when
  the server runs and the user may send commands.

### Factorio

`factoriotools/factorio:<version>-rootless`, run as the runtime uid:gid with no capabilities, the
server folder at `/factorio`, 34197/udp published, RCON 27015/tcp internal only. The panel writes the
control secret (`Instance.rcon_password`) to `config/rconpw` before every apply
(`handlers.write_control_secret`); the image passes it to the server, the agent's `rcon` driver reads
it from the file. The file API never hands that file out or changes it (see "Secret files" above). The
server loads the newest save (`LOAD_LATEST_SAVE=true`: after an unclean exit that is an autosave,
`_autosaveN.zip`, not the stale `world.zip`) and creates `saves/world.zip` (`SAVE_NAME`) from
`config/map-gen-settings.json` when it is missing (`GENERATE_NEW_SAVE=true`). A reset or an import moves
every zip aside while the server is stopped, so the new map or the upload is the only, and newest, save;
a restore brings the files back with their times. The saves page and its download name the newest save. Space Age
(`DLC_SPACE_AGE`) is chosen at creation and pinned in `game_options`. Versions come from Docker Hub's
`*-rootless` tags and factorio.com's latest stable release; a software update changes the exact tag
(pre-update backup, downgrade only when confirmed).

### Satisfactory

`wolveix/satisfactory-server` at the tag the panel release pins (`v1.9.11`), in root mode with exactly
CHOWN DAC_OVERRIDE SETUID SETGID (`PUID`/`PGID` = the runtime uid/gid; the image runs unprivileged as
uid 1000 only), the server folder at `/config`. SteamCMD downloads the game (2.9 GB) into `gamefiles/`
on the first start and validates it at every start; the server follows Steam's stable branch
(`fixed_version` in the catalog: no version to pick, no software update, "Update now" = restart).
`gamefiles/` is the profile's default backup exclude, and a restore moves it from the old server folder
into the restored one (`main._carry_over_excluded`) instead of throwing it away. The restart policy is
"no" (the definition and the agent's profile force it): only the agent starts the container, through
its start wrapper; every start a job makes is retried on SteamCMD's exit 5/8 (`handlers.watch_start`).

Ports: the game (UDP) and the HTTPS API (TCP) share one host port number (`PortSpec.same_port_as`:
`assign_named_ports` picks a number free for both protocols, the API port is never asked for); the
container port stays 7777 because the claim is stored per game port (`ServerSettings.<port>.sav`). The
reliable-messaging port is advertised to players as the server knows it, so the definition sets
`SERVERMESSAGINGPORT` to the host port and publishes it 1:1; the profile allows a container port that
such a variable names (`RuntimeProfile.env_ports`). That port never takes 7777 (the container's API port
on TCP; `PortSpec.avoid` on the panel, `env_port_error` on the agent), and for profiles with `env_ports`
the agent refuses duplicate host or container (port, protocol) pairs (`port_collision`). The server runs
on the agent's instance network only (`custom_network = False` on the panel, `instance_network_only` on
the agent, 400 `network_not_supported`).

Control is the `satisfactory-api` driver (`agent/mcagent/drivers/satisfactory.py`) on the generic
`http-api` base (`drivers/http_api.py`): HTTPS to the container's address on the instance network, a TLS
context of its own that accepts the self-signed certificate for that hop only (pinned per container run),
bounded timeouts and answer sizes, the API token read from `.mcpanel/api-token` in the server folder.
Errors usually come back as HTTP 200 with an `errorCode`, so every answer is checked. Saves are
`SaveGame` as `<session>_mcpanel`, never while no game is loaded (`QueryServerState` `isGameRunning`
false: the server still names a default `SERVER-SESSION...` session, and `SaveGame` would not answer;
the stop, the backup, reset and import skip the save then, and the backup log says so) (a session name with other characters than letters, digits, `_`, `-`,
or a long one: `<sanitised>_<8 hex of its hash>_mcpanel`, so two sessions never share a save; 204 once written; the newest save of the session is what the server
loads at its next start); stop = SaveGame, `Shutdown`, wait for the exit, then `docker stop` (a no-op for
an exited container); signals end the game without saving. Readiness is the agent's `udp-query` probe
(the Lightweight Query answers while a map loads, when the HTTPS API is briefly down). `/ping` answers
through the driver (`drivers.QueryTarget`): the player count from `QueryServerState` (no names), the
limit, the build, the server name. The panel reaches the other functions through the agent's
`POST /v1/instances/{id}/game/{action}` (feature `game_actions`): `claim`, `client_password`,
`verify_claim`, `new_game`, `sessions`, `save`, `upload_save`, `load_game`, `options`, `apply_options`,
`status`.

Claiming (setup steps `claim`, `access`, `new_game`, `publish`): the spec has no ports until
`setup_state.ports_open`; the claim waits for readiness, logs in passwordless as InitialAdmin, claims with
the control secret as admin password (or, when this panel claimed it before, logs in with it), generates
an API token (returned once; stored in `Instance.secrets["api_token"]`, written to the token file before
every apply through `control_secret_value`), then the join password (or the explicit "open" choice) is
applied and checked, the `new_game` step keeps a loaded game, else loads the newest save of the session
the panel remembers (else the newest save there is; `newest_save`, by the game's `saveDateTime`), and
creates a game only without saves, and the `publish` step has the agent verify the claim on the
unpublished container (`verify_claim`) and recreates the container with its ports. A step that needs a running server raises `SetupWaiting` (pending, the job ends quietly);
the Start job enqueues the pending steps; a step whose earlier steps are not done is refused (409
`setup_order`). Changing the join password in Settings re-runs the `access` step (`secrets_changed`);
clearing it needs `"access": "open"` in the request (the UI asks first; `SecretSpec.clear_opens`).

The published container is never left running unclaimed (features `claim_guard`, `claim_marker`;
`agent/mcagent/claimmarker.py`):

* **The claim marker** `.mcpanel/claim-verified` records size, `mtime_ns` and SHA-256 of
  `saved/ServerSettings.7777.sav` when the agent verified the claim through the API (`InitialAdmin`
  refused, `Administrator` login with the admin password the panel sends works). The game writes that
  file before anybody claimed it (and, measured on build 517554, at the claim, `SetClientPassword`,
  `CreateNewGame` and a load of another session; not for tokens, server options, `SaveGame`,
  `Shutdown` or a start), so the file alone proves nothing. The agent writes the marker only on a
  container without published ports, or after the panel's own operation that changed the file
  (`client_password`, `new_game`, `load_game`, `upload_save` with load: the driver's
  `CHANGES_SETTINGS`) on a published container whose marker matched right before it; it waits until the
  file is stable (`settle`), and a removal meanwhile (a per-server generation) cancels the write. It is
  removed by the panel's `mark_unclaimed` (`DELETE /claim-marker`), by the agent on every file operation
  under `saved/` (`claimmarker.GuardedRoot`, the file API's root; it also refuses writes onto the marker
  and the wrapper and renames into or out of `.mcpanel/`), every restore, restore of a path, snapshot
  rollback and clone, at a claim re-run, and by the check loop when the file changes outside the panel's
  operations (`check_outside_change`).
* **The start wrapper** `.mcpanel/start-guard.sh` is the container's entrypoint (`/bin/sh`, written by
  the profile's `prepare_start` before every create and start the agent makes): with published ports
  (`MCPANEL_PORTS_PUBLISHED`, injected at creation and reserved) it execs the image's `/init.sh` only
  while the marker matches (GNU `stat`/`sha256sum` in the image), and exits 78 otherwise, before the
  game runs. So a hand `docker start`/`docker restart` or a daemon restart never gets the API of an
  unverified server answering on a published port (real containers, 2026-11-11).
* The agent's `publish_guard` runs before every start of a container with published ports (apply,
  `/start`, `/restart`; `docker_ops.start_guard`): the claim file must be a non-empty regular file, the
  marker must match it, the token file must exist, and a running container's API is probed (up to 20 s):
  unclaimed -> 409 `server_unclaimed`, no answer or an unexpected one -> 409 `claim_unverified`. A
  refused `/restart` stops nothing.
* The check loop (`mcagent/claimguard.py`, every 2 s and at once on Docker's `start` events, read in
  windows of 5 s with `since` = the previous `until`) stops a published run younger than 30 s without a
  matching marker at once (before its API is up), probes every published run as soon as its API answers
  and stops an unclaimed one (no save), and records the wrapper's refusal (a `die` event with exit 78);
  `guard_stop` in `/status`, kept outside the server folder. A long-running server whose settings file
  changed (an admin in the game) keeps running; only its marker goes.
* The panel closes the ports (`mark_unclaimed`: `ports_open` false; `claim`, `access`, `new_game`,
  `publish` pending; the agent's marker removed) whenever the claim may be lost: a restore, a
  restore-path or snapshot restore, a file upload, edit, rename, delete, extract or import under
  `saved/` (`files_touched`), a `guard_stop` or an exit 78 the reconciler sees (`observed`), a 409
  `server_unclaimed` from the agent (`handlers.unclaimed_refusal`; the apply is then made again without
  ports, the restart job too, and the setup is queued), and a re-run of the claim step (which first
  recreates a published container without ports). While the ports are closed every start recreates a
  published container unpublished (`must_apply_before_start`), and a job that started the server again
  queues the setup. `setup.ports_open` in the API is true only once the publish step is done.
* A clone or a server restored as new takes over the admin password, the secrets and a fresh setup
  (`copied_state`): its copied claim is logged into, it makes its own API token and publishes after its
  setup. A clone "without the worlds" leaves out `saved/server/` and `backups/` (`world_folders`).

Settings are two virtual config "files" (`is_virtual_config`): launch settings kept in
`game_options["launch"]` and passed as the image's variables (restart), and the server options read
from and written to the API while it runs. Saves (`SatisfactorySaves`; "current" is the session the
server has loaded now, from the API, none while no game is loaded) run their own jobs
(`run_reset`, `run_import`, `export`): a stopped server is started for them and stopped again (also when
it never got ready), a safety backup comes first (without one the running game is saved first), and the
earlier sessions stay in `saved/server/`. A new game under an existing session's name is refused
(`session_exists`, panel and agent); an import never takes a name the game or the panel writes later
(`*_autosave_<n>`, `*_mcpanel`), never overwrites a file (`save_exists`), and the agent saves the running
game before it loads the upload.

### Terraria

Three variants (`server_type`), one agent profile each (game `terraria`):

* **vanilla / tModLoader**: `passivelemon/terraria-docker:terraria-<version>` /
  `:tmodloader-<version>`, root with CHOWN DAC_OVERRIDE FOWNER SETUID SETGID (the entrypoint creates a
  user from `PUID`/`PGID`, chowns `/opt/terraria` and drops to it; `--user` fails). A runtime gid that
  already exists in the image (100) becomes the uid as `PGID` (`profiles.terraria.terraria_ids`), or
  `groupadd` fails. `SERVERCONFIG=1`: the server reads the panel's `serverconfig.txt` from the server
  folder (`/opt/terraria/config`); the image's other variables are refused (they are written into that
  file unescaped). tModLoader needs `MODPACK` (the definition sets `mcpanel`) and
  `ModPacks/mcpanel/Mods/enabled.json`, else it exits 0. The server runs in tmux: commands only
  through the image's `inject` script (console driver, `exec` transport, `tmux` guard, as `PUID:PGID`).
  Readiness: the log line `Server started`. SIGTERM saves after a 5 s warning and exits 143 (a clean
  stop). Consistent backups: `save`, then vanilla's `Backing up world file` line; tModLoader prints
  no end of a save, so the driver waits until `Worlds/*.twld` gets a newer mtime and keeps it 0.5 s
  (bounded; the job log says so).
* **TShock**: `ryshe/terraria:tshock-<terraria>-<tshock>` (TShock 6+), run without the image's
  bootstrap: entrypoint `/tshock/TShock.Server`, user = runtime uid:gid, no capability, workdir `/tmp`,
  `HOME`/`DOTNET_BUNDLE_EXTRACT_BASE_DIR` = `/tmp`, arguments `-configpath /config -logpath /config/logs
  -additionalplugins /plugins -config /config/serverconfig.txt` (the TShock plugin comes from the image's
  `/plugins`, so it follows the image version); the server folder is `/config`, the image's three other
  VOLUMEs are tmpfs. Control: the `tshock-rest` driver (`drivers/tshock.py`): REST on 7878 over the
  instance network (never published) with the application token the panel writes into `config.json`
  (`Settings.ApplicationRestTokens`, user `mcpanel`, group superadmin; the token is the control secret
  `Instance.rcon_password`); `rawcmd` for commands, `/v2/world/save` (synchronous) for saves,
  `/v2/server/status` for `/ping` (players, limit), `/v2/users/*` for the owner account
  (`game_action owner_account`); stdin when the API cannot be used. Stop signal SIGINT (saves, exit 0).
  The one-time `/setup <code>` is masked in every console text the agent hands out; the setup step
  `owner_account` (secret `owner_password`) creates the owner account, so TShock disables the code at
  its next start.

The panel owns `world`, `worldpath`, `worldname`, `port`, `upnp`, `banlist` (and `modpack`/`modpath`)
in `serverconfig.txt` and writes them, plus `autocreate` when missing, before every apply. The world is
`Worlds/<game_options.world_name>.wld`; `TerrariaSaves` moves every world file aside on reset/import,
and a reset writes size, difficulty and seed (`prepare_reset`; the size is always written, never left
to an older value meeting a bigger world). Versions come from Docker Hub's tags per variant (tModLoader:
`x.y.3.z` stable, `x.y.2.z` previews; `*-latest` digests mark the latest). Players: vanilla/tModLoader
from the console's `playing` (cached 55 s; so the player history sends one `playing` a minute while
the server runs), kick and ban by command, unban by editing `banlist.txt`; TShock from its status,
kick and `ban add` by command.

### Six Steam-era games (Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2, Rust)

Contract `audit-2026-11-09/briefs/games-6-contract.md`, ground truth `audit-2026-11-09/design/probe-6games.md`
(outside the repository). The definitions share `apps/games/steamgame.py` (`SteamGameDefinition`): no
version to pick (`fixed_version`: the image runs SteamCMD or DepotDownloader at every start and follows
Steam's public branch; HKMP's server comes with the image, its tag is the version), memory model
`plain`, restart policy "no" (panel and agent), the runtime image pinned by the panel release
(`image_tag`, Rust `image_digest`; `runtime_images` may name another repository), virtual configs kept in
`game_options[<config id>]` and passed as variables (`config_values`, `write_virtual_config`,
`check_config` for rules across fields; a change makes the spec "restart required"), every published
port 1:1, `TZ` as the only owner variable, and diagnostics for a failed download, a full disk and running
out of memory (`steam_issues`). The agent's profiles (`agent/mcagent/profiles/{palworld,valheim,zomboid,
hkmp,cs2,rust}.py`, shared patterns in `steam.py`) allow exactly the variables the definitions set, with
the same value patterns; `backend/tests/test_games_six.py` runs the agent's own `build_create_kwargs`
on the specs the panel builds.

| | image (pinned) | ports (published 1:1) | control | settings | players | world |
|---|---|---|---|---|---|---|
| Palworld | `thijsvanloef/palworld-server-docker:v2.8.1` | game 8211/udp (`PORT`, `PUBLIC_PORT`) | `palworld-api` (REST 8212 internal, Basic `admin:<ADMIN_PASSWORD>`) | virtual `server`, `gameplay` (the image writes `PalWorldSettings.ini` from them) | REST list; kick/ban/unban by user id (`steam_<id>`, an online name is resolved); bans from `banlist.txt` | folder `Pal/Saved/SaveGames/0/<id>/`, named by `GameUserSettings.ini` `DedicatedServerName` |
| Valheim | `ghcr.io/community-valheim-tools/valheim-server:1.4.1` | game 2456/udp (`SERVER_PORT`), query = +1, crossplay = +2 (`when` crossplay) | none | virtual `server` (name, public, crossplay, save interval, preset/modifiers/keys as whitelisted `SERVER_ARGS`) | the three list files (`config/adminlist.txt`, `bannedlist.txt`, `permittedlist.txt`, SteamID64), no online list | folder `config/worlds_local/<world>/` (+ older `.db`/`.fwl`) |
| Project Zomboid | `indifferentbroccoli/projectzomboid-server-docker:v1.2.0` | game 16261/udp (`DEFAULT_PORT`), RakNet 16262/udp (`UDP_PORT`) | `rcon` (27015 internal, password injected by the agent), status through A2S | virtual `launch` (max players); `data/Server/<name>.ini` (`kv`; the image's keys managed; `UPnP=false`, `BackupsOnStart=false` by `before_apply`) | RCON `players`; kick/ban/unban/admin/whitelist by RCON (lists live in the game's database: not listed) | folder `data/Saves/Multiplayer/<name>/` (+ `_player`; reset may move `data/db/<name>.db`) |
| HKMP | `maximalmax90/hkmpserver:v2.4.3-2` | game 2222/udp (`PORT`) | `console` exec `hkmp <line>` (guard `screen`: no `^`, `\`, control characters) | virtual `launch` (full sync); `serversettings.json`, `whitelist.json` (`json`) | `list`; kick/ban(ip)/unban(ip)/auth/whitelist through the console; lists from the JSON files | none |
| Counter-Strike 2 | `joedwards32/cs2:5.0.1` (uid 1000:1000, `init`) | game 27015/udp (`CS2_PORT`), CSTV 27020/udp (`when` cstv) | `rcon` on the game port's number over TCP (`port_env`), status through A2S | virtual `server` (name, players, mode, map, LAN, bots, CSTV); `server.cfg` is the image's (masked, write-protected) | A2S names; kick | none (backups keep `game/csgo/cfg/`) |
| Rust | `didstopia/rust-server@sha256:c60a3ab0...` | game 28015/udp, query 28017/udp, Rust+ 28082/tcp (`when` rust_plus; never 28016/8080) | `webrcon` (WebSocket 28016 internal), status through A2S | virtual `server` (name, description, players, map size, seed fixed at creation, save interval, Rust+) | `playerlist`; kick/ban/unban/owners through WebRCON operations; owners and bans from `cfg/users.cfg`, `cfg/bans.cfg` | folder `server/<identity>/`: a reset moves map, saves and the world's databases (blueprints when asked) and picks the seed; import/export the whole folder |

Secrets: Palworld `admin_password` (generated, revealable; `ADMIN_PASSWORD`, the REST API's password)
and `server_password`; Valheim `server_password` (required, 5+ characters, not containing the server
name); Project Zomboid `admin_password` (generated, revealable: the in-game account `admin`); CS2 `gslt`
(link to Steam's token page), `server_password`, `tv_password`. The RCON passwords of PZ, CS2 and Rust are
the control secret (`Instance.rcon_password`, 32 letters and digits), injected by the agent
(`RCON_PASSWORD`, `CS2_RCONPW`, `RUST_RCON_PASSWORD`) and masked in the files that hold them. SteamCMD
failures are retried on the first start and every start (`retryable_install_exits`: PZ 1, CS2 5/8,
Rust 1) like Satisfactory's. Players' online counts for the metrics come from A2S (`/ping`, capability
`query`) for Project Zomboid, CS2 and Rust, from the game's own list for Palworld and HKMP (Valheim: none).

### Adding a game

1. Agent: a profile module in `agent/mcagent/profiles/` (copy `factorio.py`), registered in
   `profiles/__init__.py`; a driver in `agent/mcagent/drivers/` only if no existing one fits
   (register it in `drivers/__init__.py` `FACTORIES`); new error codes in `agent/mcagent/errors.py`.
   `profiles.register` refuses a profile whose health probe is not implemented.
2. Panel: a definition module in `backend/apps/games/` (copy `factorio.py`; an image that downloads
   its game and follows one branch: copy one of the six on `steamgame.py`), registered in
   `apps/games/__init__.py`: ports (`PortSpec`), options, config files with their schema, secrets,
   setup steps (and `run_setup_step`), versions and image rule, `game_env`, capabilities, players,
   saves model (a world that is a folder: `foldersaves.FolderSaves`).
3. Frontend: nothing game-specific is needed for the generic pages (they read the catalog entry and
   the capabilities); a game with its own concepts adds a page or a capability.
4. Tests: a definition test (spec, catalog, codecs), a profile test (create kwargs), a driver test
   against a fake of its protocol, and a run on real containers.

## Data layout on the host (`AGENT_DATA_ROOT`)

```
<data root>/instances/<instance-uuid>/   -> mounted at /data in the Minecraft container (Factorio: /factorio)
<instance dir>/.volumes/<path>           -> other games: an image VOLUME that is not the profile's mount
<instance dir>/saves-replaced/<stamp>/   -> Factorio: the saves a world reset or an import moved aside
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
<data root>/agent/saving-paused/<id>     -> marker while a backup/snapshot holds save-off (save-on is sent if it is left)
<data root>/agent/instance-networks.json -> last known instance-network subnets for the source guard (0600)
<data root>/agent/last-run/<instance-uuid>.json -> record of a stopped server's last run: exit state and the last
                                           2,000 console lines (folder 0700, file 0600; never in a backup)
<data root>/agent/cache/file-hashes/     -> persistent file-digest cache for /files/hashes (AGENT_CACHE_ROOT; rebuildable)
```

The agent sets `<data root>` and `<data root>/instances` to mode 0750, owner root, at every start
(`maintenance.restrict_data_root`), so other users of the host (a guest-readable appdata share)
cannot read server folders and their RCON passwords. Also at every start, in a thread,
`maintenance.sweep_leftovers` removes what interrupted operations left behind (`.mcpanel-tmp-*`,
`.mcpanel-world-new-*`, clone copies, `staging/` and `trash/` entries) when it is older than the
agent process and no operation runs for that server. Copies that may be the only copy of a
server's files (a restore or previous copy whose server folder is missing or empty,
`.mcpanel-world-old-*`) are kept and reported in the log.

## Backend apps

`accounts` (users, roles, per-instance grants, permission matrix) · `audit` · `nodes` (Node + agent
client) · `games` (game definitions: Minecraft, Factorio; config codecs; `GET /api/catalog/games`) · `catalog` (Mojang/Fabric/Quilt/Forge/NeoForge/Paper/Purpur metadata, Java resolution) ·
`instances` (model, spec builder, state, reconcile, handlers, schedules, files/properties/console
proxies) · `jobs` (runner, watchdog) · `modpacks` (Modrinth, CurseForge, FTB adapters) · `content`
(mods/plugins manager) · `backups` · `players` · `console` (WebSocket consumers, event fan-out) · `core`
(errors, permissions helpers, settings, crypto, request limits, streaming).

Management commands (`docker compose exec panel python manage.py <command>`): `host_shutdown`
and `retire_stopped_containers` (`apps/instances`), `ensure_backup_schedules` (`apps/backups`),
`adopt_backups` (`apps/backups`), `add_node` (`apps/nodes`), `reencrypt_secrets` (`apps/core`).

Deployment code outside the two images: `deploy/scripts/upgrade.sh` (upgrade, rollback,
database restore; state in `deploy/upgrade-state.env`) and `deploy/unraid/` (the
`stopping_docker` hook, `install-hook.sh`, and `backup-db.sh`, the daily dump of the database
off the cache drive).

## Real-time

* `/ws/instances/<id>/console/` — the panel relays the agent's log stream
  (`/v1/instances/<id>/logs/stream`, follows across restarts) and executes commands via RCON with a
  per-command permission check and audit entry. The game's stop word typed there (Minecraft:
  `stop`, any case, with or without `/`; Factorio: `/quit`) is not sent: over RCON it makes the server exit and Docker's restart policy starts it again at
  once. With `instance.control` the console enqueues the Stop button's job instead and sends a
  `notice` saying so; without it the command is refused (`stop_refused`). The notice carries the
  command's `id` (and `job_id`); the `command_result` with the same id and text follows, and the SPA
  ends the command's wait at the notice and does not show the result's text again. If the agent's log pump
  fails it reports the error and retries, and when it ends the agent closes the socket so that the
  panel's relay connects again. Both consumers cancel their relay and watchdog tasks in a
  `finally`, also when they end by an exception.
* `/ws/events/` — instance status and job progress, filtered by what the user may view; the SPA uses
  it to invalidate cached queries (polling remains as a fallback). The consumer renews its Channels
  group membership every hour (groups expire after 24 hours). After a 4401 the SPA reconnects if
  the tab is still signed in (a password change ends the old session's sockets); 4403 never.
