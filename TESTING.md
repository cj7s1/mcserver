# Testing

## Suites

| Suite | Command | Needs | What it covers |
|---|---|---|---|
| Backend unit + API | `cd backend && uv run pytest -q -m "not integration"` | Postgres + Redis | state machine, retention, properties, spec/env builder, catalog parsing, ports, permission matrix, auth/CSRF/setup, permission enforcement through the API (scenario 8), validation, job locks, content manager, diagnostics signatures, crash-loop tracking, notifications, metrics, mrpack preflight, world replace and world reset (plan and blockers, the job with its safety-backup checks and failure handling, and `test_world_reset_contract.py`, which compares names, fields and error codes with the agent and frontend sources) |
| Backend integration | `cd backend && MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration` (+ the btrfs variables below) | running agent(s) + Docker + internet | see "Integration tests" below: vanilla lifecycle + backup/restore, backup browser + single-path restore, Paper + Modrinth plugin, zip import, btrfs snapshots over a TLS-pinned node, world reset |
| Agent | `cd agent && uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test` for the real btrfs tests: run them twice, see "Agent tests on a real btrfs filesystem" below) | — (restic/btrfs tests skip without them) | symlink/traversal-safe file ops, archive extraction (zip-slip, links, bombs), spec/security policy, auth, fetch allow-list, storage parsing, real restic backup/ls/dump/restore-path, real btrfs snapshots (incl. deleting a server purges its snapshots), world replace and world reset on a real filesystem (which folders count as a world, undo on failure, leftovers) |
| Frontend | `cd frontend && npx tsc -b && npm run lint && npm run build && npx vitest run` | — | type safety, lint, build, component tests |
| Browser | Playwright scripts used during development (see DEVELOPMENT_STATUS.md) | dev server + API | every route at desktop and mobile widths, console errors, layout |

The backend `tests/conftest.py` sets `MCPANEL_TESTING=1` (locmem cache, in-memory channel layer, eager
Celery, fast hasher). Postgres is still required because JSON `contains` lookups are Postgres-only.
Set `MCPANEL_TEST_DB_NAME` (default `test_mcpanel`) to give concurrent runs on one Postgres separate
throw-away databases.

## Agent tests on a real btrfs filesystem

The agent deletes btrfs snapshots in two ways: with `btrfs subvolume delete` where the kernel allows it,
and file by file where it does not. The agent container of the default stack has no `CAP_SYS_ADMIN`, so
the second way is the one most installs use. A run as full root only covers the first, so run the suite
**twice** on a btrfs mount that is *not* mounted with `user_subvol_rm_allowed` (Unraid mounts pools that
way), as root:

```bash
cd agent
# 1. every capability: `btrfs subvolume delete` works (delete_mode native)
MCPANEL_TEST_BTRFS=/mnt/btrfs MCPANEL_TEST_BTRFS_EXPECT=native .venv/bin/python -m pytest -q

# 2. Docker's default capability set plus no-new-privileges, which is what the agent container of the
#    default stack runs with (no CAP_SYS_ADMIN): deletes go file by file (delete_mode fallback)
MCPANEL_TEST_BTRFS=/mnt/btrfs MCPANEL_TEST_BTRFS_EXPECT=fallback \
  setpriv --no-new-privs --inh-caps -all --bounding-set \
  -all,+chown,+dac_override,+fsetid,+fowner,+mknod,+net_raw,+setgid,+setuid,+setfcap,+setpcap,+net_bind_service,+sys_chroot,+kill,+audit_write \
  .venv/bin/python -m pytest -q
```

`setpriv --bounding-set -sys_admin --inh-caps -sys_admin` (everything but `CAP_SYS_ADMIN`) is a shorter
form of the second run. `MCPANEL_TEST_BTRFS_EXPECT` says which way a run is meant to exercise: a run with
the wrong privileges then fails instead of quietly testing the other way. Afterwards
`btrfs subvolume list /mnt/btrfs` must print nothing (no test may leave a subvolume behind).

Two more situations are worth a run of `tests/test_snapshots_btrfs.py tests/test_snapshots_btrfs_delete.py
tests/test_security_audit.py tests/test_storage.py` with the capability set of run 2: the data directory
as a bind mount of a subdirectory of the pool, which is how the agent container sees it
(`MCPANEL_TEST_BTRFS=<bind mount>`, expects `fallback`), and the pool remounted with
`mount -o remount,user_subvol_rm_allowed` (expects `native`: the delete works once the agent has cleared
a snapshot's read-only flag).

Without a spare btrfs disk and a loop device, a throw-away QEMU guest with a btrfs image file as its
disk does the job; that is how the runs of 2026-11-10 were made (kernel 7.2.14, btrfs-progs 6.14, all
five passes clean). Everywhere else these tests run against `tests/fake_btrfs.py`, a stand-in for the
`btrfs` command that refuses deletes the way the kernel does.

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
| `MCPANEL_IT_AGENT_URL` | — (skip) | agent, backup browser, content, import, world reset tests |
| `MCPANEL_IT_AGENT_TOKEN` | `dev-agent-token` | same |
| `MCPANEL_IT_AGENT_DATA_ROOT` | `/srv/mcpanel-dev` | on-disk checks (removal, ownership); empty skips them |
| `MCPANEL_IT_READY_TIMEOUT` | `360` | seconds to wait for a server to become healthy |
| `MCPANEL_IT_BIND_IP` | `127.0.0.1` | host address the test servers' game ports are published on; the default keeps them off the LAN |
| `MCPANEL_IT_BTRFS_AGENT_URL`, `_TOKEN`, `_FINGERPRINT` | — (skip) | snapshot tests: an `https://` agent on btrfs, pinned via `Node.tls_fingerprint` |
| `MCPANEL_IT_BTRFS_DATA_ROOT` | `/srv/btrfs-test/data` | on-disk subvolume checks; empty skips them |

| File | Starts Minecraft | What it verifies | Runtime (sandbox) |
|---|---|---|---|
| `test_integration_agent.py` | vanilla 1.20.4, ports 25600-25650 | install, readiness, RCON, stop/start persistence, online backup → modify → restore with safety backup, delete | ~70 s |
| `test_integration_content.py` | Paper 1.21.1, 1024 MB, ports 25800-25820 | content service installs Chunky from Modrinth (agent fetch with hash verification; a wrong hash is refused), inventory identifies it, restart loads it (RCON `plugins` + "Enabling" in the log), uninstall, stop, delete | ~57-74 s |
| `test_integration_backup_browse.py` | no | backup of a stopped server, browse 3 levels + binary download through the API, `restore-path` of one file (siblings untouched) and of a deleted folder, ownership, audit | ~17 s |
| `test_integration_import.py` | no | `POST /instances/<id>/import` of a zipped server folder: top folder stripped, files byte-identical, `../` entry and symlink skipped, archive removed, `installed_at` set, everything owned by the runtime uid/gid | ~3 s |
| `test_integration_snapshots.py` | no | pinned TLS (wrong fingerprint refused before the token is sent); install creates a subvolume; snapshot → modify → rollback (safety snapshot holds the pre-rollback state) → delete snapshots; deleting a server removes its remaining snapshots | ~10 s |
| `test_integration_world_reset.py` | test 1: vanilla 1.20.4, ports 25600-25650 (three starts); test 2: no | through `GET` / `POST /api/instances/<id>/world/reset`. Test 1: reset of a running server with a new seed, safety backup and "stop if running" (new seed reported by `seed` and stored in level.dat, placed block gone, old world with its seed in the “Before restore” snapshot, no `.mcpanel-world-old-*` folder, ops.json / eula.txt / other folders unchanged, server.properties changed only in `level-seed`), then a reset of the stopped server with the seed kept and no backup. Test 2: Paper-style folders written through the file API: three folders deleted, datapacks and per-world config carried over, the snapshot holds all three, a folder that is no world is refused by the panel and by the agent. Without restic on the agent the safety-backup parts are skipped | ~114 s for both (2 passed on 2026-11-10 against a real agent, real restic and a real vanilla server) |

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
Reset world (2026-11-09) is at levels **U** and **I**: its integration tests (`test_integration_world_reset.py`)
ran against a real agent, real restic and a real vanilla server on 2026-11-10 (2 passed, 114 s). What else is
not verified yet is listed in DEVELOPMENT_STATUS.md → "Reset world".

## Heavy tests

Large modpacks (Better MC etc.) download hundreds of MB and need 8+ GB RAM. They are **not** part of CI.
To reproduce on a real host, create the server from the wizard and follow `DEVELOPMENT_STATUS.md` →
"How Better MC was validated".
