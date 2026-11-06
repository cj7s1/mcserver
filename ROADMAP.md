# Roadmap

Legend: `[x]` implemented · `[~]` partial · `[ ]` not started · `[!]` blocked. Verification level lives in
`DEVELOPMENT_STATUS.md` (implemented ≠ tested ≠ validated on a real server ≠ validated on Unraid).

## Phase 1 — Foundation
- [x] Repo structure, agent / backend / frontend split
- [x] Django + Postgres + Redis + Channels + Celery; OpenAPI at `/api/docs`
- [x] Local auth: owner setup, login/logout, scrypt hashing, CSRF, throttling
- [x] Instance persistence, desired vs observed state, reconcile loop (10 s)
- [x] Agent: validated container spec, create/start/stop/restart/kill/recreate, status, health-based readiness
- [x] Job system with per-instance locks and interruption handling
- [x] Frontend shell (auth, layout, routing, theme)
- [~] Dashboard (frontend page workstream)

## Phase 2 — Modded support
- [x] Server types: Vanilla, Paper, Purpur, Folia, Fabric, Quilt, Forge, NeoForge (itzg)
- [x] Version catalog from Mojang/Fabric/Quilt/Forge/NeoForge/Paper(Fill v3)/Purpur; Java resolution from Mojang `javaVersion`
- [x] Modrinth modpack search, releases, resolve, pinned deployment (`MODRINTH_MODPACK` + `MODRINTH_VERSION`)
- [x] CurseForge adapter (requires API key) + pinned deployment (`AUTO_CURSEFORGE`, `CF_FILE_ID`)
- [x] FTB adapter (public API) + pinned deployment (`FTBA`)
- [x] Install progress (job + status `installing` until first healthy check)
- [x] Better MC acceptance: BMC4 Forge + BMC2 Fabric via CurseForge running (real servers); Modrinth editions diagnosed + preflight warning
- [x] Manual-download detection API + UI (missing_mods_checker / MODS_NEED_DOWNLOAD.txt, CF_DOWNLOADS_REPO=/data/downloads)
- [x] Import server / server pack from archive (safe extraction)  — [ ] wizard UI

## Phase 3 — Core administration
- [x] Live console over WebSocket (agent log stream, survives restarts), RCON commands with audit
- [x] Historical logs (container log tail)
- [x] server.properties typed editor + raw editor, managed keys protected
- [x] JVM presets/args, memory, CPU, stop timeout, restart policy
- [x] File manager API (list/read/write/upload/download/rename/delete/mkdir/search), symlink-safe
- [x] Resource stats (CPU, memory, network, block IO, disk usage)
- [x] Port allocation + conflict detection (panel DB + Docker host bindings)
- [x] Restart-required detection (spec hash)
- [x] Background job history
- [~] Frontend pages for all of the above (page workstream)

## Phase 4 — Data protection
- [x] restic engine: per-instance encrypted repos, manual backups, coordinated save-off/save-all/save-on
- [x] Scheduled backups (cron schedules), retention policy (last/daily/weekly/monthly, pinned, safety)
- [x] Restore in place (staging + swap, pre-restore safety backup), restore as new instance
- [x] Integrity check (`restic check --read-data-subset`)
- [x] Pre-update backups (modpack updates)
- [x] Backup end-to-end validation on a real server (API + worker + restic)
- [x] Btrfs snapshot provider (subvolumes, rollback, convert, schedules) — [ ] ZFS
- [x] Backup browser (list/download/restore single path) incl. UI

## Phase 5 — Mods, plugins, players
- [x] Mods/plugins inventory, Modrinth identification by hash, update check, compatible search/install/remove/toggle/upload
- [x] Fast inventory for large modpacks: persistent digest cache on the agent, progressive first load with progress
- [x] Whitelist / operators / bans / kick (RCON when running, JSON files when stopped)
- [x] Online players (RCON `list`)
- [ ] CurseForge mod search for individual mods
- [x] Hangar plugin source (search/versions/install) incl. its browse tab
- [x] SpigotMC plugin source via Spiget (search/details/install/update of free jars from Spiget's mirror; premium/external/archives link to SpigotMC) — not yet tried in a browser or on a running Paper server

## Phase 6 — Multi-user
- [x] Permission matrix, system roles, custom roles, per-server grants
- [x] User management API, admin-issued reset links
- [x] Audit trail with secret redaction
- [~] UI for users/roles/access (page workstream)

## Phase 7 — Advanced management
- [x] Clone instance (stopped source), restore as new
- [x] Archive / unarchive, delete with typed confirmation
- [x] Modpack update with pre-update backup, history, rollback to pre-update backup
- [x] Cron schedules: restart/start/stop/backup/command
- [x] Import an existing server folder/archive (upload + extract with safe extraction)
- [ ] World import / export
- [x] Software (non-modpack) version update workflow with automatic pre-update backup

## Phase 8 — Unraid
- [x] Compose deployment, UID/GID mapping (`MC_UID`/`MC_GID`), persistent paths
- [x] Storage capability detection (fs type, Unraid shfs warning, btrfs/zfs presence)
- [ ] Unraid Community Applications template (XML)
- [ ] docs/UNRAID.md validated on real Unraid  [!] needs real hardware

## Multi-node
- [x] Node model + management API, per-node tokens, server creation on a chosen node (verified with a 2nd agent)
- [x] TLS + certificate pinning for remote agents, agent-only node compose, `add_node` command — [ ] Node admin UI

## Phase 9 — UX refinement
- [ ] Command palette, keyboard shortcuts
- [x] Notifications (webhooks/Discord) for crashes, failed backups — [ ] admin UI
- [x] Player activity history + metrics history (1-min sampling) incl. charts

## Phase 10 — Hardening
- [~] Systematic security review of agent + API (full audit ON HOLD until the user asks; earlier fixes: role escalation, grant escalation, RCON env override, proxy rate-limit bypass, login CSRF, single owner, upload limit, agent source-network guard)
- [x] Dependency scanning in CI (pip-audit, npm audit, non-blocking)
- [ ] Upgrade/reinstall test (data survives image upgrades)
- [ ] Load test of console fan-out

## Dependencies
- Modpack update/rollback depends on backups (done) and provider adapters (done).
- Content manager depends on agent `/files/hashes` + `/files/fetch` (done).
- Snapshot providers depend on agent running with btrfs-progs/zfs tools and access to the pool path.
