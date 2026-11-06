import { describe, expect, it } from "vitest";
import type { PermissionCode } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { navEntries, powerConfirmCopy, powerOptions, serverEntry, serverSectionEntries } from "./entries";

const ALL_VIEW: PermissionCode[] = ["instance.view"];

describe("navEntries", () => {
  const groups = [
    {
      heading: "Navigation",
      items: [
        { to: "/", label: "Dashboard", icon: null },
        { to: "/servers/new", label: "Create server", icon: null, perm: "instance.create" as const },
      ],
    },
    { heading: "Administration", items: [{ to: "/admin/users", label: "Users & roles", icon: null, perm: "system.users" as const }] },
  ];

  it("hides entries (and empty groups) the user lacks permission for", () => {
    const res = navEntries(groups, () => false);
    expect(res).toHaveLength(1);
    expect(res[0].entries.map((e) => e.label)).toEqual(["Dashboard"]);
  });

  it("shows everything for a user with the permissions, label first in keywords", () => {
    const res = navEntries(groups, () => true);
    expect(res.map((g) => g.heading)).toEqual(["Navigation", "Administration"]);
    expect(res[1].entries[0]).toMatchObject({ id: "nav:/admin/users", to: "/admin/users" });
    expect(res[1].entries[0].keywords[0]).toBe("Users & roles");
  });
});

describe("serverSectionEntries", () => {
  it("follows the server tab rules (permissions, content kind, modpack)", () => {
    const viewer = makeInstance({ permissions: ALL_VIEW });
    const labels = serverSectionEntries(viewer).map((e) => e.label);
    expect(labels).toContain("Overview");
    expect(labels).toContain("Settings");
    expect(labels).not.toContain("Console");
    expect(labels).not.toContain("Files");
    expect(labels).not.toContain("Backups");
    expect(labels).not.toContain("Access");
    expect(labels).not.toContain("Mods");
    expect(labels).not.toContain("Modpack");

    const modded = makeInstance({ content_kind: "mods", is_modpack: true });
    const moddedLabels = serverSectionEntries(modded).map((e) => e.label);
    expect(moddedLabels).toEqual(expect.arrayContaining(["Console", "Files", "Backups", "Mods", "Modpack"]));
    expect(serverSectionEntries(makeInstance({ content_kind: "plugins" })).map((e) => e.label)).toContain("Plugins");
  });

  it("links to the right routes and limits the default view to the main pages", () => {
    const i = makeInstance({ content_kind: "mods" });
    const byKey = Object.fromEntries(serverSectionEntries(i).map((e) => [e.id.split(":")[2], e.to]));
    expect(byKey.overview).toBe(`/servers/${i.id}`);
    expect(byKey.console).toBe(`/servers/${i.id}/console`);
    expect(byKey.content).toBe(`/servers/${i.id}/content`);
    const primary = serverSectionEntries(i, { primaryOnly: true }).map((e) => e.label);
    expect(primary).toEqual(["Overview", "Console", "Files", "Mods", "Backups", "Settings"]);
  });

  it("describes servers with status and software", () => {
    const e = serverEntry(makeInstance({ status: "stopped", server_type: "PAPER" }));
    expect(e.hint).toBe("Stopped · Paper 1.21.1");
    expect(e.keywords[0]).toBe("Vanilla Test");
    expect(serverEntry(makeInstance({ archived_at: "2026-11-02T00:00:00Z" })).hint).toMatch(/^Archived/);
  });
});

describe("powerOptions", () => {
  it("offers Restart and Stop (both confirmed) for a running server", () => {
    const opts = powerOptions(makeInstance({ status: "running" }));
    expect(opts.map((o) => [o.action, o.needsConfirm])).toEqual([
      ["restart", true],
      ["stop", true],
    ]);
  });

  it("offers Start (no confirmation) for a stopped or failed server", () => {
    expect(powerOptions(makeInstance({ status: "stopped" })).map((o) => [o.action, o.needsConfirm])).toEqual([["start", false]]);
    expect(powerOptions(makeInstance({ status: "failed" })).map((o) => o.action)).toEqual(["start"]);
  });

  it("offers nothing without instance.control, for archived servers or while stopping", () => {
    expect(powerOptions(makeInstance({ permissions: ALL_VIEW }))).toEqual([]);
    expect(powerOptions(makeInstance({ archived_at: "2026-11-02T00:00:00Z" }))).toEqual([]);
    expect(powerOptions(makeInstance({ status: "stopping" }))).toEqual([]);
  });

  it("disables actions while a job holds the server", () => {
    const opts = powerOptions(
      makeInstance({ active_job: { id: "j", kind: "backup", title: "Backup", progress: 0.3, message: "", status: "running" } }),
    );
    expect(opts.every((o) => o.disabledReason === "Busy: Backup")).toBe(true);
  });

  it("explains what stop and restart do", () => {
    const i = makeInstance({ stop_timeout: 45 });
    expect(powerConfirmCopy(i, "stop")).toMatchObject({ title: "Stop Vanilla Test?", confirmLabel: "Stop server", destructive: true });
    expect(powerConfirmCopy(i, "stop").description).toContain("45 s");
    expect(powerConfirmCopy(makeInstance({ restart_required: true }), "restart").description).toContain("container is recreated");
  });
});
