# Development status

_Last updated: 2026-11-06 (session 2: SpigotMC source, mods-listing speed-up, integration coverage, btrfs
delete fix, Unraid simulation, modpack update checks, restart warnings, security-audit fixes, ZFS snapshot
provider). Previous: 2026-11-03, end of
session 1._ Keep this file honest. Implemented,
unit-tested, integration-tested, validated on a real Minecraft server, and validated on Unraid are
**different** levels, and nothing has been validated on Unraid yet.

## Last verified working state

The sandbox runs the agent on :8700 (ext4), a second agent on :8701 (btrfs loopback), the API on :8000
and the worker+beat. All tests pass on `main` of this branch:

| Suite | Result |
|---|---|
| `backend: uv run pytest -q -m "not integration"` | **812 passed** (2026-11-06, after merging the security-audit fixes and the ZFS snapshot provider; incl. OpenAPI fail-on-warn) |
| `backend: … -m integration` (real agents + Docker, see TESTING.md → "Integration tests") | **7 passed in 157 s** (2026-11-06, before the audit/ZFS merges): vanilla lifecycle + backup/restore 70 s, Paper + Modrinth plugin 57 s, backup browser + restore-path 17 s, btrfs snapshots over pinned TLS 9 s (3 tests), zip import 2 s |
| `agent: uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test`) | **468 passed** (2026-11-06; real btrfs + restic, real HTTPS listener, fake-`zfs` ZFS suite) |
| `frontend: npx tsc -b && npm run lint && npm run build && npx vitest run` | clean (lint warnings only), 140 tests in 22 files (2026-11-06) |
| `ruff check` / `ruff format --check` | clean (backend, agent) |
| Compose stack smoke test (`deploy/docker-compose.yml`) | 6 services healthy, a real vanilla server ran, RCON + restic backup through the containerized stack |

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
→ server started again in 95 s. Production SPA served with no console errors. Still not run on real
Unraid hardware.

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
worker slot while it sleeps (like other long jobs). Notification channels still have no admin UI, so
`modpack.update_available` is selectable through the API only.

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

### Not yet verified

* **Unraid hardware** (docs/UNRAID.md follows Unraid conventions; first real run is the user's).
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

## Next recommended tasks (in order)

1. Browser pass over the new screens (Playwright, 1440 and 390 px, dark/light) and fix what it finds,
   including the restart-warning dialog/banner and the modpack update badge; run one real restart with a
   warning on a vanilla server (≤1.21.4 and ≥1.21.5) and watch the chat.
2. Admin UI still missing: Nodes screen (API + `manage.py add_node` exist), notifications channels
   screen (API exists), job "Mark as failed" button (`POST /jobs/<id>/abandon`), import-existing-server
   wizard mode (`POST /instances/<id>/import` exists; Files upload + extract also works).
3. Validate on Unraid; then (only when the user asks) the Phase 10 security audit.
4. Roadmap: verify ZFS snapshots on a real pool, CurseForge per-mod search (needs a key). SpigotMC: install
   one plugin through the UI on a running Paper server and check it loads (only the adapter + agent path was
   run live).

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
  data must delete them with `btrfs subvolume delete` (`shutil.rmtree` cannot).
* Several agents/sessions share one Postgres here: run pytest with a unique `MCPANEL_TEST_DB_NAME`.
