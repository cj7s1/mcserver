# Development status

_Last updated: 2026-11-06 (session 2: SpigotMC source, mods-listing speed-up, integration coverage, btrfs
delete fix, Unraid simulation). Previous: 2026-11-03, end of session 1._ Keep this file honest. Implemented,
unit-tested, integration-tested, validated on a real Minecraft server, and validated on Unraid are
**different** levels, and nothing has been validated on Unraid yet.

## Last verified working state

The sandbox runs the agent on :8700 (ext4), a second agent on :8701 (btrfs loopback), the API on :8000
and the worker+beat. All tests pass on `main` of this branch:

| Suite | Result |
|---|---|
| `backend: uv run pytest -q -m "not integration"` | **480 passed** (2026-11-06, after merging SpigotMC + hash cache + integration tests; incl. OpenAPI fail-on-warn) |
| `backend: … -m integration` (real agents + Docker, see TESTING.md → "Integration tests") | **7 passed in 157 s** (2026-11-06): vanilla lifecycle + backup/restore 70 s, Paper + Modrinth plugin 57 s, backup browser + restore-path 17 s, btrfs snapshots over pinned TLS 9 s (3 tests), zip import 2 s |
| `agent: uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test`) | **354 passed**, 3 btrfs tests skipped without the mount (2026-11-06) |
| `frontend: npx tsc -b && npm run lint && npm run build && npx vitest run` | clean (lint warnings only), 15 tests (2026-11-06) |
| `ruff check` / `ruff format --check` | clean (backend, agent) |
| Compose stack smoke test (`deploy/docker-compose.yml`) | 6 services healthy, a real vanilla server ran, RCON + restic backup through the containerized stack |

### Integration coverage added 2026-11-06 (and a bug it found)

New `-m integration` files: `test_integration_snapshots.py` (btrfs over a TLS-pinned node),
`test_integration_backup_browse.py`, `test_integration_content.py` (Paper 1.21.1 + Chunky from
Modrinth), `test_integration_import.py`; shared helpers in `tests/integration_support.py`.

**Fixed: deleting a server on btrfs orphaned its snapshots.** The agent's `DELETE /v1/instances/<id>/data`
removed the container, the folder and the restic repo but never `<data root>/snapshots/<id>/`: read-only
subvolumes stayed on disk, invisible in the panel (also hit by migrate with "delete source"). It now
purges them (`BtrfsSnapshots.purge()`) *before* deleting the files, so a btrfs failure leaves the server
intact for a retry. Agent tests: `test_purge_deletes_read_only_snapshots`,
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

### Not yet verified

* **Unraid hardware** (docs/UNRAID.md follows Unraid conventions; first real run is the user's).
* Frontend screens added in the last block (overview diagnostics/metrics, modpack tab, backup
  browser, snapshots, files zip/extract, Hangar tab, migrate dialog, CurseForge-by-URL wizard) were
  checked with tsc/lint/build/unit tests only; the Playwright pass and UX/correctness reviews were
  stopped to save credits.
* CurseForge **with** an API key (keyless path verified). GitHub Actions CI on GitHub. ZFS snapshots.
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

## Next recommended tasks (in order)

1. Browser pass over the new screens (Playwright, 1440 and 390 px, dark/light) and fix what it finds.
2. Admin UI still missing: Nodes screen (API + `manage.py add_node` exist), notifications channels
   screen (API exists), job "Mark as failed" button (`POST /jobs/<id>/abandon`), import-existing-server
   wizard mode (`POST /instances/<id>/import` exists; Files upload + extract also works).
3. Validate on Unraid; then (only when the user asks) the Phase 10 security audit.
4. Roadmap: ZFS snapshots, CurseForge per-mod search (needs a key). SpigotMC: install one plugin through the
   UI on a running Paper server and check it loads (only the adapter + agent path was run live).

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
