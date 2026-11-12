# CLAUDE.md — guide for future sessions

Self-hosted game server management platform (Unraid-first): Minecraft, Satisfactory, Terraria, and Factorio since multi-game
phases 2 + 3, plus Palworld, Valheim, Project Zomboid, Hollow Knight (HKMP), Counter-Strike 2 and Rust on a shared
base (docs/design/GAME-DEFINITIONS.md; ARCHITECTURE.md "Games"; user notes docs/GAMES.md). Read `DEVELOPMENT_STATUS.md` first:
it records what works, what was verified and the next task. `ARCHITECTURE.md` explains why things are
the way they are; `docs/reference/*.md` holds **verified** upstream API notes (itzg, Modrinth,
CurseForge, FTB, version metadata, restic) — use them instead of guessing endpoints.

## Layout

```
agent/      FastAPI node agent (only component with Docker socket). mcagent/{main,docker_ops,lastrun,safefs,backups,snapshots,zfs,world,archive,fetch,hashcache,storage,rcon,tls,schemas,operations,maintenance,netguard,claimguard,claimmarker,slp,errors}.py,
            mcagent/profiles/ (runtime profile per game: minecraft, factorio, satisfactory, terraria = 3 variants), mcagent/drivers/ (control drivers: minecraft-rcon, rcon, http-api + satisfactory-api, console (+ console_io: exec/stdin primitives), tshock-rest, none)
backend/    Django 5.2 + DRF + Channels + Celery. mcpanel/ (settings, urls, asgi, celery), apps/<app>/,
            container/ (entrypoint.sh, supervise.py, healthcheck.py: the single panel container)
frontend/   React 19 + TS 7 + Vite 8 + Tailwind 4 + shadcn/ui (radix-nova) + TanStack Query + react-router 8 + xterm.js
deploy/     docker-compose.yml (prod: panel, agent, postgres), docker-compose.node.yml, docker-compose.dev.yml,
            .env.example, scripts/ (generate-secrets.sh, upgrade.sh), unraid/ (stopping_docker hook, install-hook.sh, backup-db.sh,
            instance-firewall.sh, array-start.sh: the one User Script at array start; plugin/: the Unraid plugin, src/ + build.sh -> mcpanel.plg;
            first-run.sh: the plugin's first-run checklist as a program, through the plugin's runner)
docs/       QUICKSTART-UNRAID.md, UNRAID.md, UNRAID-SHUTDOWN.md, SECURITY.md, FRIENDS.md, GAMES.md, reference/ (upstream notes)
.github/    workflows/ci.yml, scripts/ (panel-acceptance.sh, upgrade-rehearsal.sh: real containers)
scripts/cloud-dev/  sandbox-only helpers (egress forwarder, env files, start-services.sh)
```

## Commands

```bash
# backend
cd backend && uv sync && MCPANEL_DEBUG=1 uv run python manage.py migrate
cd backend && uv run pytest -q -m "not integration"          # conftest sets MCPANEL_TESTING=1
cd backend && MCPANEL_TEST_DB_NAME=test_mcpanel_<name> uv run pytest -q ...  # own test DB for parallel runs
cd backend && uv run ruff check . && uv run ruff format .
# agent
cd agent && uv sync && uv run pytest -q && uv run ruff check .
# frontend
cd frontend && npm ci && npx tsc -b && npm run lint && npm run build && npx vitest run
cd frontend && npx vite --port 5173          # proxies /api and /ws to 127.0.0.1:8000
# integration (needs a running agent + Docker; starts a real vanilla server)
cd backend && MCPANEL_IT_AGENT_URL=http://127.0.0.1:8700 uv run pytest -q -m integration
# real containers (need a built stack; see TESTING.md)
cd deploy && ../.github/scripts/panel-acceptance.sh
.github/scripts/upgrade-rehearsal.sh <site checkout of the old revision> <new revision>
```

Two backend tests read the documentation (`tests/test_panel_container.py`,
`tests/test_security_audit_http.py`): DEPLOYMENT.md must keep the `upgrade.sh` commands and the
heading text "Coming from the 7-container layout"; README.md, ARCHITECTURE.md, docs/UNRAID.md and
docs/QUICKSTART-UNRAID.md must not name the old periodic-worker service. Run both after editing docs.

### Cloud sandbox specifics (not needed on a real host)

* Start daemons: `dockerd &`, `service postgresql start`, `service redis-server start`
  (DB `mcpanel`/user `mcpanel`/pw `mcpanel`, CREATEDB).
* Containers can only reach the internet through the sandbox proxy:
  `python3 scripts/cloud-dev/egress_forwarder.py 0.0.0.0 3128 127.0.0.1 <port of $HTTPS_PROXY> &` (the port
  changes between container restarts; bootstrap.sh derives it); the agent injects proxy
  env + CA via `scripts/cloud-dev/agent.env` (`AGENT_RUNTIME_EXTRA_ENV`, `AGENT_RUNTIME_CA_BUNDLE`,
  `AGENT_RUNTIME_JAVA_TRUSTSTORE`). Network `mcpanel_instances` gateway is 172.18.0.1.
* After a container restart run `scripts/cloud-dev/bootstrap.sh` (daemons, btrfs mount, egress forwarder,
  agents :8700/:8701, API, worker, Vite). Files under /srv and the venvs/node_modules persist.
* `scripts/cloud-dev/start-services.sh` (run it by path; don't inline its `pkill` lines in a shell whose
  command line contains the same pattern) starts agent :8700, API :8000, worker+beat.
* `git push` may hang on `push.negotiate`; use `git -c push.negotiate=false push -u origin <branch>`.
* restic binary: copy from the `restic/restic` image (`docker create` + `docker cp`); github.com
  release downloads are blocked. Docker Hub sometimes returns 429 — retry later.

## Subsystem map (where things live)

| Area | Backend | Agent |
|---|---|---|
| Games: definitions, capabilities, catalog (`GET /api/catalog/games`, `.../<id>/versions`) | `apps/games/{__init__,base,minecraft,factorio,codecs,views}.py`; `games.for_instance(i)` is the one way to ask game-specific questions; Minecraft's spec never gets new keys (pin tests `tests/test_games_phase1_pins.py`) | `profiles/{__init__,base,minecraft,factorio}.py` (images, tag pattern, user, caps, mounts, env allow-list, ports, never_publish, health, redaction, backup excludes), `drivers/{base,rcon,minecraft}.py`, `errors.py` (error codes); pins `tests/test_games_pins.py` + `pins/create_kwargs.json` |
| Other games: named ports, config files, secrets, setup steps, saves, software update, secret files | `apps/instances/{game_views,game_handlers,secret_files}.py` (create, `/config`, `/secrets`, `/setup`, `/saves*`; jobs `setup`, `saves_reset`, `saves_import`, `wait_first_start`), `services.assign_named_ports` / `ports_in_use_by_protocol`, `handlers.write_control_secret` / `_game_software_update`; frontend `create-server/GameWizard.tsx`, `settings/game-settings-tab.tsx`, `backups/SavesSection.tsx`, `network/GameNetwork.tsx`, `overview/SetupCard.tsx`, `players/GamePlayers.tsx` | `docker_ops.{agent_health,wait_for_log,log_redactor,image_volume_plan}`, `main._log_waiter`, `main._profile_backup_excludes` |
| Satisfactory (claim before publish, API token, HTTPS API driver, saves through the API) | `apps/games/satisfactory.py` (setup steps `claim`/`access`/`new_game`/`publish`, `ports_open`, virtual configs `launch`/`server-options`, `SatisfactorySaves` jobs), hooks in `games/base.py` (`SetupWaiting`, `control_secret_value`, `secrets_changed`, `is_virtual_config`, `PortSpec.same_port_as`, `SecretSpec.required_when`, `fixed_version`), `AgentClient.game_action`; frontend `create-server/FixedVersion.tsx`, `players/PlayerCount.tsx`, `overview/BranchVersion.tsx` | `drivers/{http_api,satisfactory}.py` (TLS for the internal hop only, pinned per run; `game_action`, `publish_guard` = claim file + token + live probe, run before every start by `docker_ops.start_guard`, `channel`, `QueryTarget`), `claimguard.py` (loop + Docker events: stops a published unclaimed server, `guard_stop`), `claimmarker.py` (claim marker `.mcpanel/claim-verified`, start wrapper `.mcpanel/start-guard.sh` = the entrypoint, exit 78; `GuardedRoot` of the file API), tests `tests/test_claim_marker.py`, `profiles/satisfactory.py` (`env_ports`), health kinds `udp-query`/`driver` in `docker_ops.agent_health`, `POST /game/{action}`, `main._carry_over_excluded` (restore keeps `gamefiles/`); tests `tests/test_games_satisfactory.py`, `tests/test_claim_guard.py`, `tests/test_review_items.py` + `tests/sf_fake.py`. Panel: `mark_unclaimed`/`files_touched`/`observed`/`copied_state`, `handlers.unclaimed_refusal`/`watch_start`, tests `tests/test_review_mg_*.py`, `tests/test_review_fx_claim.py` |
| Six Steam-era games: Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2, Rust (pinned images that download the game, virtual configs as env, 1:1 / derived / conditional ports, folder saves) | `apps/games/{steamgame,foldersaves,palworld,valheim,zomboid,hkmp,cs2,rust}.py` (`SteamGameDefinition`, `FolderSaves`), hooks in `games/base.py` (`PortSpec.offset_of`/`one_to_one`/`when`, `SecretSpec.generate`/`reveal`/`pattern`, `secret_error`, `initial_secrets`, `first_start_timeout`), `game_views.SecretRevealView` (`POST /secrets/<key>/reveal`), config field `pattern` in `codecs.coerce`; tests `tests/test_games_six.py` (runs the agent's `build_create_kwargs` on the panel's specs), pins of the older games `tests/test_games_other_pins.py` | `profiles/{palworld,valheim,zomboid,hkmp,cs2,rust,steam}.py`, `drivers/{palworld,webrcon}.py`, `a2s.py` (health kind `a2s`, `/ping` for PZ/CS2/Rust), `rcon` driver `port_env`/`a2s_port_env`, console guard `screen`; profile fields `init`, `image_digests`, `redact_env`, `protected_paths`, `env_port_offsets`; interface notes `audit-2026-11-09/more-games/agent-notes.md` |
| Terraria (variants vanilla / tModLoader / TShock, console input, TShock REST) | `apps/games/terraria.py` (per-variant files/secrets/capabilities, `before_apply` = panel-owned `serverconfig.txt` keys + TShock REST token, `TerrariaSaves` with `prepare_reset`, owner-account setup step), hooks in `games/base.py` (`config_files_for`, `secret_specs_for`, `default_memory_mb_for`, `before_apply`, `masked_text`, `clean_exit_codes`, `version_key`); frontend `create-server/gameWizardModel.ts` (`variantView`), `lib/games.ts` | `profiles/terraria.py` (3 profiles, `terraria_ids`), `drivers/console_io.py` (the only exec, stdin attach, sanitising, console lock), `drivers/console.py`, `drivers/tshock.py`; profile fields `entrypoint`/`command`/`working_dir`/`clean_exit_codes` |
| Lifecycle, spec, status, crash loops | `apps/instances/{handlers,spec,state,reconcile}.py`. A job starts a server only through `handlers.start_server` (creates the container when there is none). A job that stopped a running server and then fails starts it again when nothing was changed, else records it stopped (`put_back`, `Progress`) | `docker_ops.py`; apply creates the new container before it removes the old one |
| Memory admission | `apps/instances/capacity.py` (node RAM minus system setting `memory_reserve_mb` vs. limits of running containers; 409 `insufficient_memory`, override `allow_overcommit`); frontend `lib/power.ts` (`sendPower`, the one power path), `components/{MemoryOvercommitDialog,MemoryCommitBar}.tsx` | `GET /v1/info` (`docker.memory_bytes`) |
| Runtime images | `spec.current_image` (channel reaches existing servers), `handlers.apply_container` (`pull="newer"` on every start that creates a container) | `main.apply_spec`, `docker_ops` (bounded pull, superseded images removed); feature `pull_newer` |
| Cron schedules | `apps/instances/scheduling.py`: one try/except per schedule, impossible dates refused/paused, backups before snapshots before power actions, deferral when busy (`Schedule.deferred_at`, 60 min), misfire rule after downtime, wall-clock DST handling; Celery beat runs in UTC | — |
| Stopped server = no container (retire, last-run record) | `reconcile.py` (`retire_blocker`, `maybe_retire`, `retire_container`), `state.compute` (reads `last_run`), `diagnostics.py`, `manage.py retire_stopped_containers`, setting `MCPANEL_RETIRE_STOPPED_CONTAINERS` | `docker_ops.py` (`retire`, `instance_lock`: the per-server lock around apply/start/restart/remove/retire), `lastrun.py`, `POST /retire`, `DELETE /last-run`, `FEATURES` in `main.py` |
| Restart required | `spec.py`: `spec_outdated` (hash differs) vs `restart_required` (hash differs **and** the server has a container); `CONTAINER_FORMAT` is mixed into the hash | `docker_ops.CONTAINER_FORMAT` (reported in `/v1/info` and by every apply). Raise both together |
| Graceful stop, Docker stop timeout | `handlers._log_stop`, `spec.build_env` (`CREATE_CONSOLE_IN_PIPE`), `nodes/client.py` (`STOP_EXTRA_SECONDS`) | `docker_ops.py` (`graceful_stop`, `_RconStopCourier`, `build_create_kwargs`, `create_container`), `config.py` (`shutdown_stop_timeout` = `AGENT_SHUTDOWN_STOP_TIMEOUT`) |
| Unraid plugin `mcpanel` (page, boot tasks at Docker start, pinned Compose) | `deploy/unraid/plugin/src/` (`MCPanel.page` + `mcpanel.js`, endpoint `include/{action,common}.php`, runner `bin/mcpanel`: fixed task list, flock lock, run folder /run/mcpanel checked on every use, strict root-owned checkout for the boot run (NEEDS YOU); `event/docker_started`; `install/*.sh`), `build.sh` -> `mcpanel.plg` (commit both; version = first heading of `CHANGES.md`); tests `tests/test_deploy_unraid_plugin.py`, `tests/test_deploy_unraid_plugin_php.py` (+ `tests/unraid_plugin_php/endpoint_test.php`); not run on Unraid yet | — |
| Host shutdown + resume (Unraid hook), Unraid boot | `manage.py host_shutdown`, `scheduling.resume_flagged_servers` (from `tasks.scheduler_tick`), `Instance.resume_after_boot` / `resume_flagged_at`; hook and installer in `deploy/unraid/`; docs/UNRAID-SHUTDOWN.md; `deploy/unraid/array-start.sh` (at every array start: Compose link `--compose-bin`, state `/boot/config/plugins/mcpanel/array-start.env`, wait for Docker, hook, firewall where `INSTALLED=yes`, repair of a platform container that misses a network of its project, by Compose's labels, one-offs skipped, an EXIT trap starts what it stopped; tests `tests/test_deploy_array_start.py`); Docker tab labels `net.unraid.docker.webui`/`icon` on the panel (`frontend/public/unraid-icon.png`, never `net.unraid.docker.managed`) | the stop endpoint |
| Friends: Share card (B1) | `apps/instances/share.py` (`GET /api/instances/<id>/share`; the password only with `instance.configure`), `apps/games/joining.py` (join steps, requirements, protection per game; `join_password`); tests `tests/test_friends_share.py`; docs/FRIENDS.md | — |
| Stop when empty (B2) | `apps/instances/idle.py` (`check_idle`, from the player samples), Celery beat `idle-stop` (`tasks.idle_stop_tick`), `Instance.idle_stop_minutes`, `games/base.py` (`player_count_unavailable`), notification `instance.idle_stopped`; tests `tests/test_friends_idle.py` | — |
| Discord bot (B3) | `apps/discordbot/{bot,logic,config,state,views}.py` (gateway client, the five guild commands, rate limits, `/api/system/discord`, token encrypted in the system settings), `manage.py run_discord_bot`, supervisor child `discord` (optional: never makes the container unhealthy; exit 78 = idle, wake file `/run/mcpanel/discord.wake`), service account by id (system setting `discord_service_user_id`; names `discord-bot[-n]` reserved), no mentions, per server `discord_available`/`discord_roles`/`discord_share_password`; tests `tests/test_friends_discord.py` (fakes of discord.py) | — |
| Friend role + invite links (B4) | `apps/accounts/permissions.py` (role `friend`, code `instance.overview`; start and stop only, a reduced short answer, no global host answers), `apps/accounts/views.py` (`POST /api/users/invite`), `apps/core/permissions.py`, `instances/serializers.py` (short answer `limited`), `console/consumers.py` (no job events for a friend); tests `tests/test_friends_access.py` | — |
| Panel container (supervisor, health, start-up guard) | `backend/container/{entrypoint.sh,supervise.py,healthcheck.py}` (children redis, web, worker, periodic, beat and `discord`, which may be `idle`), `mcpanel/celery.py` (tick age, quiet tick logging), `apps/core/views.py` (`HealthView`: `components`, `tick_age_seconds`) | — |
| Jobs: locks, heartbeat, re-sending, watchdog | `apps/jobs/{runner,tasks}.py` (`dispatch`, `resend_queued_jobs`, `watchdog`; `finish()` only ends an active job), `apps/jobs/shutdown.py` (a stopping worker ends job waits at once), `mcpanel/celery.py` (`worker_ready`, `worker_shutting_down`), `apps/backups/handlers.settle_orphaned_backups` | `operations.py` (one operation per server) |
| Request limits, uploads, downloads | `apps/core/asgi.py` (`RequestBodyLimit` before Django, `PanelASGIHandler` spools to `MCPANEL_UPLOAD_TMP`), `apps/core/uploads.py` (one copy on disk), `apps/core/streaming.py` (`AgentStreamingResponse`); web process has `CONN_MAX_AGE=0` | — |
| Agent housekeeping at start | — | `maintenance.py` (data root 0750, sweep of interrupted-operation leftovers), `netguard.py` (source guard: instance networks, the pool, bridge gateways `AGENT_REFUSE_BRIDGE_GATEWAYS`), `backups.startup_unlock` (restic locks), saving-paused markers in `main.py`, `platformnet.py` (repair of a member of its own Compose project, never a one-off or game server, that lost a network or its aliases, stepwise reconnect, every 60 s, `AGENT_REPAIR_PLATFORM_NETWORKS`, `platform-networks.json`; game containers are repaired before a start in `docker_ops`) |
| Networks of the game servers (one bridge per server, A-1/I-6), host firewall | `agent_networks` in the instance answer (`instances/serializers.py`, from `nodes/client.py`; A-6); frontend `network/NetworkExposureNotice.tsx` | `pernet.py` (`zz_mcpanel_i_<id12>`, joined with gateway priority -1000 only while its server runs, `AGENT_INSTANCE_POOL`, `AGENT_PER_SERVER_NETWORKS`, `server-networks.json`, fallback to the shared network); `deploy/unraid/instance-firewall.sh` (MCPANEL-FWD/-IN, tests `tests/test_deploy_instance_firewall.py`) |
| Sessions bound to the tab (D-1, I-7), sign-in limits (D-3) | `apps/core/session_binding.py` (`MCPANEL_SESSION_BINDING`, header `X-MCPanel-Session-Token`, `POST /api/auth/download-ticket`, WebSocket auth frame, known-browser cookies, `manage.py unlock_login`); frontend `lib/api.ts`; CI `.github/scripts/upgrade-rehearsal.sh` sends the token | — |
| Agent capabilities for mixed versions | `nodes/client.py` (`features`, `supports`), `spec.applied_container_format` | `main.py` (`FEATURES`, `GET /v1/info`) |
| Upgrade, rollback, database restore | `deploy/scripts/upgrade.sh` (state in `deploy/upgrade-state.env`; `--archive-other-games`), `manage.py rollback_other_games`, `instances/migrations/0007_restore_other_games.py`, `instances/migrations/_other_games_guard.py` (0005/0006 refuse to be undone while other games are not archived); tests `tests/test_upgrade_script.py` + `tests/upgrade_shim/docker`, `tests/test_rollback_other_games.py`, `.github/scripts/upgrade-rehearsal.sh`; a checkout that group or others can write: `upgrade.sh` refuses (`check_modes`), `install-hook.sh` and `backup-db.sh` tighten it themselves as root, else refuse (tests `tests/test_upgrade_script_permissions.py`, `tests/test_deploy_permissions.py`) | — |
| Diagnostics / manual downloads | `apps/instances/diagnostics.py` | (reads logs/files) |
| Modpacks (Modrinth/CurseForge/FTB), preflight, pinning, update/rollback | `apps/modpacks/{providers,mrpack,pinning,handlers}.py` | — |
| Mods/plugins (Modrinth + Hangar + SpigotMC via Spiget) | `apps/content/{service,hangar_source,spigot_source}.py`, `apps/content/packfiles.py` (pack-managed jars, `*_IGNORE_MISSING_FILES`), `apps/modpacks/{hangar,spiget}.py` | `fetch.py`, `hashcache.py`, `/files/hashes` (null hash above 2 GiB, 503 `hashing_busy`) |
| Backups (restic), backup browser, retention | `apps/backups/{handlers,retention,views}.py`; `manage.py adopt_backups` (rows for snapshots the database does not know); `deploy/unraid/backup-db.sh` (daily database dump) | `backups.py` |
| Backup safeguards (notice, nightly schedule) | `apps/instances/services.py` (`add_nightly_backup_schedule`, `nightly_backup_cron`, `backup_overview_annotations`), `serializers.py` (`last_backup_at`, `backup_schedules`, `nightly_backup`), `manage.py ensure_backup_schedules`; frontend `lib/backup-health.ts`, `components/BackupNotice.tsx` | — |
| Snapshots (btrfs, ZFS) | `apps/backups/handlers.py` (snapshot_*), schedules in `apps/instances/scheduling.py` | `snapshots.py` (`delete_support`: probes whether deletes are `native` or file-by-file `fallback`), `zfs.py` |
| Import / export (archives) | `apps/instances/views.py` (Import/Extract/Archive) | `archive.py` |
| Power warnings / update checks | `apps/instances/power_warnings.py` (in-game countdown before restart/stop), `apps/modpacks/{updates,tasks}.py` (periodic modpack update checks) | (RCON via `rcon.py`) |
| World import / export (level-name, Bukkit dims, replace + rollback) | `apps/instances/{world,views,handlers}.py` (`world_replace` job) | `world.py` |
| World reset (plan + blockers, verified safety backup, seed, delete + undo; UI: Settings → Danger zone, `frontend/src/pages/instance/settings/reset-world-*`) | `apps/instances/world.py` (`reset_plan`), `views.py` (`WorldResetView`), `handlers.py` (`world_reset` job); contract tests `tests/test_world_reset_contract.py` | `world.py` (`classify`, `reset`), `POST /world/reset` |
| Metrics + player history | `apps/metrics` (players by status ping, RCON `list` only for older agents) | `/stats`, `/ping` (`slp.py`) |
| Console | `apps/console/consumers.py` (typed `stop` becomes the stop job + a notice; tasks cleaned up in `finally`; events group renewed hourly) | log stream (`main.stream_logs`) |
| Frontend resilience | `components/RouteError.tsx`, `lib/chunk-reload.ts`, `ConnectionLostBanner` in `components/States.tsx`, `lib/clipboard.ts`, `lib/batches.ts` | — |
| Notifications | `apps/notifications` | — |
| Nodes (multi-host) | `apps/nodes` | one agent per host |

## Conventions

* **No mock functionality in product paths.** If something is not implemented, the UI says so.
* Every state-changing long operation is a `Job` (`apps/jobs/runner.py`: `@register(kind)`, `enqueue()`);
  exclusive jobs lock the instance. Handlers live next to their domain (`apps/instances/handlers.py`,
  `apps/backups/handlers.py`) and are imported in the app's `ready()`.
* Permission checks: `apps.core.permissions.get_instance_for(user, pk, "instance.<code>")` for instance
  endpoints (404 if invisible, 403 if lacking the code), `require(user, code)` for global ones. New codes go
  in `apps/accounts/permissions.py`.
* Errors: raise `apps.core.errors.ApiError(message, code=..., status_code=...)`; the frontend shows
  `error.message` and field errors. Agent errors surface as `AgentError` with the agent's code.
* Audit every mutation with `apps.audit.service.record(...)` — never put secrets in `details`.
* The agent re-validates everything; never rely on the panel for agent-side safety.
* itzg env is built only in `apps/instances/spec.py`; keys in `MANAGED_ENV` cannot be overridden by users.
  Other games: the environment comes from the definition (`game_env`, `allowed_user_env`) and the agent's
  profile re-checks it (allow-list with value patterns).
* A new game is a definition (panel) plus a runtime profile (agent) plus tests; ARCHITECTURE.md "Adding a
  game" lists the steps. Never add keys to a Minecraft spec (it would make every server read restart-required).
* Frontend: API calls via `src/lib/api.ts`, query keys in `src/lib/queries.ts`, types in `src/lib/types.ts`,
  shared UI in `src/components/`, instance pages get `{instance, can}` from `useInstanceContext()`.
* Keep `DEVELOPMENT_STATUS.md` and `ROADMAP.md` updated when a feature lands; commit often.

* Security audit: on hold in session 1 at the user's request; the user asked to start it on 2026-11-06
  (session 2). Results and open findings are tracked in DEVELOPMENT_STATUS.md, also for the audit of
  2026-11-09 (work lists in `audit-2026-11-09/briefs/` next to the checkout, outside the repository).

## Security rules (non-negotiable)

* Never mount the Docker socket into the panel/worker; never publish the agent port.
* Never accept host paths, image names outside the allow-list, privileged flags, capabilities or
  devices from users.
* Never run shell commands built from user input (agent uses argv lists only).
* Never log credentials, tokens, RCON passwords or API keys.
* Destructive operations require explicit confirmation and are audited; restores take a safety backup
  by default; deletes of a server require typing its name.
