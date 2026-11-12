/**
 * Browser storage hygiene for the signed-in user.
 *
 * Some panel pages keep per-user data in Web Storage (e.g. console command history in
 * sessionStorage). That data must not outlive the session it belongs to: on logout, and whenever
 * the signed-in user changes, every panel-owned key is removed except a small allow-list of
 * per-browser preferences (theme, view modes) that contain no user data. Keys holding user data
 * should additionally include the user id (see `userScopedKey`) so a missed cleanup can never
 * show one user's data to another.
 */
import { SESSION_TOKEN_KEY } from "./session-token";

/** Marker recording which user id the panel keys in a storage area belong to. */
export const STORAGE_OWNER_KEY = "mcpanel.storage.owner";

const PANEL_PREFIXES = ["mcpanel.", "mcpanel-", "mcpanel:"];

/** localStorage keys that are per-browser UI preferences (no user data) and survive logout. */
export const BROWSER_PREFERENCE_KEYS: ReadonlySet<string> = new Set([
  "mcpanel-theme",
  "mcpanel.power.warnMinutes",
  "mcpanel.servers.view",
  "mcpanel.players.show-heads",
]);

export interface StorageArea {
  storage: Storage;
  /** Keys that are kept when the area is cleared. */
  keep: (key: string) => boolean;
}

export function isPanelKey(key: string): boolean {
  return PANEL_PREFIXES.some((p) => key.startsWith(p));
}

/** Storage key for user-specific data, e.g. `userScopedKey("mcpanel.console.history", 3, id)`. */
export function userScopedKey(base: string, userId: number, ...parts: string[]): string {
  return [base, `u${userId}`, ...parts].join(".");
}

/** Remove every panel-owned key not kept by `keep`; returns the removed keys. Never throws. */
export function clearPanelKeys(storage: Storage, keep: (key: string) => boolean = () => false): string[] {
  const removed: string[] = [];
  try {
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const k = storage.key(i);
      if (k !== null) keys.push(k);
    }
    for (const k of keys) {
      if (isPanelKey(k) && !keep(k)) {
        storage.removeItem(k);
        removed.push(k);
      }
    }
  } catch {
    /* storage unavailable (private mode / blocked): nothing persisted, nothing to clear */
  }
  return removed;
}

/**
 * Make sure the panel keys in each area belong to `userId` (null = nobody signed in). When the
 * recorded owner differs, or nobody is signed in, the area is cleared first. Returns true if any
 * area was cleared.
 */
export function syncStorageOwner(userId: number | null, areas: StorageArea[] = defaultAreas()): boolean {
  const want = userId === null ? null : String(userId);
  let cleared = false;
  for (const { storage, keep } of areas) {
    try {
      if (want !== null && storage.getItem(STORAGE_OWNER_KEY) === want) continue;
      clearPanelKeys(storage, keep);
      cleared = true;
      if (want !== null) storage.setItem(STORAGE_OWNER_KEY, want);
    } catch {
      /* storage unavailable */
    }
  }
  return cleared;
}

/** Forget all user data in browser storage (logout). */
export function clearUserStorage(areas: StorageArea[] = defaultAreas()): void {
  for (const { storage, keep } of areas) clearPanelKeys(storage, keep);
}

function defaultAreas(): StorageArea[] {
  const areas: StorageArea[] = [];
  try {
    if (typeof sessionStorage !== "undefined") areas.push({ storage: sessionStorage, keep: () => false });
  } catch {
    /* access can throw SecurityError when storage is disabled */
  }
  try {
    // the session token is not user data of a page: lib/session-token.ts sets and clears it with the sign-in
    // (a sign-in makes the stored owner change, which must not wipe the token it just stored)
    if (typeof localStorage !== "undefined")
      areas.push({ storage: localStorage, keep: (k) => BROWSER_PREFERENCE_KEYS.has(k) || k === SESSION_TOKEN_KEY });
  } catch {
    /* ignore */
  }
  return areas;
}
