# Development status

_Last updated: 2026-11-03, end of session 1 work block._ Keep this file honest. Implemented,
unit-tested, integration-tested, validated on a real Minecraft server, and validated on Unraid are
**different** levels, and nothing has been validated on Unraid yet.

## Last verified working state

The sandbox runs the agent on :8700 (ext4), a second agent on :8701 (btrfs loopback), the API on :8000
and the worker+beat. All tests pass on `main` of this branch:

| Suite | Result |
|---|---|
| `backend: uv run pytest -q -m "not integration"` | **393 passed** |
| `backend: … -m integration` (real agent + Docker) | passed: vanilla lifecycle + backup/restore (78.6 s) |
| `agent: uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test`) | **307 passed** (btrfs + restic tests included) |
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
| btrfs snapshots: create (save-off coordinated) → modify → rollback (with safety snapshot) | through the second node |
| Multi-node: second agent registered as a node, server created/deleted there | |
| Webhook notifications (test + real backup event) | local receiver |
| Modrinth content manager: LuckPerms on Paper, Ledger + deps on Fabric, update/toggle/delete | live Modrinth |

### Not yet verified

* Frontend pages in a browser (the page workstream is in progress; see below).
* Running on real Unraid hardware (docs/UNRAID.md is based on Unraid conventions).
* CurseForge **with** an API key (no key available). The keyless path is verified.
* FTB deployment (adapter unit-tested against recorded API shapes; no server started).
* GitHub Actions CI on GitHub (the workflow file passes actionlint locally).
* ZFS snapshots (not implemented).

## In flight

* Workflow `frontend-pages`: all screens, then browser integration testing plus UX and correctness
  reviews. The frontend **must** then add UI for features added after its spec (task list below).
* Workflow `security-audit`: five audit lenses over backend and agent with adversarial verification
  and fixes.

## Next recommended tasks (in order)

1. Merge the frontend workflow output; `npx tsc -b && npm run lint && npm run build`; Playwright pass.
2. Frontend for the newer backend features:
   * diagnostics card (`GET /instances/<id>/diagnostics`) and manual downloads (`/manual-downloads`)
   * modpack tab (`/modpack`, `/modpack/plan/<v>`, `/modpack/update`, `/modpack/rollback`)
   * software update (`POST /software`)
   * import an existing server (`POST /instances/<id>/import`)
   * backup browser (`/backups/<id>/files|download|restore-path`)
   * snapshots (`/snapshots…`)
   * metrics charts (`/metrics?range=`) and player history (`/players/history`)
   * notifications admin (`/notifications/…`), nodes admin (`/nodes…`), job abandon
     (`POST /jobs/<id>/abandon`)
   * wizard: CurseForge URL/slug mode without a key, show `preflight` warnings, and the new container
     memory formula (mods: heap + max(1536, 40%); others: heap + max(768, 30%))
3. Merge the security-audit fixes; re-run all suites.
4. Remaining roadmap: ZFS snapshots, Hangar/CurseForge mod sources, world import/export UI, Unraid
   validation.

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
