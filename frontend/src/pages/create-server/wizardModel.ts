/**
 * Create-server wizard model: state shape, defaults, zod validation per step and the
 * POST /instances/ payload (CreateInstanceSerializer in backend/apps/instances/serializers.py).
 */
import { z } from "zod";
import type {
  Instance,
  Job,
  JavaInfo,
  Me,
  Project,
  Provider,
  Schedule,
  ServerType,
} from "@/lib/types";
import {
  DEFAULT_MODPACK_FILTERS,
  type ModpackFilters,
} from "../modpacks/ModpackBrowser";
import type {
  PortsInfo,
  ResolvedModpackData,
} from "../modpacks/modpackQueries";
import { validateImportArchive } from "./import/importModel";

export type Category = "vanilla" | "plugins" | "mods" | "modpack" | "import";
export type StandardCategory = "vanilla" | "plugins" | "mods";
export type StepId = "type" | "software" | "resources" | "gameplay" | "review";
export type JvmPreset = "aikar" | "meowice" | "none";

export const CATEGORY_TYPES: Record<StandardCategory, ServerType[]> = {
  vanilla: ["VANILLA"],
  plugins: ["PAPER", "PURPUR", "FOLIA"],
  mods: ["FABRIC", "QUILT", "FORGE", "NEOFORGE"],
};

/** Import mode: the archive can come from any standard server software. */
export const IMPORT_TYPES: ServerType[] = [
  ...CATEGORY_TYPES.vanilla,
  ...CATEGORY_TYPES.plugins,
  ...CATEGORY_TYPES.mods,
];

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

/**
 * Panel-managed environment keys (backend/apps/instances/spec.py _PANEL_ENV), checked here only as
 * a quick hint. The backend refuses more (download, command and player-rights variables, by name,
 * prefix or suffix: spec.env_override_error) and its precise message is shown on the env field
 * (SERVER_FIELD_MAP.env -> errors.env in StepGameplay).
 */
export const MANAGED_ENV = new Set([
  "EULA",
  "TYPE",
  "VERSION",
  "MEMORY",
  "INIT_MEMORY",
  "MAX_MEMORY",
  "JVM_OPTS",
  "USE_AIKAR_FLAGS",
  "USE_MEOWICE_FLAGS",
  "FABRIC_LOADER_VERSION",
  "QUILT_LOADER_VERSION",
  "FORGE_VERSION",
  "NEOFORGE_VERSION",
  "PAPER_BUILD",
  "PURPUR_BUILD",
  "FOLIABUILD",
  "MODRINTH_MODPACK",
  "MODRINTH_VERSION",
  "MODRINTH_PROJECT",
  "MODRINTH_VERSION_ID",
  "CF_API_KEY",
  "CF_SLUG",
  "CF_FILE_ID",
  "CF_PAGE_URL",
  "FTB_MODPACK_ID",
  "FTB_MODPACK_VERSION_ID",
  "STOP_DURATION",
  "MODPACK_PLATFORM",
  "MOD_PLATFORM",
  "UID",
  "GID",
  "SERVER_PORT",
  "RCON_PORT",
  "ENABLE_RCON",
  "RCON_PASSWORD",
  "RCON_PASSWORD_FILE",
  "SKIP_SUDO",
  "LOAD_ENV_FROM_FILE",
  "LOAD_ENV_FROM_ARCHIVE",
  "LOAD_ENV_FROM_GENERIC_PACK",
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
  /** "url": deploy a CurseForge pack by page URL/slug (no API key needed; the runtime installs it). */
  modpackSource: "browse" | "url";
  cfRef: string;
  cfMinecraftVersion: string;
  cfFileId: string;
  // resources & network
  name: string | null;
  memoryMb: number | null;
  gamePort: string | null;
  hostIp: string;
  start: boolean;
  /** Create the default "Nightly backup" schedule with the server (sent only when the user may schedule backups). */
  nightlyBackup: boolean;
  /** Node id; null = the panel's default node. */
  node: number | null;
  // import
  importFile: File | null;
  /** Archive files replace the new server's defaults (the server.properties written at creation). */
  importOverwrite: boolean;
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

export const GAMEPLAY_DEFAULTS = {
  difficulty: "easy",
  gamemode: "survival",
  maxPlayers: "20",
} as const;

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
    modpackSource: "browse",
    cfRef: "",
    cfMinecraftVersion: "",
    cfFileId: "",
    name: null,
    memoryMb: null,
    gamePort: null,
    hostIp: "",
    start: true,
    nightlyBackup: true,
    node: null,
    importFile: null,
    importOverwrite: true,
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
  /** Name of the chosen node when the panel has more than one; otherwise null. */
  nodeName: string | null;
  /** The user may give the new server a backup schedule (nightlyBackupAllowed); the option is hidden otherwise. */
  nightlyBackupAllowed: boolean;
}

/**
 * Whether the signed-in user may add a backup schedule to the server they are about to create, i.e.
 * whether the wizard offers "Nightly backup". Mirrors the backend (InstanceListView.post, then the
 * rule of ScheduleListView.post: "instance.schedules" plus "instance.backup" on that server): the
 * owner may; a creator without global "instance.view" becomes Server manager of the new server,
 * which includes both; everyone else needs both in their own role. The backend decides in the end
 * and creates the server without the schedule when the user may not.
 */
export function nightlyBackupAllowed(user: Pick<Me, "is_owner" | "permissions"> | null): boolean {
  if (!user) return false;
  if (user.is_owner) return true;
  const perms = user.permissions;
  if (!perms.includes("instance.view")) return true;
  return perms.includes("instance.schedules") && perms.includes("instance.backup");
}

/** Container memory limit for a heap size; mirrors Instance.container_memory_mb in the backend
 * (modded: heap + max(1536, 40%), others: heap + max(768, 30%) for off-heap memory). */
export function containerLimitMb(memoryMb: number, modded: boolean): number {
  return memoryMb + (modded ? Math.max(1536, Math.floor(memoryMb * 0.4)) : Math.max(768, Math.floor(memoryMb * 0.3)));
}

/** Whether the wizard selection runs mods (modded loaders and modpacks). */
export function isModded(state: WizardState, d: Derived): boolean {
  return (
    d.isModpack ||
    state.category === "mods" ||
    (state.category === "import" &&
      !!d.serverType &&
      CATEGORY_TYPES.mods.includes(d.serverType))
  );
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
  if (/[\r\n\0]/.test(value.trim()))
    return "JVM arguments must be on a single line";
  for (const arg of value.split(/\s+/).filter(Boolean)) {
    if (!arg.startsWith("-"))
      return `'${arg}' does not look like a JVM option (must start with '-')`;
    if (arg.startsWith("-Xmx") || arg.startsWith("-Xms"))
      return "Set heap size with the memory setting, not -Xmx/-Xms";
  }
  return null;
}

const intInRange = (label: string, min: number, max: number) =>
  z
    .string()
    .trim()
    .min(1, `${label} is required`)
    .regex(/^\d+$/, `${label} must be a whole number`)
    .refine(
      (v) => Number(v) >= min && Number(v) <= max,
      `${label} must be between ${min} and ${max}`,
    );

export function validateStep(
  step: StepId,
  state: WizardState,
  d: Derived,
): FieldErrors {
  if (step === "type") {
    if (!state.category)
      return { category: "Choose what kind of server to create" };
    return {};
  }
  if (step === "software") {
    if (d.isModpack && state.modpackSource === "url") {
      const r = z
        .object({
          cfRef: z
            .string()
            .trim()
            .refine(
              (v) => CF_REF.test(v),
              "Paste the CurseForge modpack page URL (curseforge.com/minecraft/modpacks/…) or its slug",
            ),
          cfMinecraftVersion: z
            .string()
            .trim()
            .regex(
              /^\d+\.\d+(\.\d+)?$/,
              "Enter the pack's Minecraft version, e.g. 1.20.1 (shown on its CurseForge page)",
            ),
          cfFileId: z
            .string()
            .trim()
            .regex(
              /^\d{0,12}$/,
              "File ids are numbers (from the …/files/<id> URL)",
            ),
        })
        .safeParse({
          cfRef: state.cfRef,
          cfMinecraftVersion: state.cfMinecraftVersion,
          cfFileId: state.cfFileId,
        });
      return r.success ? {} : issuesToErrors(r.error);
    }
    if (d.isModpack) {
      const r = z
        .object({
          projectId: z.string().min(1, "Choose a modpack"),
          versionId: z
            .string()
            .min(1, "Choose a release (releases are pinned)"),
        })
        .safeParse({ projectId: state.projectId, versionId: state.versionId });
      const errors = r.success ? {} : issuesToErrors(r.error);
      if (r.success && !d.resolveOk) {
        errors.versionId = d.resolvePending
          ? "Still checking this release…"
          : "This release cannot be deployed; pick another release";
      }
      return errors;
    }
    const r = z
      .object({
        serverType: z.string().min(1, "Choose the server software"),
        minecraftVersion: z.string().min(1, "Choose a Minecraft version"),
      })
      .safeParse({
        serverType: d.serverType ?? "",
        minecraftVersion: d.minecraftVersion,
      });
    const errors = r.success ? {} : issuesToErrors(r.error);
    if (state.category === "import") {
      const archive = validateImportArchive(state.importFile);
      if (archive) errors.importFile = archive;
    }
    return errors;
  }
  if (step === "resources") {
    const inUse = new Map(
      (d.ports?.in_use ?? []).map((p) => [p.port, p.used_by]),
    );
    const r = z
      .object({
        name: z
          .string()
          .trim()
          .min(1, "Give the server a name")
          .max(80, "Name must be at most 80 characters"),
        memoryMb: z
          .number({ error: "Memory is required" })
          .int("Memory must be a whole number of MB")
          .min(512, "Memory must be at least 512 MB")
          .max(262144, "Memory must be at most 256 GB"),
        gamePort: intInRange("Port", 1024, 65535).superRefine((v, ctx) => {
          const owner = inUse.get(Number(v));
          if (owner)
            ctx.addIssue({
              code: "custom",
              message: `Port ${v} is already used by ${owner}`,
            });
        }),
        hostIp: z.union([z.literal(""), z.ipv4(), z.ipv6()], {
          error: "Enter a valid IPv4 or IPv6 address",
        }),
      })
      .safeParse({
        name: d.name,
        memoryMb: d.memoryMb,
        gamePort: d.gamePort,
        hostIp: state.hostIp.trim(),
      });
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
              if (!ENV_KEY.test(key))
                ctx.addIssue({
                  code: "custom",
                  path: [i],
                  message:
                    "Use letters, digits and _ (not starting with a digit)",
                });
              else if (MANAGED_ENV.has(key.toUpperCase()))
                ctx.addIssue({
                  code: "custom",
                  path: [i],
                  message: `${key} is managed by MCPanel; use the dedicated setting`,
                });
              else if (seen.has(key))
                ctx.addIssue({
                  code: "custom",
                  path: [i],
                  message: `${key} is defined twice`,
                });
              else if (row.value.length > 8192)
                ctx.addIssue({
                  code: "custom",
                  path: [i],
                  message: "Value is too long",
                });
              seen.add(key);
            });
          }),
        description: z
          .string()
          .max(2000, "Description must be at most 2000 characters"),
        group: z.string().max(60, "Group must be at most 60 characters"),
        iconUrl: z.union([
          z.literal(""),
          z
            .url({ protocol: /^https?$/, error: "Enter a full http(s) URL" })
            .max(500, "URL is too long"),
        ]),
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
  return state.acceptEula
    ? {}
    : { acceptEula: "You must accept the Minecraft EULA to deploy a server" };
}

/** CurseForge modpack page URL (optionally …/files/<id>) or bare slug; mirrors the backend parser. */
export const CF_REF =
  /^(?:https:\/\/(?:www\.)?curseforge\.com\/minecraft\/modpacks\/[a-z0-9-]{1,128}(?:\/files\/\d{1,12})?\/?(?:[?#].*)?|[a-z0-9][a-z0-9-]{0,127})$/;

/** Which step owns a (client or server) error key, used to jump to the first error. */
export function stepForField(key: string): StepId {
  const k = key.split(".")[0];
  if (k === "category") return "type";
  if (
    [
      "serverType",
      "minecraftVersion",
      "loaderVersion",
      "projectId",
      "versionId",
      "javaOverride",
      "cfRef",
      "cfMinecraftVersion",
      "cfFileId",
      "importFile",
    ].includes(k)
  )
    return "software";
  if (["name", "memoryMb", "gamePort", "hostIp", "start", "nightlyBackup", "node"].includes(k))
    return "resources";
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
  nightly_backup: "nightlyBackup",
  node: "node",
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
  /** Also create the default nightly backup schedule (the backend skips it when the user may not schedule backups). */
  nightly_backup: boolean;
  /** Omitted = the default node. */
  node?: number;
}

export interface CreateResponse {
  instance: Instance;
  job: Job;
  warnings: string[];
  java: JavaInfo;
  /** The nightly backup schedule created with the server; null when not asked for or not permitted. */
  backup_schedule?: Schedule | null;
}

export function serverProperties(
  state: WizardState,
): Record<string, string | number | boolean> {
  const props: Record<string, string | number | boolean> = {};
  if (state.motd.trim()) props.motd = state.motd.trim();
  if (state.difficulty !== GAMEPLAY_DEFAULTS.difficulty)
    props.difficulty = state.difficulty;
  if (state.gamemode !== GAMEPLAY_DEFAULTS.gamemode)
    props.gamemode = state.gamemode;
  if (state.maxPlayers.trim() !== GAMEPLAY_DEFAULTS.maxPlayers)
    props["max-players"] = Number(state.maxPlayers);
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
    // an imported server stays stopped: the archive is uploaded into it after creation
    start: state.category === "import" ? false : state.start,
    // offered for every kind of server, imports included; never sent as true while the option is hidden
    nightly_backup: state.nightlyBackup && d.nightlyBackupAllowed,
  };
  if (state.node !== null) payload.node = state.node;
  if (d.isModpack && state.modpackSource === "url") {
    payload.modpack_provider = "curseforge";
    payload.modpack_project_id = state.cfRef.trim();
    payload.minecraft_version = state.cfMinecraftVersion.trim();
    if (state.cfFileId.trim())
      payload.modpack_version_id = state.cfFileId.trim();
  } else if (d.isModpack) {
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
