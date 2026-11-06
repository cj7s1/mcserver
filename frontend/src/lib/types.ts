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

export interface ConnectionInfo {
  host_port: number;
  container_port: number;
  bind_address: string;
  addresses: { kind: "bind" | "lan" | "public"; label: string; address: string }[];
  notes: string[];
}

export interface ActiveJob {
  id: string;
  kind: string;
  title: string;
  progress: number;
  message: string;
  status: JobStatus;
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
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  connection: ConnectionInfo;
  permissions: PermissionCode[];
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
  last_result: string;
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
}

export interface PlayersResponse {
  running: boolean;
  whitelist: PlayerEntry[];
  ops: PlayerEntry[];
  bans: PlayerEntry[];
  ip_bans: PlayerEntry[];
  known_players: { name: string; uuid: string; expiresOn?: string }[];
  online?: { online: number | null; max: number | null; players: string[]; error?: string };
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
  };
  secrets_configured: Record<string, boolean>;
  env: { port_range_default: [number, number] };
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
