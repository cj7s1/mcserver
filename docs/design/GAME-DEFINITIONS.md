# Design: game definitions (multi-game support)

Status: phases 1 to 3 (section 10) are **built**: Minecraft as the first definition, the generic
pieces, and Factorio (ARCHITECTURE.md, "Games"; DEVELOPMENT_STATUS.md, "Multi-game phases 2 and 3").
Where the real images differed from this proposal, the code follows the spike of 2026-11-10 (for
Factorio: the `-rootless` tag, readiness from a log line, the RCON password from a file). The rest
of this document is the proposal as it was reviewed, 2026-11-10. It is based on a
read-only map of the code at commit `082b949` and on research of each game's server software as it
is in November 2026. Facts about games come from their official docs, the image repositories and
SteamCMD documentation; points that could not be confirmed are marked *to confirm*. The research
notes with sources are kept next to the checkout, outside the repository
(`audit-2026-11-09/design/`).

Games in scope: **Minecraft** (today), **Factorio**, **Terraria** (vanilla, tModLoader, TShock),
**Satisfactory**, **Don't Starve Together** (DST) and **Stardew Valley**.

---

## 1. Summary

MCPanel can host these games without giving up what it does well (one container per running
server, the agent as the only holder of the Docker socket, jobs with locks, restic backups, the
shutdown hook, retiring stopped containers, the memory check, the web UI). About half of the code
is already game-neutral. The other half assumes Minecraft in specific, mostly well-contained places.

The design adds one concept with two halves:

* a **game definition** in the panel (Python): which variants and versions exist, how the
  container's environment and ports are built, which config files there are and how to edit them,
  which features apply (the *capabilities*), how a world is reset, how players are listed;
* a **runtime profile** in the agent (data shipped in the agent image, never sent by the panel):
  which images are allowed, the container user, capabilities, mount points, health probe, stop
  signal, and a **control driver** that knows how to send a command, save, stop and query that game.

Minecraft becomes the first definition and profile. Its container description must stay
byte-identical, so no existing server is recreated by the refactor.

Fit, from easiest to hardest:

| Game | Fit | Why |
|---|---|---|
| Factorio | very good | One container, one UDP port, RCON (the agent already speaks it), saves on exit, no account needed |
| Terraria (vanilla, tModLoader) | good | One container, one TCP port; commands go to the console, not RCON (new agent primitive) |
| Terraria (TShock) | good | Same, plus a REST API; different image |
| Satisfactory | moderate | One container but heavy (8-16 GB); controlled through an HTTPS API; must be *claimed* on first start; must follow the game's stable branch |
| DST | hard | A server with caves is two processes (Master + Caves); needs a Klei cluster token; long saves |
| Stardew Valley | hard, and questionable | No official dedicated server: the full game runs headless with mods; needs a Steam account that owns the game, Steam Guard, a sidecar container; grey area under Steam's terms |

**Recommendation:** refactor Minecraft into the first definition, then add Factorio, Terraria,
Satisfactory and DST in that order. Decide on Stardew after DST (section 11).

---

## 2. Goals and non-goals

Goals

1. One panel, one Unraid deployment (panel, agent, postgres), one container per running server
   (DST: one or two, section 7).
2. Every game gets the generic machinery: create, start/stop/restart with graceful save, retire
   when stopped, resume after host shutdown, memory admission, schedules, restic backups (and
   snapshots on btrfs/ZFS), file manager, console log, metrics, permissions, audit, notifications.
3. Each game gets what it really supports: commands, players, world reset, mods, settings forms;
   the UI shows only what applies.
4. The security model does not weaken: users still cannot choose images, mounts, capabilities or
   devices; the agent re-validates everything, now per game.
5. No behaviour change for the 8 existing Minecraft servers.

Non-goals (for this project)

* A generic "run any Docker image" feature. Every game is reviewed and added as code + profile.
* Running games as processes inside the panel container (loses per-server limits and isolation).
* Mod managers for every game at once. Mods come per game, later, and start as "upload a file".
* Steam account management beyond what a game needs (Stardew only).

---

## 3. What stays and what changes

**Already generic (unchanged):** the job system and its safety rules; apply / start_server / stop /
restart / recreate / archive / delete / migrate; retire and last-run records; host shutdown and
resume; crash-loop detection; restart-required hashing; memory admission (given a per-game memory
model); the agent's container replace sequence, per-server lock, file manager, hash-verified
fetch, restic, snapshots, clone, migration, netguard, TLS, maintenance sweep; accounts,
permissions, audit, notifications, nodes, schedules (except the `command` action), metrics
(CPU/memory/network); the frontend shell, file manager, backups, schedules, access and admin pages,
and the schema-driven properties form.

**Minecraft-only today, to be routed through the definition:**

| Area | Today | With definitions |
|---|---|---|
| Container description | `spec.build_spec` builds itzg env, image from Java version, port 25565 | `definition.build_spec(instance)`; Minecraft returns the identical dict |
| Agent container creation | `build_create_kwargs` always injects itzg env, `/data`, six caps, `mc-health`, SIGTERM | common kwargs + runtime profile |
| Commands, save, stop | RCON 25575, password read back from `RCON_PASSWORD`, `save-all flush`, `stop` courier, `save-off/on` around backups | control driver per protocol |
| Players | Minecraft server list ping, RCON `list` | driver `query()` / definition `players` adapter (optional) |
| Worlds | `level-name`, `server.properties`, vanilla/bukkit layouts | definition `world` model (optional) |
| Config editing | `server.properties` schema | definition `config_files` (several files, several formats) |
| Software/versions | catalog of Minecraft types and versions, Java selection | definition `variants` + `versions` (optional) |
| Mods/modpacks | Modrinth, CurseForge, FTB, Hangar, Spiget | Minecraft only; other games get per-game content later |
| Diagnostics | Minecraft log signatures | definition `analyze_logs` (optional) |
| Frontend tabs | gated by permission + `content_kind`/`is_modpack` | gated by permission + `capabilities` |

The full map with file:line references is in the research notes (`codebase-map.md`); the
17 implicit assumptions it lists (port 25565 in four places, `memory_mb` meaning Java heap, the
default world name `world`, uid/gid via itzg env, 16-character player names, `lstrip("/")` on
commands, …) are each handled in sections 4-6.

---

## 4. The game definition (panel side)

A new app `backend/apps/games/` with one module per game and a registry. A definition is a Python
class (data plus a few methods); data that the UI needs is exposed through the API.

```python
class GameDefinition:
    id: str                      # "minecraft", "factorio", "terraria", "satisfactory", "dst", "stardew"
    label: str
    variants: list[Variant]      # minecraft: vanilla/paper/fabric/…; terraria: vanilla/tmodloader/tshock
    profile: str                 # agent runtime profile id, per variant ("terraria-vanilla", …)
    capabilities: set[str]       # section 4.2
    memory_model: MemoryModel    # jvm (heap + overhead) or plain (container memory)
    ports: list[PortSpec]        # named, with protocol and container port
    config_files: list[ConfigFile]
    secrets: list[SecretSpec]    # control password, tokens the user provides
    setup_steps: list[SetupStep] # first-start actions (claim, token, …)

    def versions(self, variant) -> VersionCatalog | None
    def image(self, instance) -> str               # repository:tag, per-game channel rule
    def build_spec(self, instance) -> dict         # the agent spec; hashed for restart-required
    def initial_files(self, instance, create_data) -> dict[str, str]
    def world(self) -> WorldModel | None           # save locations, reset rules
    def players(self) -> PlayersAdapter | None
    def analyze_logs(self, lines) -> list[Issue]
    def normalise_command(self, text) -> str       # Factorio keeps its leading "/"
    stop_words: set[str]                           # console words that must become a panel Stop
    default_backup_excludes: list[str]
```

### 4.1 Data model changes

* `Instance.game` (CharField, default and database default `"minecraft"`): rollback-safe like
  `resume_after_boot`. `server_type` stays and means *variant* within the game (Terraria uses it
  for vanilla/tModLoader/TShock).
* `minecraft_version` is kept as the column name (renaming costs a migration and many test edits)
  but shown as "Game version"; non-Minecraft definitions store their version string there. A later
  clean-up can rename it.
* `rcon_password` keeps its column and becomes the **control secret** (Factorio RCON password,
  Satisfactory admin password, TShock REST token); the API and docs call it that.
* New `Instance.ports` JSON (named ports `{name, container_port, protocol, host_port, host_ip}`),
  replacing the single `game_port` for non-Minecraft games. Minecraft keeps `game_port` +
  `extra_ports` exactly as today so its spec does not change. Port allocation and conflict checks
  become protocol-aware (TCP 7777 and UDP 7777 are different ports).
* New `Instance.secrets` (encrypted JSON) for user-provided tokens: Klei cluster token, Factorio
  mod portal token, Steam refresh token (Stardew).
* `Instance.setup_state` (JSON) for first-start steps (Satisfactory claimed, admin token stored).
* `PlayerSeen.name` widened from 16 to 64 characters.
* Backup `meta.software` gains `game`; restoring software from a backup of another game is refused.
* `SystemSetting` runtime image repository/channel becomes per game (`runtime_images` JSON:
  `{game: {repository, channel}}`), with today's two keys still read for Minecraft.

### 4.2 Capabilities

Flags the panel and the UI read; each definition (and variant) declares its set.

| Capability | Minecraft | Factorio | Terraria | TShock | Satisfactory | DST | Stardew |
|---|---|---|---|---|---|---|---|
| `console_commands` | ✓ (RCON) | ✓ (RCON) | ✓ (console) | ✓ (console/REST) | – (API only) | ✓ (console, Lua) | – |
| `broadcast` (in-game stop warnings) | ✓ | ✓ | ✓ (`say`) | ✓ | – | ✓ (`c_announce`) | – |
| `players` (online list) | ✓ | ✓ | ✓ (`playing`) | ✓ | count only | ✓ (Lua) | via its API, *to confirm* |
| `player_actions` (kick/ban/op) | ✓ | ✓ | ✓ | ✓ | – | ✓ | – |
| `query` (status/player count without a command) | ✓ (ping) | – | – | ✓ (REST) | ✓ (UDP query) | – | ✓ (REST) |
| `consistent_backups` (save before copy) | ✓ (save-off/on) | ✓ (`/server-save`) | ✓ (`save`) | ✓ | ✓ (API SaveGame) | ✓ (`c_save()`) | – (saves at in-game day end) |
| `world_reset` | ✓ | ✓ (new map) | ✓ | ✓ | ✓ (new game via API) | ✓ (`c_regenerateworld` / delete save) | – |
| `world_import_export` | ✓ | ✓ (save zip) | ✓ (`.wld`) | ✓ | ✓ (`.sav`) | ✓ (cluster folder) | ✓ (save folder) |
| `software_update` (pick a version) | ✓ | ✓ (pinned tag) | ✓ | ✓ | – (follows stable) | – (follows Steam) | – |
| `content` (mods/plugins manager) | ✓ | later (mod portal) | later (tModLoader) | later (plugins) | – | later (Workshop ids) | later (upload) |
| `modpacks` | ✓ | – | – | – | – | – | – |
| `jvm` (heap, JVM flags) | ✓ | – | – | – | – | – | – |
| `eula` | ✓ | – | – | – | – | – | – |

### 4.3 Memory model

`jvm`: today's behaviour (heap + overhead = container limit). `plain`: the field is the container
limit; no overhead, no heap wording. The formula moves to one backend function that the API
exposes, and the frontend stops duplicating it. Defaults per game: Factorio 2 GB, Terraria 1-2 GB
(tModLoader 3 GB), Satisfactory 12 GB (minimum 8), DST 1.5 GB per shard, Stardew 2-3 GB.

### 4.4 Config files

Each definition lists its files, the codec, a schema for the fields the form shows, the keys the
panel owns (never editable), and the secret keys (masked):

| Game | Files (codec) | Owned by the panel |
|---|---|---|
| Minecraft | `server.properties` (Java properties) | as today |
| Factorio | `config/server-settings.json` (JSON), `map-gen-settings.json`, `map-settings.json`, `server-adminlist.json`, `server-whitelist.json` | port, RCON password file |
| Terraria | `serverconfig.txt` (key=value) | `port`, `world`, `worldpath` |
| TShock | `serverconfig.txt`, `tshock/config.json` (JSON) | REST settings and token |
| Satisfactory | settings through the API; `Game.ini`/`Engine.ini` (ini) for advanced | ports |
| DST | `cluster.ini`, `Master/server.ini`, `Caves/server.ini` (ini); `modoverrides.lua`, `dedicated_server_mods_setup.lua`, `leveldataoverride.lua` (text) | ports, `[SHARD]` link settings, cluster key |
| Stardew | the server mod's config (JSON), *to confirm* | ports, API settings |

Codecs: properties (exists), JSON, INI, key=value; Lua files are edited as plain text in the file
editor only. The properties form renderer (already schema-driven) renders any of these.

### 4.5 Secrets and first-start setup

Some games need something only the owner can provide, or a step on first start:

| Game | What | How the panel handles it |
|---|---|---|
| DST | Klei cluster token (from an account that owns DST) | Wizard field with a link to Klei's token page; stored encrypted; written to `cluster_token.txt` at start; the server does not start without it |
| Factorio | factorio.com username + token, only for mod downloads | Optional; later, with the mod manager |
| Satisfactory | The server must be **claimed**: whoever reaches an unclaimed server first owns it | Agent claims it over the internal network **before any port is published**: start with ports unpublished → PasswordlessLogin → ClaimServer (admin password = control secret) → generate an API token (stored encrypted) → CreateNewGame (or load an imported save) → recreate the container with ports published |
| TShock | One-time `/setup` code in the log on first start | Panel shows it on the Overview page until a user account exists, or sets up the REST token and an owner account automatically, *to confirm* |
| Stardew | Steam login that owns the game, Steam Guard every ~200 days | Interactive login step in the UI or a pasted refresh token; stored encrypted; section 8.7 |

Setup steps are a small state machine in `Instance.setup_state`, run by a `setup` job after the
install job; the UI shows "Setup needed" with the next action.

---

## 5. Runtime profiles and control drivers (agent side)

### 5.1 Runtime profile

Shipped in the agent image (`agent/mcagent/profiles/*.py` or `.toml`), selected by `spec.game` +
`spec.variant`. The panel sends only the game and variant; everything below is looked up by the
agent, so a compromised panel still cannot pick caps, mounts or users.

| Item | Minecraft (today, unchanged) | Example for a new game |
|---|---|---|
| `images` (allowed repositories, optional tag pattern) | `itzg/minecraft-server` | `factoriotools/factorio` with exact version tags |
| `user` model | image starts as root, drops to `UID`/`GID` env | `PUID`/`PGID` env, or `user=99:100` set by the agent |
| `cap_add` | CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID, KILL | empty where the image runs unprivileged; the same six for images that drop privileges |
| `mounts` (instance subfolder → container path) | `.` → `/data` | `.` → `/factorio`; Satisfactory `.` → `/config` (game files in a subfolder excluded from backups) |
| `injected_env` / `reserved_env` | `EULA`, `UID`, `GID`, `ENABLE_RCON`, `RCON_PASSWORD`, `RCON_PORT`, `SERVER_PORT` | the image's own names |
| `env_policy` | today's panel policy, **also enforced by the agent** | each image's variables that download code or run scripts are refused |
| `ports` (container ports a spec may publish) | 25565/tcp + free extra ports | only the named game ports |
| `never_publish` | 25575/tcp | Factorio 27015/tcp, TShock REST 7878 unless opted in, any VNC port |
| `health` | `mc-health` | image healthcheck, TCP/UDP probe or log pattern (5.4) |
| `stop_signal`, `stop_timeout` floor | SIGTERM, 90 s | Satisfactory SIGINT after API save; DST 300 s |
| `tty` / `stdin_open` | stdin open, no TTY | Terraria vanilla needs a TTY |
| `fetch_hosts` | Minecraft CDNs | Factorio mod portal, later |
| `control` | driver `minecraft-rcon` | `rcon`, `console`, `http-api`, `none` |

`GET /v1/info` gains a `games` feature and lists the profiles it has; the panel refuses to create
or start a non-Minecraft server on an agent that lacks the profile (an older agent would silently
ignore the `game` field and start the image with Minecraft's env and health check).

### 5.2 Control drivers

One driver per protocol family; each implements the subset its game supports.

| Operation | Used by | `minecraft-rcon` (today's code, moved) | `rcon` (Factorio) | `console` (Terraria, DST) | `http-api` (Satisfactory, TShock REST) |
|---|---|---|---|---|---|
| `command(text)` | console, schedules, players | RCON | RCON | write a sanitised line to the console; output read from the log after it | API call (TShock); none for Satisfactory |
| `save()` | backups, before stop | `save-all flush` | `/server-save` | `save` / `c_save()` | `SaveGame` |
| `pause_saving()` / `resume_saving()` | consistent backups | `save-off` / `save-on` | – (save, then copy) | – | – |
| `stop()` | graceful stop | `docker stop` + RCON `stop` courier | `/server-save`, then `docker stop` | `exit` / `c_shutdown(true)` per shard, then wait | `SaveGame`, then SIGINT |
| `broadcast(text)` | stop warnings | `tellraw` | chat message | `say` / `c_announce("…")` | – |
| `query()` | players, overview | server list ping | `/players online` | `playing` / Lua | UDP query or `QueryServerState` |
| `ready()` | status | `mc-health` | RCON answers | log line (`Sim paused` for DST) or port | API health |

The generic `graceful_stop` becomes: driver `save()` (bounded, warning on failure) → driver
`stop()` or `docker stop` → wait → Docker kill at the timeout. `StopResult` keeps its JSON keys so
an older panel still reads it.

**New agent primitive: console input.** Terraria (vanilla, tModLoader) and DST take commands on
the server's console only. The agent gets a "write one line to the container's console" primitive
through the Docker attach API (stdin), with the line sanitised (printable, length-capped, no
control characters) and a per-server lock. For images whose console sits in tmux/screen and that
offer an injection script (PassiveLemon's `inject`, ich777's images), a profile may instead name a
**fixed exec argv** (`["inject", "{line}"]`) — the only form of `exec` the agent would allow, and
only per reviewed profile. Output is matched from the log stream after the write (bounded wait),
so console commands return "sent" rather than a guaranteed reply.

### 5.3 Ports

The agent accepts only container ports the profile names; the panel allocates host ports per
protocol from the range. Defaults keep the game's well-known port when free (players often expect
it), otherwise the next free one. The connection panel shows each named port with its protocol and
what to forward on the router.

### 5.4 Readiness

`installed_at` and RUNNING depend on a working health check. Per profile:

* the image's own healthcheck (Satisfactory, if present), or
* an agent-side probe: TCP connect (Terraria 7777), UDP query (Satisfactory), RCON answer
  (Factorio), log pattern since start (DST `Sim paused`, Stardew "ready" line *to confirm*).

A profile without any probe is refused (the crash-loop and "installing" logic need one).

### 5.5 Backups and saves

Backups stay restic over the server folder. The "consistent" mode becomes: driver `pause_saving()`
if it has one, else driver `save()` and wait for its completion (log line or API answer), then
copy, then `resume_saving()`. Profiles list large re-downloadable folders (SteamCMD game files,
Satisfactory's ~8 GB install) as default excludes, so backups contain saves and config, not the
game itself.

---

## 6. Frontend

* **Create wizard:** a new first step, *Game* (cards with the game's name and a one-line note:
  memory needed, what the owner has to provide). Minecraft keeps today's steps. Other games: variant
  and version (when the game has a catalog), resources, ports, initial settings rendered from the
  definition's schema, secrets and setup notes.
* **Instance tabs:** one section registry driven by `capabilities` replaces the four duplicated
  lists (layout tabs, command palette, overview shortcuts, settings tabs). Minecraft shows exactly
  what it shows today.
* **Settings:** "Properties" becomes "Game settings", rendering each config file of the definition;
  Resources shows heap only for `jvm` games; Network lists named ports with protocol.
* **Console:** the hint and stop interception use the definition's `stop_words`; Factorio commands
  keep their leading slash.
* **Labels and icons:** one function for game and variant labels/colours.
* **Setup panel:** "Setup needed" on the Overview with the next step (enter a token, claiming in
  progress, TShock setup code).

---

## 7. Servers made of several containers (DST, Stardew)

DST with caves runs two processes, Master and Caves, which talk over UDP with a shared cluster key.
Stardew's maintained setup (JunimoServer) is a server container plus a Steam-login sidecar. Two
ways to fit them:

**A. One container that runs every part.** For DST, ich777's image (the one in Unraid's Community
Applications) runs both shards in one container under `screen`. No change to the "one container
per server" model; but the image updates through SteamCMD at every start, and its consoles are
inside `screen` (commands only through a fixed exec argv, if the image has a way to inject).

**B. A server with parts.** An instance gets `parts` (for DST: `master`, `caves`), each its own
container from the same profile, on a private per-server network; lifecycle operations act on all
parts in order (start Master first, stop Caves first), status is the combination, retire and
last-run records are per part, backups cover the shared folder. This touches container naming,
labels, the per-server lock, retire, last-run, the console relay and the status computation.

**Recommendation:** do **A** for DST first (with superjump22's image as a candidate if it can be
run with both shards in one container — *to confirm*; otherwise ich777's), and build **B** only if
Stardew is wanted or a single-container DST turns out unreliable. B is the largest single change of
this project and is not needed for Factorio, Terraria or Satisfactory.

---

## 8. The games

Image choices are the research's recommendations, to be confirmed in a short spike per game
(build a profile, start a server on the desktop, save, stop, back up, restore).

### 8.1 Minecraft (refactor only)

Everything as today. Definition `minecraft`, profile `minecraft` reproducing `build_create_kwargs`
exactly, driver `minecraft-rcon` = today's code moved. The spec hash for each of the 8 existing
servers must not change (a test pins `build_spec` output for fixture instances before the
refactor starts).

### 8.2 Factorio

* **Image:** `factoriotools/factorio` (MIT, ~30 M pulls, arm64), pinned to exact version tags
  (e.g. `2.1.24`); "software update" picks a newer tag. Headless download needs no account and
  already contains the Space Age data.
* **Ports:** 34197/udp. RCON 27015/tcp internal only (never published).
* **User:** image default 845:845; set PUID 99 / PGID 100 on Unraid.
* **Control:** `rcon` driver: `/server-save`, `/players online`, chat; stops cleanly and saves on
  exit; `docker stop` with a 120 s timeout.
* **Config:** `server-settings.json` (the example defaults to a **public** listing: the panel
  writes `visibility.public=false` unless the owner turns it on), map-gen and map settings, admin
  list, whitelist.
* **World:** saves are zip files in `saves/`; reset = generate a new map with chosen map-gen
  settings; import = upload a save.
* **Risks:** the image enables Space Age by default at every start, so players without the DLC
  cannot join, and switching it under an existing save breaks the save: the wizard asks once
  ("Space Age: on/off"), the panel pins it, and changing it later is refused for an existing save.
  Clients and server need exactly the same version and mods. Mods (later) need a factorio.com token.

### 8.3 Terraria (vanilla, tModLoader)

* **Image:** `passivelemon/terraria-docker` (vanilla and tModLoader, PUID/PGID, saves on SIGTERM,
  commands through its `inject` script). Alternative for tModLoader: `jacobsmile/tmodloader1.4`
  (in Unraid CA).
* **Ports:** 7777/tcp.
* **Control:** `console` driver via the image's fixed `inject` argv: `save`, `exit`, `say`,
  `playing`, `kick`, `ban`.
* **Config:** `serverconfig.txt` (world, autocreate size, seed, difficulty, max players, password,
  motd). The vanilla server needs a TTY and a preset world, or it stops at an interactive world
  picker: the definition always writes `world` + `autocreate`.
* **World:** `.wld` files; reset = new world file with chosen size/difficulty/seed; import = upload.
  An `autocreate` smaller than the existing world can corrupt it: the reset flow sets size explicitly.
* **Risks:** client and server versions must match (vanilla updates are not on a version catalog
  that is easy to query: *to confirm* the source the image uses); tModLoader has no arm64 and runs
  as root in most images.

### 8.4 Terraria (TShock)

* **Image:** `ryshe/terraria:tshock-<version>` (~10 M pulls, arm64). Runs as root and does not pass
  a stop signal to the server: stop is `off` written to the console or a REST call.
* **Ports:** 7777/tcp; REST 7878 internal (never published unless the owner opts in).
* **Control:** `http-api` driver (TShock REST with a token the panel sets) for commands, players
  and stop; console as fallback.
* **Setup:** the one-time `/setup` code from the first start's log (section 4.5).

### 8.5 Satisfactory

* **Image:** `wolveix/satisfactory-server` (MIT, ~4 M pulls; source moved to
  `robtme/satisfactory-server`), SteamCMD app 1690800, anonymous login, PUID/PGID, built-in API
  healthcheck.
* **Ports:** 7777/tcp (HTTPS API), 7777/udp (game and query), 8888/tcp.
* **Memory:** 8 GB minimum, 12-16 GB for larger saves; the memory check matters most here.
* **Control:** `http-api` driver: health, query state (player count), SaveGame, Shutdown,
  settings, sessions/saves, upload/download save. No console, no player names.
* **Setup:** claim before publishing ports (section 4.5).
* **Stop:** the server does not save on shutdown and SIGTERM kills it at once: the driver calls
  SaveGame, waits, then stops with SIGINT and a long timeout.
* **Updates:** client and server builds must match, so the server follows the stable branch
  (update at restart); "software update" is not offered. The image re-validates its ~8 GB install
  at every start (slow starts; the game files are excluded from backups).
* **Risks:** the API is down while a save loads; mods are impractical (every client needs the same
  mod set) and are left out.

### 8.6 Don't Starve Together

* **Image:** candidates `superjump22/dontstarvetogether` (rebuilt within ~30 minutes of each Steam
  update, server as PID 1 so commands go to its console; one shard per container) and ich777's
  `steamcmd:dontstarve` (both shards in one container, in Unraid CA). See section 7.
* **Ports:** per shard UDP: game (Master 10999, Caves 11000), Steam (27016/27017 or 12346/12347),
  auth (8766/8767); the shard link stays internal. Ports must not be NATed to other numbers.
* **Memory:** ~1-1.5 GB per shard.
* **Control:** `console` driver: `c_save()`, `c_shutdown(true)` sent to every shard (Caves first,
  then Master), `c_announce("…")`, Lua for players and kick/ban.
* **Readiness:** `Sim paused` in each shard's log.
* **Config:** `cluster.ini` (game mode, max players, password, PvP), per-shard `server.ini`,
  world settings Lua files, mods (`dedicated_server_mods_setup.lua` + `modoverrides.lua`, Steam
  Workshop ids, no account needed).
* **Setup:** Klei cluster token (section 4.5).
* **Stop and saves:** saving at shutdown can take up to ~5 minutes: stop timeout 300 s, and the
  host-shutdown hook's 45 s budget is too short for DST (the hook would save first with `c_save()`
  and accept that the shutdown save may be cut; documented).
* **Updates:** an outdated server cannot be joined, so it follows Steam (update at restart).
* **Risks:** images are ~3.6 GB; whether stopping Master also stops Caves is disputed in old forum
  posts (hence "stop every shard").

### 8.7 Stardew Valley

* **State of the art:** there is no official dedicated server. The maintained option is
  **JunimoServer** (`sdvd/server` + `sdvd/steam-service`, MIT): the full game runs headless with
  SMAPI and server mods, plus a sidecar that holds the Steam login. Older docker projects are
  deprecated and point to it. Its own docs call the latest release unstable.
* **What it requires:** a Steam account that owns the game, logged in inside the container, with
  an interactive Steam Guard step about every 200 days. Steam allows one session per account, so
  the owner effectively needs a second copy to play while the server runs.
* **Saves:** the game saves only at the end of an in-game day; stopping mid-day loses that day.
  No consistent backup mode beyond "back up after a day ended".
* **Terms:** running the game client unattended as a server with a bot-like account is a grey area
  under the Steam Subscriber Agreement (personal use; no-bots clause). Fine for a private server
  among friends is a judgment call for the owner, not something the panel can settle.
* **Fit:** needs section 7 option B (server + sidecar), credential handling, and has no Unraid
  CA template to lean on. **Recommendation: decide after DST**; if wanted, it is the last game.

---

## 9. Security

The rule stays: the panel and users cannot express images, mounts, capabilities, devices, users or
arbitrary commands. Changes:

* **Per-game allow-list:** an image is valid only with its own profile. An operator who adds a
  repository must name the game (`AGENT_GAME_<ID>_IMAGES`), so it inherits that profile's caps and
  mounts and nothing else.
* **Fewer privileges where possible:** profiles for images that can run unprivileged set
  `user=99:100` and `cap_add=[]`, which is stricter than Minecraft today.
* **Env policy on the agent:** the dangerous-variable policy (today only in the panel) moves into
  each profile and is enforced by the agent as well. Game images commonly have variables that
  download code or run scripts (`*_URL`, mod lists, pre-start hooks).
* **Never-publish control ports**, enforced by the agent (today only the panel refuses 25575):
  RCON, TShock REST, Satisfactory before it is claimed, any VNC/noVNC port.
* **Console writes and exec:** the console primitive writes one sanitised line; exec only with a
  fixed argv named by a reviewed profile. No shell, no user-supplied argv.
* **Secrets:** tokens stored encrypted like the RCON password, never logged, written to the
  server folder only where the game reads them (`cluster_token.txt`), masked in the file editor and
  excluded from support bundles.
* **Netguard** keeps refusing agent API calls from game containers; with SteamCMD, Wine/Mono and
  less-audited images it matters more.
* **Satisfactory claiming** happens before any port is published.

---

## 10. Plan

Each phase ends with the same checks as this release: unit tests, a review, and a real-server test
on the desktop (start, save, stop, back up, restore, reset, upgrade from the previous release).

| Phase | Work | Size | Ships to the owner |
|---|---|---|---|
| 0 | Pin the Minecraft spec and agent create arguments in tests; per-game spikes for the image choices (start each server once on the desktop, record ports, save/stop behaviour, readiness) | small | – |
| 1 | `apps/games` registry, `Instance.game`, capabilities in the API, Minecraft definition routing `spec`, properties, world, players, power warnings, diagnostics, console; agent profiles and drivers with Minecraft only; frontend section registry | large | yes, with no visible change |
| 2 | Generic pieces the next games need: named ports with protocol, memory model, multi-file config forms, secrets, setup steps, readiness probes, driver-based consistent backups, console-write primitive | medium | with phase 3 |
| 3 | **Factorio** | medium | yes |
| 4 | **Terraria** vanilla + tModLoader, then TShock | medium | yes |
| 5 | **Satisfactory** (API driver, claiming) | medium | yes |
| 6 | **DST** (single-container option first) | medium-large | yes |
| 7 | Decide on **Stardew**; if yes: servers with parts (section 7 B) + Steam login flow | large | maybe |
| later | Per-game mods: Factorio mod portal, tModLoader, DST Workshop ids, TShock plugins | per game | |

Test strategy: the existing 1,621 + 1,049 + 330 tests protect phase 1 (no behaviour change for
Minecraft); each game gets definition tests (spec, config codecs, ports), driver tests against a
fake of its protocol (RCON fake exists; console and HTTP fakes are new), and one integration test
per game against a real server (marked `integration`, like today's).

---

## 11. Decisions for the owner

1. **Order of games.** Proposed: Factorio, Terraria, Satisfactory, DST, then decide Stardew.
2. **Stardew Valley:** worth the second Steam copy, the credential handling inside a container and
   the terms question? Proposed: decide after DST.
3. **DST shape:** accept the single-container image (simpler, slower starts) first? Proposed: yes.
4. **Name:** keep "MCPanel" or rename (e.g. in the UI title only, to avoid churn in container
   names, labels and paths). Proposed: rename the UI only, keep technical names.
5. **Satisfactory memory:** a 12-16 GB server next to BMC3 (17.5 GB) on a 62 GB host is fine, but
   all servers running at once is not; the memory check will refuse or ask. OK?
6. **Public listings:** Factorio and DST can list a server publicly; proposed default is private
   (password or LAN/whitelist) everywhere, opt-in per server.
