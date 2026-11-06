# Development status

_Last updated: 2026-11-03, end of session 1 (wrap-up at the user's request)._ Keep this file honest. Implemented,
unit-tested, integration-tested, validated on a real Minecraft server, and validated on Unraid are
**different** levels, and nothing has been validated on Unraid yet.

## Last verified working state

The sandbox runs the agent on :8700 (ext4), a second agent on :8701 (btrfs loopback), the API on :8000
and the worker+beat. All tests pass on `main` of this branch:

| Suite | Result |
|---|---|
| `backend: uv run pytest -q -m "not integration"` | **431 passed** (incl. OpenAPI fail-on-warn, real-TLS node tests) |
| `backend: … -m integration` (real agent + Docker) | passed: vanilla lifecycle + backup/restore (78.6 s) |
| `agent: uv run pytest -q` (+ `MCPANEL_TEST_BTRFS=/srv/btrfs-test`) | **321 passed** (btrfs, restic, real HTTPS listener) |
| `frontend: npx tsc -b && npm run lint && npm run build && npx vitest run` | clean (lint warnings only), 57 tests (44 of them for the command palette) |
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
* CurseForge **with** an API key (keyless path verified). GitHub Actions CI on GitHub. ZFS snapshots.

## Next recommended tasks (in order)

1. Browser pass over the new screens (Playwright, 1440 and 390 px, dark/light) and fix what it finds.
2. Admin UI still missing: Nodes screen (API + `manage.py add_node` exist), notifications channels
   screen (API exists), job "Mark as failed" button (`POST /jobs/<id>/abandon`), import-existing-server
   wizard mode (`POST /instances/<id>/import` exists; Files upload + extract also works).
3. Validate on Unraid; then (only when the user asks) the Phase 10 security audit.
4. Roadmap: ZFS snapshots, SpigotMC source, CurseForge per-mod search (needs a key).

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
