import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { ServerQuickActions } from "./ServerActions";
import { memoryCommitment } from "./server-utils";

beforeAll(() => {
  // jsdom lacks what the menu's popper measures with
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

afterEach(() => vi.restoreAllMocks());

function show(over: Partial<Instance>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ServerQuickActions instance={makeInstance({ name: "Crashy", ...over })} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return Object.assign(userEvent.setup(), { unmount: view.unmount });
}

describe("server list quick actions", () => {
  it("offer Stop for a failed server that still has its container (same rule as the server header)", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "job" } as never);
    const user = show({ status: "failed", desired_state: "running", container_state: "exited" });
    expect(screen.getByRole("button", { name: "Start Crashy" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More actions for Crashy" }));
    await user.click(await screen.findByRole("menuitem", { name: "Stop" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(expect.stringMatching(/\/power$/), { action: "stop" }));
  });

  it("do not offer it once the container is gone or the server is meant to be stopped", async () => {
    for (const over of [
      { status: "failed", desired_state: "running", container_state: "" },
      { status: "stopped", desired_state: "stopped", container_state: "exited" },
    ] as Partial<Instance>[]) {
      const user = show(over);
      await user.click(screen.getByRole("button", { name: "More actions for Crashy" }));
      expect(await screen.findByRole("menuitem", { name: "Start" })).toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: /^Stop/ })).toBeNull();
      user.unmount();
    }
  });
});

describe("memoryCommitment", () => {
  const list = [
    makeInstance({ id: "a", status: "running", container_memory_mb: 8192, node: 1 }),
    makeInstance({ id: "b", status: "starting", container_memory_mb: 4096, node: 1 }),
    makeInstance({ id: "c", status: "stopped", container_memory_mb: 16384, node: 1 }),
    makeInstance({ id: "d", status: "running", container_memory_mb: 2048, node: 2 }),
    makeInstance({ id: "e", status: "running", container_memory_mb: 1024, node: 1, archived_at: "2026-11-01T00:00:00Z" }),
  ];

  it("sums the container limits of running servers and of all servers", () => {
    expect(memoryCommitment(list)).toEqual({ runningMb: 8192 + 4096 + 2048, allMb: 8192 + 4096 + 16384 + 2048 });
  });

  it("limits to one node and can leave out the server being edited", () => {
    expect(memoryCommitment(list, { nodeId: 1 })).toEqual({ runningMb: 12288, allMb: 28672 });
    expect(memoryCommitment(list, { nodeId: 1, excludeId: "a" })).toEqual({ runningMb: 4096, allMb: 20480 });
  });
});
