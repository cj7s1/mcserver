# Testing

## Suites

| Suite | Command | Needs | What it covers |
|---|---|---|---|
| Backend unit + API | `cd backend && uv run pytest -q -m "not integration"` | Postgres + Redis | state machine, retention, properties, spec/env builder, catalog parsing, ports, permission matrix, auth/CSRF/setup, permission enforcement through the API (scenario 8), validation, job locks, content manager, diagnostics signatures, crash-loop tracking, notifications, metrics, mrpack preflight |
| Backend integration | `cd backend && MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration` | running agent + Docker + internet | real vanilla server: install, readiness, RCON, stop/start persistence, delete (scenario 1); backup/modify/restore (scenario 7) when restic is installed |
| Agent | `cd agent && uv run pytest -q` | — (restic tests skip without restic) | symlink/traversal-safe file ops, archive extraction (zip-slip, links, bombs), spec/security policy, auth, fetch allow-list, storage parsing, real restic backup/ls/dump/restore-path |
| Frontend | `cd frontend && npx tsc -b && npm run lint && npm run build && npx vitest run` | — | type safety, lint, build, component tests |
| Browser | Playwright scripts used during development (see DEVELOPMENT_STATUS.md) | dev server + API | every route at desktop and mobile widths, console errors, layout |

The backend `tests/conftest.py` sets `MCPANEL_TESTING=1` (locmem cache, in-memory channel layer, eager
Celery, fast hasher). Postgres is still required because JSON `contains` lookups are Postgres-only.

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
| 7 | Backup → modify → restore → verify | R, I | consistent (save-off/flush) backup while running; pre-restore safety backup; single-file restore from the backup browser also verified |
| 8 | Restricted user rejected by backend | U | viewer/operator/per-instance grants, CSRF, 404 for invisible servers |
| 9 | Restart app services, instances intact | R | agent/API/worker restarted many times during development; containers kept running and were re-discovered by the reconciler |
| 10 | Failure handling | U, R | crash loop → failed + auto-stop + diagnosis; downgrade/invalid version rejected; port conflict 409; corrupt archives rejected |

Extra verified flows (R): import an existing server from a zip, software update 1.20.4 → 1.21.1 with
Java 17 → 21 and a pre-update backup, webhook notifications, Docker Compose deployment smoke test.

## Heavy tests

Large modpacks (Better MC etc.) download hundreds of MB and need 8+ GB RAM. They are **not** part of CI.
To reproduce on a real host, create the server from the wizard and follow `DEVELOPMENT_STATUS.md` →
"How Better MC was validated".
