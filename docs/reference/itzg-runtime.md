# itzg/minecraft-server runtime reference

Verified 2026-11-03 against `master` of itzg/docker-minecraft-server: docs at `docs/**/*.md` (the nav config moved from `mkdocs.yml`, which is now 404, to `zensical.toml`; the page list comes from `https://docker-minecraft-server.readthedocs.io/en/latest/sitemap.xml`), plus `Dockerfile`, `scripts/start*`, `files/property-definitions.json`, `images.json`, and mc-image-helper (`main`) sources. "script:" marks something read from the startup scripts rather than the prose docs. **UNVERIFIED** marks anything I could not confirm.
Image at the time of checking: latest release `2026.10.1` (Docker Hub). Dockerfile pins mc-image-helper 1.75.0, mc-server-runner 1.16.2, mc-monitor 1.18.3, rcon-cli 1.7.9.

## 1. Container basics
| Item | Value |
|---|---|
| Entrypoint | `/image/scripts/start` (legacy shim `/start`); `WORKDIR /data`; `VOLUME ["/data"]`; `STOPSIGNAL SIGTERM` |
| Dockerfile ENV | `TYPE=VANILLA VERSION=LATEST EULA="" UID=1000 GID=1000` |
| Ports | 25565/tcp game (only port `EXPOSE`d); 25575/tcp RCON (`RCON_PORT`, in-container only, don't publish); query is UDP `QUERY_PORT` (default 25565) when `ENABLE_QUERY=true`; JMX 7091 (`ENABLE_JMX`, `JMX_HOST`, `JMX_PORT`) |
| HEALTHCHECK | `--start-period=2m --retries=2 --interval=30s CMD mc-health` (Dockerfile) |
| Required | `EULA=TRUE`. script: checked only when `/data/eula.txt` is missing; writes `eula=true` (lowercased). If missing, logs `[init] Please accept the Minecraft EULA at ...` and exits 1. `EULA` cannot come from `LOAD_ENV_FROM_*`. |
| UID/GID | default 1000/1000. script: the container starts as root, runs `usermod`/`groupmod` on user `minecraft`, `chown -R /data` if the owner differs (`SKIP_CHOWN_DATA=true` skips this), then `gosu`. Running with `--user` or `SKIP_SUDO=true` skips switching. `UMASK` defaults to `0002`. UID 0 runs as root. |
| Other | `TZ` (default UTC), `DEBUG=true` (startup trace), `DEBUG_MEMORY`, `PROXY`/`PROXY_HOST`/`PROXY_PORT`/`PROXY_NON_PROXY_HOSTS`, `PREFER_IPV6` |

## 2. Image tags and Java versions
Tags (docs + `images.json` at `https://raw.githubusercontent.com/itzg/docker-minecraft-server/refs/heads/master/images.json`, a JSON list of `{tag, java, distribution, jvm, architectures, lts?, deprecated?, jdk?, notes?}`):

| Tag | Java | Notes |
|---|---|---|
| `latest` | 25 | tracks newest commit; moves to the newest Java that Mojang requires |
| `stable` / `stable-<variant>` | 25 | tracks newest release |
| `java25`, `java25-alpine`, `java25-jdk` | 25 | `java25-graalvm` is deprecated in images.json |
| `java21`, `java21-alpine`, `java21-jdk` | 21 | `java21-graalvm` is deprecated in images.json |
| `java17` | 17 | `java17-graalvm` exists; `java17-alpine` is deprecated |
| `java16`, `java11`, `java8` | 16/11/8 | pre-17 tags pin an old mc-image-helper with **no bundled CurseForge API key**, so set `CF_API_KEY` yourself |
| `<release>-<java tag>` | — | pinned build, e.g. `2026.10.1-java21`, `2024.4.0-java17` |

Deprecated (from docs): adopt13/14/15, openj9-*, multiarch-latest, java19, java20*, java23-*, java24*, java8-alpine/-jdk/-openj9/-graalvm-ce, java17-graalvm-ce. GraalVM (Oracle Linux) tags can't run the Forge installer. FTBA doesn't work on Alpine.

Java required per MC version (docs): the docs point to the Minecraft wiki. Forge below 1.18 **must** use `java8`. Some 1.21-era mods still need `java17`. The error "class file version 65.0" means Java 21 is needed. The actual minimum Java per version comes from Mojang's piston-meta `javaVersion.majorVersion`, which I queried (`https://piston-meta.mojang.com/mc/game/version_manifest_v2.json`, then each version's JSON):

| MC | 1.16.5 | 1.17 | 1.18 | 1.20.4 | 1.20.5 | 1.21.11 | 26.1 | 26.3 |
|---|---|---|---|---|---|---|---|---|
| majorVersion | 8 | 16 | 17 | 17 | 21 | 21 | 25 | 25 |

`"javaVersion": {"component": "java-runtime-delta", "majorVersion": 21}` (1.20.5). The manifest's `latest` was `{"release": "26.3", "snapshot": "26.4-snapshot-7"}`.

## 3. VERSION and TYPE
`VERSION`: `LATEST` (default), `SNAPSHOT`, an exact version (`1.21.4`, `25w35a`, `1.21.11-rc1`, new scheme `26.1`, `26.1-snapshot-1`, `26.1-pre-1`, `26.1-rc-1`, `b1.7.3`). With LATEST/SNAPSHOT, a restart upgrades the server. Old server jars stay in /data and can be deleted. script: `DECLARED_VERSION`/`DECLARED_TYPE` keep the original values.

Accepted `TYPE` values (script `start-configuration` case list): `VANILLA, BUKKIT, SPIGOT, PAPER, FOLIA, FORGE, CLEANROOM, NEOFORGE (alias NEOFORGED), FABRIC, QUILT, SPONGEVANILLA, CUSTOM, MAGMA, MAGMA_MAINTAINED, KETTING, MOHIST, YOUER, BANNER, PURPUR, PUFFERFISH, CANYON, LIMBO, NANOLIMBO, PUMPKIN, CRUCIBLE, LEAF, ARCLIGHT, POSEIDON`. Modpack platforms: `AUTO_CURSEFORGE, MODRINTH, CURSEFORGE, FTBA, GTNH` (`FTB` is an alias for legacy `CURSEFORGE`). These go in `MODPACK_PLATFORM` (alias `MOD_PLATFORM`) or in `TYPE`. Setting both TYPE and MODPACK_PLATFORM logs a warning. An invalid value logs `Invalid TYPE: '...'` and exits 1.

| TYPE | Version vars (defaults from scripts) | Notes |
|---|---|---|
| PAPER | `PAPER_BUILD` (alias `PAPERBUILD`; empty = latest), `PAPER_CHANNEL=default` (or `experimental`), `PAPER_DOWNLOAD_URL`, `PAPER_CUSTOM_JAR`, `PAPER_PROJECT=paper` | Error `No build found for version 1.21 with channel 'default'` means you need experimental. `CLEAN_SERVER_LIBRARIES=true` |
| PURPUR | `PURPUR_BUILD=LATEST`, `PURPUR_DOWNLOAD_URL` | also `USE_FLARE_FLAGS`, `USE_SIMD_FLAGS` |
| FOLIA / LEAF / PUFFERFISH | `FOLIABUILD`, `FOLIA_CHANNEL` / `LEAF_BUILD` / `PUFFERFISH_BUILD=lastSuccessfulBuild` | |
| FABRIC | `FABRIC_LOADER_VERSION=LATEST`, `FABRIC_LAUNCHER_VERSION=LATEST` (alias `FABRIC_INSTALLER_VERSION`), `FABRIC_LAUNCHER`, `FABRIC_LAUNCHER_URL`, `FABRIC_FORCE_REINSTALL`, `FABRIC_META_BASE_URL=https://meta.fabricmc.net` | most mods need `MODRINTH_PROJECTS: fabric-api` |
| FORGE | `FORGE_VERSION=RECOMMENDED` (alias `FORGEVERSION`; `latest` or exact e.g. `14.23.5.2854`), `FORGE_INSTALLER`, `FORGE_INSTALLER_URL`, `FORGE_FORCE_REINSTALL`, `FORGE_PROMOTIONS_URL`, `FORGE_MAVEN_REPO_URL` | starts through Forge's `run.sh` when it exists |
| NEOFORGE | `NEOFORGE_VERSION=latest` (or `beta`, or exact e.g. `47.1.79`), `NEOFORGE_INSTALLER`, `NEOFORGE_FORCE_REINSTALL` | default is the latest non-beta |
| QUILT | `QUILT_LOADER_VERSION`, `QUILT_INSTALLER_VERSION`, `QUILT_LAUNCHER` (deprecated), `QUILT_INSTALLER_URL` | script: `QUILT_LAUNCHER_URL` is no longer supported |
| CLEANROOM | `CLEANROOM_LOADER_VERSION`, `CLEANROOM_INSTALLER_VERSION` | 1.12.2 only; `VERSION` is ignored. `APPLY_CLEANROOM=true` applies it to Forge-like types |
| CUSTOM | `CUSTOM_SERVER` (URL or path; a URL is downloaded only once), `CUSTOM_JAR_EXEC` (`-cp ... Main` or `-jar x.jar`) | `MODRINTH_PROJECTS` needs `MODRINTH_LOADER` |
| Hybrids | `MOHIST_BUILD`, `MAGMA_MAINTAINED_TAG`+`FORGE_VERSION`, `KETTING_VERSION`, `ARCLIGHT_TYPE` | |
| BUKKIT/SPIGOT | `BUKKIT_DOWNLOAD_URL`, `SPIGOT_DOWNLOAD_URL` | getbukkit no longer allows automated downloads; docs recommend Paper |

`FORCE_REDOWNLOAD=true` re-downloads the server jar (VANILLA, FORGE, BUKKIT, SPIGOT, PAPER, CURSEFORGE, SPONGEVANILLA).

## 4. Modrinth modpacks (`TYPE=MODRINTH`)
| Var | Meaning (script defaults) |
|---|---|
| `MODRINTH_MODPACK` (alias `MODRINTH_PROJECT`) | **required**: slug, project ID, project or version page URL, `.mrpack` URL, or a local path (must end in `.mrpack`) |
| `MODRINTH_VERSION` (alias `MODRINTH_VERSION_ID`) | pins a version ID or version number. A version page URL in `MODRINTH_MODPACK` also pins |
| `MODRINTH_LOADER` | `forge`, `fabric`, `quilt` (mc-image-helper also accepts `neoforge`) |
| `MODRINTH_MODPACK_VERSION_TYPE` | `release`/`beta`/`alpha` (optional `+` suffix). Default `release` for VERSION=LATEST or exact, `beta` for SNAPSHOT (legacy alias `MODRINTH_DEFAULT_VERSION_TYPE`) |
| `VERSION` | exact MC version is passed as `--game-version` to narrow the search. LATEST/SNAPSHOT means any version |
| `MODRINTH_EXCLUDE_FILES` / `MODRINTH_FORCE_INCLUDE_FILES` | comma/newline list; case-insensitive substring, or `/regex/`; `#` comments allowed |
| `MODRINTH_DEFAULT_EXCLUDE_INCLUDES` | default `/image/modrinth-exclude-include.json`. Set to `""` to disable |
| `MODRINTH_OVERRIDES_EXCLUSIONS` | ant-style paths (`*`, `**`, `?`) relative to /data, skipped when extracting `overrides/` |
| `MODRINTH_IGNORE_MISSING_FILES` | files whose absence shouldn't trigger a reinstall |
| `MODRINTH_FORCE_SYNCHRONIZE`, `MODRINTH_FORCE_MODLOADER_REINSTALL` (alias `FORCE_MODLOADER_REINSTALL`) | re-evaluate everything / reinstall the loader (read from env by mc-image-helper) |

Upgrades: with no pin, each start picks the newest matching file. Upgrading or downgrading installs the matching loader and deletes files from the old version (they're listed in the manifest). `REMOVE_OLD_MODS` is force-disabled with a warning. Manifest at `/data/.modrinth-modpack-manifest.json` has fields `@type, timestamp, files[], projectSlug, versionId, dependencies{}`. The results env file is `/data/.install-modrinth.env` (includes `SERVER`). The `MODPACK_NAME` and `MODPACK_VERSION` placeholders are available.

## 5. AUTO_CURSEFORGE
| Var | Meaning (script defaults) |
|---|---|
| `CF_API_KEY` / `CF_API_KEY_FILE` | a key is bundled on Java 17+ images; set your own on pre-17 images. Escape `$` as `$$` in compose |
| `CF_PAGE_URL` / `CF_SLUG` | modpack page URL (a file URL pins), or the slug |
| `CF_FILE_ID`, `CF_FILENAME_MATCHER` (substring, or `/regex/`) | pin. Don't pick a "server pack" file (it has no manifest). Pinning the pack also pins the loader |
| `CF_MOD_LOADER_VERSION` | override the loader version the pack declares |
| `CF_EXCLUDE_MODS` / `CF_FORCE_INCLUDE_MODS` | project slugs or IDs (comma/space/newline, `#` comments). Force-include doesn't download extra mods. `CF_EXCLUDE_ALL_MODS=true` |
| `CF_EXCLUDE_INCLUDE_FILE` | default `/image/cf-exclude-include.json`. `""` disables it |
| `CF_FORCE_SYNCHRONIZE=false` | re-evaluate excludes/includes and overrides exclusions on an existing install |
| `CF_OVERRIDES_EXCLUSIONS`, `CF_OVERRIDES_SKIP_EXISTING=false`, `CF_IGNORE_MISSING_FILES`, `CF_SET_LEVEL_FROM` (`WORLD_FILE`/`OVERRIDES`) | |
| `CF_DOWNLOADS_REPO` | default `/downloads` if that directory exists, otherwise disabled. Subdirectories `mods/`, `modpacks/`, `worlds/` are also checked |
| `CF_MODPACK_ZIP`, `CF_MODPACK_MANIFEST` | unpublished pack (still needs a slug/URL) |
| `CF_PARALLEL_DOWNLOADS=4`, `CF_FORCE_REINSTALL_MODLOADER=false`, `CF_API_CACHE_DEFAULT_TTL` (ISO-8601, mc-image-helper default `P2D`), `CF_USE_HTTP2=false` | |

Mods whose authors don't allow distribution: mc-image-helper logs `WARN The authors of the mod '<name>' have disallowed project distribution. Manually download the file '<display>' from <url> and supply via downloads repo or separately.` It then prints "Some mod authors disallow automated downloads. The following need to be manually downloaded into the repo or excluded:" and a table (`Mod | Version Name | Filename | Download page`, where the page is `<websiteUrl>/download/<fileId>`), and writes that table to `/data/MODS_NEED_DOWNLOAD.txt`. The helper exits non-zero, so the script logs `Failed to auto-install CurseForge modpack` and **the container exits 1**. To fix: put the files in `/downloads` (download them with a browser), or exclude the mods. The log line `Mod file <path> obtained from downloads repo` confirms the pickup. Other log lines: `Requested CurseForge modpack <name> is already installed`, `Re-installing due to missing files from modpack: [...]`. Manifest at `/data/.curseforge-manifest.json` has fields `timestamp, files[], modpackName, modpackVersion, slug, modId, fileId, fileName, minecraftVersion, modLoaderId, levelName`. Results file `/data/.install-curseforge.env`. World data is never auto-removed.

## 6. FTBA (`TYPE=FTBA`)
`FTB_MODPACK_ID` (required, numeric), `FTB_MODPACK_VERSION_ID` (optional; when absent the latest is used, which upgrades on restart), `FTB_FORCE_REINSTALL`, `FTB_BASE_URL=https://api.feed-the-beast.com/v1/modpacks`. script: the latest version is `jq '.versions | sort_by(.updated)[-1].id'` from `GET {FTB_BASE_URL}/public/modpack/{id}`, and the installer comes from `.../public/modpack/1/1/server[/arm]/linux`. I checked `GET .../public/modpack/119`, which returned 200 with keys `id,name,slug,status,type,updated,versions[...]`. Each version is `{"id":100051,"name":"1.16.1","type":"release","updated":1741881037,"targets":[{"name":"minecraft","version":"1.20.1","type":"game"},{"name":"neoforge","version":"47.1.84","type":"modloader"},{"name":"java","version":"17.0.7+7","type":"runtime"}]}`. Install marker `/data/.ftb-installed` contains `ID=VERSIONID` (it reinstalls when that changes). Manifest `/data/.manifest.json` has `.name, .versionName, .modPackTargets.modLoader.{name,version}, .modPackTargets.mcVersion`. Log lines: `Installing modpack ID X, version ID Y` / `... is ready to go`. Ubuntu/Hotspot images only.

## 7. Individual mods/plugins
- `MODRINTH_PROJECTS`: comma/newline list of entries in the forms `[prefix:]project[?][:version|:release|beta|alpha[+]]` or `@/listing/file`. Prefixes: `datapack`, `fabric`, `forge`, `paper`. A trailing `?` marks a project optional (a warning instead of a failure). Related vars: `MODRINTH_DOWNLOAD_DEPENDENCIES=none|required|optional`, `MODRINTH_PROJECTS_DEFAULT_VERSION_TYPE=release`, `VERSION_FROM_MODRINTH_PROJECTS=true` (the image resolves VERSION itself and logs `Resolved Minecraft version X from Modrinth projects`). Removing an entry deletes its file; an empty value removes everything it managed. Manifest at `/data/.modrinth-manifest.json` (exact fields **UNVERIFIED**).
- `CURSEFORGE_FILES`: slug, ID, page/file URL, `slug:fileId`, `slug@partialFilename`, `@listing`. The same auto-cleanup applies. It doesn't resolve dependencies.
- `MODS` / `PLUGINS`: URLs, jar paths, or directories. Removing an entry removes the file. `MODS_FILE` / `PLUGINS_FILE` take a listing file (`#` comments allowed).
- `SPIGET_RESOURCES` (SpigotMC numeric IDs, e.g. `28140,34315`), `MODPACK` (zip of jars), `GENERIC_PACK(S)` (+`_PREFIX`/`_SUFFIX`, `oci://`), `PACKWIZ_URL`, `DATAPACKS` (go to `/data/$LEVEL/datapacks`), `APPLY_EXTRA_FILES` / `DOWNLOAD_EXTRA_CONFIGS` (`dest<src`), `WORLD` (URL/zip/dir; `FORCE_WORLD_COPY`), `LOAD_ENV_FROM_FILE` / `_ARCHIVE` / `_GENERIC_PACK` (these **override** `-e` values).
- `REMOVE_OLD_MODS=true` deletes mods/plugins before copying or installing (skipped when `MODS_FILE` is set). Also `REMOVE_OLD_MODS_INCLUDE` (script default `*.jar,*-version.json`), `REMOVE_OLD_MODS_EXCLUDE`, `REMOVE_OLD_MODS_DEPTH` (docs say 16, but the script default is `1`: a conflict). `REMOVE_OLD_CONFIGS` (+`_INCLUDE=*`, `_DEPTH=16`, `CONFIG_OUT_DIR=/data/config`).
- Mount points: `/plugins` is copied into `/data/plugins`, `/mods` into `/data/mods`, `/config` into `/data/config` (`COPY_{PLUGINS,MODS,CONFIG}_{SRC,DEST}`, `USES_PLUGINS`, `SYNC_SKIP_NEWER_IN_DESTINATION`). Env interpolation runs on synced files with suffixes in `REPLACE_ENV_SUFFIXES` (`REPLACE_ENV_DURING_SYNC=false` disables it).

## 8. Memory and JVM
- `MEMORY=1G` sets both `-Xms` and `-Xmx`. `INIT_MEMORY` / `MAX_MEMORY` override each one (units `g/G/m/M/k/K`, or `N%`, which becomes `-XX:InitialRAMPercentage` / `-XX:MaxRAMPercentage`). `MEMORY=""` lets the JVM decide (25% of the container limit). Allow ~25% headroom in the container memory limit. Log line: `[init] Setting initial memory to 1G and max to 4G`.
- `JVM_OPTS` (space-separated), `JVM_XX_OPTS` (`-XX` flags, placed first), `JVM_DD_OPTS` (`a=b,c:d` becomes `-Da=b -Dc=d`), `EXTRA_ARGS` (after the jar), `JAVA_AGENTS` / `JVM_AGENTS` (copied to `/data/agents/{java,jvm}`).
- `USE_AIKAR_FLAGS=true` (adjusted for MEMORY≥12G), `USE_MEOWICE_FLAGS=true` (Java 17+; `USE_MEOWICE_GRAALVM_FLAGS`; the docs disagree on its default), `USE_FLARE_FLAGS`, `USE_SIMD_FLAGS`. Final command: `mc-server-runner [args] -- java $JVM_XX_OPTS $JVM_OPTS -D... -jar <server> $EXTRA_ARGS`. `SERVER_RUNNER` replaces mc-server-runner. `PRE_START_SCRIPT` runs just before launch, and a non-zero exit stops startup.

## 9. server.properties
- Defaults: `OVERRIDE_SERVER_PROPERTIES=true`, `SKIP_SERVER_PROPERTIES=false`, `SERVER_PROPERTIES=/data/server.properties`.
- script flow: if the file is missing, log `Creating server properties in ...`, default MOTD to `A <Type> Minecraft Server powered by Docker`, and apply the env vars. If it exists and OVERRIDE is true, apply the env vars **on every start**. If OVERRIDE is false, log `server.properties already created and managed manually`. If SKIP is true, log `Skipping setup of server.properties`.
- **Only variables that are set get written.** An unset variable leaves the existing value alone. An empty string clears the property. So a hand edit to a property that also has an env var set is reverted on the next start; other hand edits survive. Values are written with `mc-image-helper set-properties --definitions /image/property-definitions.json`.
- Placeholders in values: `%VAR%`, `%env:VAR%`, `%date:FMT%`, plus `MODPACK_NAME`, `MODPACK_VERSION`, `DECLARED_VERSION`. They don't work when OVERRIDE is false. `CUSTOM_SERVER_PROPERTIES` takes newline-separated `k=v` lines. `SERVER_PROPERTIES_ESCAPE_UNICODE` (escaping is automatic below 1.20). `SERVER_PROPERTIES_REPO_URL` downloads a base file.
- `DUMP_SERVER_PROPERTIES=true` logs `[init] DEBUG Dumping server.properties` followed by the whole file, before launch.
- Env-to-property mapping (from `property-definitions.json`, 75 entries): `MOTD`→motd (literal `\n` becomes a newline), `DIFFICULTY`→difficulty (peaceful/easy/normal/hard or 0-3; docs default easy), `MODE`→gamemode (survival/creative/adventure/spectator, prefixes or 0-3; numeric below 1.13), `LEVEL`→level-name (default world), `SEED`→level-seed (quote negative seeds), `LEVEL_TYPE`, `GENERATOR_SETTINGS`, `MAX_PLAYERS`, `ONLINE_MODE`, `PVP`, `VIEW_DISTANCE`, `SIMULATION_DISTANCE`, `SPAWN_PROTECTION`, `ALLOW_FLIGHT`, `ALLOW_NETHER`, `HARDCORE`, `FORCE_GAMEMODE`, `ENABLE_COMMAND_BLOCK`, `MAX_TICK_TIME`, `PAUSE_WHEN_EMPTY_SECONDS`, `PLAYER_IDLE_TIMEOUT`, `OP_PERMISSION_LEVEL`, `ENFORCE_SECURE_PROFILE`, `ENFORCE_WHITELIST`, `SERVER_PORT`→server-port, `SERVER_IP`→server-ip, `ENABLE_QUERY`/`QUERY_PORT`, `ENABLE_RCON`→enable-rcon, `RCON_PASSWORD`→rcon.password, `RCON_PORT`→rcon.port, `RESOURCE_PACK`/`_SHA1`/`_ID`/`_PROMPT`, `RESOURCE_PACK_ENFORCE`→require-resource-pack, `ENABLE_JMX`→enable-jmx-monitoring, `INITIAL_ENABLED_PACKS`/`INITIAL_DISABLED_PACKS`, `MANAGEMENT_SERVER_*`, `WHITELIST_PROP`→white-list (internal), and others (`SYNC_CHUNK_WRITES`, `NETWORK_COMPRESSION_THRESHOLD`, `RATE_LIMIT`, `LOG_IPS`, `HIDE_ONLINE_PLAYERS`, `ENABLE_STATUS`, `SPAWN_ANIMALS`/`MONSTERS`/`NPCS`, `MAX_WORLD_SIZE`, `MAX_BUILD_HEIGHT`, `MAX_CHAINED_NEIGHBOR_UPDATES`, `ENTITY_BROADCAST_RANGE_PERCENTAGE`, `FUNCTION_PERMISSION_LEVEL`, `GENERATE_STRUCTURES`, `BROADCAST_CONSOLE_TO_OPS`, `BROADCAST_RCON_TO_OPS`, `ACCEPTS_TRANSFERS`, `REGION_FILE_COMPRESSION`, `STATUS_HEARTBEAT_INTERVAL`, `USE_NATIVE_TRANSPORT`, `SERVER_NAME`, `PREVENT_PROXY_CONNECTIONS`, `PREVIEWS_CHAT`, `SNOOPER_ENABLED`, `ANNOUNCE_PLAYER_ACHIEVEMENTS`, `BUG_REPORT_LINK`).
- Icon: `ICON` (URL or path; converted to a 64x64 PNG), `OVERRIDE_ICON=false`.

## 10. Whitelist and ops
- `WHITELIST` / `OPS`: usernames or UUIDs, comma or newline separated. `WHITELIST_FILE` / `OPS_FILE`: URL or path to a JSON file. Lookups use PlayerDB by default (`USER_API_PROVIDER=mojang` switches to Mojang). Use Java profile names, not Xbox gamertags.
- script: if `WHITELIST`, `WHITELIST_FILE`, or `ENABLE_WHITELIST=true` is set, it writes white-list=true **and** enforce-whitelist=true and logs `Enabling whitelist functionality`. On first setup, or with `ENABLE_WHITELIST=false`, it writes white-list=false. Otherwise it leaves them alone.
- `EXISTING_WHITELIST_FILE` / `EXISTING_OPS_FILE` (default `SYNC_FILE_MERGE_LIST`): `SKIP` = leave an existing file alone. `SYNCHRONIZE` = make the file exactly match the list or file (for ops, `level` and `bypassesPlayerLimit` are kept). `MERGE` = add list entries to the existing file (not allowed with `*_FILE`). `SYNC_FILE_MERGE_LIST` = `*_FILE` overwrites the file, then list entries are merged in. Legacy: `OVERRIDE_WHITELIST` / `OVERRIDE_OPS` (true = SYNCHRONIZE, false = SKIP), `APPEND_*` (= MERGE). This runs every start (step `start-setupRbac`), so with the default mode, players added in game stay (merge) unless a `*_FILE` is set.

## 11. RCON and console
- `ENABLE_RCON=true` (default). `RCON_PORT=25575`; don't change `rcon.port` in the file directly. `RCON_PASSWORD`: **a new random one each start** (`openssl rand -hex 12`) unless set. `RCON_PASSWORD_FILE` overrides it (missing file means exit 1). script: the password is written to `/data/.rcon-cli.env` (`password=<pw>`) and `/data/.rcon-cli.yaml` (`password: "<pw>"`), which is how `rcon-cli` authenticates (`HOME=/data`). The panel can read the password from those files. Inferred, **UNVERIFIED**: with OVERRIDE=false and no fixed password, server.properties keeps the old password while rcon-cli gets a new one.
- `docker exec mc rcon-cli <cmd...>` runs one command (`docker exec -i mc rcon-cli` is interactive). Without RCON: set `CREATE_CONSOLE_IN_PIPE=true` (named pipe `CONSOLE_IN_NAMED_PIPE=/tmp/minecraft-console-in`), then `docker exec --user 1000 mc mc-send-to-console <cmd...>`. That must run as `$UID`; it errors if the pipe is disabled or missing.
- Interactive or color console needs RCON plus `tty: true` and `stdin_open: true`. Attach with `docker attach` and detach with Ctrl-P Ctrl-Q. TTY can make the logs messy.
- `RCON_CMDS_STARTUP`, `_ON_CONNECT`, `_FIRST_CONNECT`, `_ON_DISCONNECT`, `_LAST_DISCONNECT` (newline-separated commands; script `RCON_CMDS_PERIOD=10`). `BROADCAST_RCON_TO_OPS=false`. SSH and websocket consoles exist (`ENABLE_SSH`, which turns on mc-server-runner `--remote-console`).

## 12. Health, stop, autopause/autostop, setup-only
- `mc-health` runs `mc-monitor status --host ${SERVER_HOST:-localhost} --port ${SERVER_PORT:-25565}`. With `DISABLE_HEALTHCHECK=true` it prints `Healthcheck disabled` and exits 0. When autopaused it prints `Java process suspended by Autopause function` and exits 0. Versions below b1.8 disable the check; legacy pings are used below 1.7; `--use-proxy` is added when `USES_PROXY_PROTOCOL` is set (all via `/data/.mc-health.env`). Status check: `docker inspect -f '{{.State.Health.Status}}' mc`. Compose override example: `healthcheck: {test: mc-health, start_period: 1m, interval: 5s, retries: 20}`.
- Stop: on SIGTERM, mc-server-runner sends `stop` (via RCON or console) and waits `STOP_DURATION` (default 60 s) before killing. **Docker's own stop timeout must be longer** (`docker stop -t`, `stop_grace_period`); exit 137 means it was killed. `STOP_SERVER_ANNOUNCE_DELAY=<s>` announces with `say` and then waits. `STOP_SERVER_DELAY_COMMAND` replaces the announcement. SIGUSR1 skips the delay. Runner log lines: `gracefully stopping server...`, `Sending 'stop' to Minecraft server...`, `Waiting for completion...`, `Took too long, so killing server process`, `Done`. On a crash: `Minecraft server failed. Inspect logs above for errors that indicate cause. DO NOT report this line as an error.` with `exitCode`; for 137 it adds `Exit code 137 usually indicates the process was killed due to excessive memory use.`
- Autopause: `ENABLE_AUTOPAUSE=false`. Docs say it isn't recommended from 1.21.2 on; use `PAUSE_WHEN_EMPTY_SECONDS` instead. Vars `AUTOPAUSE_TIMEOUT_EST=3600`, `_INIT=600`, `_KN=120`, `AUTOPAUSE_PERIOD=10`, `AUTOPAUSE_KNOCK_INTERFACE=eth0`, `AUTOPAUSE_STATUS_RETRY_LIMIT=10`, `AUTOPAUSE_STATUS_RETRY_INTERVAL=2s`, `DEBUG_AUTOPAUSE`. script: sets `MAX_TICK_TIME=-1` when unset. Marker files `/data/.paused` (while paused) and `/data/.skip-pause`. Rootless needs `CAP_NET_RAW`.
- Autostop: `ENABLE_AUTOSTOP=false`, `AUTOSTOP_TIMEOUT_EST=3600`, `AUTOSTOP_TIMEOUT_INIT=1800`, `AUTOSTOP_PERIOD=10`, `DEBUG_AUTOSTOP`, `/data/.skip-stop`. Use `restart: no`. It can't be combined with autopause (startup error, exit 1).
- `SETUP_ONLY=true`: installs everything and prints `SETUP_ONLY: java <args>` (or `SETUP_ONLY: bash <run.sh>`) instead of launching.

## 13. Startup log format (`docker logs`)
Startup lines from the image are prefixed `[init] ` (with `LOG_TIMESTAMP=true` or DEBUG: `[init] 2022-02-05 16:58:33+00:00 ...`). Typical order (script strings):
```
[init] Running as uid=1000 gid=1000 with /data as 'drwxrwxr-x ...'
[init] Image info: buildtime=...,version=...,revision=...
[init] Resolving type given PAPER                      # skipped for modpack platforms
...mc-image-helper download/install output...          # exact format UNVERIFIED
[init] Successfully setup vanilla Minecraft version X  # VANILLA only
[init] Downloading world from ... / Copying any mods from /mods to /data/mods ...
[init] Creating server properties in /data/server.properties
[init] Setting initial memory to 1G and max to 1G
[init] Starting the Minecraft server...                # last [init] line; the java process starts here
```
Error lines go through `logError`/`logWarning` (colored, still prefixed `[init]`), e.g. `Failed to download ...`, `Failed to install ...`. After launch, the output is the server's own log4j output (`LOG_CONSOLE_FORMAT` default `[%d{HH:mm:ss}] [%t/%level]: %msg%n`; also `logs/latest.log`). The readiness line is the vanilla `Done (N.NNNs)! For help, type "help"` (the docs don't show it; **UNVERIFIED**; prefer the health status). Other log settings: `GENERATE_LOG4J2_CONFIG`, `LOG_LEVEL`, `ROLLING_LOG_*`.

## 14. /data layout (paths verified from docs and scripts)
| Path | Contents |
|---|---|
| `server.properties`, `eula.txt`, `ops.json`, `whitelist.json` (`white-list.txt`/`ops.txt` below 1.7.3), `server-icon.png` | core config |
| `$LEVEL/` (default `world/`, plus `world_nether`/`world_the_end` on Spigot-like types), `$LEVEL/datapacks/` | world data |
| `mods/`, `plugins/`, `config/` | mods (Forge/Fabric/NeoForge/Quilt), plugins (Paper/Spigot/Purpur), mod configs. Defaults are `MODS_OUT_DIR`, `PLUGINS_OUT_DIR`, `CONFIG_OUT_DIR` |
| `logs/latest.log`, `logs/*.log.gz` | server logs |
| `agents/java`, `agents/jvm`, `packs/` (`packs/oci`) | agents, generic packs |
| `.rcon-cli.env`, `.rcon-cli.yaml`, `.mc-health.env` | RCON creds, health args |
| `.install-modrinth.env`, `.install-curseforge.env`, `.paper.env`, `.generic_pack.sum` | helper results/state |
| `.modrinth-modpack-manifest.json`, `.curseforge-manifest.json`, `.modrinth-manifest.json`, `MODS_NEED_DOWNLOAD.txt` | modpack manifests, missing-mods report |
| `.ftb-installed`, `.manifest.json`, `ftb-installer-v2`, `ftb-installer.log` | FTBA |
| `.paused`, `.skip-pause`, `.skip-stop` | autopause/autostop markers |
| `run.sh`, `libraries/`, `*.jar` | server jars and loader files (Forge uses run.sh) |

Optional mount points: `/mods`, `/plugins`, `/config` (synced into /data), `/downloads` (CurseForge manual downloads), plus your own paths for `WORLD`, `MODS_FILE`, `@listing` files and secrets. Use `:Z` on volumes for SELinux or rootless Podman.
