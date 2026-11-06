/**
 * Per-browser palette preferences in localStorage: recently opened entries and whether single-key
 * shortcuts are enabled. Storage can be unavailable (private mode, blocked site data), so every access
 * is wrapped in try/catch and falls back to defaults.
 */

export const RECENT_KEY = "mcpanel.palette.recent";
export const SINGLE_KEY_SHORTCUTS_KEY = "mcpanel.shortcuts.single-key";
export const MAX_RECENT = 5;

type StorageLike = Pick<Storage, "getItem" | "setItem">;

function defaultStorage(): StorageLike | undefined {
  try {
    return globalThis.localStorage;
  } catch {
    return undefined;
  }
}

/** Recent entry ids (`nav:<path>` or `server:<id>:<section>`), most recent first; no names or other data. */
export function readRecent(storage: StorageLike | undefined = defaultStorage()): string[] {
  try {
    const raw = storage?.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((v): v is string => typeof v === "string" && v.length > 0 && v.length < 200).slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

export function pushRecent(id: string, storage: StorageLike | undefined = defaultStorage()): string[] {
  const next = [id, ...readRecent(storage).filter((r) => r !== id)].slice(0, MAX_RECENT);
  try {
    storage?.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable: recents just are not remembered */
  }
  return next;
}

export function readSingleKeyShortcuts(storage: StorageLike | undefined = defaultStorage()): boolean {
  try {
    return storage?.getItem(SINGLE_KEY_SHORTCUTS_KEY) !== "off";
  } catch {
    return true;
  }
}

export function writeSingleKeyShortcuts(enabled: boolean, storage: StorageLike | undefined = defaultStorage()): void {
  try {
    storage?.setItem(SINGLE_KEY_SHORTCUTS_KEY, enabled ? "on" : "off");
  } catch {
    /* storage unavailable: the choice lasts until the page reloads */
  }
}
