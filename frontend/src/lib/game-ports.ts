/**
 * Named ports of a game (catalog `ports`) beyond "a host port per port": ports that always have another
 * port's number (`same_port_as`, Satisfactory's API) or that number plus an offset (`derived_from`,
 * Valheim's query port = game + 1) are shown, never asked for; ports published only while a setting is
 * on (`when`: Valheim's crossplay, CS2's CSTV, Rust+); ports published 1:1 (`one_to_one`: the server
 * listens on the host port's number inside its container, so the container port is not fixed).
 */
import { displayValue } from "./game-config";
import type { ConfigField, ConfigValue, GameInfo, GamePortSpec } from "./types";

/** A port the panel never asks for: it follows another port's number. */
export function followsOtherPort(p: Pick<GamePortSpec, "same_port_as" | "derived_from">): boolean {
  return !!p.same_port_as || !!p.derived_from;
}

/** The ports the owner chooses a host port for (in the catalog's order). */
export function editablePorts(ports: readonly GamePortSpec[] | undefined): GamePortSpec[] {
  return (ports ?? []).filter((p) => !followsOtherPort(p));
}

/** The ports whose number is derived from `leader` (with an offset). */
export function derivedPorts(ports: readonly GamePortSpec[] | undefined, leader: string): GamePortSpec[] {
  return (ports ?? []).filter((p) => p.derived_from?.port === leader);
}

/** "Game port + 1": how a derived port's number is made. */
export function derivedRule(ports: readonly GamePortSpec[] | undefined, p: GamePortSpec): string {
  if (!p.derived_from) return "";
  const leader = (ports ?? []).find((x) => x.name === p.derived_from!.port);
  return `${leader?.label ?? p.derived_from.port} port + ${p.derived_from.offset}`;
}

/** The host number of a derived port for its leader's number (NaN when the leader's is not known). */
export function derivedNumber(p: GamePortSpec, leaderPort: number): number {
  return p.derived_from && Number.isFinite(leaderPort) ? leaderPort + p.derived_from.offset : Number.NaN;
}

/** The config field a conditional port depends on (undefined when the catalog does not describe it). */
function conditionField(game: Pick<GameInfo, "config_files"> | undefined, p: GamePortSpec): ConfigField | undefined {
  if (!p.when) return undefined;
  return game?.config_files?.find((f) => f.id === p.when!.config)?.fields.find((f) => f.key === p.when!.key);
}

/** "only while “CSTV (spectators)” is on": when a conditional port is published ("" for a port that always is). */
export function conditionText(game: Pick<GameInfo, "config_files"> | undefined, p: GamePortSpec): string {
  if (!p.when) return "";
  const field = conditionField(game, p);
  const label = field?.label ?? p.when.key;
  if (p.when.value === true) return `only while “${label}” is on`;
  return `only while “${label}” is ${field ? displayValue(field, p.when.value) : String(p.when.value)}`;
}

/**
 * Whether a conditional port is published with these settings: `values` maps config ids to their values
 * (a server's `game_options`, where the panel keeps virtual configs; the wizard's chosen settings); a
 * missing value is the field's default. True for a port without a condition; null when unknown.
 */
export function portPublished(
  game: Pick<GameInfo, "config_files"> | undefined,
  p: GamePortSpec,
  values: Record<string, unknown> | null | undefined,
): boolean | null {
  if (!p.when) return true;
  const file = values?.[p.when.config];
  const stored = file && typeof file === "object" && !Array.isArray(file) ? (file as Record<string, ConfigValue>)[p.when.key] : undefined;
  const value = stored !== undefined ? stored : conditionField(game, p)?.default;
  if (value === undefined) return null;
  return value === p.when.value;
}
