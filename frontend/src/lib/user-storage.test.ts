import { afterEach, describe, expect, it } from "vitest";
import {
  BROWSER_PREFERENCE_KEYS,
  STORAGE_OWNER_KEY,
  clearPanelKeys,
  clearUserStorage,
  isPanelKey,
  syncStorageOwner,
  userScopedKey,
  type StorageArea,
} from "./user-storage";

function areas(): StorageArea[] {
  return [
    { storage: sessionStorage, keep: () => false },
    { storage: localStorage, keep: (k) => BROWSER_PREFERENCE_KEYS.has(k) },
  ];
}

function keys(storage: Storage): string[] {
  return Array.from({ length: storage.length }, (_, i) => storage.key(i)!).sort();
}

afterEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

describe("user storage hygiene", () => {
  it("recognises panel keys only", () => {
    expect(isPanelKey("mcpanel.console.history.u1.abc")).toBe(true);
    expect(isPanelKey("mcpanel-theme")).toBe(true);
    expect(isPanelKey("other-app.token")).toBe(false);
    expect(isPanelKey("xmcpanel.foo")).toBe(false);
  });

  it("scopes keys by user id", () => {
    expect(userScopedKey("mcpanel.console.history", 7, "srv-1")).toBe("mcpanel.console.history.u7.srv-1");
    expect(userScopedKey("mcpanel.console.history", 7, "srv-1")).not.toBe(userScopedKey("mcpanel.console.history", 8, "srv-1"));
  });

  it("clears panel keys except kept ones and leaves foreign keys alone", () => {
    localStorage.setItem("mcpanel-theme", "light");
    localStorage.setItem("mcpanel.servers.collapsedGroups", '["secret group"]');
    localStorage.setItem("someone-else", "x");
    const removed = clearPanelKeys(localStorage, (k) => BROWSER_PREFERENCE_KEYS.has(k));
    expect(removed).toEqual(["mcpanel.servers.collapsedGroups"]);
    expect(keys(localStorage)).toEqual(["mcpanel-theme", "someone-else"]);
  });

  it("logout wipes session data (console history) but keeps browser preferences", () => {
    sessionStorage.setItem("mcpanel.console.history.u1.srv", '["op alice"]');
    sessionStorage.setItem("mcpanel.console.history.srv", '["legacy unscoped"]');
    sessionStorage.setItem("mcpanel.content.pending-restart.srv", "t");
    localStorage.setItem("mcpanel-theme", "dark");
    localStorage.setItem("mcpanel.servers.view", '"list"');
    clearUserStorage(areas());
    expect(keys(sessionStorage)).toEqual([]);
    expect(keys(localStorage)).toEqual(["mcpanel-theme", "mcpanel.servers.view"]);
  });

  it("clears when the signed-in user changes, not when the same user reloads", () => {
    expect(syncStorageOwner(1, areas())).toBe(true); // first sighting: legacy data is dropped
    sessionStorage.setItem("mcpanel.console.history.u1.srv", '["op alice"]');
    expect(syncStorageOwner(1, areas())).toBe(false);
    expect(sessionStorage.getItem("mcpanel.console.history.u1.srv")).not.toBeNull();

    expect(syncStorageOwner(2, areas())).toBe(true);
    expect(sessionStorage.getItem("mcpanel.console.history.u1.srv")).toBeNull();
    expect(sessionStorage.getItem(STORAGE_OWNER_KEY)).toBe("2");
    expect(localStorage.getItem(STORAGE_OWNER_KEY)).toBe("2");
  });

  it("clears and forgets the owner when nobody is signed in", () => {
    syncStorageOwner(1, areas());
    sessionStorage.setItem("mcpanel.console.history.u1.srv", '["x"]');
    localStorage.setItem("mcpanel-theme", "light");
    expect(syncStorageOwner(null, areas())).toBe(true);
    expect(keys(sessionStorage)).toEqual([]);
    expect(keys(localStorage)).toEqual(["mcpanel-theme"]);
  });

  it("never throws when storage is unavailable", () => {
    const broken = {
      get length(): number {
        throw new DOMException("denied", "SecurityError");
      },
      getItem: () => {
        throw new DOMException("denied", "SecurityError");
      },
    } as unknown as Storage;
    expect(clearPanelKeys(broken)).toEqual([]);
    expect(() => syncStorageOwner(3, [{ storage: broken, keep: () => false }])).not.toThrow();
    expect(() => clearUserStorage([{ storage: broken, keep: () => false }])).not.toThrow();
  });
});
