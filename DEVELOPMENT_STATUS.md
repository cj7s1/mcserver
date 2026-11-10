# Development status

_Last updated: 2026-11-10 (fixes for the open findings of the audit of 2026-11-09 on branch
`fix/audit-remaining`, then the fixes from the review of those fixes and from its real-stack test,
all unit-tested only; see "Audit of 2026-11-09" and "Review of the audit fixes" below. Before that, the release on branch `feature/reset-world`: three containers, upgrade script,
stopped servers without a container, stop safety, Unraid shutdown hook, btrfs snapshots without
`CAP_SYS_ADMIN`, backup safeguards, Reset world). Before that: 2026-11-06 (session 2), 2026-11-03 (end
of session 1)._

Keep this file honest. Implemented, unit-tested, integration-tested, validated on a real Minecraft
server, and validated on Unraid are **different** levels.

## Where things stand (2026-11-10)

* **On Unraid:** the panel has been running on the owner's Unraid server (Unraid 7.3.4, Docker 29.8.2,
  btrfs cache pool) since 2026-11-07, at commit `22a2c79`: seven platform containers, started from the
  command line. In use there: install, one modpack server created in the panel (running and healthy),
  seven servers imported from older setups, start and stop. Not used there so far: backups (the backup
  folder was empty on 2026-11-09, so restic on the array share is untested). Whether snapshots were
  used there is not known.
* **This release has not run on Unraid.** Everything under "Release of 2026-11-10" below was verified on
  a Linux desktop, with real Docker, real `itzg/minecraft-server` vanilla servers and real restic where
  the section says so.
* **The real-stack runs came before the last round of fixes.** They were made on 2026-11-10 between
  about 00:45 and 04:10. After that, `deploy/scripts/upgrade.sh`, the supervisor and several agent and
  backend files (`docker_ops.py`, `lastrun.py`, `handlers.py`, `reconcile.py`, `scheduling.py`) were
  changed once more. On the final tree the unit suites below pass, which includes `upgrade.sh` against
  the `docker` stand-in. A final gate run with real containers was still in progress when this file
  was written; its results are not in here.

* **The audit fixes of 2026-11-09 are unit-tested only.** Four packages (agent, backend lifecycle,
  backend web, frontend) on branch `fix/audit-remaining`: nothing of them has run on real Docker, with
  a real Minecraft server, in a browser, or on the Unraid host. Section "Audit of 2026-11-09" lists
  the findings and what is left of them.

## Audit of 2026-11-09: open findings fixed (branch `fix/audit-remaining`)

The audit round of 2026-11-09 left open findings in four work lists (`audit-2026-11-09/briefs/open-
{agent,backend-lifecycle,backend-web,frontend}.md`, next to the checkout, outside the repository;
each heading is one finding with its id). They were fixed in parallel by package. How the result
works is in ARCHITECTURE.md ("Schedules", "Request limits and file transfers", "Container
lifecycle", decisions 3 and 7); how to use it is in DEPLOYMENT.md and docs/UNRAID.md (section 10,
"When the cache drive died").

**Verification level of every row below: unit/API tests only** (the new test files are listed in
TESTING.md, "Tests of the audit fixes of 2026-11-09"). Suites after the four packages were joined
(2026-11-10, with `tests/test_contracts_2026_11_10.py` for the joins between the layers): backend
1565 passed, agent 991 passed and 45 skipped, frontend 327 tests. Not run: the
integration tests, `panel-acceptance.sh`, `upgrade-rehearsal.sh`, a browser pass, anything on
Unraid. In particular never run for real: `backup-db.sh` against a real postgres container, the
cache-drive recovery of docs/UNRAID.md section 10, `adopt_backups` against a real restic
repository, `pull="newer"` against Docker Hub (rate limits on frequent starts are unknown), the
status ping against real servers (vanilla, Paper, modded), `restart: always` after a real
reboot, the memory check against a real host's RAM, and daylight saving time on a real night.

| Package | Closed | Closed with a remaining point (list below) | Partly done |
|---|---|---|---|
| agent | agent-docker#6, agent-files#1, agent-files#4, agent-files#6, agent-files#7, agent-files#8, agent-backups#7, agent-backups#8, live-ops#7 | agent-docker#3, agent-docker#5, agent-files#3, agent-files#5, agent-backups#4, agent-backups#6, backend-content#3 | agent-docker#4 |
| backend-lifecycle | backend-jobs#1, backend-backups#3, backend-jobs#2, scenario-boot-shutdown#5, backend-jobs#5, backend-lifecycle#4, backend-backups#4, backend-modpacks#1, scenario-boot-shutdown#7, scenario-data#4, backend-jobs#6, backend-backups#8, backend-jobs#3, backend-lifecycle#8, backend-jobs#7, live-ops#8, frontend-create-admin#1, scenario-boot-shutdown#6 | backend-backups#5, deploy#5, live-ops#5 | backend-jobs#4, backend-lifecycle#2, scenario-boot-shutdown#2, backend-backups#6 |
| backend-web | backend-api#1, backend-api#2, backend-api#4, backend-api#6, backend-api#7, backend-realtime#3, backend-realtime#4, backend-content#5, backend-content#6, backend-content#7, backend-modpacks#3, backend-auth#4, frontend-instance-b#3 | backend-api#3, backend-realtime#6, backend-content#1, backend-content#4, backend-modpacks#4, backend-modpacks#5 | — |
| frontend | frontend-core#1, frontend-core#3, frontend-core#4, frontend-core#5, frontend-core#6, frontend-instance-a#6, frontend-instance-b#1, frontend-instance-b#2, frontend-create-admin#3, frontend-create-admin#5, backend-auth#5 | frontend-create-admin#2, frontend-create-admin#6, frontend-create-admin#7, frontend-create-admin#8 | frontend-instance-b#5 |

What is left (checked against the code when this was written):

* **Schedules and jobs.** backend-jobs#4: jobs still queued when the panel went down are sent
  again at the next start if they are younger than 2 hours (only schedules have the misfire
  limit); older ones are failed by the watchdog as `not_started`. Missed backups of all servers
  are queued in the same tick once each server has settled. The "Hourly snapshot" template still
  runs at minute 0 (the nightly backup at 04:xx no longer collides: the later one waits).
* **Memory.** backend-lifecycle#2: creating or resizing a server only checks one container against
  the node's RAM, not the sum; `AGENT_MAX_MEMORY_MB` is still 65536. frontend-create-admin#2: the
  commit bar in the wizard and the Resources tab compares with the whole RAM, the start check with
  RAM minus `memory_reserve_mb`, so a start can be refused while the bar shows no warning.
  frontend-create-admin#6: a user without `system.settings` sees 4096 MB in the wizard while the
  panel's default memory applies. (A create with "start now" now gets the Start anyway dialog: see
  "Review of the audit fixes", finding 13.)
* **Boot.** scenario-boot-shutdown#2: without the shutdown hook Docker still starts every server
  that was running at the same moment (the servers' restart policy is still `unless-stopped`);
  the stagger and the memory check only apply to servers the hook marked. live-ops#5: the agent of
  an additional node (`docker-compose.node.yml`) and the optional `redis` container still use
  `unless-stopped`; there is no "started outside the panel" state.
* **Backups and recovery.** backend-backups#5 / deploy#5: `adopt_backups` only reads repositories
  of servers the database knows, so it needs the database dump first; a server created after the
  last dump can only be restored with restic by hand. The dump of `backup-db.sh` is not encrypted
  and the `.env` copy is clear text (0600 in a 0700 folder). The agent still overwrites
  `<data root>/agent/restic-password` when the password changes. backend-backups#6: the agent
  still accepts restic exit code 3 for `forget`. agent-backups#4: the start-up unlock uses
  `restic unlock --remove-all` (safe only while no other host uses the repository); `ls`,
  `snapshots`, `stats`, `dump` and `restore` rely on unlock-and-retry instead of `--retry-lock`.
  agent-backups#6: a failed removal of the old tree after a single-path restore is only logged.
  agent-docker#3: the panel shows no warning when the agent lost an operation while world saving
  was paused (the agent's marker switches it on again by itself).
* **Agent.** agent-docker#5: a CPU limit above the node's CPUs is refused when the setting is
  saved (when the node can be asked) and by the agent's apply (`cpu_limit`; a job treats it, like
  `create_failed`, as "nothing changed" and starts the server again); the UI still allows up to
  256; if the old container was removed but the rename of the new one fails (`rename_failed`), no
  container is left and the server is left stopped (the next start creates one). agent-docker#4: no "check for image updates" action, no indicator that a newer
  build is waiting, no weekly check; images are only refreshed at starts. agent-files#3: no list
  of files left out of a zip; two names that differ only in invalid bytes can give duplicate
  entries. agent-files#5: an import refused before the agent's operation started (the server is
  running, another job holds it) now deletes its uploaded `.mcpanel-import-*` file; one left by a
  job that was cut off is still not removed by the agent's start-up sweep. backend-content#3: a blocking hash request still holds an agent thread for up to 240 s.
* **Web.** backend-api#3: a raw single-file upload (DRF `FileUploadParser`) still keeps two copies
  on disk; `MCPANEL_UPLOAD_TMP` is not passed by the compose file (default in the container's
  writable layer) and nothing empties it at start (since the review it is created at start by the
  web process, finding 23); the comment in `settings.py` names
  `apps/core/middleware.UPLOAD_PATH`, which is in `apps/core/asgi.py`. backend-realtime#6: the
  status ping's sample holds about 12 names, so busy servers' player history misses players (the
  names now go through the same name check as the RCON `list` parser). backend-content#1: disabled pack jars are kept out only for the pinned release (a
  modpack update installs the new release as the pack ships it); `CF_EXCLUDE_MODS` /
  `MODRINTH_EXCLUDE_FILES` are not used. backend-content#4: a bulk update is still one web request
  (no job, no lock, no pre-update backup, no rollback); it re-checks the lock between batches of
  50. backend-modpacks#5: `CF_API_KEY` still goes to every CurseForge modpack container.
* **Frontend.** frontend-instance-b#5: the console has a Reconnect button while live, but no
  automatic stall detection. frontend-create-admin#8: import does not read the world's version
  (nothing is preselected instead). frontend-create-admin#7: the Java override is cleared on every
  change of software or version, but a too-low Java is not refused.
* **Joined 2026-11-10 (contracts between the layers).** Fixed while joining the packages, all
  unit-tested only: the panel turns the agent's 415 `not_utf8` into the editor's read-only answer
  (`binary_or_non_utf8`, lossy preview) and writes a `*.properties` file the agent keeps in
  ISO-8859-1 back in ISO-8859-1 with only the characters it cannot store as `\uXXXX`; the mods
  inventory shows jars over 2 GiB as unidentified (`too_large`) and an agent busy hashing as
  pending, and asks for `.jar` / `.jar.disabled` only; the Overview and Dashboard count players by
  status ping (no more RCON `list` every 20 s while a page is open); the reconciler retries the
  keyless CurseForge pinning (once a minute per server); a typed `stop` is shown once in the
  console (the notice carries the command's `id`); clone sends `level_name` only to agents with
  `clone_level_name` and warns otherwise; `memory_reserve_mb` and a valid `default_bind_ip` are
  checked when System settings are saved (and nothing is saved when one value is refused);
  daylight saving: a doubled fall-back time runs once only for schedules whose hour field names
  hours; `*` or step hour fields run on real elapsed time through the doubled hour. The test fake
  agent (`tests/conftest.py`) now answers like the current agent (all four features, strict text
  decoding); tests of older agents say so.

### Review of the audit fixes (2026-11-10)

A code review of the tree above (frozen copy `audit-2026-11-09/r3-src`, reproductions in
`audit-2026-11-09/work/review-r3/`) found 22 points, and a real-stack test of the same frozen copy
(logs in `audit-2026-11-09/r3-e2e/logs/`) six more (23-28). All were taken; none was rejected.
**Verification level: unit/API tests only**, each new test checked by mutation (the fix taken out
on a scratch copy, the test fails; runners in `/tmp/r3be/tools` and `/tmp/r3mut/tools` on the
desktop, not kept). Nothing of this round has run on real Docker, in a browser or on Unraid; the
real-stack findings were fixed from their logs, not re-run. New test files: TESTING.md, "Tests of
the audit fixes". Suites afterwards: backend 1617 passed (2 skipped: no shellcheck), agent 1049
passed and 45 skipped, frontend 330 tests; ruff, `tsc -b`, lint and the build clean.

* **1 (blocker) pack files.** Deleting or renaming a folder (`mods/`, `config/`) in the file manager
  recorded every manifest path below it as removed, so itzg never reinstalled them. Now only a
  single `.jar` directly in `mods/` or `plugins/` is recorded (`packfiles.recordable`): disabled,
  deleted or replaced on the Mods tab, or deleted / renamed alone in the file manager; never
  folders, config files, several files at once, or an operation that timed out (outcome unknown).
  The Modpack tab lists the record ("Removed pack files", `GET /modpack/removed-files`) with "Let
  the pack reinstall them" (`DELETE`, audited `modpack.removed_files_cleared`; the configuration
  revision changes, so the next restart recreates the container).
* **2** The upload gate parses cookies with Django's `parse_cookie`: a JSON cookie of another app
  in front no longer hides the session (signed-in users got 413 above 25 MB).
* **3** Editing a schedule resets `deferred_at` as well as `next_run_at`.
* **4** A run whose wait reached 60 minutes is given up before it runs (also after downtime), and a
  waiting power action is "Missed" when the panel was restarted while it waited and the wait is
  past the 10-minute grace. Deviation from the review's wording: the grace is not applied to every
  wait of a power action, only to one the panel was down for (`runtime_started_at`); otherwise a
  restart that waits 20 minutes for a backup would no longer run at all.
* **5, 6a, 6b, 10, 12 (agent tests).** The fake restic records start and end times (no repository
  command before its start-up unlock ended); the fake Docker removes an image with one tag like
  Docker (the tagged-image guard is what keeps it); the mountpoint check before `zfs destroy`; the
  sweep's age and busy checks for siblings, staging/ and trash/ (and `O_NOFOLLOW`); the clean-up
  after a failed create, remove or rename.
* **6c, 7 (tests).** The modpack rollback counts its restore as a change; restore, software and
  modpack updates start a running server again without the memory check; a recreating restart
  checks memory before it stops; a job marked as failed after its stop leaves the server stopped
  and drops the resume mark.
* **8** The start-up sweep keeps and reports both halves of an interrupted swap (convert +
  preconvert, restore-<t> + old-<t>, staging restore + trash pre-restore, rollback + old), whatever
  the server folder holds. Before, both copies were deleted once the folder had been recreated.
* **9** The sweep renames each leftover to a unique `*discard-<hex>` name (inode checked) before it
  deletes it, so a clone starting into the fixed staging name is never reached.
* **11** Deleting a server and removing its container (archive, migration) also remove a stale
  `<name>-replacement` container, under the instance lock.
* **13** Memory: a resume after boot that gives up for memory sends a `job.failed` notification
  (scheduled starts already did, after their 60-minute wait; now tested). Creating a server that
  starts, a software or modpack update with "start afterwards" of a stopped server, recreating a
  running server and moving a running server are checked when requested (409
  `insufficient_memory`) and take `allow_overcommit`; the frontend asks "Start anyway?" for all of
  them (`lib/power.ts`: `postAllowingOvercommit`, `postOrDecline`). A migration also checks the
  target node in the job, before anything is stopped or copied.
* **14** A recreate (restart with changes, Recreate) and a world reset remember that they stopped
  the server (`stopped_server`).
* **15** A worker stop is reported as an interruption, not as "marked as failed" (start_again, world
  reset). Every step that starts a server again goes through `handlers.starting_again`: once the
  node works on that start, an interrupted job no longer records the server as stopped.
* **16** `create_failed` counts as "nothing changed" only with agents that have `pull_newer` (older
  agents remove the old container first).
* **17** A restore cut off by a panel stop after its request went out says in the log and in the
  job's error that the files may be the backup's while the software is the newer one, and to
  restore the backup again or set the versions first.
* **18** More than 25 MB needs an active user and a session valid for the current password
  (session auth hash): the session as before, then one database read for the user.
* **19** A CurseForge key with a character a header cannot carry is refused as
  `invalid_curseforge_key` without asking CurseForge (also one already in the settings: 503 with
  that code instead of a 500).
* **20** The file editor writes `server.properties` as the Properties tab does: ASCII with
  `\uXXXX` (also through an older agent, which now reads an ISO-8859-1 `*.properties` exactly
  instead of offering a lossy copy), ISO-8859-1 when the agent keeps it so.
* **21** Test that a download the browser leaves closes the agent's response itself.
* **22 (docs)** docs/UNRAID.md section 2 and DEPLOYMENT.md ("The data folder is root-only after the
  upgrade"): the 0750 data root ends SMB, file-manager, other-container and non-root access.
* **23 (high, real stack)** Every request body over 5 MB was a 500 on the built image: importing the
  settings during the build (collectstatic, root) created `/tmp/mcpanel-uploads` as root 0700. The
  settings create nothing now; the web process (`mcpanel/asgi.py`) and the entrypoint create the
  folder as UID 10001; `/api/health` reports `"uploads": "error"` while it is not writable; the
  Dockerfile fails the build if the folder exists after collectstatic; `panel-acceptance.sh` sends
  a 10 MB body and checks the folder's owner (the script was not run).
* **24** The agent, retire and backup-browse integration tests check the data root through `docker
  exec` with `MCPANEL_IT_AGENT_CONTAINER`, or skip with a message (not run).
* **25** Streamed downloads (file, zip, backup file) close their source when the answer ends, also
  when the client goes away: the file closes at once, the zip producer stops, restic gets SIGINT.
* **26** "Leave out worlds" in a clone also leaves out every top-level folder that directly holds a
  `level.dat` and the `.mcpanel-world-*` folders (listed in `skipped_worlds`); the hint says so.
* **27** `pull="newer"` asks the registry at most once per tag per 24 h (`image-checks.json` in the
  agent's state folder; a failed check is not recorded). The superseded image stayed because
  Docker's containerd image store (new Docker 29 installs) shows it without RepoTags or
  RepoDigests: its repository now comes from the reference the agent knew it by. That cause was
  found on a similar image on the test host, not reproduced with itzg; Unraid's default store
  keeps RepoDigests.
* **28** `backup-db.sh` runs one at a time per destination (lock folder with the pid; a dead
  holder's lock is taken over) and names a second dump of the same second `_2`, `_3`.

Still open from this round: superseded runtime images are only removed during an apply, so an
image whose last container went away through delete, archive or retire can stay until a later
apply lists it.

## Last verified working state

Suites of this tree, run on the desktop on 2026-11-10. The restic, btrfs and zfs binaries are not
installed there, so the agent tests that need them are skipped in this run (the btrfs ones were run
separately in a VM, see the release section).

| Suite | Result |
|---|---|
| `backend: pytest -q -m "not integration"` | **1565 passed**, 2 skipped (shellcheck), 10 integration tests deselected (2026-11-10, after the audit packages were joined) |
| `agent: pytest -q` | **991 passed, 45 skipped** (2026-11-10, after the join) |
| `frontend: npx vitest run` | **327 tests** passed (2026-11-10, after the join); `tsc -b`, lint and build clean |
| `backend: … -m integration` | Run one file at a time against a real agent on the desktop, before the last round of fixes: `test_integration_agent.py` 1 passed (75-86 s), `test_integration_retire.py` 1 passed (89-98 s), `test_integration_world_reset.py` 2 passed (103-114 s). The content, backup-browse, import and btrfs-snapshot files were **not** re-run for this release (last run 2026-11-06 in the sandbox) |
| `.github/scripts/panel-acceptance.sh` (real containers) | 29 checks passed, by hand on the desktop, before the last changes to the supervisor and to the script |
| `.github/scripts/upgrade-rehearsal.sh` (real containers, 7 → 3 → 7 → 3) | passed, by hand on the desktop, before the last fixes to `upgrade.sh` and to the script |
| GitHub Actions | The workflow with the new jobs "docker" and "upgrade" has not run on GitHub |

Older results, from the development sandbox on 2026-11-06 (real restic and a real btrfs loopback
there; the agent ran on :8700 (ext4) and :8701 (btrfs)):

| Suite | Result |
|---|---|
| `backend: uv run pytest -q -m "not integration"` | 862 passed (after the security-audit integration follow-ups; incl. OpenAPI fail-on-warn) |
| `backend: … -m integration` (real agents + Docker, see TESTING.md → "Integration tests") | 7 passed in 157 s (before the audit/ZFS/world merges): vanilla lifecycle + backup/restore 70 s, Paper + Modrinth plugin 57 s, backup browser + restore-path 17 s, btrfs snapshots over pinned TLS 9 s (3 tests), zip import 2 s |
| `agent: uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test`) | 533 passed (real btrfs + restic, real HTTPS listener, fake-`zfs` ZFS suite, world swap/rollback on a real filesystem) |
| Compose stack smoke test (`deploy/docker-compose.yml`) | 6 services healthy, a real vanilla server ran, RCON + restic backup through the containerized stack (the layout of that time; the stack has three services now) |

## Release of 2026-11-10 (branch `feature/reset-world`)

What it contains. How each part works is in ARCHITECTURE.md; how to use it is in DEPLOYMENT.md and
docs/UNRAID.md.

1. **Three containers instead of seven** (`panel`, `agent`, `postgres`). The panel container runs a
   supervisor (`backend/container/supervise.py`) with five children: Redis (Unix socket only, nothing on
   disk), web, worker, periodic worker, scheduler. Entry point mode `all`; the one-role modes still
   exist. Redis as its own container: Compose profile `external-redis`. `/api/health` reports
   `components` and `tick_age_seconds`.
2. **`deploy/scripts/upgrade.sh`**: `status`, `dry-run`, `run`, `rollback`, `restore-db`.
3. **A stopped server has no container.** The panel removes the exited container 10 to 20 s after a
   stop; the agent keeps a last-run record (exit state and the last 2,000 console lines). Setting
   `MCPANEL_RETIRE_STOPPED_CONTAINERS` (default on); `manage.py retire_stopped_containers [--dry-run]`.
4. **Stop safety.** `save-all flush` before every stop, `docker stop` always, `stop` through RCON for a
   server that is still starting, Docker stop timeout min(server stop timeout,
   `AGENT_SHUTDOWN_STOP_TIMEOUT` = 20), `CREATE_CONSOLE_IN_PIPE=true`. Containers made before read
   "restart required" and are recreated at their next start or restart.
5. **Host shutdown hook** for Unraid (`deploy/unraid/stopping_docker`, `install-hook.sh`),
   `manage.py host_shutdown`, and the resume step that starts the servers again, one per minute.
6. **btrfs snapshots without `CAP_SYS_ADMIN`** (file-by-file delete where the kernel refuses
   `btrfs subvolume delete`; a probe decides; the capability reports `delete_mode`).
7. **Backup safeguards.** "No backup yet" / "Last backup N days ago", "Nightly backup" on by default in
   the create wizard, `manage.py ensure_backup_schedules [--dry-run]`.
8. **Reset world** (Settings → Danger zone). Details in "Reset world" below.

Also in this release: the job worker sends queued jobs again when it starts, and the watchdog settles
backup rows whose job was cut off.

Corrected in this file and in ROADMAP.md on 2026-11-10, not part of the release: the System page has
had a Nodes section and a Notifications section since before it. Both files said they were missing.

### Verified on a real stack on the desktop (2026-11-10)

Real Docker, real `itzg/minecraft-server` vanilla servers, real restic. Logs and scripts are outside
the repository, on the development desktop in the folder `audit-2026-11-09/` next to the checkout
(`e2e1/`, `e2e2/`, `bc/`, `d-run/`, `work/snapshots-btrfs/`).

| What | Result |
|---|---|
| Reset world through the browser (three resets in two test rounds, desktop and phone width) | A running vanilla 1.21.1 server with a custom seed: stopped, safety backup taken and checked, world deleted, started again with a new world; the job succeeded each time. Refusals checked through the API: wrong or missing server name, folders that are not the world, a `level-name` that points at a server folder |
| The three integration tests against a real agent in a container | `test_integration_world_reset.py` 2 passed, `test_integration_agent.py` 1 passed, `test_integration_retire.py` 1 passed, each in three separate runs |
| Stopped server without a container | After Stop the container was gone 15 to 20 s later; the Console page showed the end of the run with the end-of-run marker; Start created a new container and the world was intact. With the restart policy "no", a server killed with `kill -9` while it was meant to run kept its container and showed "failed", and Stop removed it. With the default policy Docker restarted the killed server and the panel showed it running again |
| Stop safety | Docker's stop timeout read back as 20 on a new container (unset on a container of the old version). "The world was saved (save-all flush)" in the job log. Stop requested 0 s, 1.5 s and 4 s after the start job finished: the server exited with code 0 each time, after 5 s, 3 s and 1 s. A plain `docker stop` was limited to 20 s |
| `manage.py host_shutdown` + resume | Two running servers stopped in 1 to 7 s and marked; after the platform containers were stopped and started again (a simulated reboot, not a real one) the panel started them one minute apart; a stopped server stayed stopped |
| The hook script, run by hand with a stand-in for `logger` | Normal case (panel stops the servers) and "no panel container running" (fallback `docker stop`). The case "panel command fails" was run with an earlier version of the hook, which still used the fallback there; the current behaviour (leave them to Docker) is covered by the unit tests only. `install-hook.sh install --project` and `status` were run against a temp folder (`MCPANEL_HOOK_ROOT`), not as root on Unraid |
| Upgrade from `22a2c79` with a running, a stopped and a never-started server | 7 → 3 containers; the running server was not restarted and stayed reachable throughout (game port pinged once a second); `/api/health` was away for about 7 s; rollback to 7 containers; upgrade again. **Made before the last round of fixes to `upgrade.sh`** |
| The panel container (`.github/scripts/panel-acceptance.sh`) | `kill -9` of each child, Redis on a socket only, a queued job sent again after a restart, health check, ordered stop, start-up guard: 29 checks passed |
| Stopping the panel container with 10 s (what Unraid's Docker tab does) | Idle: stopped cleanly in 5.2 s. With a backup job running: killed after 10 s; the job was marked "interrupted" 6 min 9 s later and its server was locked until then |
| Backup safeguards | Wizard with "Nightly backup": schedule created; notice shown before the first backup and gone after it; `ensure_backup_schedules --dry-run`, then for real, then again (nothing to add) |
| btrfs without `CAP_SYS_ADMIN` | The whole agent suite on a real btrfs filesystem in a QEMU guest (kernel 7.2.14, btrfs-progs 6.14) at three privilege levels, plus a bind-mounted data directory and a pool with `user_subvol_rm_allowed`: 816 passed, 4 skipped per full pass, no subvolume left behind. One snapshot of 100,000 small files plus 1 GiB: deleted in 2.0 s file by file. Not Unraid's kernel (6.12) |

Two things seen in these runs were changed in the code afterwards and have not been run on a real
stack since: the diagnostics request answered 502 while Stop was removing the container of a failed
server, and the end-of-run marker in the console gave its time in UTC instead of the server's time
zone.

### Not verified anywhere

* **Anything on the Unraid server itself:** Unraid calling the `stopping_docker` event, the 30 s after
  which Unraid kills the Docker daemon, `install-hook.sh` as root, the upgrade from the running
  7-container install, btrfs snapshots without `CAP_SYS_ADMIN` on the cache pool, restic on the array
  share.
* A real host reboot or a real stop of the Docker daemon (only the platform containers were stopped and
  started).
* Large or modded servers against the 20 s (Docker) and 45 s (hook) limits. All runs used small vanilla
  servers, which stop in 1 to 7 s.
* The upgrade script on real containers after its last fixes (see "Where things stand").
* ZFS snapshots on a real pool.
* More than one node with different versions of panel and agent.
* A Compose plugin managing the stack, and its own stop of stacks when Docker stops.
* Reset world on Paper and on modpack servers with real servers (unit tests and one integration test
  with Paper-style folders written by hand).

### Things to know when this release goes onto the Unraid server

* The checkout on the server followed the branch `claude/busy-planck-y5nw1u` on 2026-11-09; this
  release is on `feature/reset-world`. `git pull` there brings the release only once it is on the
  branch the server follows.
* The command is `./scripts/upgrade.sh run --with-migrations` (one new migration); the dry run prints it.
* Images there were built by hand, so the script has to work out the deployed revision: it is `22a2c79`.
  It can do that after one `git pull`. If the dry run does not name `22a2c79` as "from" (for example
  because the checkout was switched with `git checkout` instead), pass `--from 22a2c79`.
* On 2026-11-09 seven of the eight servers there were stopped. Their exited containers are removed
  within seconds of the first start of the new panel (the dry run says how many). The last 2,000 console
  lines of each are kept.
* The one running server shows "restart required" afterwards and has Docker's old 10 s until it is
  restarted.
* Then install the shutdown hook and do the check in docs/UNRAID-SHUTDOWN.md, with a small server first.
* No backup had been taken there by 2026-11-09. `manage.py ensure_backup_schedules --dry-run` lists
  the servers without a backup schedule; take one manual backup of a small server first to see that
  restic works on the share.
* Do not remove the "orphan image" rows named `mcpanel-rollback/...` in the Docker tab while a
  rollback should stay possible (docs/UNRAID.md, section 4).

## Earlier work (up to 2026-11-06, development sandbox)

Everything from here to "Next recommended tasks" was written before the release of 2026-11-10 and
describes the state of that time, except where a line says otherwise. The stack had six, later
seven, containers then.

### Integration coverage added 2026-11-06 (and a bug it found)

New `-m integration` files: `test_integration_snapshots.py` (btrfs over a TLS-pinned node),
`test_integration_backup_browse.py`, `test_integration_content.py` (Paper 1.21.1 + Chunky from
Modrinth), `test_integration_import.py`; shared helpers in `tests/integration_support.py`.

**Fixed: deleting a server on btrfs orphaned its snapshots.** The agent's `DELETE /v1/instances/<id>/data`
removed the container, the folder and the restic repo but never `<data root>/snapshots/<id>/`: read-only
subvolumes stayed on disk, invisible in the panel (also hit by migrate with "delete source"). It now
purges them (`BtrfsSnapshots.destroy_all()`, via the snapshot provider) *before* deleting the files, so a
btrfs failure leaves the server intact for a retry. Agent tests: `test_purge_deletes_read_only_snapshots`,
`test_delete_data_removes_snapshots` (real btrfs). The sandbox agents on :8700/:8701 run from the main
checkout and must be restarted to pick the fix up; until then
`test_deleting_a_server_removes_its_snapshots` fails against :8701 (it passed against the fixed agent
code run on :8711 with the same data root).

### Verified on real Minecraft servers through the real API (sandbox)

| Flow | Notes |
|---|---|
| Vanilla 1.20.4: install → `installing` → health-checked `running` → RCON → properties edit → restart → stop | block placed before restart still present |
| Import existing server from a zip (`POST /instances/<id>/import`) | world + settings came across |
| Backup (consistent, server running) → modify → restore (with safety backup) | `restic`, via worker jobs |
| Backup browser: list snapshot, download a file, restore a single file | |
| Software update 1.20.4 → 1.21.1 (Java 17 → 21 image switch, pre-update backup) | downgrade refused without confirmation |
| Fabric 1.21.1 with pinned loader 0.19.4 (restart-required → recreate) | log confirms the loader version |
| **Better MC [FORGE] BMC4 v62hf via CurseForge (keyless)** | ~3.5 min install, 311 mods, 20 TPS, auto-pinned to file 9104992 |
| **Better MC [FABRIC] BMC2 v40 via CurseForge (keyless)** | ~2 min install, 294 mods, auto-pinned to file 7517035 |
| Better MC BMC4 v59 **Modrinth** edition | needs 51 CurseForge-only files: the crash loop is detected and auto-stopped, diagnostics name the cause, manual-downloads list with links; the wizard preflight now warns *before* deploying |
| **FTB StoneBlock 3 v1.11.6 (Forge 1.18.2)** | ~2.5 min install, 220 mods, pinned `FTB_MODPACK_VERSION_ID=100359`, Java 17 |
| btrfs snapshots: create (save-off coordinated) → modify → rollback (with safety snapshot) | through the second node |
| Multi-node: second agent registered as a node, server created/deleted there | |
| Webhook notifications (test + real backup event) | local receiver |
| Modrinth content manager: LuckPerms on Paper, Ledger + deps on Fabric, update/toggle/delete | live Modrinth |
| Hangar plugin install (Chunky 1.4.40, SHA-256 verified) loaded by Paper 1.21.1 | live Hangar |
| SpigotMC source (2026-11-06): `spigot_source.install/update/details` against **live** Spiget + Modrinth and a real (throwaway) agent process: LuckPerms 5.5.71 (mirror current) and Chunky 1.5.3 (mirror outdated, installed only after confirmation) landed with the right SHA-256; premium, external and `.zip` resources refused; re-install refused; update reports up to date; a wrong SHA-256 is refused by the agent and leaves no file | then through the **HTTP API** on the dev panel: search, install of PlaceholderAPI 2.12.3 (resource 6245) on 'Paper Plugins', SHA-256 on disk matches, and the **running Paper 1.21.1 server loaded it** (RCON `plugins`: Chunky, PlaceholderAPI). UI not opened in a browser yet |
| World export as zip (`/files/archive?path=world`) | zip validated |
| **World import/export** (`GET /world`, `POST /world/replace`, job `world_replace`) through the real API + worker + agent + restic, on an isolated stack (own DB, agent :8702, API :8010, worker on Redis db 7), Playwright at 1440/390 px, light/dark | singleplayer save zip → vanilla `world`; whole Paper server zip → `survival` + `survival_nether` (level-name, plugins/server.properties ignored); zip without level.dat → job failed `no_level_dat`, world unchanged; `.tar.gz` already in the server folder → replaced and deleted; level.dat hashes match the sources; export zip valid; safety backups (“Before restore”) exclude the temporary upload. **No Minecraft server loaded the replaced worlds** (no container was started) |
| World import/export **review fixes**, again on an isolated stack (own DB, agent :8705, API :8015, worker on Redis db 9, Vite :5175, Playwright 1440 px light / 390 px dark) | archive inside `world/` refused before anything happens (`archive_in_world`, toast, world and archive untouched); macOS Finder zip with a nested world + `__MACOSX/` from the server folder → replaced (with a real restic safety backup), no `__MACOSX` warning; singleplayer upload without safety backup (server name typed) → replaced, no leftovers, upload removed; a user with only read files + backups sees no “Replace world…”; no horizontal overflow at 390 px. Running-server race, lost connection and start-after-replace failure: unit tests only |
| Server migration between nodes (Fabric Test, 145 MB, local → btrfs-node) | started on the target with its world |
| TLS-pinned remote node (btrfs-node over HTTPS): wrong fingerprint refused before any request; start, `wss` console + RCON, btrfs snapshot, stop | |
| `deploy/docker-compose.node.yml` agent container (healthy, TLS-aware healthcheck) added with a pin; a real vanilla 1.21.1 server ran there (RCON ok); instance-network containers get 403 | |
| Docker daemon restart with a running server | status "unknown: Docker is not reachable" during the outage, then the container came back and the panel went to running on its own |

### Unraid simulation (production compose stack, 2026-11-06)

Followed docs/QUICKSTART-UNRAID.md against images built from a clean export of the repository, with
Unraid values (MC_UID=99, MC_GID=100, data under a `/mnt/cache/appdata`-style path, backups under a
`/mnt/user/backups`-style path, `MCPANEL_POSTGRES_DIR` bind mount; only a sandbox proxy override added):
all six services healthy; owner setup; **Better MC Forge BMC4 deployed by CurseForge page URL**
(keyless) → running in 3 min 16 s, auto-pinned to file 9104992; all 7,719 server files owned 99:100;
RCON ok; restic backup to the array-style path ok; `docker compose up -d --force-recreate` (update)
left the running server untouched and the session valid; `docker compose down` + `up -d` (reboot)
→ server started again in 95 s. Production SPA served with no console errors. This was a simulation in
the sandbox. The real install on the owner's Unraid server followed on 2026-11-07 (see "Where things
stand").

### Performance: Mods tab of a large modpack (2026-11-06)

Better MC 4 (CurseForge) `44d1f199…`: 311 jars, 943 MB in `mods/`. Real dev data. "Before" is the dev
agent :8700 and API :8000 on `main`. "After" is the worktree agent on :8790 (its own `AGENT_CACHE_ROOT` in
a temp dir) and the worktree backend code (`service.inventory`, LocMem cache so Modrinth starts cold).

| Step | Before | After |
|---|---|---|
| Agent `/files/hashes`, first read with a cold page cache | **44.6 s** (disk-bound, ~21 MB/s) | same disk cost, but progressive: StoneBlock (220 jars, 240 MB, cold) answered after 3.0 s with partial results and was complete after 10.0 s (3 polls, ~24 MB/s) |
| Agent, first read with a warm page cache | 0.7 s (repeated after every restart) | 0.72 s (only once) |
| Agent, repeat call | 3 ms (in-memory LRU, lost on restart) | 4–12 ms |
| Agent, **after an agent restart** | 44.6 s cold / 0.7 s warm (re-reads every jar) | **5–8 ms**, no jar read (page-cache residency stayed 0 %) |
| Modrinth lookups (cold cache): `version_files` + `/projects` | 0.28 s + 1.59 s (3 requests of 100) | 0.28 s + 0.68 s (1 request, batch 250) |
| Backend inventory, cold Modrinth cache, warm agent | 1.86–2.10 s | 1.14–1.30 s |
| Backend inventory, warm | 0.02–0.05 s | 0.02 s |

Where the time went: reading the jars from disk. SHA-1 of the same 943 MB from RAM takes 0.62 s (SHA-512
1.27 s), and 4 reader threads on the cold disk gave no gain (20 MB/s), so there is no thread pool. Changes:
a persistent per-folder digest cache on the agent (`agent/mcagent/hashcache.py`, keyed by name, size,
`mtime_ns`, `ctime_ns` and inode, under `<state dir>/cache/file-hashes`, atomic, bounded, ignored if corrupted), an
optional `wait` on `/files/hashes` (partial results while one background pass per folder keeps hashing),
`GET /content/?progressive=1` (pending files are listed as `source: "pending"` and the page polls every
second with a progress notice), one `/projects` request per 250 ids, and Modrinth cache TTLs of 6 h
instead of 1 h. Suites: agent 350 passed, backend 435 passed, frontend tsc, lint, vitest and build clean.
Not yet checked in a browser.

### Added 2026-11-06: unit-tested only (not yet run against a real server or provider)

| Feature | What exists | Verified by |
|---|---|---|
| **Modpack "update available"** | `apps/modpacks/{models,updates,tasks}.py` (`ModpackUpdateCheck`, migration `modpacks/0001`). Hourly beat tick checks each non-archived modpack server at most every 6 h (max 120 per tick, 20 s stagger, grouped by project so the adapters' 5-min release cache is shared). Newest release for the **pinned Minecraft version + loader** (stable only unless the pin is a pre-release) is stored with its date; failures are recorded (`status=error`, last success kept), never raised. Keyless CurseForge records "Update check unavailable without a CurseForge API key" without calling CurseForge. `modpack_update` on the instance serializer, `update_check` on `GET /instances/<id>/modpack`, `POST /instances/<id>/modpack/check` (instance.view, 30 s cooldown, audited). Notification event `modpack.update_available`, sent once per new release. UI: "Update available: X" badge in the server header → Modpack tab `?review=<id>` opens the existing update-plan dialog; check status + "Check now" on the Modpack tab. | backend tests with respx (Modrinth/CurseForge-with-key/FTB shapes, cache reuse, notify-once, errors, keyless CF, due selection, API); Modrinth modpack version shape re-checked live (read-only) |
| **Restart/stop warnings** | `apps/instances/power_warnings.py`. `warn_minutes` (0/1/5/10/15) + optional `warn_message` on `POST /power` (stop/restart) and in restart/stop schedule payloads. The job warns via `tellraw @a` at T-N, the standard marks below N (10/5/1 min) and T-10 s, then acts; no warning when the server is not running, nobody is online or RCON is unreachable; acts early when everybody leaves; skips the action if the server stopped meanwhile. **Lock choice:** warning jobs are enqueued non-exclusive and claim the instance lock only for the last 10 s + the action (waiting up to 20 min for another exclusive job), so backups etc. are not blocked by a 15-min warning; a second countdown is refused (`countdown_pending`), an immediate stop/restart supersedes a pending countdown. `POST /instances/<id>/power/cancel` (instance.control, audited) or abandoning the job stops it within ~2 s and players are told it was cancelled. Operator text is validated (one printable line, no `§`, ≤100 chars) and only placed inside a `json.dumps` text component (root compound with `extra`, valid as JSON and as 1.21.5+ SNBT). UI: Restart/Stop confirm dialog (server header **and** library quick actions) with the warning select, message and online-player hint; live countdown banner with Cancel; warning fields in the schedule dialog. | backend tests with the FakeAgent capturing RCON commands on a fake clock (timing, lock timing, cancel, abandon, supersede, lock-wait timeout, players leaving, server stopping, injection, validation, schedules); vitest for the pure helpers |

Verified live 2026-11-06: the exact `tellraw` command `power_warnings.tellraw_command` builds (with quotes,
backslash and non-ASCII in the operator message) parses on real vanilla **26.3** (SNBT) and **1.21.1**
(JSON) servers via the panel's RCON endpoint ("No player was found"; a malformed control command is
rejected on both). Not verified yet: what players see in chat (needs a client), Paper/Forge/Fabric, the `say` fallback, Celery beat actually firing the hourly tick, a real
CurseForge key, and the new screens in a browser. A worker restart during a countdown fails the job
as `interrupted` (players were warned but get no "cancelled" message). The countdown occupies one
worker slot while it sleeps (like other long jobs). Correction of 2026-11-10: an earlier version of this
file said that notification channels have no admin UI. They have one, the Notifications section of the
System page, which offers the events the API lists (`modpack.update_available` among them). Whether it
was opened in a browser is not recorded.

### Command palette + keyboard shortcuts (frontend)

`frontend/src/components/command-palette/` (hooked into the top bar in `AppLayout.tsx`). Ctrl+K / Cmd+K or
the search button opens it: navigation (permission-filtered, admin pages included), every visible server
(archived too) with its pages, per-server Start/Stop/Restart (same rules and `useServerPower` hook as the
library buttons; Stop and Restart **always** go through `ConfirmDialog` from the palette, Start runs
directly like the Start buttons), toggle/system theme, recent items (localStorage, try/catch), fuzzy
order-independent search with groups ordered by best match. Shortcuts: `?` help, `g d`/`g s`/`g m`/`g a`,
`c` create server (only with `instance.create`); ignored while typing (inputs, textareas, selects,
contenteditable, xterm) or while a dialog/menu is open; a switch in the help dialog turns single-key
shortcuts off (WCAG 2.1.4). Reduced motion removes the palette's zoom animation.

Verified: unit tests (key-sequence matcher, typing-target detection, fuzzy scoring, group ordering,
storage fallbacks, permission rules) and jsdom component tests (open/close, navigation, recents, stop
confirm cancel → no request, confirm → `POST /power`, start without confirm, current-server group,
shortcuts on/off). Playwright against a worktree Vite + the live dev API at 1440 px and 390 px, dark and
light, reduced motion, and Ctrl+K from inside the xterm console: 33 + 4 checks passed, no page errors.
For the confirmation flow one server was shown as running by rewriting the API response in the browser,
and every `/power` request was aborted in the browser; no real server was started or stopped.

Independent review (second pass) fixed: (1) closing the palette, the shortcuts help or the Stop/Restart
confirmation dropped keyboard focus on `<body>` (Radix returns focus to a `DialogTrigger`, and none of
these dialogs has one); focus now goes back to the element that had it when the palette or help was
opened, or to the search button if that element is gone (`ConfirmDialog` gained an optional
`onCloseAutoFocus` prop for this). (2) On macOS, Ctrl+K inside text fields (the system "delete to end of
line" key) is no longer taken; Cmd+K works everywhere. (3) The screen-reader result count could
re-announce the previous search's count when a new search started. Re-verified with jsdom tests (focus
return after Escape, after the help and after cancelling Stop; Restart confirm posts `{action:
"restart"}`; macOS hotkey rule) and Playwright on a worktree Vite against the live API: focus return,
Ctrl+K ignored while a menu is open, keyboard-only navigation, the result count, Stop/Restart
confirmation with Cancel focused (server shown as running by a browser-only response rewrite), macOS
emulation (`navigator.platform` overridden), and a **view-only user simulated by rewriting
`/auth/state` and `/instances` responses in the browser** (no admin group, no Create server, no power
actions, no console/files/backups/access pages, `c` inert, help without "Create a server"): 32 checks
passed, no console errors. Every non-GET API request except the login was aborted in the browser (none
was attempted). Still not verified: a real restricted account on the backend, and a real Mac.

### Not yet verified (features from before the release of 2026-11-10)

What is unverified in the release itself is listed under "Not verified anywhere" above.

* **Unraid:** an earlier version has run on the owner's Unraid server since 2026-11-07 (see "Where
  things stand"). Of the items in this list, none is known to have been checked there.
* Frontend screens added in the last block (overview diagnostics/metrics, modpack tab, backup
  browser, snapshots, files zip/extract, Hangar tab, migrate dialog, CurseForge-by-URL wizard) were
  checked with tsc/lint/build/unit tests only; the Playwright pass and UX/correctness reviews were
  stopped to save credits.
* CurseForge **with** an API key (keyless path verified). GitHub Actions CI on GitHub.
* **ZFS snapshots: implemented + unit-tested; NOT verified on real ZFS** (no ZFS kernel module in the
  sandbox). See "ZFS snapshot provider" below.
* SpigotMC tab, SpigotMC dialog and the Hangar browse tab (the Hangar install dialog existed but no tab
  rendered it until now): tsc/lint/build/vitest only, not opened in a browser. The SpigotMC endpoints are
  covered by 45 mocked backend tests plus the live adapter/agent run above, not by an API-level live run.
* World replace with a **running** server (stop → replace → start) and a Minecraft server actually loading the
  replaced world (Paper migrating a singleplayer world's `DIM-1` into `world_nether`): unit-tested with the fake
  agent only. Worlds above the panel upload limit (`MCPANEL_MAX_UPLOAD_BYTES`, 2 GiB) must be copied into the
  server folder first and chosen with “From the server folder”.
* **Reset world:** run end to end on real vanilla servers on 2026-11-10 (browser and integration tests).
  What is still assumed, mostly about Paper and modpack servers, is in "Reset world" below.
* World replace on Paper with a custom `world-container` (bukkit.yml / `--world-container`): the panel looks for
  the world in the server folder root only, so it would install a world there that Paper ignores (nothing is
  deleted). A dropped connection to the agent while a long replace runs fails the job (the agent keeps
  going and refuses power actions until it is done); the server is then left stopped on purpose.

### World import/export (merged into the audit branch 2026-11-06)

Reconciled with the security-audit fixes: the multi-folder zip export (`world` + Bukkit dimension folders)
streams through the audit's bounded, cancellable zip producer; archive extraction keeps the audit's
streaming entry/size limits and runs the world job's free-space check after the first pass, before anything
is written; a running `world_replace` job can only be marked failed with `instance.restore` +
`instance.files.write` (the permissions that start it); the safety backup goes to the repository on the
server's current node (tagged with its `node_id`, like every backup). The world staging folders
(`.mcpanel-world-*`) are handled through SafeRoot inside the server folder, so they work unchanged on btrfs
subvolumes and ZFS datasets.

### Reset world (2026-11-09, part of the release of 2026-11-10; this section is current)

Status: **implemented, unit-tested, and run end to end on real vanilla servers on a Linux desktop**
(real agent, Docker, restic; through the browser and through the integration tests, 2026-11-10). Not
run on Paper or modpack servers, and not on Unraid.

What exists:

* **UI:** Settings → Danger zone → "Reset world…" (`frontend/src/pages/instance/settings/reset-world-dialog.tsx`,
  `reset-world-model.ts`, `reset-world-api.ts`, the row in `danger-tab.tsx`). Step 1 shows the plan (the
  folders that are deleted with their size, what is kept, why a reset is refused) and takes the options:
  seed (random, keep the current setting, custom), carry-overs (datapacks; `serverconfig` and
  `paper-world.yml`), safety backup. Step 2 lists what happens in order and asks for the server's name.
  It needs `instance.restore` + `instance.files.write`, as replacing the world does.
* **Panel:** `GET /api/instances/<id>/world/reset` (the plan) and `POST` (queues the exclusive job
  `world_reset`; audit `instance.world_reset` without the seed value; status "Restoring"). Code:
  `apps/instances/world.py` (`reset_plan`, `is_live`, `write_seed` / `restore_seed`,
  `verify_world_in_backup`), `views.py` (`WorldResetView`), `handlers.py` (`world_reset`);
  `run_backup(policy_excludes=False)` in `apps/backups/handlers.py`.
* **Agent:** `POST /v1/instances/<id>/world/reset` (operation `world_reset`, which blocks power
  actions), `GET /world?sizes=false`, and `classify` / `keepable` / `reset` in `mcagent/world.py`.

What keeps it from deleting the wrong thing:

* Only folders that look like a world are deleted (a `level.dat`, `session.lock`, `region` … among the
  direct entries). Anything else at a world path refuses the whole reset (`not_a_world`,
  `not_a_directory`).
* The request carries the folders the dialog showed. The view, the job and the agent each compare them
  with what they find (`world_changed`).
* Setups in which the world folder cannot be told for certain are refused: a `level-name` with spaces
  around it or an empty one, a `LEVEL` variable that differs from server.properties (server not started
  since) or is set but empty, Paper's `world-container`, world arguments in `EXTRA_ARGS`.
* "Running" is the container state (running or restarting), never the panel status, so a crash-looping
  server is stopped first.
* The safety backup ignores the server's own backup exclusions. The job stops, with nothing deleted,
  when restic reported warnings or the snapshot does not list the entries that make each folder a world.
* The agent moves the folders into `.mcpanel-world-old-*` first and moves them back on any failure. Its
  error code says whether the world was touched. After a failure that left the world as it was, the seed
  is put back and a server that was running is started again. After anything unclear (the move back
  failed, the agent was unreachable, the operation was lost) the server is left stopped.
* A job that was marked as failed meanwhile does nothing more to the server.

Verified with real servers (desktop, 2026-11-10):

* `backend/tests/test_integration_world_reset.py` against a real agent, Docker and restic: 2 passed, in
  three separate runs (103 to 114 s). Test 1 is a vanilla 1.20.4 server: reset while running with a new
  seed and a safety backup, then a reset of the stopped server without a backup. Test 2 writes
  Paper-style folders through the file API (no Paper server runs).
* Three resets through the dialog in a browser against a live panel (vanilla 1.21.1, running, custom
  seed, safety backup on; desktop and phone width): the job succeeded and the server came back with a
  new world. Refusals were checked through the API.
* So these are no longer open: the dialog against a live panel; the backup without the server's
  exclusions and `restic ls` of the world folder for a vanilla world.

Verified without a real server:

* Agent: `tests/test_world_reset.py` (125 tests: the reset on a real temporary filesystem, and the
  route with a faked Docker status).
* Backend: `tests/test_world_reset_api.py` (123 tests with the simulated agent) and
  `tests/test_world_reset_contract.py` (8 tests that read the agent and frontend sources and compare
  names, request and result fields and error codes across the three layers).
* Frontend: model, dialog and danger-tab tests in jsdom with a mocked API. The session that built the
  dialog also reports one headless-Chromium pass at 390 px and 1280 px against mocked responses; its
  harness was temporary and is not in the repository.
* One-off checks during integration, not in the repository: 61 folder layouts gave identical results,
  errors and resulting trees in the real `mcagent.world.info` / `reset` and in the simulated agent of
  the backend tests; and the code of `test_integration_world_reset.py` ran to the end against the
  simulated agent plus a small stand-in for a vanilla server.

Not verified:

* restic: what `restic ls` returns for a path that is not in a snapshot. An empty listing and an error
  both stop the job before anything is deleted.
* Docker: whether a container in restart back-off reports `running` or `restarting` (both count as
  running here).
* Minecraft, Paper and the runtime image: that a world folder holding only `datapacks/` or
  `serverconfig/` yields a new world with those in use; that Paper reads a kept `paper-world.yml`;
  whether a modpack installer brings a pack-provided world back (`CF_SET_LEVEL_FROM`: warning only);
  what the runtime writes for an empty `LEVEL` or `SEED` variable (both count as set); the folder layout
  of Paper 26.1+.
* A reset on a real Paper server or a real modpack server, and anything on Unraid.

Known limits:

* The main world only: `level-name` and, on Bukkit-layout servers, its `_nether` and `_the_end`
  folders. Other worlds (Multiverse) and plugin data outside the world folder are not touched.
* Agent and panel must be updated together. With an older agent the plan reports `agent_outdated`.
* The file-manager routes are not blocked while the agent operation runs (as for world replace).
* On a server whose backups were never used, the safety backup of a reset is the first restic run: take
  one manual backup first.
* Only the reset checks its safety backup (no user exclusions, no warnings, world present). Restore,
  world replace and software update still take theirs with the server's exclusions and accept restic
  warnings.

### Security-audit integration follow-ups (2026-11-06, unit-tested; not yet browser-checked)

* `REST_FRAMEWORK.DEFAULT_PERMISSION_CLASSES` = IsAuthenticated + `PasswordChangeEnforced` (also the schema/docs
  `SERVE_PERMISSIONS`); auth views and `/api/health` pin their own classes. `apps/core/errors.py` imports DRF's
  handler lazily (DRF resolves the default classes on import, which imports `apps.core.permissions`).
* Archived servers are never started: `apps/instances/handlers.apply_container` re-reads the flag (409
  `archived` with `start=True`, also for jobs queued before the archive; without `start` it skips creating a
  container, which the next start after unarchiving does); `POST /modpack/update` with `start` refuses archived
  servers up front. UI: notices on the Software tab and the Recreate row.
* Backups after a migration: browse/download use the repository node's agent (`backup_agent`), restore and
  restore-path refuse at once (409 `backup_on_other_node`), restore-as-new checks/allocates the port on and
  creates the server on the repository node, modpack rollback refuses at once and only offers local rollback
  points. The backup list carries `node_id` (null = node deleted), `node_name`, `on_current_node`; the
  Backups table shows an "on <node>" badge and disables in-place restores (tooltip).
* Snapshot schedules label their snapshots `scheduled #<id> <name>`; retention matches by id (survives
  renames) and still prunes old `scheduled <name>` snapshots while that name is unique on the server.
* Env validation reports `spec.env_override_error` (managed vs refused download/command/player variables).
* `GET /system/ports?node=<id>` (non-numeric/unknown node = 404); the Network page and restore-as-new pass
  the server's (backup's) node.
* Compose: `worker-periodic` (`worker -Q periodic -c 2 -n periodic@%h`, own ping health check); `.env.example`
  documents `MCPANEL_SETUP_TOKEN` and the job-reliability variables, passed through `x-panel-env`.
* Frontend: Properties tab shows `redacted_keys` as "•••••••• (hidden)" (no reveal toggle); Environment tab
  notice for `env_values_hidden`; schedule dialog offers only actions the user may run (snapshot keep count
  needs Restore); roles UI mirrors `OWNER_GRANTED_CODES`; "Mark as failed" mirrors `ABANDON_PERMISSIONS`;
  node dialog requires the token when the URL changes; System settings warns about `secrets_unreadable`.

### SpigotMC realities (verified live, see `docs/reference/spiget-api.md`)

* Only **Spiget's mirror of the latest file** can be installed: per-version downloads go to
  `www.spigotmc.org`, which serves automated clients a Cloudflare challenge. Older versions are links only.
* The mirror can lag (Chunky: mirror 1.5.3, SpigotMC 1.5.66). Detected from the B2 upload timestamp vs the
  latest release date; the user must confirm, and the version recorded is the jar's `plugin.yml` version.
* No publisher hash exists. The panel downloads once (cdn.spiget.org only, no redirects), checks the CDN's
  SHA-1, inspects the jar (Bukkit/Paper only, Folia needs `folia-supported`), and streams it to the agent with
  a SHA-256 the agent verifies (`PUT /files/upload?sha256=`). The agent download allow-list is unchanged.
  An older agent ignores `sha256`; the install then reports that the node did not re-check the hash.
* Tested Minecraft versions are author-declared per resource ("1.21"), so they only produce a warning that
  must be confirmed, never a guarantee.
* Jars that are byte-identical to a Modrinth release (LuckPerms, Chunky) are listed as Modrinth files in the
  inventory, so Modrinth's update check handles them; other SpigotMC installs are labelled "SpigotMC" and
  updated from the SpigotMC dialog.

## ZFS snapshot provider (merged 2026-11-06)

Status: **implemented + unit-tested; NOT verified on real ZFS.**

* Agent: `mcagent/zfs.py` (`ZfsSnapshots`, same interface as `BtrfsSnapshots`), picked by
  `snapshots.snapshot_provider()` from the instances directory's filesystem. Dataset per server
  (`<parent>/mcpanel-<id>`, explicit mountpoint) at prepare time; convert = dataset at a temp mountpoint,
  `cp -a`, swap; snapshots with `mcpanel:*` user properties; rollback = clone + rename + promote (never
  `zfs rollback -r`, never `destroy -R`; newer snapshots stay on `<dataset>-old-<epoch>` history
  datasets; every step undone in reverse on failure); delete refuses snapshots that history datasets
  depend on. Capability checks: fstype, `zfs` binary, `/dev/zfs`, same host/container path, `:rshared`
  propagation when containerised, dataset name pattern, `zfs list` reaching the kernel module.
* Deleting a server deletes its snapshots through the provider (`destroy_all()`, before the files, so a
  btrfs/ZFS refusal leaves the server intact for a retry); on ZFS that destroys the dataset itself, because
  a mount point cannot be renamed into the trash.
* Restic restores and clones keep the folder kind (`snapshots.prepare_replacement`): a ZFS dataset (a
  mount point) is filled **in place** (staging folder inside it, entries swapped by `swap_entries`, which
  renames between directory descriptors and moves everything back on failure); a btrfs subvolume gets a
  **new subvolume next to it** that is renamed into place (the security-audit design, now also used for
  clones, which used to produce plain folders); plain folders use `<data root>/staging`. A missing target on
  ZFS is created as its own dataset first.
* Backend: provider-agnostic convert message, rollback warnings in the job log; fake agent can report
  `zfs`. Frontend: wording follows `capability.provider`, "Kept from before a rollback" marker,
  `instance_ready` with fallback to `instance_is_subvolume`.
* Image: `zfsutils-linux` 2.3.9 (Debian trixie contrib) installs in `python:3.13-slim` (+22 packages,
  ~33 MB) — verified with the same `apt-get install` in a throwaway container; the full agent image build
  was not run here because the sandbox proxy rejects plain-HTTP apt (405).
* Tests: `agent/tests/test_snapshots_zfs.py` (62 tests after the review and the merge) drive a fake `zfs` executable
  (`tests/fake_zfs.py`, emulates datasets/snapshots/clones/promote with real files) incl. real restic
  restore into a dataset; btrfs tests (real loopback btrfs) extended. Playwright: Backups page at 1440 and
  390 px with the real btrfs node and route-mocked ZFS responses (ready / convert / unavailable / dialogs),
  no horizontal overflow.
* Needs a real host: a ZFS pool, the override in docs/UNRAID.md section 6, and a check that a dataset
  created by the agent is visible to the Minecraft container (shared propagation).
* Independent review (same day), fixed with regression tests:
  * Single-path restores from the backup browser failed with EXDEV on every btrfs subvolume and ZFS
    dataset (staging was renamed across filesystems); now staged inside the volume (real btrfs test
    reproduces the old failure). The merged code uses the security audit's fd-anchored staging in the
    destination's deepest existing folder, which covers datasets too.
  * Docker copies bind mounts *inside* an `:rshared` data mount back onto the host at every agent start and
    they multiply (reproduced: 1, 3, 7 stacked mounts) — the default `${MCPANEL_DATA_DIR}/backups` mount
    does that. The agent now refuses ZFS snapshots while such a mount exists; docs require
    `MCPANEL_BACKUP_DIR` outside the data dir. The propagation design itself was checked with a tmpfs
    stand-in in Docker 29 (agent mount visible on the host and in a later container; remount propagates).
  * A bind mount at a server folder was taken for that server's dataset (server delete would have run
    `zfs destroy -r` on the bind's source dataset); now refused unless the dataset's `mountpoint` is the
    server folder.
  * A successful rollback was reported as failed when promote/tidy-up hit an error (and `origin` could be
    unbound); these are warnings now. Deleting the last snapshot of a history dataset no longer fails when
    the empty dataset can't be removed.
  * The "is the current state captured" check before a rollback counted snapshots of other tools
    (sanoid, zfs-auto-snapshot); it now uses `written@<newest MCPanel snapshot>`.
  * Scheduled-snapshot retention failed every run on ZFS once a pruned snapshot had dependents; it keeps
    that snapshot and logs it. The capability also reports a missing CAP_SYS_ADMIN, and the node storage
    view honours `AGENT_HOST_DATA_ROOT`; server delete logs agent warnings in the job.
* Merge with the security-audit branch (2026-11-06): the in-place staging folder is visible to the file
  manager, so it is handled like the audit's restore-path staging: `.mcpanel-tmp-*` (excluded from
  backups), opened by descriptor, restic writes through `/proc/self/fd/<n>` (`Restic.restore` gained
  `pass_fds`), the copy for a clone too; removed on failure only if it is still the folder that was
  created. Clones keep skipping FIFOs/sockets/devices. Scheduled-snapshot retention stays per schedule
  and best effort (a snapshot with dependents is logged as "kept"). New tests: real-btrfs clone keeps a
  subvolume (fresh and prepared target), ZFS restore writes through a descriptor and cleans up on failure,
  restore into a new server creates its dataset.

## Security audit (Phase 10, run 2026-11-06 at the user's request)

Nine read-only lenses (agent, backend authz, input/SSRF, secrets/sessions/HTTP, data integrity, frontend,
deployment, backend bugs, frontend bugs), each finding adversarially verified, then fixed by area with
regression tests that fail on the pre-fix code:
`agent/tests/test_security_audit.py`, `backend/tests/test_security_audit_authz.py`,
`backend/tests/test_security_audit_http.py`, `backend/tests/test_reliability_audit.py`.
Highlights: bounded RCON replies and console buffers, FIFO-safe file reads, save-on on every path, bounded
archive parsing; RCON password / env values hidden from viewers; WebSockets re-check access (4401/4403) and
check the full Origin; schedules need the scheduled action's permission (every snapshot schedule needs
Restore, since all of them prune); node tokens cannot be redirected; only the owner grants
system.users/settings; optional `MCPANEL_SETUP_TOKEN`; reset tokens in the URL fragment and bound to the
password; `must_change_password` enforced; job heartbeat + 24 h visibility timeout; periodic queue +
`worker-periodic`; backups track their repository node; env deny-list covers code-download/command/op
variables; Java-exact `server.properties` parsing; webhook SSRF guard; CSP + frame-ancestors on the SPA;
trusted-proxy default narrowed to loopback; node agent bind address required. Frontend: reset page reads
`#token=`, setup token field, sockets stop retrying on 4401/4403, CSRF cookie primed before unsafe requests
(fixed a real reset-link bug in fresh browsers), live events no longer refetch every query.
Verified live: password reset end to end in a fresh browser; final browser smoke over 10 pages with no
console or 5xx errors. Not re-run at that time: the `-m integration` suite and the full compose stack
(7 services with `worker-periodic` then). Since then (2026-11-10): three of the integration files and
the compose stack in its three-container layout were run, see the top of this file; the content,
backup-browse, import and btrfs-snapshot integration files were not.

## Next recommended tasks (in order)

0. The audit fixes of 2026-11-09 (branch `fix/audit-remaining`): run the integration tests and
   `panel-acceptance.sh` on the final tree, a browser pass over the changed screens (memory dialog
   and bar, System settings reserve, schedules list, Files with non-UTF-8 files and names, Mods tab
   of a modpack server, console `stop`), one real `backup-db.sh` run and the recovery of
   docs/UNRAID.md section 10 on a test stack, then the remaining points in "Audit of 2026-11-09".

1. Read the result of the final gate run (real containers, after the last fixes to `upgrade.sh`) and
   record it under "Release of 2026-11-10".
2. Upgrade the owner's Unraid server: `./scripts/upgrade.sh dry-run`, then the command it prints
   (`run --with-migrations`; `--from 22a2c79` if the script cannot tell the deployed revision). Check
   `./scripts/upgrade.sh status` afterwards. Record what happened.
3. On Unraid: install the shutdown hook, raise the shutdown time-out to 150 s, and do the check at the
   end of docs/UNRAID-SHUTDOWN.md with one small server. Then an array stop with a modded server
   running, to see whether 45 s is enough for it.
4. On Unraid: one manual backup of a small server and a restore of one file from it (restic on the
   array share has never run there), then `manage.py ensure_backup_schedules`.
5. On Unraid: a btrfs snapshot, a rollback and a snapshot delete with the default stack (`delete_mode`
   should read `fallback`).
6. Re-run the integration files that were not run for this release (content, backup-browse, import,
   btrfs snapshots), and Reset world on a real Paper server.
7. Browser pass over the audit-touched screens (properties redaction, schedules permissions, backups on
   other nodes, nodes dialog, roles matrix) and the restart-warning dialog.
8. ZFS on a real pool (only fake-`zfs` unit tests so far; check `:rshared` visibility on Unraid).
9. A second node with an older agent next to the new panel (the feature list and the container format
   are untested with mixed versions).
10. CurseForge per-mod search (needs a key); run one real restart with a warning and watch the chat.

## Environment notes (desktop, 2026-11-10)

The release was built and tested on the owner's Linux desktop, not in the cloud sandbox. Backend tests
there use a throw-away Postgres on `127.0.0.1:55432`:
`cd backend && POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=55432 POSTGRES_PASSWORD=mcpanel
MCPANEL_TEST_DB_NAME=test_mcpanel_<name> .venv/bin/python -m pytest -q -m "not integration"`.
The restic, btrfs and zfs binaries are not installed, so those agent tests skip; real btrfs runs need
the QEMU guest described in TESTING.md. Test stacks, logs and the guest harness are under
the folder `audit-2026-11-09/` next to the checkout, outside the repository.

## Environment notes (cloud sandbox)

See `CLAUDE.md` → "Cloud sandbox specifics". Dev data root `/srv/mcpanel-dev`; btrfs test mount
`/srv/btrfs-test` (loopback `/srv/btrfs.img`; remount after a container restart:
`mount -o loop /srv/btrfs.img /srv/btrfs-test`). Second agent:
`AGENT_DATA_ROOT=/srv/btrfs-test/data AGENT_TOKEN=btrfs-node-token`, port 8701. Dev owner:
`admin` / `correct-horse-battery-9` (sandbox only). Test servers in the dev DB: Vanilla Test, Imported
Server, Better MC 4 (Modrinth, failed on purpose), Better MC 4 (CurseForge), Better MC 2 Fabric
(CurseForge), Fabric Test, Btrfs Snapshot Test (node 2). All stopped.

## Lessons recorded (do not relearn)

* Modrinth Better MC editions require manual CurseForge downloads. Recommend the CurseForge editions,
  which the runtime's built-in key installs automatically.
* Docker's `RestartCount` is cumulative, so crash-loop detection must track a baseline per not-ready
  streak (`apps/instances/reconcile.py`).
* Off-heap memory for a 300-mod pack is about 1.3 GB above heap, so container limits need generous
  overhead.
* A newer API with an older worker leaves unknown job kinds behind. The runner now fails them cleanly,
  and `POST /jobs/<id>/abandon` exists for stuck jobs.
* `pkill -f` patterns in a shell whose command line contains the same text kill that shell. Use the
  bracket trick (`[c]elery`) and run `scripts/cloud-dev/restart.sh` by path.
* The sandbox proxy port changes when the container restarts; `bootstrap.sh` reads it from
  `$HTTPS_PROXY` for the egress forwarder (a stale port silently breaks downloads in containers).
* A published agent port must arrive through a network that is not an instance network, or the
  agent's own instance-network guard refuses it (`docker-compose.node.yml` uses a dedicated `api` net).
* Identifying a modpack's mods is disk-bound, not CPU-bound: the sandbox volume reads ~21 MB/s cold,
  SHA-1 from RAM is ~1.5 GB/s, and parallel reads did not help. So the agent caches digests on disk.
  `posix_fadvise(DONTNEED)` only empties the guest page cache (the host still caches the file), so it
  cannot reproduce a cold read. Use folders that have not been read recently and check them with
  `mincore`.
* btrfs snapshots are read-only subvolumes outside the server folder: anything that deletes a server's
  data must delete them through `snapshots.delete_subvolume` (a plain `shutil.rmtree` cannot).
  `btrfs subvolume delete` needs `CAP_SYS_ADMIN`, which the agent container does not have. Without it
  the way is: clear the read-only flag, remove the files, `rmdir` the empty subvolume. Even on a pool
  mounted with `user_subvol_rm_allowed` a read-only snapshot must be made writable first.
* Several agents/sessions share one Postgres here: run pytest with a unique `MCPANEL_TEST_DB_NAME`.
* Unraid stops only the containers that carry its own label when the array stops. Everything else is
  stopped by the Docker daemon, which Unraid kills after 30 s. Hence `AGENT_SHUTDOWN_STOP_TIMEOUT=20`
  (a server's Docker stop timeout must fit inside those 30 s) and the `stopping_docker` hook.
* A container stopped with `docker stop` (also from Unraid's Docker tab or a backup plugin) counts as
  stopped by hand: `restart: unless-stopped` does not start it again, not even after a reboot. That is
  why the hook never uses `docker stop` while the panel runs, and why the three platform containers
  use `restart: always` since the audit fixes of 2026-11-09 (Minecraft containers still use
  `unless-stopped`).
* Django's ASGI handler reads the whole request body to disk before any middleware runs, and uvicorn
  has no body limit: a size limit in a Django middleware only picks the status code after the upload
  is on disk. Limits belong in front of Django (`apps/core/asgi.py`).
* Under ASGI every request runs in its own thread, so `CONN_MAX_AGE` > 0 in the web process leaks one
  PostgreSQL connection per request until the garbage collector closes it.
* Celery beat adds its intervals in its own time zone as wall-clock time: with a DST zone it skipped
  an hour of ticks on the fall-back night. Beat runs in UTC; cron schedules compute their own times.
* restic never removes a stale lock on its own; a killed restic blocks its repository until
  `restic unlock`. Stop restic with SIGINT so it removes its own lock.
* docker-py's `containers.create()` rejects `stop_timeout`; only the low-level `create_container()`
  sets Docker's stop timeout (`docker_ops.create_container`).
* The itzg runner tries `rcon-cli stop` once on SIGTERM. Before the server has opened RCON that fails,
  and without `CREATE_CONSOLE_IN_PIPE=true` its console fallback has nowhere to write: the stop is lost
  and the server is killed later without saving.
* Celery's Redis transport has no `unix://` scheme. A Redis on a Unix socket is `redis+socket:///path`
  with `?virtual_host=N` for the database (`settings.celery_broker_url`).
* A value in the Redis cache is a pickle, and Redis loads `dump.rdb` at start. Any process that can
  write into Redis's directory can therefore run code in the panel; the supervisor removes such files
  before every Redis start.
* The upgrade script is replaced or removed by its own `git checkout` during a rollback. It ends with
  `main "$@"; exit $?` on one line so that bash has read all of it before anything runs.
* Real-stack runs made before a round of fixes do not cover the fixes. Record the order (see "Where
  things stand").
