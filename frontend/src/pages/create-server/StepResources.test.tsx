import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { StepResources } from "./StepResources";
import { type Category, type Derived, initialState, type WizardState } from "./wizardModel";

const PORTS = { range: [25565, 25600], in_use: [], suggested: 25565 };
const SYSTEM = { version: "test", node: { id: 1, name: "local" }, agent: { reachable: true }, counts: { instances: 0, running: 0 } };

beforeAll(() => {
  // jsdom lacks what Radix's slider measures with
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

beforeEach(() => {
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    if (path === "/system/ports") return PORTS;
    if (path === "/system/info") return SYSTEM;
    throw new Error(`unexpected GET ${path}`);
  }) as never);
});

afterEach(() => vi.restoreAllMocks());

function setup({ allowed, category = "plugins" }: { allowed: boolean; category?: Category }) {
  const d = {
    isModpack: false,
    serverType: "PAPER",
    name: "Lobby",
    suggestedName: "Paper 1.21.1",
    memoryMb: 4096,
    defaultMemoryMb: 4096,
    gamePort: "25565",
    nightlyBackupAllowed: allowed,
  } as unknown as Derived;
  const state: WizardState = { ...initialState(), category };
  const patch = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <StepResources state={state} patch={patch} d={d} errors={{}} />
    </QueryClientProvider>,
  );
  return { patch, user: userEvent.setup() };
}

describe("StepResources: nightly backup option", () => {
  it("is offered, switched on, to users who may schedule backups", async () => {
    const { patch, user } = setup({ allowed: true });
    const option = screen.getByRole("switch", { name: "Nightly backup" });
    expect(option).toBeChecked();
    expect(option).toHaveAccessibleDescription(/every night between 03:00 and 05:00/);
    await user.click(option);
    expect(patch).toHaveBeenCalledWith({ nightlyBackup: false });
  });

  it("is offered for an imported server too, where “Start after creation” is not", () => {
    setup({ allowed: true, category: "import" });
    expect(screen.getByRole("switch", { name: "Nightly backup" })).toBeChecked();
    expect(screen.queryByRole("switch", { name: "Start after creation" })).toBeNull();
  });

  it("is hidden from users who may not schedule backups", () => {
    setup({ allowed: false });
    expect(screen.queryByRole("switch", { name: "Nightly backup" })).toBeNull();
    expect(screen.getByRole("switch", { name: "Start after creation" })).toBeInTheDocument();
  });
});
