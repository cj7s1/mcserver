import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { makeFactorioInstance, makeInstance, withGameFields } from "@/test/fixtures";
import { DiagnosticsCard } from "./diagnostics";

function show(instance: Instance) {
  const get = vi.spyOn(api, "get").mockResolvedValue({ issues: [], restart_count: 0, lines_scanned: 0 } as never);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <DiagnosticsCard instance={instance} can={() => true} now={Date.now()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return get;
}

afterEach(() => vi.restoreAllMocks());

describe("DiagnosticsCard (real-stack run 2026-11-11, item 22)", () => {
  it("asks for diagnostics of a Minecraft server", async () => {
    const get = show(withGameFields(makeInstance()));
    await waitFor(() => expect(get).toHaveBeenCalledWith(expect.stringMatching(/\/diagnostics$/)));
  });

  it("does not ask for a game without the diagnostics capability", async () => {
    const get = show(makeFactorioInstance());
    await new Promise((r) => setTimeout(r, 20));
    expect(get.mock.calls.filter(([url]) => String(url).endsWith("/diagnostics"))).toEqual([]);
  });
});
