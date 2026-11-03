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
- [~] Dashboard (built by page workstream)

## Phase 2 — Modded support
- [x] Server types: Vanilla, Paper, Purpur, Folia, Fabric, Quilt, Forge, NeoForge (itzg)
- [x] Version catalog from Mojang/Fabric/Quilt/Forge/NeoForge/Paper(Fill v3)/Purpur; Java resolution from Mojang `javaVersion`
- [x] Modrinth modpack search, releases, resolve, pinned deployment (`MODRINTH_MODPACK` + `MODRINTH_VERSION`)
- [x] CurseForge adapter (requires API key) + pinned deployment (`AUTO_CURSEFORGE`, `CF_FILE_ID`)
- [x] FTB adapter (public API) + pinned deployment (`FTBA`)
- [x] Install progress (job + status `installing` until first healthy check)
- [~] Better MC acceptance run (metadata/resolve verified; full server start pending)
- [ ] CurseForge "manual download" helper UI for mods that disallow distribution (`/downloads` mount)
- [ ] Manual server-pack upload (zip) as instance source

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
- [ ] Backup end-to-end validation on a real server (needs restic binary in sandbox)
- [ ] Btrfs / ZFS snapshot providers (capability detection exists)
- [ ] Backup browser (list/restore individual files from a snapshot)

## Phase 5 — Mods, plugins, players
- [~] Mods/plugins inventory, Modrinth identification by hash, update check, compatible search/install/remove/toggle/upload (backend in progress)
- [x] Whitelist / operators / bans / kick (RCON when running, JSON files when stopped)
- [x] Online players (RCON `list`)
- [ ] CurseForge mod search for individual mods
- [ ] Hangar / SpigotMC plugin sources

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
- [ ] Import an existing server folder/archive (upload + extract with safe extraction)
- [ ] World import / export
- [ ] Software (non-modpack) version update workflow with automatic pre-update backup

## Phase 8 — Unraid
- [~] Compose deployment, UID/GID mapping (`MC_UID`/`MC_GID`), persistent paths (deploy workstream)
- [x] Storage capability detection (fs type, Unraid shfs warning, btrfs/zfs presence)
- [ ] Unraid Community Applications template (XML)
- [ ] docs/UNRAID.md validated on real Unraid  [!] needs real hardware

## Phase 9 — UX refinement
- [ ] Command palette, keyboard shortcuts
- [ ] Notifications (webhooks/Discord) for crashes, failed backups
- [ ] Player activity history

## Phase 10 — Hardening
- [ ] Systematic security review of agent + API
- [ ] Dependency scanning in CI (pip-audit, npm audit) — in deploy workstream
- [ ] Upgrade/reinstall test (data survives image upgrades)
- [ ] Load test of console fan-out

## Dependencies
- Modpack update/rollback depends on backups (done) and provider adapters (done).
- Content manager depends on agent `/files/hashes` + `/files/fetch` (done).
- Snapshot providers depend on agent running with btrfs-progs/zfs tools and access to the pool path.
