# Development status

_Last updated: 2026-11-03 (session 1)._ Keep this file honest: **implemented** ≠ **unit-tested** ≠
**integration-tested** ≠ **validated on a real Minecraft server** ≠ **validated on Unraid**.

## Last verified working state

Verified manually through the real API against a real `itzg/minecraft-server` container in the cloud
sandbox (commit `afce649` + later work):

| Flow | Level |
|---|---|
| Owner setup → login (session + CSRF) | real server |
| Create Vanilla 1.20.4 → install job → `installing` → health-checked `running` | real server |
| RCON commands (`setblock`, `save-all flush`, `list`) via API | real server |
| server.properties edit (typed + managed-key rejection) persisted across restart | real server |
| Restart → world block still present (`execute if block` → "Test passed") | real server |
| Graceful stop (exit 0, "All dimensions are saved") | real server |
| Instance lock conflict → 409 `instance_busy` | real server |
| Agent: dropped capabilities + no-new-privileges work with the itzg image | real server |
| Agent log stream WebSocket, stats, disk usage | real server |
| Modrinth search/releases/resolve for Better MC (BMC4 `4BV47HRn` v59 `bdRIjRgs` → Forge 1.20.1, Java 17) | live provider API |
| Agent `safefs` symlink/traversal protections | unit tests (12) |

Not yet verified: backups/restore on a real server (restic binary not yet in sandbox), Better MC
full start, frontend pages in a browser, Docker Compose deployment, Unraid.

## In flight (session 1)

* Workflow `backend-parallel-build`: content manager backend, test suites (unit/API/integration),
  deployment packaging (Dockerfiles, compose, CI). Results to be merged and recorded here.
* Workflow `frontend-pages`: all screens (dashboard, library, wizard, modpack browser, instance tabs,
  admin), followed by browser-based integration testing and UX/correctness reviews.

## Next recommended tasks

1. Merge workflow outputs; run all suites; record results above.
2. Backup → modify → restore validation on a real server (scenario 7).
3. Better MC BMC4 or BMC2 full deployment and startup attempt (scenario 4) — record RAM/time.
4. Import existing server (archive upload + safe extraction) and world import.
5. Unraid CA template + docs/UNRAID.md review.

## Environment notes (cloud sandbox)

See `CLAUDE.md` → "Cloud sandbox specifics". Dev data root: `/srv/mcpanel-dev`. Dev owner account:
`admin` / `correct-horse-battery-9` (sandbox only).

## Decisions not to revisit without evidence

See `ARCHITECTURE.md` → "Key design decisions".
