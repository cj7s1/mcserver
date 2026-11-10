import { render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { FileEntry } from "@/lib/types";
import { FileTable, type FileActions } from "./file-table";

const entry = (name: string, extra: Partial<FileEntry> = {}): FileEntry => ({
  name,
  path: `plugins/${name}`,
  type: "file",
  size: 10,
  modified: 1_700_000_000,
  mode: "0644",
  ...extra,
});

const actions: FileActions = {
  open: vi.fn(),
  edit: vi.fn(),
  rename: vi.fn(),
  remove: vi.fn(),
  extract: vi.fn(),
  downloadHref: () => "#",
  archiveHref: () => "#",
};

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

describe("entries whose name is not valid UTF-8 (name_valid: false)", () => {
  it("cannot be selected, opened, renamed or deleted, and say why", () => {
    const bad = entry("caf�.yml", { name_valid: false });
    render(
      <TooltipProvider>
        <FileTable
          entries={[bad, entry("ok.yml")]}
          selected={new Set()}
          onSelectedChange={() => {}}
          canWrite
          sort={{ key: "name", dir: "asc" }}
          onSort={() => {}}
          actions={actions}
          now={Date.now()}
        />
      </TooltipProvider>,
    );
    expect(screen.getByRole("checkbox", { name: `Select ${bad.name}` })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Select ok.yml" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: `Actions for ${bad.name}` })).toBeNull();
    expect(screen.getByLabelText(/No actions for caf.*not valid UTF-8/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for ok.yml" })).toBeEnabled();
    // the name itself is no link into the editor
    expect(screen.queryByRole("button", { name: bad.name })).toBeNull();
  });
});
