/** Rules and wording of "stop when empty" (B2), shared by the settings section and its tests. */
import { parseNum } from "./settings-ui";

export const IDLE_STOP_MIN = 5;
export const IDLE_STOP_MAX = 1440;

/** Why a value cannot be saved, or null. */
export function idleStopError(raw: string): string | null {
  const n = parseNum(raw.trim());
  if (n === null || !Number.isInteger(n)) return "A whole number of minutes";
  if (n !== 0 && (n < IDLE_STOP_MIN || n > IDLE_STOP_MAX)) return `0 (off) or ${IDLE_STOP_MIN} to ${IDLE_STOP_MAX} minutes`;
  return null;
}

/** The plain sentence of what the setting does. */
export function idleStopText(minutes: number): string {
  return minutes > 0
    ? `Stops the server after ${minutes} minute${minutes === 1 ? "" : "s"} with nobody on it; it saves the world first. It stays stopped until someone starts it again.`
    : "Off: the server keeps running when nobody is on it.";
}
