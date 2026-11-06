import { describe, expect, it } from "vitest";
import type { ModpackUpdateInfo } from "@/lib/types";
import { availableUpdate, reviewUpdatePath, updateCheckSummary } from "./modpack-api";

const base: ModpackUpdateInfo = {
  status: "ok",
  detail: "",
  checked_at: "2026-11-06T10:00:00Z",
  last_success_at: "2026-11-06T10:00:00Z",
  available: false,
  stale: false,
  latest: { id: "V1", name: "1.0.0", published: null, minecraft_version: "1.20.1" },
};

describe("modpack update check helpers", () => {
  it("exposes the available release only for modpack servers with an update", () => {
    const update = { ...base, available: true, latest: { ...base.latest!, id: "V2", name: "1.1.0" } };
    expect(availableUpdate({ is_modpack: true, modpack_update: update })?.name).toBe("1.1.0");
    expect(availableUpdate({ is_modpack: true, modpack_update: base })).toBeNull();
    expect(availableUpdate({ is_modpack: false, modpack_update: update })).toBeNull();
    expect(availableUpdate({ is_modpack: true, modpack_update: null })).toBeNull();
  });

  it("links to the Modpack tab with the release to review", () => {
    expect(reviewUpdatePath("abc", "V 2")).toBe("/servers/abc/modpack?review=V%202");
  });

  it("summarises every check state", () => {
    expect(updateCheckSummary(null).text).toMatch(/Not checked/);
    expect(updateCheckSummary({ ...base, status: "pending", latest: null }).tone).toBe("muted");
    const keyless = { ...base, status: "unavailable" as const, latest: null, detail: "Update check unavailable without a CurseForge API key" };
    expect(updateCheckSummary(keyless)).toEqual({ tone: "muted", text: keyless.detail });
    expect(updateCheckSummary({ ...base, available: true, latest: { ...base.latest!, name: "2.0" } })).toEqual({
      tone: "update",
      text: "Update available: 2.0",
    });
    expect(updateCheckSummary(base)).toEqual({ tone: "ok", text: "Up to date with the newest compatible release." });
    expect(updateCheckSummary({ ...base, latest: null }).text).toBe("No compatible release found.");
    expect(updateCheckSummary({ ...base, stale: true }).text).toMatch(/Check again/);
    const failedFirst = { ...base, status: "error" as const, detail: "Modrinth returned HTTP 503", last_success_at: null, latest: null };
    expect(updateCheckSummary(failedFirst)).toEqual({ tone: "error", text: "Update check failed: Modrinth returned HTTP 503" });
    const failedLater = { ...base, status: "error" as const, detail: "timeout" };
    expect(updateCheckSummary(failedLater).text).toBe("Up to date with the newest compatible release. The last check failed: timeout");
  });
});
