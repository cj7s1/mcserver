/**
 * Create wizard for games other than Minecraft (backend contract B5), driven by the game's catalog
 * entry (GET /catalog/games): version (from GET /catalog/games/<id>/versions), the options pinned at
 * creation (Factorio: Space Age, off by default), plain container memory, the named ports, the config
 * fields marked `initial`, and the secrets. Minecraft keeps its own wizard (wizardModel.ts).
 */
import { fromText, initialTexts, valuesForCreate } from "@/lib/game-config";
import type { ConfigField, ConfigValue, GameInfo, GameVariantInfo, GameVersionsResponse } from "@/lib/types";
import type { FieldErrors, StepId } from "./wizardModel";

export const GAME_WIZARD_STEPS: { id: StepId; label: string; short: string }[] = [
  { id: "game", label: "Game", short: "Game" },
  { id: "software", label: "Version & options", short: "Version" },
  { id: "resources", label: "Resources & network", short: "Resources" },
  { id: "settings", label: "Game settings", short: "Settings" },
  { id: "review", label: "Review & deploy", short: "Review" },
];

export interface GameWizardState {
  /** The game variant (Terraria: vanilla, tmodloader, tshock); null for a game without variants. */
  variant: string | null;
  /** null = the catalog's latest stable version */
  version: string | null;
  /** option key -> form text (defaults from the catalog) */
  options: Record<string, string>;
  name: string;
  /** memory text in MB; null = the game's default */
  memory: string | null;
  /** port name -> host port text; "" = automatic (the game's port when free) */
  ports: Record<string, string>;
  hostIp: string;
  /** config file id -> field key -> form text */
  settings: Record<string, Record<string, string>>;
  /** secret key -> value */
  secrets: Record<string, string>;
  description: string;
  group: string;
  start: boolean;
  nightlyBackup: boolean;
  node: number | null;
}

/** The config fields the wizard asks for (initial: true), per file. */
export function initialFields(game: GameInfo): { file: string; label: string; fields: ConfigField[] }[] {
  return (game.config_files ?? [])
    .map((f) => ({ file: f.id, label: f.label || f.path, fields: f.fields.filter((x) => x.initial) }))
    .filter((f) => f.fields.length > 0);
}

/** The variants the wizard offers: those of a game whose catalog describes them (Minecraft's server types are not). */
export function wizardVariants(game: GameInfo): GameVariantInfo[] {
  return (game.variants ?? []).filter((v) => v.default_memory_mb !== undefined || v.notes !== undefined);
}

function forVariant<T extends { variants?: string[] }>(items: T[] | undefined, variant: string | null): T[] | undefined {
  if (!items) return items;
  return items.filter((x) => !x.variants?.length || (variant !== null && x.variants.includes(variant)));
}

/**
 * The catalog entry as one variant sees it: its memory, notes, capabilities and stop words, and only the
 * options, config files, secrets and setup steps it has. A game without variant data is returned as it is.
 */
export function variantView(game: GameInfo, variant: string | null): GameInfo {
  const info = wizardVariants(game).find((v) => v.id === variant);
  if (!info) return game;
  const secrets = forVariant(game.secrets, variant);
  const secretKeys = new Set((secrets ?? []).map((s) => s.key));
  return {
    ...game,
    default_memory_mb: info.default_memory_mb ?? game.default_memory_mb,
    notes: info.notes ?? game.notes,
    capabilities: info.capabilities ?? game.capabilities,
    stop_words: info.stop_words ?? game.stop_words,
    player_list_labels: info.player_list_labels ?? game.player_list_labels,
    options: forVariant(game.options, variant),
    config_files: forVariant(game.config_files, variant),
    secrets,
    setup_steps: game.setup_steps?.filter((s) => !s.secret || secretKeys.has(s.secret)),
  };
}

/** The wizard's first state; for a game with variants, of ``variant`` (default: the first). */
export function initialGameState(game: GameInfo, variant: string | null = wizardVariants(game)[0]?.id ?? null): GameWizardState {
  const view = variantView(game, variant);
  return {
    variant,
    version: null,
    options: initialTexts(view.options ?? []),
    name: "",
    memory: null,
    ports: Object.fromEntries((view.ports ?? []).map((p) => [p.name, ""])),
    hostIp: "",
    settings: Object.fromEntries(initialFields(view).map((f) => [f.file, initialTexts(f.fields)])),
    secrets: {},
    description: "",
    group: "",
    start: true,
    nightlyBackup: true,
    node: null,
  };
}

/** The version the wizard creates: the chosen one, else the catalog's latest, else the newest stable listed. */
export function effectiveVersion(state: GameWizardState, catalog: GameVersionsResponse | undefined): string {
  if (state.version) return state.version;
  if (catalog?.latest) return catalog.latest;
  return catalog?.versions.find((v) => v.stable)?.id ?? catalog?.versions[0]?.id ?? "";
}

/** The memory the server gets (MB); NaN when the text is not a whole number. */
export function memoryMb(game: GameInfo, state: Pick<GameWizardState, "memory">): number {
  if (state.memory === null) return game.default_memory_mb;
  return /^\d+$/.test(state.memory.trim()) ? Number(state.memory.trim()) : Number.NaN;
}

export function defaultName(game: GameInfo, version: string): string {
  return `${game.label}${version ? ` ${version}` : ""}`.slice(0, 80);
}

const IP_RE = /^(\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F:.]*:[0-9a-fA-F:.]*)$/;

export function validateGameStep(step: StepId, game: GameInfo, state: GameWizardState, version: string): FieldErrors {
  const errors: FieldErrors = {};
  if (step === "software") {
    if (!version.trim()) errors.version = "Choose a version";
    else if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(version.trim())) errors.version = "Not a valid version";
    const variants = wizardVariants(game);
    if (variants.length && !variants.some((v) => v.id === state.variant)) errors.variant = "Choose a variant";
    for (const o of game.options ?? []) {
      const parsed = fromText(o, state.options[o.key] ?? "");
      if ("error" in parsed) errors[`options.${o.key}`] = parsed.error;
    }
  } else if (step === "resources") {
    const name = state.name.trim() || defaultName(game, version);
    if (!name) errors.name = "Give the server a name";
    else if (name.length > 80) errors.name = "Name must be at most 80 characters";
    const min = Math.max(512, game.min_memory_mb ?? 512);
    const mem = memoryMb(game, state);
    if (!Number.isInteger(mem)) errors.memoryMb = "Memory must be a whole number of MB";
    else if (mem < min) errors.memoryMb = `Memory must be at least ${min} MB`;
    else if (mem > 262144) errors.memoryMb = "Memory must be at most 256 GB";
    const seen = new Map<string, string>();
    for (const p of game.ports ?? []) {
      const t = (state.ports[p.name] ?? "").trim();
      if (!t) continue;
      if (!/^\d+$/.test(t) || Number(t) < 1024 || Number(t) > 65535) {
        errors[`ports.${p.name}`] = "A port between 1024 and 65535, or empty for automatic";
        continue;
      }
      const key = `${Number(t)}/${p.protocol}`;
      if (seen.has(key)) errors[`ports.${p.name}`] = `Same port and protocol as ${seen.get(key)}`;
      seen.set(key, p.label);
    }
    if (state.hostIp.trim() && !IP_RE.test(state.hostIp.trim())) errors.hostIp = "Enter a valid IPv4 or IPv6 address";
  } else if (step === "settings") {
    for (const f of initialFields(game)) {
      const { errors: e } = valuesForCreate(f.fields, state.settings[f.file] ?? {});
      for (const [k, v] of Object.entries(e)) errors[`settings.${f.file}.${k}`] = v;
    }
    if (state.description.length > 2000) errors.description = "Description must be at most 2000 characters";
    if (state.group.length > 60) errors.group = "Group must be at most 60 characters";
  } else if (step === "review") {
    for (const s of game.secrets ?? []) {
      if ((s.required || secretRequiredNow(s, state)) && !(state.secrets[s.key] ?? "").trim()) errors[`secrets.${s.key}`] = `${s.label} is required`;
    }
  }
  return errors;
}

/** A secret that is required only with one choice of an option (Satisfactory's join password with access "password"). */
export function secretRequiredNow(s: { required_when?: { option: string; value: string } }, state: Pick<GameWizardState, "options">): boolean {
  return !!s.required_when && (state.options[s.required_when.option] ?? "") === s.required_when.value;
}

/** Which step a (client or server) error key belongs to. */
export function gameStepForField(key: string): StepId {
  const k = key.split(".")[0];
  if (k === "game") return "game";
  if (["version", "variant", "options"].includes(k)) return "software";
  if (["name", "memoryMb", "memory_mb", "ports", "hostIp", "host_ip", "node"].includes(k)) return "resources";
  if (["settings", "description", "group"].includes(k)) return "settings";
  return "review";
}

export interface GameCreatePayload {
  game: string;
  variant?: string;
  name: string;
  version: string;
  memory_mb: number;
  start: boolean;
  nightly_backup: boolean;
  options?: Record<string, ConfigValue>;
  ports?: Record<string, number>;
  settings?: Record<string, Record<string, ConfigValue>>;
  secrets?: Record<string, string>;
  host_ip?: string;
  description?: string;
  group?: string;
  node?: number;
}

/** POST /instances/ body for a game other than Minecraft (contract B5). */
export function buildGamePayload(game: GameInfo, state: GameWizardState, version: string, nightlyBackupAllowed: boolean): GameCreatePayload {
  const payload: GameCreatePayload = {
    game: game.id,
    name: state.name.trim() || defaultName(game, version),
    version: version.trim(),
    memory_mb: memoryMb(game, state),
    start: state.start,
    nightly_backup: state.nightlyBackup && nightlyBackupAllowed,
  };
  if (state.variant) payload.variant = state.variant;
  // every option is sent: they are pinned on the server, so the choice is explicit
  const options: Record<string, ConfigValue> = {};
  for (const o of game.options ?? []) {
    const parsed = fromText(o, state.options[o.key] ?? "");
    if (!("error" in parsed)) options[o.key] = parsed.value;
  }
  if (Object.keys(options).length) payload.options = options;
  const ports: Record<string, number> = {};
  for (const p of game.ports ?? []) {
    const t = (state.ports[p.name] ?? "").trim();
    if (t) ports[p.name] = Number(t);
  }
  if (Object.keys(ports).length) payload.ports = ports;
  const settings: Record<string, Record<string, ConfigValue>> = {};
  for (const f of initialFields(game)) {
    const { changes } = valuesForCreate(f.fields, state.settings[f.file] ?? {});
    if (Object.keys(changes).length) settings[f.file] = changes;
  }
  if (Object.keys(settings).length) payload.settings = settings;
  const secrets: Record<string, string> = {};
  for (const s of game.secrets ?? []) {
    const v = (state.secrets[s.key] ?? "").trim();
    if (v) secrets[s.key] = v;
  }
  if (Object.keys(secrets).length) payload.secrets = secrets;
  if (state.hostIp.trim()) payload.host_ip = state.hostIp.trim();
  if (state.description.trim()) payload.description = state.description.trim();
  if (state.group.trim()) payload.group = state.group.trim();
  if (state.node !== null) payload.node = state.node;
  return payload;
}

/** Server field errors (POST /instances/) -> wizard keys. */
export function gameServerErrors(fields: Record<string, string>): FieldErrors {
  const map: Record<string, string> = { memory_mb: "memoryMb", host_ip: "hostIp", non_field_errors: "form" };
  const out: FieldErrors = {};
  for (const [k, v] of Object.entries(fields)) out[map[k] ?? k] = v;
  return out;
}
