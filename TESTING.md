# Testing

## Suites

| Suite | Command | Needs | What it covers |
|---|---|---|---|
| Backend unit + API | `cd backend && uv run pytest -q -m "not integration"` | Postgres + Redis | state machine, retention, properties, spec/env builder, catalog parsing, ports, permission matrix, auth/CSRF/setup, permission enforcement through the API (scenario 8), validation, job locks, content manager, diagnostics signatures, crash-loop tracking, notifications, metrics, mrpack preflight |
| Backend integration | `cd backend && MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration` (+ the btrfs variables below) | running agent(s) + Docker + internet | see "Integration tests" below: vanilla lifecycle + backup/restore, backup browser + single-path restore, Paper + Modrinth plugin, zip import, btrfs snapshots over a TLS-pinned node |
| Agent | `cd agent && uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test` for the real btrfs tests) | — (restic/btrfs tests skip without them) | symlink/traversal-safe file ops, archive extraction (zip-slip, links, bombs), spec/security policy, auth, fetch allow-list, storage parsing, real restic backup/ls/dump/restore-path, real btrfs snapshots (incl. deleting a server purges its snapshots) |
| Frontend | `cd frontend && npx tsc -b && npm run lint && npm run build && npx vitest run` | — | type safety, lint, build, component tests |
| Browser | Playwright scripts used during development (see DEVELOPMENT_STATUS.md) | dev server + API | every route at desktop and mobile widths, console errors, layout |

The backend `tests/conftest.py` sets `MCPANEL_TESTING=1` (locmem cache, in-memory channel layer, eager
Celery, fast hasher). Postgres is still required because JSON `contains` lookups are Postgres-only.
Set `MCPANEL_TEST_DB_NAME` (default `test_mcpanel`) to give concurrent runs on one Postgres separate
throw-away databases.

## Integration tests

Marker `integration`; every file skips cleanly when its variables are not set. They call the panel's own
code paths (job handlers through the job runner, `apps.content.service`, API views with a logged-in
owner) against real agents through the real `AgentClient`. Everything they create is named `it-…` and
removed by fixture finalizers (`tests/integration_support.py`), also when an assertion fails; they never
touch other servers.

```bash
cd backend
FP=$(cd ../agent && AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TLS=auto uv run python -m mcagent fingerprint)
MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 \
MCPANEL_IT_BTRFS_AGENT_URL=https://127.0.0.1:8701 MCPANEL_IT_BTRFS_TOKEN=btrfs-node-token \
MCPANEL_IT_BTRFS_FINGERPRINT="$FP" uv run pytest -q -m integration -s
```

| Variable | Default | Used by |
|---|---|---|
| `MCPANEL_IT_AGENT_URL` | — (skip) | agent, backup browser, content, import tests |
| `MCPANEL_IT_AGENT_TOKEN` | `dev-agent-token` | same |
| `MCPANEL_IT_AGENT_DATA_ROOT` | `/srv/mcpanel-dev` | on-disk checks (removal, ownership); empty skips them |
| `MCPANEL_IT_READY_TIMEOUT` | `360` | seconds to wait for a server to become healthy |
| `MCPANEL_IT_BTRFS_AGENT_URL`, `_TOKEN`, `_FINGERPRINT` | — (skip) | snapshot tests: an `https://` agent on btrfs, pinned via `Node.tls_fingerprint` |
| `MCPANEL_IT_BTRFS_DATA_ROOT` | `/srv/btrfs-test/data` | on-disk subvolume checks; empty skips them |

| File | Starts Minecraft | What it verifies | Runtime (sandbox) |
|---|---|---|---|
| `test_integration_agent.py` | vanilla 1.20.4, ports 25600-25650 | install, readiness, RCON, stop/start persistence, online backup → modify → restore with safety backup, delete | ~70 s |
| `test_integration_content.py` | Paper 1.21.1, 1024 MB, ports 25800-25820 | content service installs Chunky from Modrinth (agent fetch with hash verification; a wrong hash is refused), inventory identifies it, restart loads it (RCON `plugins` + "Enabling" in the log), uninstall, stop, delete | ~57-74 s |
| `test_integration_backup_browse.py` | no | backup of a stopped server, browse 3 levels + binary download through the API, `restore-path` of one file (siblings untouched) and of a deleted folder, ownership, audit | ~17 s |
| `test_integration_import.py` | no | `POST /instances/<id>/import` of a zipped server folder: top folder stripped, files byte-identical, `../` entry and symlink skipped, archive removed, `installed_at` set, everything owned by the runtime uid/gid | ~3 s |
| `test_integration_snapshots.py` | no | pinned TLS (wrong fingerprint refused before the token is sent); install creates a subvolume; snapshot → modify → rollback (safety snapshot holds the pre-rollback state) → delete snapshots; deleting a server removes its remaining snapshots | ~10 s |

## Acceptance scenarios: verification log

Levels: **U** unit/API test · **I** automated integration test · **R** manually verified on a real
Minecraft server through the real API · **N** not yet verified. Nothing here has been verified on Unraid.

| # | Scenario | Level | Notes |
|---|---|---|---|
| 1 | Vanilla: create, launch, readiness, stop, restart, persistence | R, I | 1.20.4; placed block survived restart; graceful stop exit 0 |
| 2 | Fabric server with selected MC + loader | U (env), R (via BMC2 Fabric pack — see below) | |
| 3 | Forge server with Java selection | R | Better MC BMC4 (Forge 47.x, Java 17 chosen from Mojang metadata) |
| 4 | Better MC | R | CurseForge BMC4 v62hf: installed in ~3.5 min, health-checked running, 311 mods, 20 TPS, auto-pinned to file 9104992. Modrinth BMC4 v59: correctly diagnosed as needing 51 manual CurseForge files (crash loop detected + stopped); the wizard preflight warns before deploying |
| 5 | Change a property, restart, persists | R | `max-players`, `motd` |
| 6 | Port allocation, conflict rejection, survives recreation | U, R | 409 `port_conflict`; ports re-applied on recreate |
| 7 | Backup → modify → restore → verify | R, I | consistent (save-off/flush) backup while running; pre-restore safety backup; backup browser download and single-file/folder restore (I) |
| 8 | Restricted user rejected by backend | U | viewer/operator/per-instance grants, CSRF, 404 for invisible servers |
| 9 | Restart app services, instances intact | R | agent/API/worker restarted many times during development; containers kept running and were re-discovered by the reconciler |
| 10 | Failure handling | U, R | crash loop → failed + auto-stop + diagnosis; downgrade/invalid version rejected; port conflict 409; corrupt archives rejected |

Extra verified flows (R): FTB StoneBlock 3 (FTBA, pinned version), multi-node, software update 1.20.4 → 1.21.1 with
Java 17 → 21 and a pre-update backup, webhook notifications, Docker Compose deployment smoke test.
Also automated (I) since 2026-11-06: btrfs snapshot/rollback over a TLS-pinned node, import an existing server
from a zip, Modrinth plugin install on Paper.

## Heavy tests

Large modpacks (Better MC etc.) download hundreds of MB and need 8+ GB RAM. They are **not** part of CI.
To reproduce on a real host, create the server from the wizard and follow `DEVELOPMENT_STATUS.md` →
"How Better MC was validated".
