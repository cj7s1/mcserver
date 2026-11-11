/**
 * What the UI knows about an instance's game: id and label, capabilities, memory model, and the
 * labels and colours of its variants (server types). Panels with game definitions send `game`,
 * `game_label`, `capabilities` and `memory_model` with every instance; an older panel sends none of
 * them, and the instance is then a Minecraft server gated exactly as before (content_kind, is_modpack).
 */
import { Axe, Biohazard, Bug, Cog, Crosshair, Factory, Gamepad2, Hammer, PawPrint, Pickaxe, TreePine, type LucideIcon } from "lucide-react";
import type { GameCapability, GameInfo, Instance, MemoryModel } from "./types";

export const MINECRAFT = "minecraft";

type GameFields = Pick<Instance, "game" | "game_label" | "capabilities" | "memory_model" | "content_kind" | "is_modpack">;

/** Every capability a Minecraft server has, before the per-instance content/modpack rules. */
const MINECRAFT_BASE: readonly GameCapability[] = [
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

/** Labels, icons and icon colours of the games the panel knows (the catalog's label wins when the API sends one). */
const GAMES: Record<string, { label: string; icon: LucideIcon; color?: string }> = {
  [MINECRAFT]: { label: "Minecraft", icon: Pickaxe },
  factorio: { label: "Factorio", icon: Cog },
  terraria: { label: "Terraria", icon: TreePine },
  satisfactory: { label: "Satisfactory", icon: Factory },
  // the six Steam-era games (games-6-contract.md); their servers have no variant, so the game gives the colour
  palworld: { label: "Palworld", icon: PawPrint, color: "from-cyan-500/30 to-sky-700/30 text-cyan-700 dark:text-cyan-300" },
  valheim: { label: "Valheim", icon: Axe, color: "from-yellow-500/30 to-stone-700/30 text-yellow-800 dark:text-yellow-200" },
  "project-zomboid": { label: "Project Zomboid", icon: Biohazard, color: "from-red-500/30 to-red-800/30 text-red-700 dark:text-red-300" },
  hkmp: { label: "Hollow Knight (HKMP)", icon: Bug, color: "from-slate-400/30 to-indigo-900/30 text-slate-700 dark:text-slate-200" },
  cs2: { label: "Counter-Strike 2", icon: Crosshair, color: "from-orange-400/30 to-amber-700/30 text-orange-700 dark:text-orange-300" },
  rust: { label: "Rust", icon: Hammer, color: "from-orange-700/30 to-red-900/30 text-orange-800 dark:text-orange-300" },
};

const GAME_LABELS: Record<string, string> = Object.fromEntries(Object.entries(GAMES).map(([id, g]) => [id, g.label]));

export function gameId(i: Pick<Instance, "game">): string {
  return i.game || MINECRAFT;
}

export function isMinecraft(i: Pick<Instance, "game">): boolean {
  return gameId(i) === MINECRAFT;
}

/** Display name of a game id, or of an instance's game ("Minecraft"). */
export function gameLabel(of: string | Pick<Instance, "game" | "game_label">): string {
  if (typeof of !== "string" && of.game_label) return of.game_label;
  const id = typeof of === "string" ? of || MINECRAFT : gameId(of);
  return GAME_LABELS[id] ?? id.charAt(0).toUpperCase() + id.slice(1);
}

/**
 * The instance's capabilities: what the API sends, or (older panel, Minecraft) today's rules: every
 * capability, `content` only for plugin and mod servers, `modpacks` only for modpack servers. An
 * instance of another game without a list gets none (only panels with game definitions create them).
 */
export function instanceCapabilities(i: GameFields): ReadonlySet<GameCapability> {
  if (i.capabilities) return new Set(i.capabilities);
  if (!isMinecraft(i)) return new Set();
  const caps = new Set<GameCapability>(MINECRAFT_BASE);
  if (i.content_kind === "mods" || i.content_kind === "plugins") caps.add("content");
  if (i.is_modpack) caps.add("modpacks");
  return caps;
}

export function hasCapability(i: GameFields, cap: GameCapability): boolean {
  return instanceCapabilities(i).has(cap);
}

export function hasCapabilities(i: GameFields, caps: readonly GameCapability[] | undefined): boolean {
  if (!caps?.length) return true;
  const have = instanceCapabilities(i);
  return caps.every((c) => have.has(c));
}

export function memoryModel(i: Pick<Instance, "memory_model">): MemoryModel {
  return i.memory_model ?? "jvm";
}

// --- variants (Minecraft server types) ------------------------------------------------------------

const VARIANT_LABELS: Record<string, string> = {
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
  CUSTOM: "Custom",
  // Terraria's variants (lower-case ids)
  vanilla: "Vanilla",
  tmodloader: "tModLoader",
  tshock: "TShock",
};

const VARIANT_COLORS: Record<string, string> = {
  VANILLA: "from-emerald-500/30 to-emerald-700/30 text-emerald-700 dark:text-emerald-300",
  PAPER: "from-sky-500/30 to-sky-700/30 text-sky-700 dark:text-sky-300",
  PURPUR: "from-violet-500/30 to-violet-700/30 text-violet-700 dark:text-violet-300",
  FOLIA: "from-teal-500/30 to-teal-700/30 text-teal-700 dark:text-teal-300",
  FABRIC: "from-amber-500/30 to-amber-700/30 text-amber-700 dark:text-amber-200",
  QUILT: "from-fuchsia-500/30 to-fuchsia-700/30 text-fuchsia-700 dark:text-fuchsia-300",
  FORGE: "from-orange-500/30 to-orange-800/30 text-orange-700 dark:text-orange-300",
  NEOFORGE: "from-orange-400/30 to-red-700/30 text-orange-700 dark:text-orange-200",
  vanilla: "from-lime-500/30 to-green-700/30 text-green-700 dark:text-green-300",
  tmodloader: "from-indigo-500/30 to-indigo-700/30 text-indigo-700 dark:text-indigo-300",
  tshock: "from-rose-500/30 to-rose-700/30 text-rose-700 dark:text-rose-300",
};

const FALLBACK_COLOR = "from-zinc-500/30 to-zinc-700/30 text-zinc-700 dark:text-zinc-300";

/** Known label of a variant id, or undefined (callers choose their own fallback). */
export function knownVariantLabel(variant: string): string | undefined {
  return VARIANT_LABELS[variant];
}

/** Label of a variant ("PAPER" -> "Paper"); unknown ids are capitalised ("SPONGE" -> "Sponge"). */
export function variantLabel(variant: string): string {
  return VARIANT_LABELS[variant] ?? variant.charAt(0) + variant.slice(1).toLowerCase();
}

/** Tailwind classes of a variant's icon gradient; grey for unknown variants. */
export function variantColor(variant: string | undefined): string {
  return VARIANT_COLORS[variant ?? ""] ?? FALLBACK_COLOR;
}

/** The icon of a game (the create wizard's game cards); a gamepad for a game the UI does not know. */
export function gameIcon(game: string | null | undefined): LucideIcon {
  return GAMES[game || MINECRAFT]?.icon ?? Gamepad2;
}

/** Tailwind classes of a game's icon gradient, for games whose servers have no variant colour; undefined otherwise. */
export function gameColor(game: string | null | undefined): string | undefined {
  return GAMES[game || MINECRAFT]?.color;
}

/** The icon colour of a server: its variant's (Minecraft server types, Terraria), else its game's, else grey. */
export function serverIconColor(variant: string | undefined, game?: string | null): string {
  return VARIANT_COLORS[variant ?? ""] ?? gameColor(game) ?? FALLBACK_COLOR;
}

/**
 * Whether a game without versions to choose (catalog `fixed_version`) updates when it restarts, as the
 * catalog says (`updates_on_restart`: the six Steam-era games; HKMP's server comes with the panel's image
 * and changes only with a panel release). Without the flag (Satisfactory) the game follows a branch whose
 * newest build the image downloads at every start.
 */
export function updatesOnRestart(fixed: Pick<NonNullable<GameInfo["fixed_version"]>, "updates_on_restart"> | null | undefined): boolean {
  return !!fixed && fixed.updates_on_restart !== false;
}

/** Label of a mod loader id ("neoforge" -> "NeoForge"); "—" when empty. */
export function loaderLabel(loader: string): string {
  if (!loader) return "—";
  const known = VARIANT_LABELS[loader.toUpperCase()];
  if (known && !known.endsWith("modpack")) return known;
  return loader.charAt(0).toUpperCase() + loader.slice(1);
}

// --- memory -------------------------------------------------------------------------------------

/**
 * Container memory limit for a memory setting; mirrors Instance.container_memory_mb in the backend.
 * jvm: heap + max(1536, 40 %) for modded servers, heap + max(768, 30 %) otherwise (off-heap
 * headroom), or heap + `overheadMb` when one is set. plain: the setting is the limit (+ overhead).
 */
export function containerMemoryMb(memoryMb: number, opts: { model?: MemoryModel; modded?: boolean; overheadMb?: number } = {}): number {
  const { model = "jvm", modded = false, overheadMb = 0 } = opts;
  if (overheadMb) return memoryMb + overheadMb;
  if (model !== "jvm") return memoryMb;
  return memoryMb + (modded ? Math.max(1536, Math.floor(memoryMb * 0.4)) : Math.max(768, Math.floor(memoryMb * 0.3)));
}

/** Wording of the memory setting for a memory model (the Resources tab and the create wizard). */
export interface MemoryWording {
  /** Section description in the Resources tab. */
  settingsDescription: string;
  /** Section description in the create wizard. */
  wizardDescription: string;
  /** Label of the value ("Heap size (-Xmx)"). */
  fieldLabel: string;
  /** Short label for slider/value ("Heap"). */
  shortLabel: string;
  /** Accessible name of the slider. */
  sliderLabel: string;
  /** Number input label in the wizard ("Heap (MB)"). */
  inputLabel: string;
  /** What the container adds on top, after "heap + <n> " in the wizard. */
  headroomFor: string;
  /** Explains the automatic headroom (Resources → Advanced memory). */
  autoOverheadHint: (modded: boolean) => string;
  /** The rule as shown in the wizard ("30%, at least 768 MB"). */
  autoOverheadRule: (modded: boolean) => string;
}

export function memoryWording(model: MemoryModel): MemoryWording {
  if (model === "jvm") {
    return {
      settingsDescription:
        "Java heap for the server. The container limit adds headroom for off-heap memory so the server is not OOM-killed.",
      wizardDescription: "Java heap size. The container gets extra headroom for the JVM itself.",
      fieldLabel: "Heap size (-Xmx)",
      shortLabel: "Heap",
      sliderLabel: "Heap size",
      inputLabel: "Heap (MB)",
      headroomFor: "for the JVM itself",
      autoOverheadHint: (modded) => (modded ? "40% of heap, at least 1.5 GB for modded servers" : "30% of heap, at least 768 MB"),
      autoOverheadRule: (modded) => (modded ? "mods: 40%, at least 1.5 GB" : "30%, at least 768 MB"),
    };
  }
  return {
    settingsDescription: "Memory limit of the server's container.",
    wizardDescription: "Memory limit of the server's container.",
    fieldLabel: "Memory limit",
    shortLabel: "Memory",
    sliderLabel: "Memory limit",
    inputLabel: "Memory (MB)",
    headroomFor: "extra",
    autoOverheadHint: () => "no extra memory",
    autoOverheadRule: () => "none",
  };
}

// --- console ----------------------------------------------------------------------------------------

/** How a game's console takes commands (GET /catalog/games: stop_words, keeps_command_slash, command_hint). */
export interface ConsoleRules {
  /** Commands keep their leading "/" (Factorio); Minecraft strips it. */
  keepsSlash: boolean;
  /** Words the panel turns into its own Stop (lower case, as typed after normalisation). */
  stopWords: string[];
  /** Example commands for the input placeholder. */
  example: string;
}

export const MINECRAFT_CONSOLE: ConsoleRules = { keepsSlash: false, stopWords: ["stop"], example: "list or say Hello" };

/** The console rules of a game: Minecraft's without catalog data (Minecraft, older panels). */
export function consoleRules(game: Pick<GameInfo, "id" | "stop_words" | "keeps_command_slash" | "command_hint"> | undefined | null): ConsoleRules {
  if (!game || game.id === MINECRAFT) return MINECRAFT_CONSOLE;
  return {
    keepsSlash: !!game.keeps_command_slash,
    stopWords: (game.stop_words ?? []).map((w) => w.toLowerCase()),
    example: game.command_hint || (game.keeps_command_slash ? "/players online" : "help"),
  };
}

/** A typed console line as it is sent: trimmed, and without leading slashes unless the game keeps them. */
export function normaliseConsoleCommand(text: string, rules: ConsoleRules = MINECRAFT_CONSOLE): string {
  const t = text.trim();
  return rules.keepsSlash ? t : t.replace(/^\/+/, "");
}

/** Whether a (normalised) command is one of the game's stop words, which the panel turns into a Stop. */
export function isStopWord(command: string, rules: ConsoleRules = MINECRAFT_CONSOLE): boolean {
  return rules.stopWords.includes(command.trim().toLowerCase());
}

export function hasAnyCapability(i: GameFields, caps: readonly GameCapability[]): boolean {
  const have = instanceCapabilities(i);
  return caps.some((c) => have.has(c));
}
