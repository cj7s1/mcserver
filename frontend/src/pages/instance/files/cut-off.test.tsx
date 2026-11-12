/**
 * B-6 (security review 2026-11-11, last pass): the node's caps reach the user. A folder listing or the
 * backup browser with `truncated` says "List cut off at N entries"; a file search with `truncated` says it
 * stopped early; an incomplete content inventory says "Not all files checked yet". Answers without the flags
 * look as before.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Backup, FileEntry, Instance } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { BrowseBackupDialog } from "../backups/BrowseBackupDialog";
import Content from "../Content";
import Files from "../Files";
import { InstanceLayout } from "../InstanceLayout";
import { FileSearchSheet } from "./file-search";

const INSTANCE: Instance = makeInstance({
  id: "abababab-0000-0000-0000-000000000001",
  status: "stopped",
  server_type: "PAPER",
  content_kind: "plugins",
  permissions: ["instance.view", "instance.files.read", "instance.content", "instance.backup", "instance.restore"],
});
const file = (name: string): FileEntry => ({ name, path: name, type: "file", size: 10, modified: 1_793_000_000, mode: "-rw-r--r--" });

let gets: Record<string, unknown>;
beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= function scrollIntoView() {};
});
beforeEach(() => {
  gets = {};
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
});
afterEach(() => vi.restoreAllMocks());

function renderSection(sub: "files" | "content", element: React.ReactNode) {
  gets[`/instances/${INSTANCE.id}`] = INSTANCE;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(qk.instance(INSTANCE.id), INSTANCE);
  qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
  const router = createMemoryRouter([{ path: "/servers/:id", element: <InstanceLayout />, children: [{ path: sub, element }] }], {
    initialEntries: [`/servers/${INSTANCE.id}/${sub}`],
  });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("cut-off lists", () => {
  it("the Files page says where the folder listing was cut off", async () => {
    gets[`/instances/${INSTANCE.id}/files`] = { path: "", entries: [file("a.txt"), file("b.txt"), file("c.txt")], truncated: true };
    renderSection("files", <Files />);
    expect(await screen.findByRole("note", { name: "List cut off" })).toHaveTextContent(/^List cut off at 3 entries/);
  });

  it("a complete folder listing has no such notice", async () => {
    gets[`/instances/${INSTANCE.id}/files`] = { path: "", entries: [file("a.txt")] };
    renderSection("files", <Files />);
    expect(await screen.findByText("a.txt")).toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "List cut off" })).not.toBeInTheDocument();
  });

  it("the backup browser says so too", async () => {
    gets[`/instances/${INSTANCE.id}/backups/b1/files`] = { path: "", entries: [{ name: "world", path: "world", type: "directory", size: 0, modified: null }], truncated: true };
    const backup = { id: "b1", name: "Nightly", status: "succeeded", created_at: "2026-11-10T00:00:00Z" } as Backup;
    wrap(<BrowseBackupDialog instance={INSTANCE} backup={backup} canRestore={false} open onOpenChange={() => {}} onStarted={() => {}} />);
    expect(await screen.findByRole("note", { name: "List cut off" })).toHaveTextContent("List cut off at 1 entries: open a subfolder to see the rest.");
  });

  it("a search the node stopped early says not every match is listed, with or without matches", async () => {
    const post = vi.spyOn(api, "post");
    post.mockResolvedValueOnce({ results: [{ path: "a.txt", line: 1, text: "needle" }], truncated: true } as never);
    wrap(<FileSearchSheet instanceId={INSTANCE.id} scope="" open onOpenChange={() => {}} onOpenResult={() => {}} />);
    const user = userEvent.setup();
    const box = screen.getByRole("textbox");
    await user.type(box, "needle{Enter}");
    expect(await screen.findByRole("note", { name: "List cut off" })).toHaveTextContent(/^Search stopped early/);

    post.mockResolvedValueOnce({ results: [], truncated: true } as never);
    await user.clear(box);
    await user.type(box, "nothing{Enter}");
    expect(await screen.findByText(/in the files searched so far/)).toBeInTheDocument();
    expect(screen.getByRole("note", { name: "List cut off" })).toBeInTheDocument();

    post.mockResolvedValueOnce({ results: [] } as never);
    await user.clear(box);
    await user.type(box, "other{Enter}");
    expect(await screen.findByText("No matches")).toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "List cut off" })).not.toBeInTheDocument();
  });
});

describe("an incomplete content inventory", () => {
  it("says not all files are checked yet", async () => {
    gets[`/instances/${INSTANCE.id}/content/`] = {
      kind: "plugins",
      directory: "plugins",
      platform: "paper",
      loaders: ["paper"],
      minecraft_version: "1.21.1",
      is_modpack: false,
      restart_required_for_changes: true,
      identification: { available: true, error: null, complete: false, pending: 0 },
      warnings: [],
      summary: { total: 0, enabled: 0, disabled: 0, identified: 0, manual: 0 },
      files: [],
    };
    renderSection("content", <Content />);
    expect(await screen.findByText("Not all files checked yet")).toBeInTheDocument();
  });
});
