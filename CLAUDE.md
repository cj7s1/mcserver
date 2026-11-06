# CLAUDE.md — guide for future sessions

Self-hosted Minecraft server management platform (Unraid-first). Read `DEVELOPMENT_STATUS.md` first:
it records what works, what was verified and the next task. `ARCHITECTURE.md` explains why things are
the way they are; `docs/reference/*.md` holds **verified** upstream API notes (itzg, Modrinth,
CurseForge, FTB, version metadata, restic) — use them instead of guessing endpoints.

## Layout

```
agent/      FastAPI node agent (only component with Docker socket). mcagent/{main,docker_ops,safefs,backups,fetch,storage,rcon,schemas,operations}.py
backend/    Django 5.2 + DRF + Channels + Celery. mcpanel/ (settings, urls, asgi, celery), apps/<app>/
frontend/   React 19 + TS 7 + Vite 8 + Tailwind 4 + shadcn/ui (radix-nova) + TanStack Query + react-router 8 + xterm.js
deploy/     docker-compose.yml (prod), docker-compose.dev.yml, .env.example, scripts/
docs/       UNRAID.md, SECURITY.md, reference/ (upstream notes)
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
```

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
| Lifecycle, spec, status, crash loops | `apps/instances/{handlers,spec,state,reconcile}.py` | `docker_ops.py` |
| Diagnostics / manual downloads | `apps/instances/diagnostics.py` | (reads logs/files) |
| Modpacks (Modrinth/CurseForge/FTB), preflight, pinning, update/rollback | `apps/modpacks/{providers,mrpack,pinning,handlers}.py` | — |
| Mods/plugins (Modrinth + Hangar + SpigotMC via Spiget) | `apps/content/{service,hangar_source,spigot_source}.py`, `apps/modpacks/{hangar,spiget}.py` | `fetch.py`, `hashcache.py`, `/files/hashes` |
| Backups (restic), backup browser, retention | `apps/backups/{handlers,retention,views}.py` | `backups.py` |
| Snapshots (btrfs, ZFS) | `apps/backups/handlers.py` (snapshot_*), schedules in `apps/instances/scheduling.py` | `snapshots.py`, `zfs.py` |
| Import / export (archives) | `apps/instances/views.py` (Import/Extract/Archive) | `archive.py` |
| Power warnings / update checks | `apps/instances/power_warnings.py` (in-game countdown before restart/stop), `apps/modpacks/{updates,tasks}.py` (periodic modpack update checks) | (RCON via `rcon.py`) |
| World import / export (level-name, Bukkit dims, replace + rollback) | `apps/instances/{world,views,handlers}.py` (`world_replace` job) | `world.py` |
| Metrics + player history | `apps/metrics` | `/stats` |
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
  (session 2). Results and open findings are tracked in DEVELOPMENT_STATUS.md.

## Security rules (non-negotiable)

* Never mount the Docker socket into the panel/worker; never publish the agent port.
* Never accept host paths, image names outside the allow-list, privileged flags, capabilities or
  devices from users.
* Never run shell commands built from user input (agent uses argv lists only).
* Never log credentials, tokens, RCON passwords or API keys.
* Destructive operations require explicit confirmation and are audited; restores take a safety backup
  by default; deletes of a server require typing its name.
