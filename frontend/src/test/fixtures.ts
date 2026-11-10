/** Shared test fixtures (not used by product code). */
import type { Instance, WorldResetFolder, WorldResetPlan } from "@/lib/types";

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
