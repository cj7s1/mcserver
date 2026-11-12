/** Types mirroring the backend API serializers (backend/apps/<app>/serializers.py and views.py). */

export type InstanceStatus =
  | "created"
  | "installing"
  | "starting"
  | "running"
  | "unhealthy"
  | "stopping"
  | "stopped"
  | "restarting"
  | "updating"
  | "backing_up"
  | "restoring"
  | "failed"
  | "unknown"
  | "archived";

export type ServerType =
  | "VANILLA"
  | "PAPER"
  | "PURPUR"
  | "FOLIA"
  | "FABRIC"
  | "QUILT"
  | "FORGE"
  | "NEOFORGE"
  | "MODRINTH"
  | "AUTO_CURSEFORGE"
  | "FTBA"
  | "CUSTOM";

/**
 * What a game (and an instance of it) supports; the UI shows a section only when its capabilities are
 * present (backend/apps/games, design docs/design/GAME-DEFINITIONS.md 4.2). Strings the frontend does
 * not know yet may arrive from a newer panel and are ignored.
 */
export type GameCapability =
  | "console_commands"
  | "broadcast"
  | "players"
  | "player_actions"
  | "query"
  | "consistent_backups"
  | "world_reset"
  | "world_import_export"
  | "software_update"
  | "content"
  | "modpacks"
  | "jvm"
  | "eula"
  | "properties"
  | "diagnostics"
  /** Game settings editor for a game's own config files (non-Minecraft games; never Minecraft). */
  | "config_files";

/** "jvm": memory_mb is the Java heap and the container gets off-heap headroom; "plain": memory_mb is the container limit. */
export type MemoryModel = "jvm" | "plain";

/** One entry of GET /api/catalog/games. */
/**
 * A variant of a game in the catalog (Minecraft: server types; Terraria: vanilla / tModLoader / TShock).
 * Games whose variants differ in more than their name add the optional fields.
 */
export interface GameVariantInfo {
  id: string;
  label: string;
  default_memory_mb?: number;
  notes?: string[];
  capabilities?: GameCapability[];
  stop_words?: string[];
  player_list_labels?: Partial<Record<"whitelist" | "ops" | "bans", string>>;
  /** The game version the variant is built on, when it is not the variant's own (tModLoader: Terraria 1.4.4.9). */
  game_version?: string | null;
}

export interface GameInfo {
  id: string;
  label: string;
  variants: GameVariantInfo[];
  capabilities: GameCapability[];
  memory_model: MemoryModel;
  default_memory_mb: number;
  /** What the owner should know (memory needed, what they have to provide): a list of sentences. */
  notes: string[] | string;
  // --- present only for games that have them (contract B3); Minecraft's entry has none of them ---
  ports?: GamePortSpec[];
  config_files?: GameConfigFileSpec[];
  /** Per-server choices pinned at creation (Factorio: Space Age). */
  options?: ConfigField[];
  secrets?: GameSecretSpec[];
  setup_steps?: GameSetupStepSpec[];
  /** Console words that make the server exit; the panel runs its Stop instead (Factorio: "/quit"). */
  stop_words?: string[];
  /** Commands keep their leading "/" (Factorio); Minecraft strips it. */
  keeps_command_slash?: boolean;
  /** Example/hint for the console input. */
  command_hint?: string;
  /** The game has a version catalog (GET /catalog/games/<id>/versions). */
  has_versions?: boolean;
  /** No version to choose: what the server runs (Satisfactory: Steam's stable branch, updated at every start). */
  /** No version to choose; `updates_on_restart`: whether a restart updates the game (absent: it does). */
  fixed_version?: { id: string; label: string; help?: string; updates_on_restart?: boolean };
  min_memory_mb?: number;
  /** Seconds a stop may take before Docker kills the server (games with named ports). */
  default_stop_timeout?: number;
  /** Regex source of a valid player name. */
  player_name_pattern?: string;
  player_list_labels?: Partial<Record<"whitelist" | "ops" | "bans", string>>;
  world?: GameWorldSpec | null;
}

/**
 * One field of a game's config file, option or reset form (contract "Field schema"). Values are JSON
 * typed: bool, int, number, string, null or a list of strings.
 */
export type ConfigFieldType = "string" | "int" | "number" | "bool" | "enum" | "list";
export type ConfigValue = string | number | boolean | null | string[];

export interface ConfigField {
  key: string;
  type: ConfigFieldType;
  label: string;
  help?: string;
  group?: string;
  default?: ConfigValue;
  min?: number;
  max?: number;
  options?: string[];
  /** Empty input means null (e.g. a random seed). */
  nullable?: boolean;
  /** The API masks the value ("********"); sending the mask back means unchanged. */
  secret?: boolean;
  /** A change applies at the next start (default true). */
  restart?: boolean;
  /** Shown in the create wizard. */
  initial?: boolean;
  /** Options: the choice is fixed after creation. */
  pinned?: boolean;
  /** Options: the game variants that have it (absent: every variant). */
  variants?: string[];
  /** Enum: what the form shows for each value. */
  option_labels?: Record<string, string>;
  /** Text: the value must match this regex (Python syntax, matched whole; the server checks it too). */
  pattern?: string;
  /** What `pattern` allows, in words (shown when the value does not match). */
  pattern_help?: string;
}

export interface GamePortSpec {
  name: string;
  label: string;
  protocol: "tcp" | "udp";
  container_port: number;
  default_host_port: number;
  help?: string;
  /** Always the host port number of this other named port (Satisfactory's API on TCP = the game's UDP port). */
  same_port_as?: string;
  /** Always another port's host number plus `offset` (Valheim's query port = game + 1): shown, never asked for. */
  derived_from?: { port: string; offset: number };
  /** Published 1:1: the server listens inside its container on the host port's number (it advertises that number). */
  one_to_one?: boolean;
  /** Published only while a setting has this value (Valheim's crossplay, CS2's CSTV, Rust+); the number stays reserved. */
  when?: { config: string; key: string; value: ConfigValue };
}

export interface GameConfigFileSpec {
  id: string;
  path: string;
  codec: string;
  label: string;
  help?: string;
  fields: ConfigField[];
  /** The game variants that have this file (absent: every variant). */
  variants?: string[];
}

export interface GameSecretSpec {
  key: string;
  label: string;
  help?: string;
  required?: boolean;
  link?: string;
  /** The game variants that have this secret (absent: every variant). */
  variants?: string[];
  /** Required only when this option has this value (Satisfactory: the join password with access "password"). */
  required_when?: { option: string; value: string };
  /** The panel generates a value when the create request has none (it cannot be cleared afterwards). */
  generated?: boolean;
  /** The owner may read the value back (POST /instances/{id}/secrets/{key}/reveal; audited). */
  reveal?: boolean;
  /** The value must match this regex (it reaches the container's environment); `pattern_help` says it in words. */
  pattern?: string;
  pattern_help?: string;
}

export interface GameSetupStepSpec {
  id: string;
  label: string;
  help?: string;
  /** "auto": the panel does it (a setup job); "secret": needs a secret from the owner. */
  kind: string;
  /** kind "secret": the key of the secret it waits for. */
  secret?: string | null;
}

export interface GameWorldSpec {
  /** "saves": save files (Factorio, Satisfactory); "worlds": world files (Terraria); "folder": the world is a folder (Palworld, Valheim, Project Zomboid, Rust). */
  kind: "saves" | "worlds" | "folder" | string;
  extension?: string;
  /** The folder (in the server folder) that holds the worlds. */
  folder?: string;
  /** Where a reset or an import moves the old world. */
  set_aside_folder?: string;
  reset_fields?: ConfigField[];
  /** New game, import and export go through the running server's API (Satisfactory). */
  via_api?: boolean;
  /** kind "folder": what an imported zip must hold. */
  import_help?: string;
  /** kind "folder": the largest world (unpacked) an import may have. */
  max_import_bytes?: number;
}

/** GET /catalog/games/<id>/versions */
export interface GameVersion {
  id: string;
  label: string;
  stable: boolean;
  latest: boolean;
}

export interface GameVersionsResponse {
  versions: GameVersion[];
  latest: string | null;
}

/** A named port of a non-Minecraft server (Instance.ports). */
export interface NamedPort {
  name: string;
  label?: string;
  protocol: "tcp" | "udp";
  container_port: number;
  host_port: number;
  host_ip?: string;
}

export interface SetupStepState {
  id: string;
  label: string;
  help?: string;
  kind: string;
  /** pending | running | done | failed */
  status: string;
  message?: string;
}

/** Instance.setup: null for games without first-start setup steps. */
export interface InstanceSetup {
  needed: boolean;
  next: string | null;
  steps: SetupStepState[];
  /** Satisfactory: whether the setup published the ports yet (nothing is reachable before). */
  ports_open?: boolean;
}

/** One file of GET /instances/{id}/config. */
export interface ConfigFileState {
  id: string;
  path: string;
  label: string;
  codec: string;
  help?: string;
  exists: boolean;
  values: Record<string, ConfigValue>;
  fields: ConfigField[];
  managed_keys: string[];
  redacted_keys: string[];
}

export interface ConfigFilesResponse {
  files: ConfigFileState[];
}

/** PATCH /instances/{id}/config/{file} */
export interface ConfigPatchResult {
  values: Record<string, ConfigValue>;
  changed: string[];
  restart_required: boolean;
  redacted_keys?: string[];
}

/** GET/PUT /instances/{id}/secrets */
export interface InstanceSecret {
  key: string;
  label: string;
  help?: string;
  required?: boolean;
  link?: string;
  /** Clearing it opens the server to anyone (Satisfactory's join password): confirmed with access "open". */
  clear_opens?: boolean;
  /** Generated by the panel when the server was created; it cannot be cleared. */
  generated?: boolean;
  /** The value can be shown (POST /instances/{id}/secrets/{key}/reveal). */
  reveal?: boolean;
  pattern?: string;
  pattern_help?: string;
  set: boolean;
}

/** POST /instances/{id}/secrets/{key}/reveal */
export interface RevealedSecret {
  key: string;
  value: string;
}

export interface SecretsResponse {
  secrets: InstanceSecret[];
}

/** GET /instances/{id}/saves (non-Minecraft games). */
export interface SaveFile {
  name: string;
  path: string;
  size: number;
  /** epoch seconds or ISO string */
  modified: number | string | null;
  current: boolean;
  /** Games whose saves belong to sessions (Satisfactory): the session of this save, when the server said. */
  session?: string | null;
  /** World folders (kind "folder"): "directory" for the world folder, "file" for single world files. */
  type?: "file" | "directory" | null;
  /** false: the size is a lower bound (the panel stopped measuring a very large folder). */
  size_complete?: boolean;
}

export interface SavesInfo {
  current: string;
  saves: SaveFile[];
  set_aside: { folder: string; files: string[] }[];
  options: Record<string, ConfigValue>;
  reset_fields: ConfigField[];
  /** What the page cannot do for this server (tModLoader: the .twld file is not part of download/import). */
  notes?: string[];
  /** World folders: what an imported zip must hold. */
  import_help?: string;
  /** The panel's upload limit (bytes); null = none. */
  max_upload_bytes?: number | null;
}

export type PermissionCode =
  | "system.settings"
  | "system.users"
  | "system.audit"
  | "system.nodes"
  | "instance.create"
  | "instance.view"
  | "instance.control"
  | "instance.console"
  | "instance.command"
  | "instance.configure"
  | "instance.files.read"
  | "instance.files.write"
  | "instance.content"
  | "instance.players"
  | "instance.backup"
  | "instance.restore"
  | "instance.schedules"
  | "instance.access"
  | "instance.delete";

export interface Me {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  is_owner: boolean;
  role_name: string | null;
  permissions: PermissionCode[];
  must_change_password: boolean;
  preferences: Record<string, unknown>;
}

export interface AuthState {
  setup_required: boolean;
  /** present (true) when MCPANEL_SETUP_TOKEN is configured and setup is still open */
  setup_token_required?: boolean;
  authenticated: boolean;
  user: Me | null;
}

export interface PortMapping {
  container_port: number;
  host_port: number;
  protocol: "tcp" | "udp";
  host_ip?: string;
  label?: string;
}

export interface ConnectionAddress {
  kind: "bind" | "lan" | "public";
  label: string;
  address: string;
}

export interface ConnectionPort {
  name: string;
  label: string;
  protocol: "tcp" | "udp";
  container_port: number;
  host_port: number;
  addresses: ConnectionAddress[];
}

export interface ConnectionInfo {
  host_port: number;
  container_port: number;
  bind_address: string;
  addresses: ConnectionAddress[];
  notes: string[];
  /** Non-Minecraft servers: every named port with its addresses. */
  ports?: ConnectionPort[];
}

export interface ActiveJob {
  id: string;
  kind: string;
  title: string;
  progress: number;
  message: string;
  status: JobStatus;
  /** The job only waits for a start that downloads the game (CS2, Rust, Project Zomboid): Stop ends it. */
  stoppable?: boolean;
}

/** Result of the periodic modpack update check (backend apps/modpacks/updates.py summary()). */
export interface ModpackUpdateInfo {
  /** "pending" = never checked; "unavailable" = cannot be checked (e.g. CurseForge without an API key) */
  status: "ok" | "unavailable" | "error" | "pending";
  detail: string;
  checked_at: string | null;
  last_success_at: string | null;
  /** A newer release for the pinned Minecraft version and loader exists. */
  available: boolean;
  /** The pin changed since the last successful check (update/rollback); re-check to know more. */
  stale: boolean;
  latest: { id: string; name: string; published: string | null; minecraft_version: string } | null;
  /** Only on POST /modpack/check: the request reused a check that ran moments ago. */
  throttled?: boolean;
}

/** A restart/stop that warns players in game first and has not started yet (backend power_warnings.countdown_info). */
export interface PowerCountdown {
  job_id: string;
  action: "restart" | "stop";
  title: string;
  warn_minutes: number;
  message: string;
  /** queued = waiting for a worker; warning = announcing; final = last 10 seconds */
  phase: "queued" | "warning" | "final";
  /** When the action is due (ISO); null until the worker starts the countdown. */
  action_at: string | null;
  status_message: string;
}

export interface Instance {
  id: string;
  name: string;
  description: string;
  icon_url: string;
  group: string;
  node: number;
  node_name: string;
  edition: string;
  server_type: ServerType;
  minecraft_version: string;
  loader_version: string;
  loader: string;
  java_version: number;
  image: string;
  source: string;
  content_kind: "plugins" | "mods" | "none";
  is_modpack: boolean;
  modpack_provider: string;
  modpack_project_id: string;
  modpack_slug: string;
  modpack_name: string;
  modpack_version_id: string;
  modpack_version_name: string;
  modpack_loader: string;
  memory_mb: number;
  memory_overhead_mb: number;
  container_memory_mb: number;
  cpu_limit: number | null;
  jvm_flags_preset: "none" | "aikar" | "meowice";
  jvm_args: string;
  env: Record<string, string>;
  /** true when the API masked the env values ("********") because the caller lacks instance.configure */
  env_values_hidden?: boolean;
  restart_policy: "unless-stopped" | "on-failure" | "no";
  stop_timeout: number;
  host_ip: string;
  game_port: number;
  extra_ports: PortMapping[];
  network: string;
  desired_state: "running" | "stopped";
  status: InstanceStatus;
  status_detail: string;
  container_state: string;
  health: string;
  last_exit_code: number | null;
  oom_killed: boolean;
  started_at: string | null;
  ready_at: string | null;
  installed_at: string | null;
  status_updated_at: string | null;
  restart_required: boolean;
  active_job: ActiveJob | null;
  power_countdown: PowerCountdown | null;
  /** null for standard (non-modpack) servers */
  modpack_update: ModpackUpdateInfo | null;
  /** When the newest successful backup (any kind) was taken; null when there is none or the caller lacks instance.backup. */
  last_backup_at?: string | null;
  /** Number of enabled schedules whose action is a backup (snapshot schedules do not count). */
  backup_schedules?: number;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  connection: ConnectionInfo;
  permissions: PermissionCode[];
  /** Game id ("minecraft"). Absent from older panels: treat as Minecraft. */
  game?: string;
  /** Display name of the game ("Minecraft"). */
  game_label?: string;
  /** Sorted capability names of this instance. Absent from older panels: derived from content_kind/is_modpack (lib/games.ts). */
  capabilities?: GameCapability[];
  /** Absent from older panels: "jvm" (Minecraft). */
  memory_model?: MemoryModel;
  /** Named ports of a non-Minecraft server ([] for Minecraft, absent from older panels). */
  ports?: NamedPort[];
  /**
   * Pinned per-game choices (Factorio: {space_age}); {} for Minecraft. Games with virtual configs keep
   * those here as well, under the config's id (Valheim: {world_name, server: {crossplay, ...}}).
   */
  game_options?: Record<string, ConfigValue | Record<string, ConfigValue>>;
  /** First-start setup; null for games without setup steps. */
  setup?: InstanceSetup | null;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Job {
  id: string;
  kind: string;
  title: string;
  instance: string | null;
  instance_name: string | null;
  user: number | null;
  user_name: string | null;
  status: JobStatus;
  progress: number;
  message: string;
  error: string;
  error_code: string;
  result: Record<string, unknown>;
  log: string[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Stats {
  running: boolean;
  timestamp?: string;
  cpu_percent?: number;
  cpu_count?: number;
  memory_bytes?: number;
  memory_limit_bytes?: number;
  network_rx_bytes?: number;
  network_tx_bytes?: number;
  block_read_bytes?: number;
  block_write_bytes?: number;
  pids?: number | null;
  disk?: { bytes: number; backup_bytes?: number; measured_at: number; cached: boolean };
  players?: { online: number | null; max: number | null; players: string[]; raw?: string } | null;
}

export interface ServerTypeInfo {
  id: ServerType;
  label: string;
  category: "vanilla" | "plugins" | "mods";
  content: "plugins" | "mods" | "none";
  loader_label: string | null;
  description: string;
}

export interface VersionEntry {
  version: string;
  stable: boolean;
  type?: string;
  released?: string;
  recommended?: boolean;
  latest?: boolean;
  channel?: string;
  support?: string;
}

export interface JavaInfo {
  minecraft_version: string;
  required: number;
  java_version: number;
  reason: string;
}

export type Provider = "modrinth" | "curseforge" | "ftb";

export interface ProviderInfo {
  id: Provider;
  label: string;
  available: boolean;
  requires_key: boolean;
  note?: string | null;
}

export interface Project {
  provider: Provider;
  id: string;
  slug: string;
  title: string;
  description: string;
  body?: string;
  author?: string;
  icon_url?: string | null;
  downloads?: number;
  follows?: number;
  categories: string[];
  loaders: string[];
  game_versions: string[];
  updated?: string | number | null;
  published?: string;
  server_side?: string | null;
  client_side?: string | null;
  url?: string;
  license?: string;
  gallery?: string[];
  allow_distribution?: boolean | null;
}

export interface SearchResult<T = Project> {
  total: number;
  offset: number;
  limit: number;
  results: T[];
}

export interface Release {
  provider: Provider;
  id: string;
  project_id: string;
  name: string;
  version_number: string;
  version_type: string;
  game_versions: string[];
  loaders: string[];
  loader_version?: string;
  published?: string | number;
  downloads?: number;
  changelog?: string;
  java?: string;
  recommended_memory_mb?: number;
  minimum_memory_mb?: number;
  file?: { filename: string; url: string | null; size: number; sha1?: string; sha512?: string } | null;
  dependencies?: { project_id?: string; version_id?: string; type?: string; file_name?: string }[];
}

export interface ResolvedModpack {
  provider: Provider;
  server_type: ServerType;
  project_id: string;
  slug: string;
  name: string;
  icon_url: string;
  version_id: string;
  version_name: string;
  minecraft_version: string;
  loader: string;
  loader_version: string;
  warnings: string[];
  recommended_memory_mb: number | null;
  java: JavaInfo;
}

export interface FileEntry {
  name: string;
  path: string;
  type: "file" | "directory" | "symlink" | "other";
  size: number;
  modified: number;
  mode: string;
  /**
   * false: the real name has bytes that are not UTF-8 (shown with U+FFFD in their place), so the
   * entry cannot be opened, renamed, moved or deleted by this name. Absent = a valid name.
   */
  name_valid?: boolean;
}

export interface PropertySchema {
  key: string;
  type: "string" | "int" | "bool" | "enum";
  label: string;
  group: string;
  options?: string[];
  min?: number;
  max?: number;
  restart?: boolean;
  help?: string;
}

export interface PropertiesResponse {
  exists: boolean;
  values: Record<string, string>;
  raw: string;
  schema: PropertySchema[];
  managed_keys: string[];
  /** Secret keys whose value the API replaced with "********" (never sent to the browser). */
  redacted_keys: string[];
}

export interface Backup {
  id: string;
  name: string;
  kind: "manual" | "scheduled" | "pre_restore" | "pre_update";
  status: "running" | "succeeded" | "failed" | "deleted";
  engine: string;
  snapshot_id: string;
  data_added_bytes: number | null;
  total_bytes: number | null;
  files_count: number | null;
  duration_seconds: number | null;
  consistent: boolean;
  warnings: string[];
  error: string;
  pinned: boolean;
  notes: string;
  created_by_name: string | null;
  created_at: string;
  finished_at: string | null;
  verified_at: string | null;
  verify_ok: boolean | null;
  /** Node whose restic repository holds the backup; null when that node was deleted (unreadable). */
  node_id?: number | null;
  /** That node's name (for a deleted node: the name recorded with the backup, if any). */
  node_name?: string;
  /**
   * false after the server moved to another node: the backup stays in the old node's repository
   * and can be browsed, downloaded, deleted and restored as a new server there, not restored in place.
   */
  on_current_node?: boolean;
}

export interface RetentionPolicy {
  keep_last: number;
  keep_daily: number;
  keep_weekly: number;
  keep_monthly: number;
  keep_safety_days: number;
  excludes: string[];
}

export interface Schedule {
  id: number;
  name: string;
  action: "restart" | "start" | "stop" | "backup" | "command";
  cron: string;
  payload: Record<string, unknown>;
  enabled: boolean;
  only_when_running: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  /** null: a command schedule's result, hidden from users without console access on the server (D-13). */
  last_result: string | null;
  created_at: string;
}

export interface PlayerEntry {
  uuid?: string;
  name: string;
  level?: number;
  created?: string;
  source?: string;
  expires?: string;
  reason?: string;
  ip?: string;
  /** The player's name when `name` is an id (Rust's owners and bans: name = SteamID64). */
  display_name?: string;
}

/** An online player with the id kicks and bans use (Palworld: steam_<id>, Rust: SteamID64). */
export interface OnlinePlayerEntry {
  name: string;
  id: string;
}

export interface PlayersResponse {
  running: boolean;
  whitelist: PlayerEntry[];
  ops: PlayerEntry[];
  bans: PlayerEntry[];
  ip_bans: PlayerEntry[];
  known_players: { name: string; uuid: string; expiresOn?: string }[];
  online?: {
    online: number | null;
    max: number | null;
    players: string[];
    error?: string;
    entries?: OnlinePlayerEntry[];
    /** Names in the server's list the panel cannot act on: shown with "use the console" (C-6). */
    other_names?: string[];
  };
  /** Non-Minecraft games: what the lists are called ({ops: "Admins"}). */
  labels?: Partial<Record<"whitelist" | "ops" | "bans", string>>;
  /** The game reports a player count only (Satisfactory): no names, no lists. */
  count_only?: boolean;
  /** What a name typed into the lists may be (Valheim: SteamID64s). */
  id_hint?: string;
  /** The game cannot list who is online (Valheim). */
  no_online_list?: boolean;
  /** The lists exist in the game but cannot be read (Project Zomboid's database): changes by name only. */
  lists_unknown?: boolean;
  /** Whether the lists can be changed while the server is stopped (the six Steam-era games say so; Valheim: files). */
  changes_while_stopped?: boolean;
}

export interface Role {
  id: number;
  key: string;
  name: string;
  description: string;
  permissions: PermissionCode[];
  is_system: boolean;
  user_count: number;
}

export interface PanelUser {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
  is_active: boolean;
  is_owner: boolean;
  role: number | null;
  role_name: string | null;
  must_change_password: boolean;
  last_login: string | null;
  date_joined: string;
  permissions: PermissionCode[];
}

export interface PermissionDef {
  code: PermissionCode;
  label: string;
  description: string;
  scope: "system" | "instance";
  group: string;
}

export interface InstanceAccessGrant {
  id: number;
  user: number;
  username: string;
  role: number;
  role_name: string;
  created_at: string;
}

export interface AuditEvent {
  id: number;
  created_at: string;
  actor: number | null;
  actor_username: string;
  action: string;
  target_type: string;
  target_id: string;
  instance: string | null;
  instance_name: string | null;
  ip_address: string | null;
  success: boolean;
  details: Record<string, unknown>;
}

export interface SystemInfo {
  version: string;
  node: { id: number; name: string };
  agent: {
    reachable: boolean;
    error?: string;
    version?: string;
    docker?: Record<string, string | number | null>;
    restic?: string | null;
    storage?: Record<
      string,
      {
        path: string;
        fstype: string;
        mount_point: string | null;
        total_bytes: number;
        used_bytes: number;
        free_bytes: number;
        snapshot: { provider: string | null; available: boolean; reason: string | null };
      }
    >;
    policy?: Record<string, unknown>;
  };
  counts: { instances: number; running: number };
}

export interface SystemSettings {
  values: {
    runtime_image_channel: string;
    default_memory_mb: number;
    port_range_start: number | null;
    port_range_end: number | null;
    default_bind_ip: string;
    backup_default_retention: Partial<RetentionPolicy>;
    pre_restore_backup: boolean;
    pre_update_backup: boolean;
    public_address: string;
    lan_address: string;
    timezone: string;
    /** RAM in MB kept free for the host when servers are started (memory admission); 0-65536 */
    memory_reserve_mb: number;
    /** Per-game runtime image repository (non-Minecraft games): {game: {repository}}. */
    runtime_images?: Record<string, { repository?: string }> | null;
  };
  secrets_configured: Record<string, boolean>;
  /** PATCH only: notes about what was saved (e.g. the CurseForge key could not be verified) */
  warnings?: string[];
  /** stored secrets that cannot be decrypted with the current MCPANEL_SECRET_KEY (re-enter them) */
  secrets_unreadable?: Record<string, boolean>;
  env: { port_range_default: [number, number] };
}

/** One folder of a world (GET /instances/{id}/world, agent/mcagent/world.py info()). */
export interface WorldFolder {
  role: "overworld" | "nether" | "end";
  path: string;
  exists: boolean;
  type: string | null;
  has_level_dat: boolean;
  bytes: number;
  files: number;
  /** unix seconds of the newest file */
  modified: number | null;
  /** overworld only: vanilla-layout dimensions found inside the world folder */
  dimensions?: ("nether" | "end")[];
}

/** GET /instances/{id}/world (backend apps/instances/world.py describe() + WorldView). */
export interface WorldInfo {
  level_name: string;
  level_name_source: "env" | "server.properties" | "default";
  /** bukkit: nether/end live in <level>_nether / <level>_the_end; vanilla: inside the world folder */
  layout: "vanilla" | "bukkit";
  folders: WorldFolder[];
  total_bytes: number;
  /** the world folder exists and has a level.dat */
  exists: boolean;
  /** .mcpanel-world-* folders left behind by an interrupted replacement */
  leftovers: string[];
  /** zip export (GET /files/archive with every existing world folder); null when there is nothing to export */
  download_url: string | null;
  warnings: string[];
  error: { code: string; message: string } | null;
  can_replace: boolean;
  running: boolean;
  /** the download link asks a running server to save first (needs instance.command or instance.backup) */
  download_flushes: boolean;
  max_upload_bytes: number | null;
  pre_restore_backup_default: boolean;
  archive_suffixes: string[];
}

/** What a world reset can carry over into the new world (backend apps/instances/world.py RESET_KEEP). */
export type WorldResetKeep = "datapacks" | "config";

/** keep: server.properties is not touched; random: level-seed is cleared; custom: level-seed is set. */
export type WorldResetSeedMode = "keep" | "random" | "custom";

/** One world folder in a reset plan (agent/mcagent/world.py info() with classify() and keepable()). */
export interface WorldResetFolder {
  role: "overworld" | "nether" | "end";
  path: string;
  /** only `world` folders are deleted; `pending` holds nothing but carry-overs of an earlier reset */
  state: "missing" | "empty" | "world" | "pending" | "unknown" | "not_a_directory";
  /** the entries that make it a world (level.dat, region, session.lock, ...) */
  markers: string[];
  /** keep option -> entry names inside this folder that can be carried over */
  keepable: Partial<Record<WorldResetKeep, string[]>>;
  bytes: number;
  files: number;
  /** unix seconds of the newest file */
  modified: number | null;
}

/** GET /instances/{id}/world/reset (backend apps/instances/world.py reset_plan() + WorldResetView). */
export interface WorldResetPlan {
  level_name: string;
  level_name_source: "env" | "server.properties" | "default";
  layout: "vanilla" | "bukkit";
  folders: WorldResetFolder[];
  /** paths of the folders a reset deletes (the ones in state `world`); sent back as `folders` on POST */
  delete: string[];
  can_reset: boolean;
  blockers: { code: string; message: string }[];
  /** the seed the next world would get: SEED in the environment wins over level-seed in server.properties */
  seed: { value: string; source: "env" | "server.properties" | "unset" };
  level_type: string;
  /** the container is running or restarting (not instance.status, which is "failed" in a crash loop) */
  running: boolean;
  pre_restore_backup_default: boolean;
  /** 0: safety backups are pruned by the next backup run */
  safety_backup_keep_days: number;
  /** the server's own backup exclusion patterns, which the safety backup of a reset ignores */
  backup_exclusions: number;
  leftovers: string[];
  warnings: string[];
}

export interface ContentItem {
  filename: string;
  enabled: boolean;
  size: number;
  modified: number;
  sha1?: string;
  identified: boolean;
  project_id?: string;
  project_title?: string;
  icon_url?: string | null;
  version_id?: string;
  version_number?: string;
  loaders?: string[];
  game_versions?: string[];
  [key: string]: unknown;
}
