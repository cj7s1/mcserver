/**
 * Create-server wizard model: state shape, defaults, zod validation per step and the
 * POST /instances/ payload (CreateInstanceSerializer in backend/apps/instances/serializers.py).
 */
import { z } from "zod";
import type { Instance, Job, JavaInfo, Project, Provider, ServerType } from "@/lib/types";
import { DEFAULT_MODPACK_FILTERS, type ModpackFilters } from "../modpacks/ModpackBrowser";
import type { PortsInfo, ResolvedModpackData } from "../modpacks/modpackQueries";

export type Category = "vanilla" | "plugins" | "mods" | "modpack" | "import";
export type StandardCategory = "vanilla" | "plugins" | "mods";
export type StepId = "type" | "software" | "resources" | "gameplay" | "review";
export type JvmPreset = "aikar" | "meowice" | "none";

export const CATEGORY_TYPES: Record<StandardCategory, ServerType[]> = {
  vanilla: ["VANILLA"],
  plugins: ["PAPER", "PURPUR", "FOLIA"],
  mods: ["FABRIC", "QUILT", "FORGE", "NEOFORGE"],
};

export const TYPE_LABELS: Record<string, string> = {
  VANILLA: "Vanilla",
  PAPER: "Paper",
  PURPUR: "Purpur",
  FOLIA: "Folia",
  FABRIC: "Fabric",
  QUILT: "Quilt",
  FORGE: "Forge",
  NEOFORGE: "NeoForge",
  MODRINTH: "Modrinth modpack",
  AUTO_CURSEFORGE: "CurseForge modpack",
  FTBA: "FTB modpack",
};

export const STEPS: { id: StepId; label: string; short: string }[] = [
  { id: "type", label: "Server type", short: "Type" },
  { id: "software", label: "Software & version", short: "Software" },
  { id: "resources", label: "Resources & network", short: "Resources" },
  { id: "gameplay", label: "Gameplay & advanced", short: "Gameplay" },
  { id: "review", label: "Review & deploy", short: "Review" },
];

export const JAVA_VERSIONS = [8, 11, 16, 17, 21, 25] as const;
export const DEFAULT_MEMORY_STANDARD = 4096;
export const DEFAULT_MEMORY_MODPACK = 6144;

/** Environment keys the backend refuses (backend/apps/instances/spec.py MANAGED_ENV). */
export const MANAGED_ENV = new Set([
  "EULA", "TYPE", "VERSION", "MEMORY", "INIT_MEMORY", "MAX_MEMORY", "JVM_OPTS", "USE_AIKAR_FLAGS", "USE_MEOWICE_FLAGS",
  "FABRIC_LOADER_VERSION", "QUILT_LOADER_VERSION", "FORGE_VERSION", "NEOFORGE_VERSION", "PAPER_BUILD", "PURPUR_BUILD",
  "FOLIABUILD", "MODRINTH_MODPACK", "MODRINTH_VERSION", "MODRINTH_PROJECT", "MODRINTH_VERSION_ID", "CF_API_KEY", "CF_SLUG",
  "CF_FILE_ID", "CF_PAGE_URL", "FTB_MODPACK_ID", "FTB_MODPACK_VERSION_ID", "STOP_DURATION", "MODPACK_PLATFORM",
  "MOD_PLATFORM", "UID", "GID", "SERVER_PORT", "RCON_PORT", "ENABLE_RCON", "RCON_PASSWORD", "RCON_PASSWORD_FILE",
  "SKIP_SUDO", "LOAD_ENV_FROM_FILE", "LOAD_ENV_FROM_ARCHIVE", "LOAD_ENV_FROM_GENERIC_PACK",
]);

export interface EnvRow {
  id: number;
  key: string;
  value: string;
}

export interface WizardState {
  category: Category | null;
  serverType: ServerType | null;
  /** null = use the catalog default (newest stable). */
  minecraftVersion: string | null;
  showSnapshots: boolean;
  /** null = default build (recommended / newest stable); "" = "Latest (auto)" resolved by the runtime. */
  loaderVersion: string | null;
  /** "" = automatic (catalog / resolve); otherwise a Java major version. */
  javaOverride: string;
  // modpack
  browse: ModpackFilters;
  provider: Provider;
  projectId: string;
  projectHit: Project | null;
  versionId: string;
  // resources & network
  name: string | null;
  memoryMb: number | null;
  gamePort: string | null;
  hostIp: string;
  start: boolean;
  // gameplay
  motd: string;
  difficulty: string;
  gamemode: string;
  maxPlayers: string;
  onlineMode: boolean;
  seed: string;
  whitelist: boolean;
  // advanced
  jvmPreset: JvmPreset;
  jvmArgs: string;
  env: EnvRow[];
  description: string;
  group: string;
  iconUrl: string;
  acceptEula: boolean;
}

export const GAMEPLAY_DEFAULTS = { difficulty: "easy", gamemode: "survival", maxPlayers: "20" } as const;

export function initialState(): WizardState {
  return {
    category: null,
    serverType: null,
    minecraftVersion: null,
    showSnapshots: false,
    loaderVersion: null,
    javaOverride: "",
    browse: { ...DEFAULT_MODPACK_FILTERS },
    provider: "modrinth",
    projectId: "",
    projectHit: null,
    versionId: "",
    name: null,
    memoryMb: null,
    gamePort: null,
    hostIp: "",
    start: true,
    motd: "",
    difficulty: GAMEPLAY_DEFAULTS.difficulty,
    gamemode: GAMEPLAY_DEFAULTS.gamemode,
    maxPlayers: GAMEPLAY_DEFAULTS.maxPlayers,
    onlineMode: true,
    seed: "",
    whitelist: false,
    jvmPreset: "aikar",
    jvmArgs: "",
    env: [],
    description: "",
    group: "",
    iconUrl: "",
    acceptEula: false,
  };
}

/** Values derived from the state + API data (defaults the user has not overridden). */
export interface Derived {
  isModpack: boolean;
  serverType: ServerType | null;
  minecraftVersion: string;
  loaderVersion: string;
  loaderLabel: string | null;
  java: JavaInfo | null;
  javaVersion: number | null;
  name: string;
  suggestedName: string;
  memoryMb: number;
  defaultMemoryMb: number;
  gamePort: string;
  ports: PortsInfo | undefined;
  resolved: ResolvedModpackData | undefined;
  resolveOk: boolean;
  resolvePending: boolean;
  softwareSummary: string;
}

export function containerLimitMb(memoryMb: number): number {
  return memoryMb + Math.max(512, Math.floor(memoryMb * 0.25));
}

// --- validation ---------------------------------------------------------------------

export type FieldErrors = Record<string, string>;

const ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

function issuesToErrors(error: z.ZodError): FieldErrors {
  const out: FieldErrors = {};
  for (const issue of error.issues) {
    const key = issue.path.map(String).join(".") || "form";
    if (!out[key]) out[key] = issue.message;
  }
  return out;
}

export function validateJvmArgs(value: string): string | null {
  if (/[\r\n\0]/.test(value.trim())) return "JVM arguments must be on a single line";
  for (const arg of value.split(/\s+/).filter(Boolean)) {
    if (!arg.startsWith("-")) return `'${arg}' does not look like a JVM option (must start with '-')`;
    if (arg.startsWith("-Xmx") || arg.startsWith("-Xms")) return "Set heap size with the memory setting, not -Xmx/-Xms";
  }
  return null;
}

const intInRange = (label: string, min: number, max: number) =>
  z
    .string()
    .trim()
    .min(1, `${label} is required`)
    .regex(/^\d+$/, `${label} must be a whole number`)
    .refine((v) => Number(v) >= min && Number(v) <= max, `${label} must be between ${min} and ${max}`);

export function validateStep(step: StepId, state: WizardState, d: Derived): FieldErrors {
  if (step === "type") {
    if (!state.category) return { category: "Choose what kind of server to create" };
    if (state.category === "import") return { category: "Importing happens after creation — see the steps below" };
    return {};
  }
  if (step === "software") {
    if (d.isModpack) {
      const r = z
        .object({
          projectId: z.string().min(1, "Choose a modpack"),
          versionId: z.string().min(1, "Choose a release (releases are pinned)"),
        })
        .safeParse({ projectId: state.projectId, versionId: state.versionId });
      const errors = r.success ? {} : issuesToErrors(r.error);
      if (r.success && !d.resolveOk) {
        errors.versionId = d.resolvePending ? "Still checking this release…" : "This release cannot be deployed; pick another release";
      }
      return errors;
    }
    const r = z
      .object({
        serverType: z.string().min(1, "Choose the server software"),
        minecraftVersion: z.string().min(1, "Choose a Minecraft version"),
      })
      .safeParse({ serverType: d.serverType ?? "", minecraftVersion: d.minecraftVersion });
    return r.success ? {} : issuesToErrors(r.error);
  }
  if (step === "resources") {
    const inUse = new Map((d.ports?.in_use ?? []).map((p) => [p.port, p.used_by]));
    const r = z
      .object({
        name: z.string().trim().min(1, "Give the server a name").max(80, "Name must be at most 80 characters"),
        memoryMb: z
          .number({ error: "Memory is required" })
          .int("Memory must be a whole number of MB")
          .min(512, "Memory must be at least 512 MB")
          .max(262144, "Memory must be at most 256 GB"),
        gamePort: intInRange("Port", 1024, 65535).superRefine((v, ctx) => {
          const owner = inUse.get(Number(v));
          if (owner) ctx.addIssue({ code: "custom", message: `Port ${v} is already used by ${owner}` });
        }),
        hostIp: z.union([z.literal(""), z.ipv4(), z.ipv6()], { error: "Enter a valid IPv4 or IPv6 address" }),
      })
      .safeParse({ name: d.name, memoryMb: d.memoryMb, gamePort: d.gamePort, hostIp: state.hostIp.trim() });
    return r.success ? {} : issuesToErrors(r.error);
  }
  if (step === "gameplay") {
    const r = z
      .object({
        motd: z.string().max(512, "MOTD is too long"),
        maxPlayers: intInRange("Max players", 1, 10000),
        seed: z.string().max(200, "Seed is too long"),
        jvmArgs: z
          .string()
          .max(4000, "JVM arguments are too long")
          .superRefine((v, ctx) => {
            const msg = validateJvmArgs(v);
            if (msg) ctx.addIssue({ code: "custom", message: msg });
          }),
        env: z
          .array(z.object({ key: z.string(), value: z.string() }))
          .max(100, "At most 100 variables")
          .superRefine((rows, ctx) => {
            const seen = new Set<string>();
            rows.forEach((row, i) => {
              const key = row.key.trim();
              if (!key && !row.value) return;
              if (!ENV_KEY.test(key)) ctx.addIssue({ code: "custom", path: [i], message: "Use letters, digits and _ (not starting with a digit)" });
              else if (MANAGED_ENV.has(key.toUpperCase()))
                ctx.addIssue({ code: "custom", path: [i], message: `${key} is managed by MCPanel; use the dedicated setting` });
              else if (seen.has(key)) ctx.addIssue({ code: "custom", path: [i], message: `${key} is defined twice` });
              else if (row.value.length > 8192) ctx.addIssue({ code: "custom", path: [i], message: "Value is too long" });
              seen.add(key);
            });
          }),
        description: z.string().max(2000, "Description must be at most 2000 characters"),
        group: z.string().max(60, "Group must be at most 60 characters"),
        iconUrl: z.union([z.literal(""), z.url({ protocol: /^https?$/, error: "Enter a full http(s) URL" }).max(500, "URL is too long")]),
      })
      .safeParse({
        motd: state.motd,
        maxPlayers: state.maxPlayers,
        seed: state.seed,
        jvmArgs: state.jvmArgs,
        env: state.env,
        description: state.description,
        group: state.group,
        iconUrl: state.iconUrl.trim(),
      });
    return r.success ? {} : issuesToErrors(r.error);
  }
  // review
  return state.acceptEula ? {} : { acceptEula: "You must accept the Minecraft EULA to deploy a server" };
}

/** Which step owns a (client or server) error key, used to jump to the first error. */
export function stepForField(key: string): StepId {
  const k = key.split(".")[0];
  if (k === "category") return "type";
  if (["serverType", "minecraftVersion", "loaderVersion", "projectId", "versionId", "javaOverride"].includes(k)) return "software";
  if (["name", "memoryMb", "gamePort", "hostIp", "start"].includes(k)) return "resources";
  if (k === "acceptEula" || k === "form") return "review";
  return "gameplay";
}

/** Backend field names -> wizard field keys. */
export const SERVER_FIELD_MAP: Record<string, string> = {
  mode: "category",
  name: "name",
  description: "description",
  group: "group",
  icon_url: "iconUrl",
  accept_eula: "acceptEula",
  server_type: "serverType",
  minecraft_version: "minecraftVersion",
  loader_version: "loaderVersion",
  modpack_provider: "projectId",
  modpack_project_id: "projectId",
  modpack_version_id: "versionId",
  java_version: "javaOverride",
  memory_mb: "memoryMb",
  jvm_flags_preset: "jvmPreset",
  jvm_args: "jvmArgs",
  env: "env",
  game_port: "gamePort",
  host_ip: "hostIp",
  server_properties: "serverProperties",
  start: "start",
  non_field_errors: "form",
};

// --- payload ------------------------------------------------------------------------

export interface CreatePayload {
  mode: "standard" | "modpack";
  name: string;
  accept_eula: boolean;
  description?: string;
  group?: string;
  icon_url?: string;
  server_type?: ServerType;
  minecraft_version?: string;
  loader_version?: string;
  modpack_provider?: Provider;
  modpack_project_id?: string;
  modpack_version_id?: string;
  java_version?: number;
  memory_mb: number;
  jvm_flags_preset: JvmPreset;
  jvm_args?: string;
  env?: Record<string, string>;
  game_port: number;
  host_ip?: string;
  server_properties?: Record<string, string | number | boolean>;
  start: boolean;
}

export interface CreateResponse {
  instance: Instance;
  job: Job;
  warnings: string[];
  java: JavaInfo;
}

export function serverProperties(state: WizardState): Record<string, string | number | boolean> {
  const props: Record<string, string | number | boolean> = {};
  if (state.motd.trim()) props.motd = state.motd.trim();
  if (state.difficulty !== GAMEPLAY_DEFAULTS.difficulty) props.difficulty = state.difficulty;
  if (state.gamemode !== GAMEPLAY_DEFAULTS.gamemode) props.gamemode = state.gamemode;
  if (state.maxPlayers.trim() !== GAMEPLAY_DEFAULTS.maxPlayers) props["max-players"] = Number(state.maxPlayers);
  if (!state.onlineMode) props["online-mode"] = false;
  if (state.seed.trim()) props["level-seed"] = state.seed.trim();
  if (state.whitelist) {
    props["white-list"] = true;
    props["enforce-whitelist"] = true;
  }
  return props;
}

export function envObject(rows: EnvRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) if (r.key.trim()) out[r.key.trim()] = r.value;
  return out;
}

export function buildPayload(state: WizardState, d: Derived): CreatePayload {
  const payload: CreatePayload = {
    mode: d.isModpack ? "modpack" : "standard",
    name: d.name.trim(),
    accept_eula: state.acceptEula,
    memory_mb: d.memoryMb,
    jvm_flags_preset: state.jvmPreset,
    game_port: Number(d.gamePort),
    start: state.start,
  };
  if (d.isModpack) {
    payload.modpack_provider = state.provider;
    payload.modpack_project_id = state.projectId;
    payload.modpack_version_id = state.versionId;
  } else {
    payload.server_type = d.serverType ?? undefined;
    payload.minecraft_version = d.minecraftVersion;
    payload.loader_version = d.loaderVersion;
  }
  if (state.javaOverride) payload.java_version = Number(state.javaOverride);
  if (state.description.trim()) payload.description = state.description.trim();
  if (state.group.trim()) payload.group = state.group.trim();
  if (state.iconUrl.trim()) payload.icon_url = state.iconUrl.trim();
  if (state.jvmArgs.trim()) payload.jvm_args = state.jvmArgs.trim();
  const env = envObject(state.env);
  if (Object.keys(env).length) payload.env = env;
  if (state.hostIp.trim()) payload.host_ip = state.hostIp.trim();
  const props = serverProperties(state);
  if (Object.keys(props).length) payload.server_properties = props;
  return payload;
}
