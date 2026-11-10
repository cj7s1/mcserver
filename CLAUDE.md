# CLAUDE.md — guide for future sessions

Self-hosted Minecraft server management platform (Unraid-first). Read `DEVELOPMENT_STATUS.md` first:
it records what works, what was verified and the next task. `ARCHITECTURE.md` explains why things are
the way they are; `docs/reference/*.md` holds **verified** upstream API notes (itzg, Modrinth,
CurseForge, FTB, version metadata, restic) — use them instead of guessing endpoints.

## Layout

```
agent/      FastAPI node agent (only component with Docker socket). mcagent/{main,docker_ops,lastrun,safefs,backups,snapshots,zfs,world,archive,fetch,hashcache,storage,rcon,tls,schemas,operations,maintenance,netguard,slp}.py
backend/    Django 5.2 + DRF + Channels + Celery. mcpanel/ (settings, urls, asgi, celery), apps/<app>/,
            container/ (entrypoint.sh, supervise.py, healthcheck.py: the single panel container)
frontend/   React 19 + TS 7 + Vite 8 + Tailwind 4 + shadcn/ui (radix-nova) + TanStack Query + react-router 8 + xterm.js
deploy/     docker-compose.yml (prod: panel, agent, postgres), docker-compose.node.yml, docker-compose.dev.yml,
            .env.example, scripts/ (generate-secrets.sh, upgrade.sh), unraid/ (stopping_docker hook, install-hook.sh, backup-db.sh)
docs/       QUICKSTART-UNRAID.md, UNRAID.md, UNRAID-SHUTDOWN.md, SECURITY.md, reference/ (upstream notes)
.github/    workflows/ci.yml, scripts/ (panel-acceptance.sh, upgrade-rehearsal.sh: real containers)
scripts/cloud-dev/  sandbox-only helpers (egress forwarder, env files, start-services.sh)
```

## Commands

```bash
# backend
cd backend && uv sync && MCPANEL_DEBUG=1 uv run python manage.py migrate
cd backend && uv run pytest -q -m "not integration"          # conftest sets MCPANEL_TESTING=1
cd backend && MCPANEL_TEST_DB_NAME=test_mcpanel_<name> uv run pytest -q ...  # own test DB for parallel runs
cd backend && uv run ruff check . && uv run ruff format .
# agent
cd agent && uv sync && uv run pytest -q && uv run ruff check .
# frontend
cd frontend && npm ci && npx tsc -b && npm run lint && npm run build && npx vitest run
cd frontend && npx vite --port 5173          # proxies /api and /ws to 127.0.0.1:8000
# integration (needs a running agent + Docker; starts a real vanilla server)
cd backend && MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration
# real containers (need a built stack; see TESTING.md)
cd deploy && ../.github/scripts/panel-acceptance.sh
.github/scripts/upgrade-rehearsal.sh <site checkout of the old revision> <new revision>
```

Two backend tests read the documentation (`tests/test_panel_container.py`,
`tests/test_security_audit_http.py`): DEPLOYMENT.md must keep the `upgrade.sh` commands and the
heading text "Coming from the 7-container layout"; README.md, ARCHITECTURE.md, docs/UNRAID.md and
docs/QUICKSTART-UNRAID.md must not name the old periodic-worker service. Run both after editing docs.

### Cloud sandbox specifics (not needed on a real host)

* Start daemons: `dockerd &`, `service postgresql start`, `service redis-server start`
  (DB `mcpanel`/user `mcpanel`/pw `mcpanel`, CREATEDB).
* Containers can only reach the internet through the sandbox proxy:
  `python3 scripts/cloud-dev/egress_forwarder.py 0.0.0.0 3128 127.0.0.1 <port of $HTTPS_PROXY> &` (the port
  changes between container restarts; bootstrap.sh derives it); the agent injects proxy
  env + CA via `scripts/cloud-dev/agent.env` (`AGENT_RUNTIME_EXTRA_ENV`, `AGENT_RUNTIME_CA_BUNDLE`,
  `AGENT_RUNTIME_JAVA_TRUSTSTORE`). Network `mcpanel_instances` gateway is 172.18.0.1.
* After a container restart run `scripts/cloud-dev/bootstrap.sh` (daemons, btrfs mount, egress forwarder,
  agents :8700/:8701, API, worker, Vite). Files under /srv and the venvs/node_modules persist.
* `scripts/cloud-dev/start-services.sh` (run it by path; don't inline its `pkill` lines in a shell whose
  command line contains the same pattern) starts agent :8700, API :8000, worker+beat.
* `git push` may hang on `push.negotiate`; use `git -c push.negotiate=false push -u origin <branch>`.
* restic binary: copy from the `restic/restic` image (`docker create` + `docker cp`); github.com
  release downloads are blocked. Docker Hub sometimes returns 429 — retry later.

## Subsystem map (where things live)

| Area | Backend | Agent |
|---|---|---|
| Lifecycle, spec, status, crash loops | `apps/instances/{handlers,spec,state,reconcile}.py`. A job starts a server only through `handlers.start_server` (creates the container when there is none). A job that stopped a running server and then fails starts it again when nothing was changed, else records it stopped (`put_back`, `Progress`) | `docker_ops.py`; apply creates the new container before it removes the old one |
| Memory admission | `apps/instances/capacity.py` (node RAM minus system setting `memory_reserve_mb` vs. limits of running containers; 409 `insufficient_memory`, override `allow_overcommit`); frontend `lib/power.ts` (`sendPower`, the one power path), `components/{MemoryOvercommitDialog,MemoryCommitBar}.tsx` | `GET /v1/info` (`docker.memory_bytes`) |
| Runtime images | `spec.current_image` (channel reaches existing servers), `handlers.apply_container` (`pull="newer"` on every start that creates a container) | `main.apply_spec`, `docker_ops` (bounded pull, superseded images removed); feature `pull_newer` |
| Cron schedules | `apps/instances/scheduling.py`: one try/except per schedule, impossible dates refused/paused, backups before snapshots before power actions, deferral when busy (`Schedule.deferred_at`, 60 min), misfire rule after downtime, wall-clock DST handling; Celery beat runs in UTC | — |
| Stopped server = no container (retire, last-run record) | `reconcile.py` (`retire_blocker`, `maybe_retire`, `retire_container`), `state.compute` (reads `last_run`), `diagnostics.py`, `manage.py retire_stopped_containers`, setting `MCPANEL_RETIRE_STOPPED_CONTAINERS` | `docker_ops.py` (`retire`, `instance_lock`: the per-server lock around apply/start/restart/remove/retire), `lastrun.py`, `POST /retire`, `DELETE /last-run`, `FEATURES` in `main.py` |
| Restart required | `spec.py`: `spec_outdated` (hash differs) vs `restart_required` (hash differs **and** the server has a container); `CONTAINER_FORMAT` is mixed into the hash | `docker_ops.CONTAINER_FORMAT` (reported in `/v1/info` and by every apply). Raise both together |
| Graceful stop, Docker stop timeout | `handlers._log_stop`, `spec.build_env` (`CREATE_CONSOLE_IN_PIPE`), `nodes/client.py` (`STOP_EXTRA_SECONDS`) | `docker_ops.py` (`graceful_stop`, `_RconStopCourier`, `build_create_kwargs`, `create_container`), `config.py` (`shutdown_stop_timeout` = `AGENT_SHUTDOWN_STOP_TIMEOUT`) |
| Host shutdown + resume (Unraid hook) | `manage.py host_shutdown`, `scheduling.resume_flagged_servers` (from `tasks.scheduler_tick`), `Instance.resume_after_boot` / `resume_flagged_at`; hook and installer in `deploy/unraid/`; docs/UNRAID-SHUTDOWN.md | the stop endpoint |
| Panel container (supervisor, health, start-up guard) | `backend/container/{entrypoint.sh,supervise.py,healthcheck.py}`, `mcpanel/celery.py` (tick age, quiet tick logging), `apps/core/views.py` (`HealthView`: `components`, `tick_age_seconds`) | — |
| Jobs: locks, heartbeat, re-sending, watchdog | `apps/jobs/{runner,tasks}.py` (`dispatch`, `resend_queued_jobs`, `watchdog`; `finish()` only ends an active job), `apps/jobs/shutdown.py` (a stopping worker ends job waits at once), `mcpanel/celery.py` (`worker_ready`, `worker_shutting_down`), `apps/backups/handlers.settle_orphaned_backups` | `operations.py` (one operation per server) |
| Request limits, uploads, downloads | `apps/core/asgi.py` (`RequestBodyLimit` before Django, `PanelASGIHandler` spools to `MCPANEL_UPLOAD_TMP`), `apps/core/uploads.py` (one copy on disk), `apps/core/streaming.py` (`AgentStreamingResponse`); web process has `CONN_MAX_AGE=0` | — |
| Agent housekeeping at start | — | `maintenance.py` (data root 0750, sweep of interrupted-operation leftovers), `netguard.py` (instance-network source guard before Docker answers), `backups.startup_unlock` (restic locks), saving-paused markers in `main.py` |
| Agent capabilities for mixed versions | `nodes/client.py` (`features`, `supports`), `spec.applied_container_format` | `main.py` (`FEATURES`, `GET /v1/info`) |
| Upgrade, rollback, database restore | `deploy/scripts/upgrade.sh` (state in `deploy/upgrade-state.env`); tests `tests/test_upgrade_script.py` + `tests/upgrade_shim/docker`, `.github/scripts/upgrade-rehearsal.sh` | — |
| Diagnostics / manual downloads | `apps/instances/diagnostics.py` | (reads logs/files) |
| Modpacks (Modrinth/CurseForge/FTB), preflight, pinning, update/rollback | `apps/modpacks/{providers,mrpack,pinning,handlers}.py` | — |
| Mods/plugins (Modrinth + Hangar + SpigotMC via Spiget) | `apps/content/{service,hangar_source,spigot_source}.py`, `apps/content/packfiles.py` (pack-managed jars, `*_IGNORE_MISSING_FILES`), `apps/modpacks/{hangar,spiget}.py` | `fetch.py`, `hashcache.py`, `/files/hashes` (null hash above 2 GiB, 503 `hashing_busy`) |
| Backups (restic), backup browser, retention | `apps/backups/{handlers,retention,views}.py`; `manage.py adopt_backups` (rows for snapshots the database does not know); `deploy/unraid/backup-db.sh` (daily database dump) | `backups.py` |
| Backup safeguards (notice, nightly schedule) | `apps/instances/services.py` (`add_nightly_backup_schedule`, `nightly_backup_cron`, `backup_overview_annotations`), `serializers.py` (`last_backup_at`, `backup_schedules`, `nightly_backup`), `manage.py ensure_backup_schedules`; frontend `lib/backup-health.ts`, `components/BackupNotice.tsx` | — |
| Snapshots (btrfs, ZFS) | `apps/backups/handlers.py` (snapshot_*), schedules in `apps/instances/scheduling.py` | `snapshots.py` (`delete_support`: probes whether deletes are `native` or file-by-file `fallback`), `zfs.py` |
| Import / export (archives) | `apps/instances/views.py` (Import/Extract/Archive) | `archive.py` |
| Power warnings / update checks | `apps/instances/power_warnings.py` (in-game countdown before restart/stop), `apps/modpacks/{updates,tasks}.py` (periodic modpack update checks) | (RCON via `rcon.py`) |
| World import / export (level-name, Bukkit dims, replace + rollback) | `apps/instances/{world,views,handlers}.py` (`world_replace` job) | `world.py` |
| World reset (plan + blockers, verified safety backup, seed, delete + undo; UI: Settings → Danger zone, `frontend/src/pages/instance/settings/reset-world-*`) | `apps/instances/world.py` (`reset_plan`), `views.py` (`WorldResetView`), `handlers.py` (`world_reset` job); contract tests `tests/test_world_reset_contract.py` | `world.py` (`classify`, `reset`), `POST /world/reset` |
| Metrics + player history | `apps/metrics` (players by status ping, RCON `list` only for older agents) | `/stats`, `/ping` (`slp.py`) |
| Console | `apps/console/consumers.py` (typed `stop` becomes the stop job + a notice; tasks cleaned up in `finally`; events group renewed hourly) | log stream (`main.stream_logs`) |
| Frontend resilience | `components/RouteError.tsx`, `lib/chunk-reload.ts`, `ConnectionLostBanner` in `components/States.tsx`, `lib/clipboard.ts`, `lib/batches.ts` | — |
| Notifications | `apps/notifications` | — |
| Nodes (multi-host) | `apps/nodes` | one agent per host |

## Conventions

* **No mock functionality in product paths.** If something is not implemented, the UI says so.
* Every state-changing long operation is a `Job` (`apps/jobs/runner.py`: `@register(kind)`, `enqueue()`);
  exclusive jobs lock the instance. Handlers live next to their domain (`apps/instances/handlers.py`,
  `apps/backups/handlers.py`) and are imported in the app's `ready()`.
* Permission checks: `apps.core.permissions.get_instance_for(user, pk, "instance.<code>")` for instance
  endpoints (404 if invisible, 403 if lacking the code), `require(user, code)` for global ones. New codes go
  in `apps/accounts/permissions.py`.
* Errors: raise `apps.core.errors.ApiError(message, code=..., status_code=...)`; the frontend shows
  `error.message` and field errors. Agent errors surface as `AgentError` with the agent's code.
* Audit every mutation with `apps.audit.service.record(...)` — never put secrets in `details`.
* The agent re-validates everything; never rely on the panel for agent-side safety.
* itzg env is built only in `apps/instances/spec.py`; keys in `MANAGED_ENV` cannot be overridden by users.
* Frontend: API calls via `src/lib/api.ts`, query keys in `src/lib/queries.ts`, types in `src/lib/types.ts`,
  shared UI in `src/components/`, instance pages get `{instance, can}` from `useInstanceContext()`.
* Keep `DEVELOPMENT_STATUS.md` and `ROADMAP.md` updated when a feature lands; commit often.

* Security audit: on hold in session 1 at the user's request; the user asked to start it on 2026-11-06
  (session 2). Results and open findings are tracked in DEVELOPMENT_STATUS.md, also for the audit of
  2026-11-09 (work lists in `audit-2026-11-09/briefs/` next to the checkout, outside the repository).

## Security rules (non-negotiable)

* Never mount the Docker socket into the panel/worker; never publish the agent port.
* Never accept host paths, image names outside the allow-list, privileged flags, capabilities or
  devices from users.
* Never run shell commands built from user input (agent uses argv lists only).
* Never log credentials, tokens, RCON passwords or API keys.
* Destructive operations require explicit confirmation and are audited; restores take a safety backup
  by default; deletes of a server require typing its name.
