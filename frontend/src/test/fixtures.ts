/** Shared test fixtures (not used by product code). */
import type { GameCapability, GameInfo, Instance, WorldResetFolder, WorldResetPlan } from "@/lib/types";

export function makeInstance(over: Partial<Instance> = {}): Instance {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Vanilla Test",
    description: "",
    icon_url: "",
    group: "",
    node: 1,
    node_name: "local",
    edition: "java",
    server_type: "VANILLA",
    minecraft_version: "1.21.1",
    loader_version: "",
    loader: "",
    java_version: 21,
    image: "itzg/minecraft-server:java21",
    source: "",
    content_kind: "none",
    is_modpack: false,
    modpack_provider: "",
    modpack_project_id: "",
    modpack_slug: "",
    modpack_name: "",
    modpack_version_id: "",
    modpack_version_name: "",
    modpack_loader: "",
    memory_mb: 2048,
    memory_overhead_mb: 512,
    container_memory_mb: 2560,
    cpu_limit: null,
    jvm_flags_preset: "aikar",
    jvm_args: "",
    env: {},
    restart_policy: "unless-stopped",
    stop_timeout: 60,
    host_ip: "",
    game_port: 25565,
    extra_ports: [],
    network: "mcpanel_instances",
    desired_state: "running",
    status: "running",
    status_detail: "",
    container_state: "running",
    health: "healthy",
    last_exit_code: null,
    oom_killed: false,
    started_at: null,
    ready_at: null,
    installed_at: null,
    status_updated_at: null,
    restart_required: false,
    active_job: null,
    archived_at: null,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    connection: { host_port: 25565, container_port: 25565, bind_address: "0.0.0.0", addresses: [], notes: [] },
    permissions: ["instance.view", "instance.control", "instance.console", "instance.files.read", "instance.backup"],
    power_countdown: null,
    modpack_update: null,
    ...over,
  };
}

export function makeResetFolder(over: Partial<WorldResetFolder> = {}): WorldResetFolder {
  return {
    role: "overworld",
    path: "world",
    state: "world",
    markers: ["level.dat", "region", "session.lock"],
    keepable: {},
    bytes: 3 * 1024 * 1024,
    files: 42,
    modified: 1_793_000_000,
    ...over,
  };
}

/** A stopped vanilla server with one world folder, no seed set, safety backups on. */
export function makeResetPlan(over: Partial<WorldResetPlan> = {}): WorldResetPlan {
  return {
    level_name: "world",
    level_name_source: "server.properties",
    layout: "vanilla",
    folders: [makeResetFolder()],
    delete: ["world"],
    can_reset: true,
    blockers: [],
    seed: { value: "", source: "unset" },
    level_type: "",
    running: false,
    pre_restore_backup_default: true,
    safety_backup_keep_days: 14,
    backup_exclusions: 0,
    leftovers: [],
    warnings: [],
    ...over,
  };
}

/** A running Paper server (the example of the spec): three folders, datapacks and per-world config to carry over. */
export function makeBukkitResetPlan(over: Partial<WorldResetPlan> = {}): WorldResetPlan {
  return makeResetPlan({
    level_name: "bruh",
    layout: "bukkit",
    folders: [
      makeResetFolder({ path: "bruh", keepable: { datapacks: ["datapacks"], config: ["paper-world.yml"] }, bytes: 1024 ** 3 }),
      makeResetFolder({ role: "nether", path: "bruh_nether", markers: ["DIM-1", "level.dat"], keepable: { config: ["paper-world.yml"] }, bytes: 512 * 1024 ** 2 }),
      makeResetFolder({ role: "end", path: "bruh_the_end", markers: ["DIM1", "level.dat"], bytes: 512 * 1024 ** 2 }),
    ],
    delete: ["bruh", "bruh_nether", "bruh_the_end"],
    seed: { value: "-4172144997902289642", source: "server.properties" },
    running: true,
    ...over,
  });
}

/**
 * The fields a panel with game definitions adds to a Minecraft instance, written out the way the
 * backend states the rule (backend contract, MinecraftDefinition.capabilities): every capability,
 * `content` only when content_kind is set, `modpacks` only for modpack servers.
 */
export function withGameFields(i: Instance): Instance {
  const caps: GameCapability[] = [
    "broadcast",
    "console_commands",
    "consistent_backups",
    "diagnostics",
    "eula",
    "jvm",
    "player_actions",
    "players",
    "properties",
    "query",
    "software_update",
    "world_import_export",
    "world_reset",
  ];
  if (i.content_kind !== "none") caps.push("content");
  if (i.is_modpack) caps.push("modpacks");
  return { ...i, game: "minecraft", game_label: "Minecraft", capabilities: caps.sort(), memory_model: "jvm" };
}

/** The Factorio entry of GET /catalog/games as the backend describes it (contract B3). */
export function factorioGame(over: Partial<GameInfo> = {}): GameInfo {
  return {
    id: "factorio",
    label: "Factorio",
    variants: [],
    capabilities: [
      "broadcast",
      "config_files",
      "consistent_backups",
      "console_commands",
      "player_actions",
      "players",
      "software_update",
      "world_import_export",
      "world_reset",
    ],
    memory_model: "plain",
    default_memory_mb: 2048,
    notes: ["Headless server; no account needed."],
    ports: [{ name: "game", label: "Game", protocol: "udp", container_port: 34197, default_host_port: 34197 }],
    options: [
      { key: "space_age", type: "bool", label: "Space Age", default: false, pinned: true, help: "Players need the expansion to join." },
    ],
    config_files: [
      {
        id: "server-settings",
        path: "config/server-settings.json",
        codec: "json",
        label: "Server settings",
        fields: [
          { key: "name", type: "string", label: "Server name", initial: true },
          { key: "visibility.public", type: "bool", label: "Public listing", default: false, initial: true },
          { key: "game_password", type: "string", label: "Game password", secret: true, initial: true },
          { key: "max_players", type: "int", label: "Max players", default: 0, min: 0, max: 65535, initial: true },
          { key: "allow_commands", type: "enum", label: "Commands", options: ["true", "false", "admins-only"], default: "admins-only" },
        ],
      },
      {
        id: "map-gen-settings",
        path: "config/map-gen-settings.json",
        codec: "json",
        label: "Map generation",
        fields: [{ key: "seed", type: "int", label: "Seed", nullable: true, default: null, initial: true, restart: false }],
      },
    ],
    secrets: [],
    setup_steps: [],
    stop_words: ["/quit"],
    keeps_command_slash: true,
    command_hint: "/players online",
    has_versions: true,
    min_memory_mb: 512,
    player_name_pattern: "[A-Za-z0-9_.-]{1,60}",
    player_list_labels: { whitelist: "Whitelist", ops: "Admins", bans: "Bans" },
    world: { kind: "saves", extension: ".zip", reset_fields: [{ key: "seed", type: "int", label: "Map seed", nullable: true }] },
    ...over,
  };
}

/** A running Factorio server as the backend sends it (game fields, named ports, pinned options). */
export function makeFactorioInstance(over: Partial<Instance> = {}): Instance {
  return makeInstance({
    id: "22222222-2222-2222-2222-222222222222",
    name: "Factory",
    server_type: "" as Instance["server_type"],
    minecraft_version: "2.1.24",
    java_version: 0,
    image: "factoriotools/factorio:2.1.24-rootless",
    memory_mb: 2048,
    memory_overhead_mb: 0,
    container_memory_mb: 2048,
    game_port: 34197,
    stop_timeout: 120,
    game: "factorio",
    game_label: "Factorio",
    capabilities: factorioGame().capabilities,
    memory_model: "plain",
    ports: [{ name: "game", label: "Game", protocol: "udp", container_port: 34197, host_port: 34197, host_ip: "0.0.0.0" }],
    game_options: { space_age: false, save_name: "world" },
    setup: null,
    connection: {
      host_port: 34197,
      container_port: 34197,
      bind_address: "0.0.0.0",
      addresses: [],
      notes: ["Forward UDP 34197 on your router for players outside your network."],
      ports: [
        {
          name: "game",
          label: "Game",
          protocol: "udp",
          container_port: 34197,
          host_port: 34197,
          addresses: [{ kind: "lan", label: "LAN", address: "192.0.2.10:34197" }],
        },
      ],
    },
    ...over,
  });
}
