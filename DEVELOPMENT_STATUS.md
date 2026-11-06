# Development status

_Last updated: 2026-11-03, end of session 1 (wrap-up at the user's request)._ Keep this file honest. Implemented,
unit-tested, integration-tested, validated on a real Minecraft server, and validated on Unraid are
**different** levels, and nothing has been validated on Unraid yet.

## Last verified working state

The sandbox runs the agent on :8700 (ext4), a second agent on :8701 (btrfs loopback), the API on :8000
and the worker+beat. All tests pass on `main` of this branch:

| Suite | Result |
|---|---|
| `backend: uv run pytest -q -m "not integration"` | **434 passed** (incl. OpenAPI fail-on-warn, real-TLS node tests) |
| `backend: … -m integration` (real agent + Docker) | passed: vanilla lifecycle + backup/restore (78.6 s) |
| `agent: uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test`) | **386 passed** (btrfs, restic, real HTTPS listener, fake-`zfs` ZFS suite) |
| `frontend: npx tsc -b && npm run lint && npm run build && npx vitest run` | clean (lint warnings only), 16 tests |
| `ruff check` / `ruff format --check` | clean (backend, agent) |
| Compose stack smoke test (`deploy/docker-compose.yml`) | 6 services healthy, a real vanilla server ran, RCON + restic backup through the containerized stack |

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
| World export as zip (`/files/archive?path=world`) | zip validated |
| Server migration between nodes (Fabric Test, 145 MB, local → btrfs-node) | started on the target with its world |
| TLS-pinned remote node (btrfs-node over HTTPS): wrong fingerprint refused before any request; start, `wss` console + RCON, btrfs snapshot, stop | |
| `deploy/docker-compose.node.yml` agent container (healthy, TLS-aware healthcheck) added with a pin; a real vanilla 1.21.1 server ran there (RCON ok); instance-network containers get 403 | |
| Docker daemon restart with a running server | status "unknown: Docker is not reachable" during the outage, then the container came back and the panel went to running on its own |

### Not yet verified

* **Unraid hardware** (docs/UNRAID.md follows Unraid conventions; first real run is the user's).
* Frontend screens added in the last block (overview diagnostics/metrics, modpack tab, backup
  browser, snapshots, files zip/extract, Hangar tab, migrate dialog, CurseForge-by-URL wizard) were
  checked with tsc/lint/build/unit tests only; the Playwright pass and UX/correctness reviews were
  stopped to save credits.
* CurseForge **with** an API key (keyless path verified). GitHub Actions CI on GitHub.
* **ZFS snapshots: implemented + unit-tested; NOT verified on real ZFS** (no ZFS kernel module in the
  sandbox). See the section below.

## ZFS snapshot provider (worktree branch, 2026-11-06)

Status: **implemented + unit-tested; NOT verified on real ZFS.**

* Agent: `mcagent/zfs.py` (`ZfsSnapshots`, same interface as `BtrfsSnapshots`), picked by
  `snapshots.snapshot_provider()` from the instances directory's filesystem. Dataset per server
  (`<parent>/mcpanel-<id>`, explicit mountpoint) at prepare time; convert = dataset at a temp mountpoint,
  `cp -a`, swap; snapshots with `mcpanel:*` user properties; rollback = clone + rename + promote (never
  `zfs rollback -r`, never `destroy -R`; newer snapshots stay on `<dataset>-old-<epoch>` history
  datasets; every step undone in reverse on failure); delete refuses snapshots that history datasets
  depend on. Capability checks: fstype, `zfs` binary, `/dev/zfs`, same host/container path, `:rshared`
  propagation when containerised, dataset name pattern, `zfs list` reaching the kernel module.
* Also fixed on the way: deleting a server now deletes its snapshots (btrfs snapshot subvolumes used to be
  left behind; on ZFS the dataset must be destroyed because a mount point cannot be renamed); restic
  restores and clones into a subvolume/dataset swap contents in place, so the server stays
  snapshot-capable (previously a restic restore turned a btrfs subvolume back into a plain folder).
* Backend: provider-agnostic convert message, rollback warnings in the job log; fake agent can report
  `zfs`. Frontend: wording follows `capability.provider`, "Kept from before a rollback" marker,
  `instance_ready` with fallback to `instance_is_subvolume`.
* Image: `zfsutils-linux` 2.3.9 (Debian trixie contrib) installs in `python:3.13-slim` (+22 packages,
  ~33 MB) — verified with the same `apt-get install` in a throwaway container; the full agent image build
  was not run here because the sandbox proxy rejects plain-HTTP apt (405).
* Tests: `agent/tests/test_snapshots_zfs.py` (60 tests after the review) drive a fake `zfs` executable
  (`tests/fake_zfs.py`, emulates datasets/snapshots/clones/promote with real files) incl. real restic
  restore into a dataset; btrfs tests (real loopback btrfs) extended. Playwright: Backups page at 1440 and
  390 px with the real btrfs node and route-mocked ZFS responses (ready / convert / unavailable / dialogs),
  no horizontal overflow.
* Needs a real host: a ZFS pool, the override in docs/UNRAID.md section 6, and a check that a dataset
  created by the agent is visible to the Minecraft container (shared propagation).
* Independent review (same day), fixed with regression tests:
  * Single-path restores from the backup browser failed with EXDEV on every btrfs subvolume and ZFS
    dataset (staging was renamed across filesystems); now staged inside the volume (real btrfs test
    reproduces the old failure).
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

## Next recommended tasks (in order)

1. Browser pass over the new screens (Playwright, 1440 and 390 px, dark/light) and fix what it finds.
2. Admin UI still missing: Nodes screen (API + `manage.py add_node` exist), notifications channels
   screen (API exists), job "Mark as failed" button (`POST /jobs/<id>/abandon`), import-existing-server
   wizard mode (`POST /instances/<id>/import` exists; Files upload + extract also works).
3. Validate on Unraid; then (only when the user asks) the Phase 10 security audit.
4. Roadmap: verify ZFS snapshots on a real pool, SpigotMC source, CurseForge per-mod search (needs a key).

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
