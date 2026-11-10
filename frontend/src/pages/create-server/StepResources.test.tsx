import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { StepResources } from "./StepResources";
import { type Category, type Derived, initialState, type WizardState } from "./wizardModel";

const PORTS = { range: [25565, 25600], in_use: [], suggested: 25565 };
const SYSTEM = { version: "test", node: { id: 1, name: "local" }, agent: { reachable: true }, counts: { instances: 0, running: 0 } };
const GIB = 1024 ** 3;
let system: object = SYSTEM;
let instances: Instance[] = [];

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
    if (path === "/system/info") return system;
    if (path === "/instances/") return instances;
    throw new Error(`unexpected GET ${path}`);
  }) as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  system = SYSTEM;
  instances = [];
});

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

describe("StepResources: memory already reserved", () => {
  it("shows what running servers reserve on the host and warns when the new server would not fit", async () => {
    system = { ...SYSTEM, agent: { reachable: true, docker: { memory_bytes: 16 * GIB } } };
    instances = [
      makeInstance({ id: "a", status: "running", container_memory_mb: 8192 }),
      makeInstance({ id: "b", status: "running", container_memory_mb: 6144 }),
      makeInstance({ id: "c", status: "stopped", container_memory_mb: 16384 }),
    ];
    setup({ allowed: true });
    const bar = (await screen.findByText("Host memory already reserved")).parentElement!.parentElement!;
    // 4 GB heap -> 5.2 GB container limit (30%, at least 768 MB); stopped servers do not count
    expect(within(bar).getByText(/Running servers reserve/)).toHaveTextContent(
      "Running servers reserve 14 GB of 16 GB host memory; with the new server (5.2 GB) that is 19.2 GB.",
    );
    expect(within(bar).getByRole("alert")).toHaveTextContent(/more memory than the host has/);
  });

  it("stays quiet when everything fits", async () => {
    system = { ...SYSTEM, agent: { reachable: true, docker: { memory_bytes: 64 * GIB } } };
    instances = [makeInstance({ id: "a", status: "running", container_memory_mb: 8192 })];
    setup({ allowed: true });
    const bar = (await screen.findByText("Host memory already reserved")).parentElement!.parentElement!;
    expect(within(bar).getByText(/Running servers reserve/)).toHaveTextContent("Running servers reserve 8 GB of 64 GB host memory");
    expect(within(bar).queryByRole("alert")).toBeNull();
  });
});
