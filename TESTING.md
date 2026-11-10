# Testing

What was run where, and with which result, is in DEVELOPMENT_STATUS.md. This page says what the
suites are and how to run them.

## Suites

| Suite | Command | Needs | What it covers |
|---|---|---|---|
| Backend unit + API | `cd backend && uv run pytest -q -m "not integration"` | Postgres; bash, git and coreutils for the script tests (they skip without them) | state machine, retention, properties, spec/env builder, catalog parsing, ports, permission matrix, auth/CSRF/setup, permission enforcement through the API (scenario 8), validation, job locks, content manager, diagnostics signatures, crash-loop tracking, notifications, metrics, mrpack preflight, world replace and world reset; the platform tests in the next table |
| Backend integration | `cd backend && MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration` (+ the btrfs variables below) | running agent(s) + Docker + internet | see "Integration tests" below: vanilla lifecycle + backup/restore, a stopped server without a container, backup browser + single-path restore, Paper + Modrinth plugin, zip import, btrfs snapshots over a TLS-pinned node, world reset |
| Agent | `cd agent && uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test` for the real btrfs tests: run them twice, see "Agent tests on a real btrfs filesystem" below) | nothing (the restic, btrfs and ZFS-restore tests skip without the binaries) | symlink/traversal-safe file ops, archive extraction (zip-slip, links, bombs), spec/security policy, auth, fetch allow-list, storage parsing, real restic backup/ls/dump/restore-path, btrfs snapshots (real, or against a stand-in), the graceful stop, the last-run record and retire, world replace and world reset on a real filesystem |
| Frontend | `cd frontend && npx tsc -b && npm run lint && npm run build && npx vitest run` | nothing | type safety, lint, build, component tests |
| Real containers | `.github/scripts/panel-acceptance.sh`, `.github/scripts/upgrade-rehearsal.sh` (see "Tests with real containers") | Docker with Compose and BuildKit | the single panel container, and an upgrade with rollback from the 7-container layout |
| Browser | Playwright scripts used during development (see DEVELOPMENT_STATUS.md) | dev server + API | every route at desktop and mobile widths, console errors, layout |

The backend `tests/conftest.py` sets `MCPANEL_TESTING=1` (locmem cache, in-memory channel layer, eager
Celery, fast hasher), so the tests need no Redis. Postgres is required because JSON `contains` lookups
are Postgres-only. When Postgres is not on `localhost:5432`, set `POSTGRES_HOST`, `POSTGRES_PORT` and
`POSTGRES_PASSWORD`. Set `MCPANEL_TEST_DB_NAME` (default `test_mcpanel`) to give concurrent runs on one
Postgres separate throw-away databases.

Two backend tests read the documentation and fail when it loses what they look for:
`test_docs_describe_the_three_container_layout` (`tests/test_panel_container.py`) and
`test_docs_keep_secrets_out_of_history_shares_and_stale_images` (`tests/test_security_audit_http.py`).

## Platform tests in the unit suites

None of these needs Docker or a Minecraft server. They run in the normal backend and agent suites.

| File | What it runs | Stand-ins |
|---|---|---|
| `backend/tests/test_supervise.py` | The supervisor of the panel container (`backend/container/supervise.py`) and the health check: start order, restart with backoff, restart of the others after Redis, staged stop inside the deadline, worker pings, the status file, removal of Redis dump files, the start-up guard | Fake children on a fake clock for most tests; the last ones start real processes (a shell, the real entrypoint with stub commands) |
| `backend/tests/test_panel_container.py` | The application side: queued jobs are sent again when the job worker is ready, the reconcile tick reports its age, `/api/health` names the processes, the tick log filter; and what the Compose file, the Dockerfiles, `.env.example`, `upgrade.sh` and the docs must contain | Reads the real files |
| `backend/tests/test_upgrade_script.py` | The real `deploy/scripts/upgrade.sh` with the real `git`, on a small site built in a temp folder: an old revision with seven services and a new one with three and three migrations. Run, rollback and run again; refusals; a run that ends before the switch, during the switch (also by SIGHUP, SIGINT, SIGTERM) and during a rollback; `--resume`; migrations that cannot be undone; `restore-db`; the notice about exited server containers | `tests/upgrade_shim/docker`, a shell script that stands in for `docker`. It keeps containers, image names and applied migrations in text files and can make single steps fail. `MCPANEL_UPGRADE_SCRIPT=/path/upgrade.sh` tests another copy of the script |
| `backend/tests/test_unraid_hook.py` | `deploy/unraid/stopping_docker` and `install-hook.sh`: every row of the table in docs/UNRAID-SHUTDOWN.md, the watchdog, `--project`, the shutdown time-out warning | Scripts that stand in for `docker` and `logger`; `MCPANEL_HOOK_ROOT` and `MCPANEL_DISK_CFG` point the installer at a temp folder |
| `backend/tests/test_host_shutdown.py` | `manage.py host_shutdown` and the resume step (`scheduling.resume_flagged_servers`) with all its rules | The fake agent, a fake clock |
| `backend/tests/test_retire.py` | The retire step of the reconciler and everything that has to work for a server without a container (status, start, restart, diagnostics, console, `manage.py retire_stopped_containers`) | The fake agent |
| `backend/tests/test_stop_log.py` | What a job logs about a stop, what a delete left behind, and which container format an apply produced (`restart_required` and `spec_outdated`) | The fake agent |
| `backend/tests/test_backup_safeguards.py` | `last_backup_at` / `backup_schedules` in the server payload, the wizard's nightly backup, `manage.py ensure_backup_schedules` | Database only |
| `backend/tests/test_world_reset_api.py`, `test_world_reset_contract.py` | The reset plan and its blockers, the job with its safety-backup checks and failure handling; the contract test compares names, fields and error codes with the agent and frontend sources | The fake agent; the contract test reads source files |
| `agent/tests/test_lastrun.py` | The last-run record (bounded, cleaned, file modes), `docker_ops.retire` under the per-server lock, and what status, logs, log stream, kill, stats and command answer without a container | A fake Docker |
| `agent/tests/test_graceful_stop.py` | Flush first, always `docker stop`, `stop` through RCON for a server that is still starting, all bounds | A fake container and a scripted or hanging RCON peer. The last test runs against a real Docker daemon with `MCPANEL_TEST_DOCKER=1` |
| `agent/tests/test_container_format.py`, `test_docker_ops.py` | The container format the agent reports; the low-level create call with Docker's stop timeout sends the same request as docker-py's own `create()` | A fake Docker |
| `agent/tests/test_snapshots_btrfs_delete.py` | Deleting btrfs subvolumes and snapshots with and without `CAP_SYS_ADMIN`, the probe, `delete_mode`, "unavailable" when neither way works | `tests/fake_btrfs.py`, a stand-in for the `btrfs` command that refuses deletes the way the kernel does; a real btrfs mount with `MCPANEL_TEST_BTRFS` |
| `agent/tests/test_world_reset.py` | Which folders count as a world, the reset with undo on failure, leftovers, the route | A real temporary filesystem, a faked Docker status |

## Tests with real containers

Both scripts are called by CI (`.github/workflows/ci.yml`, jobs "docker" and "upgrade") and can be
run by hand. Neither starts a Minecraft server. As of 2026-11-10 they were run by hand on a
desktop only; these two CI jobs had not run on GitHub.

**`.github/scripts/panel-acceptance.sh`**: the single panel container, against a stack that is up.

```bash
cd deploy
docker compose up -d --build --wait      # with MCPANEL_WORKER_CONCURRENCY=4 in .env and Redis inside the panel container
../.github/scripts/panel-acceptance.sh
```

`DC=<command>` replaces `docker compose` (a wrapper that adds `-p`, `-f` or `--env-file`). It
checks: all five children up and healthy; tini as PID 1, UID 10001, no capabilities; every log
line names its process; Redis on a Unix socket only, nothing on disk, none of the secrets in its
environment; the periodic worker has 2 slots; the scheduler keeps its schedule in memory;
`kill -9` of each child (it comes back alone; after Redis all of them); a planted Redis dump file
is removed before the start; a queued job survives a restart of the container; the health check
turns unhealthy with a reason and back; `docker stop` ends in order inside the grace period; the
start-up guard refuses next to a worker container of the old layout. It kills and restarts the
processes of the stack it is pointed at: do not run it against a panel you use.

**`.github/scripts/upgrade-rehearsal.sh SITE NEW_REV [URL]`**: `deploy/scripts/upgrade.sh` with
real containers. `SITE` is a git checkout of the old revision on a branch, with `deploy/.env` in
place and its stack not running yet; `NEW_REV` is the revision to upgrade to; `URL` is where the
panel answers (default `http://127.0.0.1:8080`). CI uses commit 22a2c79, the last one with the
7-container layout, as the old revision. The script builds the old layout, creates an owner
account and changes a setting, and then checks: `status` and `dry-run` change nothing; `run`
refuses while a job runs and refuses a schema change without `--with-migrations`; a run whose new
version is not healthy in time does not finish, a second run refuses and `rollback` returns to
the old images; a run whose `docker compose up` fails is finished by `run --resume`; the new
layout has the account and the setting and no old container; `rollback` brings back the old
layout, the old images and the old schema with the data; `run` again; `restore-db` with the dump
of that run, then `rollback`.

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
disk does the job. That is how the runs of 2026-11-10 were made: kernel 7.2.14, btrfs-progs 6.14, the
whole agent suite at three privilege levels (full root, root without `CAP_SYS_ADMIN`, Docker's default
capabilities) plus the two situations above, all five passes clean and no subvolume left behind. The
guest harness is not in the repository; it is in `audit-2026-11-09/work/snapshots-btrfs/` next to
the checkout on the development desktop (`vm.sh`, `scripts/`, logs in `vm/`). That kernel is not Unraid's (6.12). Everywhere else these tests run against
`tests/fake_btrfs.py`, a stand-in for the `btrfs` command that refuses deletes the way the kernel does.

## Integration tests

Marker `integration`; every file skips cleanly when its variables are not set. They call the panel's own
code paths (job handlers through the job runner, `apps.content.service`, API views with a logged-in
owner, management commands) against real agents through the real `AgentClient`. Everything they create
is named `it-…` and removed by fixture finalizers (`tests/integration_support.py`), also when an
assertion fails; they never touch other servers.

```bash
cd backend
FP=$(cd ../agent && AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TLS=auto uv run python -m mcagent fingerprint)
MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 \
MCPANEL_IT_BTRFS_AGENT_URL=https://127.0.0.1:8701 MCPANEL_IT_BTRFS_TOKEN=btrfs-node-token \
MCPANEL_IT_BTRFS_FINGERPRINT="$FP" uv run pytest -q -m integration -s
```

| Variable | Default | Used by |
|---|---|---|
| `MCPANEL_IT_AGENT_URL` | — (skip) | agent, retire, backup browser, content, import, world reset tests |
| `MCPANEL_IT_AGENT_TOKEN` | `dev-agent-token` | same |
| `MCPANEL_IT_AGENT_DATA_ROOT` | `/srv/mcpanel-dev` | on-disk checks (removal, ownership); empty skips them |
| `MCPANEL_IT_AGENT_CONTAINER` | — | retire test: the name of the agent's own container. The last-run record is readable by root only, so its file mode and content are checked through `docker exec` there. Unset: that check is skipped, and the test says so |
| `MCPANEL_IT_READY_TIMEOUT` | `360` | seconds to wait for a server to become healthy |
| `MCPANEL_IT_BIND_IP` | `127.0.0.1` | host address the test servers' game ports are published on; the default keeps them off the LAN |
| `MCPANEL_IT_BTRFS_AGENT_URL`, `_TOKEN`, `_FINGERPRINT` | — (skip) | snapshot tests: an `https://` agent on btrfs, pinned via `Node.tls_fingerprint` |
| `MCPANEL_IT_BTRFS_DATA_ROOT` | `/srv/btrfs-test/data` | on-disk subvolume checks; empty skips them |

| File | Starts Minecraft | What it verifies | Runtime |
|---|---|---|---|
| `test_integration_agent.py` | vanilla 1.20.4, ports 25600-25650 | install, readiness, RCON, stop/start persistence, online backup → modify → restore with safety backup, delete | 75-86 s (desktop, 2026-11-10) |
| `test_integration_retire.py` | vanilla, ports 25600-25650, restart policy "no" | one server through the whole life of "no container for a stopped server": install and first start; Stop, then the reconciler retires the container; the last-run record (exit code 0, the save lines at the end of the console), status, console history, console stream, kill / stats / command without a container; Start creates a new container with the world intact and the previous run in front of the new log; `manage.py host_shutdown` keeps the container and the resume step starts it again; `kill -9` of Java while the server is meant to run keeps the container and Stop dismisses it; with `MCPANEL_RETIRE_STOPPED_CONTAINERS` off, `manage.py retire_stopped_containers --dry-run` lists the exited container and the command removes it; Delete removes the record | 89-98 s (desktop, 2026-11-10) |
| `test_integration_content.py` | Paper 1.21.1, 1024 MB, ports 25800-25820 | content service installs Chunky from Modrinth (agent fetch with hash verification; a wrong hash is refused), inventory identifies it, restart loads it (RCON `plugins` + "Enabling" in the log), uninstall, stop, delete | ~57-74 s (sandbox, 2026-11-06) |
| `test_integration_backup_browse.py` | no | backup of a stopped server, browse 3 levels + binary download through the API, `restore-path` of one file (siblings untouched) and of a deleted folder, ownership, audit | ~17 s (sandbox, 2026-11-06) |
| `test_integration_import.py` | no | `POST /instances/<id>/import` of a zipped server folder: top folder stripped, files byte-identical, `../` entry and symlink skipped, archive removed, `installed_at` set, everything owned by the runtime uid/gid | ~3 s (sandbox, 2026-11-06) |
| `test_integration_snapshots.py` | no | pinned TLS (wrong fingerprint refused before the token is sent); install creates a subvolume; snapshot → modify → rollback (safety snapshot holds the pre-rollback state) → delete snapshots; deleting a server removes its remaining snapshots | ~10 s (sandbox, 2026-11-06) |
| `test_integration_world_reset.py` | test 1: vanilla 1.20.4, ports 25600-25650 (three starts); test 2: no | through `GET` / `POST /api/instances/<id>/world/reset`. Test 1: reset of a running server with a new seed, safety backup and "stop if running" (new seed reported by `seed` and stored in level.dat, placed block gone, old world with its seed in the “Before restore” snapshot, no `.mcpanel-world-old-*` folder, ops.json / eula.txt / other folders unchanged, server.properties changed only in `level-seed`), then a reset of the stopped server with the seed kept and no backup. Test 2: Paper-style folders written through the file API: three folders deleted, datapacks and per-world config carried over, the snapshot holds all three, a folder that is no world is refused by the panel and by the agent. Without restic on the agent the safety-backup parts are skipped | 103-114 s for both (desktop, 2026-11-10) |

The content, backup-browse, import and snapshot files were last run on 2026-11-06 in the
development sandbox, before this release.

## Acceptance scenarios: verification log

Levels: **U** unit/API test · **I** automated integration test · **R** manually verified on a real
Minecraft server through the real API · **N** not yet verified. None of these was verified by a
test run on Unraid. (An earlier version of the panel has been in use on the owner's Unraid server
since 2026-11-07; DEVELOPMENT_STATUS.md says what that covers.)

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
| 9 | Restart app services, instances intact | R, I | agent/API/worker restarted many times during development; containers kept running and were re-discovered by the reconciler. Upgrade and rollback of the whole stack with a server running: the server was not restarted (2026-11-10) |
| 10 | Failure handling | U, R | crash loop → failed + auto-stop + diagnosis; downgrade/invalid version rejected; port conflict 409; corrupt archives rejected |

Extra verified flows (R): FTB StoneBlock 3 (FTBA, pinned version), multi-node, software update 1.20.4 → 1.21.1 with
Java 17 → 21 and a pre-update backup, webhook notifications, Docker Compose deployment smoke test.
Also automated (I) since 2026-11-06: btrfs snapshot/rollback over a TLS-pinned node, import an existing server
from a zip, Modrinth plugin install on Paper.
Since 2026-11-10 also at level **I** and **R**: Reset world, a stopped server without a container, the
graceful stop, and `host_shutdown` with resume. What was run and what is still unverified is in
DEVELOPMENT_STATUS.md.

## Heavy tests

Large modpacks (Better MC etc.) download hundreds of MB and need 8+ GB RAM. They are **not** part of CI.
To reproduce on a real host, create the server from the wizard as docs/QUICKSTART-UNRAID.md section 7
describes. What was measured is in `DEVELOPMENT_STATUS.md` ("Verified on real Minecraft servers" and
"Unraid simulation").
