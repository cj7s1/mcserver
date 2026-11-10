/**
 * Pins which sections a Minecraft server shows (instance tabs, command palette pages, Overview quick
 * links, Settings tabs) for each kind of server and each relevant permission, as they were before
 * the section registry (src/lib/sections.ts) replaced the four separate lists. Every case runs twice:
 * as an older panel sends the instance (no game fields) and as a panel with game definitions sends it
 * (game, capabilities, memory_model): both must look exactly the same.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { serverSectionEntries } from "@/components/command-palette/entries";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";
import { makeInstance, withGameFields } from "@/test/fixtures";
import { InstanceLayout } from "./InstanceLayout";
import Overview from "./Overview";
import Settings from "./Settings";

const ALL_PERMS: PermissionCode[] = [
  "instance.view",
  "instance.control",
  "instance.console",
  "instance.command",
  "instance.configure",
  "instance.files.read",
  "instance.files.write",
  "instance.content",
  "instance.players",
  "instance.backup",
  "instance.restore",
  "instance.schedules",
  "instance.access",
  "instance.delete",
];

const KINDS: Record<string, Partial<Instance>> = {
  vanilla: {},
  paper: { name: "Paper Lobby", server_type: "PAPER", content_kind: "plugins", loader: "paper", loader_version: "120" },
  fabric: { name: "Fabric Test", server_type: "FABRIC", content_kind: "mods", loader: "fabric", loader_version: "0.16.9" },
  modpack: {
    name: "Pack Test",
    server_type: "MODRINTH",
    content_kind: "mods",
    is_modpack: true,
    modpack_provider: "modrinth",
    modpack_project_id: "abc",
    modpack_name: "Cobblemon",
    modpack_version_name: "1.6",
    loader: "fabric",
  },
  archived: { status: "archived", desired_state: "stopped", container_state: "", archived_at: "2026-11-02T00:00:00Z" },
};

/** Today's tab list with every permission, per kind of server. */
const TABS_ALL: Record<string, string[]> = {
  vanilla: ["Overview", "Console", "Files", "Players", "Backups", "Schedules", "Network", "Settings", "Access", "Activity"],
  paper: ["Overview", "Console", "Files", "Plugins", "Players", "Backups", "Schedules", "Network", "Settings", "Access", "Activity"],
  fabric: ["Overview", "Console", "Files", "Mods", "Players", "Backups", "Schedules", "Network", "Settings", "Access", "Activity"],
  modpack: ["Overview", "Console", "Files", "Mods", "Modpack", "Players", "Backups", "Schedules", "Network", "Settings", "Access", "Activity"],
  archived: ["Overview", "Console", "Files", "Players", "Backups", "Schedules", "Network", "Settings", "Access", "Activity"],
};
/** The sections the palette lists for the current server before anything is typed. */
const PRIMARY_ALL: Record<string, string[]> = {
  vanilla: ["Overview", "Console", "Files", "Backups", "Settings"],
  paper: ["Overview", "Console", "Files", "Plugins", "Backups", "Settings"],
  fabric: ["Overview", "Console", "Files", "Mods", "Backups", "Settings"],
  modpack: ["Overview", "Console", "Files", "Mods", "Backups", "Settings"],
  archived: ["Overview", "Console", "Files", "Backups", "Settings"],
};
const SHORTCUTS_ALL = ["Console", "Files", "Backups", "Players"];
const SETTINGS_TABS = ["Properties", "Resources & JVM", "Software", "Environment", "General", "Danger zone"];
/** Which section label each gating permission hides. */
const GATED: Partial<Record<PermissionCode, string>> = {
  "instance.console": "Console",
  "instance.files.read": "Files",
  "instance.backup": "Backups",
  "instance.access": "Access",
};

interface PermCase {
  name: string;
  perms: PermissionCode[];
  hidden: string[];
}
const PERM_CASES: PermCase[] = [
  { name: "every permission", perms: ALL_PERMS, hidden: [] },
  ...Object.entries(GATED).map(([code, label]) => ({
    name: `without ${code}`,
    perms: ALL_PERMS.filter((p) => p !== code),
    hidden: [label],
  })),
  { name: "view only", perms: ["instance.view"], hidden: Object.values(GATED) },
];

const FORMS: { name: string; make: (i: Instance) => Instance }[] = [
  { name: "older panel (no game fields)", make: (i) => i },
  { name: "panel with game definitions", make: withGameFields },
];

function instanceFor(kind: string, perms: PermissionCode[], make: (i: Instance) => Instance): Instance {
  return make(makeInstance({ ...KINDS[kind], permissions: perms }));
}

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  // jsdom has no layout: the settings tab bar scrolls its active tab into view
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
});

let current: Instance;
beforeEach(() => {
  // the instance comes from the cache; everything else the pages fetch stays pending
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path === `/instances/${current.id}` ? Promise.resolve(current) : new Promise(() => {})) as never);
});
afterEach(() => vi.restoreAllMocks());

function renderAt(instance: Instance, sub: "" | "settings") {
  current = instance;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(qk.instance(instance.id), instance);
  const router = createMemoryRouter(
    [
      {
        path: "/servers/:id",
        element: <InstanceLayout />,
        children: [
          { index: true, element: <Overview /> },
          { path: "settings", element: <Settings /> },
        ],
      },
    ],
    { initialEntries: [`/servers/${instance.id}${sub ? `/${sub}` : ""}`] },
  );
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const linkNames = (nav: HTMLElement) => within(nav).getAllByRole("link").map((a) => a.textContent?.trim() ?? "");
const without = (labels: string[], hidden: string[]) => labels.filter((l) => !hidden.includes(l));

describe.each(FORMS)("Minecraft sections, $name", ({ make }) => {
  describe.each(Object.keys(KINDS))("%s server", (kind) => {
    it.each(PERM_CASES)("tabs, palette pages and quick links with $name", ({ perms, hidden }) => {
      const instance = instanceFor(kind, perms, make);
      const tabs = without(TABS_ALL[kind], hidden);

      renderAt(instance, "");
      expect(linkNames(screen.getByRole("navigation", { name: "Server sections" }))).toEqual(tabs);
      const shortcuts = within(screen.getByRole("navigation", { name: "Server shortcuts" })).getAllByRole("link");
      expect(shortcuts.map((a) => within(a).getByText(/./, { selector: "span.font-medium" }).textContent)).toEqual(
        without(SHORTCUTS_ALL, hidden),
      );
      expect(shortcuts.map((a) => a.getAttribute("href"))).toEqual(
        without(SHORTCUTS_ALL, hidden).map((l) => `/servers/${instance.id}/${l.toLowerCase()}`),
      );

      const entries = serverSectionEntries(instance);
      expect(entries.map((e) => e.label)).toEqual(tabs);
      expect(serverSectionEntries(instance, { primaryOnly: true }).map((e) => e.label)).toEqual(without(PRIMARY_ALL[kind], hidden));
      const content = entries.find((e) => e.id.endsWith(":content"));
      if (content) expect(content.to).toBe(`/servers/${instance.id}/content`);
    });

    it.each(PERM_CASES)("settings tabs with $name", ({ perms }) => {
      renderAt(instanceFor(kind, perms, make), "settings");
      const list = screen.getByRole("tablist", { name: "Settings sections" });
      expect(within(list).getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual(SETTINGS_TABS);
      expect(within(list).getByRole("tab", { name: "Properties" })).toHaveAttribute("aria-selected", "true");
    });
  });

  it("header subtitle and palette hint keep the Minecraft wording", () => {
    const paper = instanceFor("paper", ALL_PERMS, make);
    renderAt(paper, "");
    expect(screen.getByText("Paper 1.21.1 · 120 · Java 21 · port 25565")).toBeInTheDocument();
  });
});
