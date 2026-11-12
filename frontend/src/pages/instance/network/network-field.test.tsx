/**
 * R-8 (security review 2026-11-11, last pass): games whose catalog entry says `custom_network: false`
 * (Satisfactory, Factorio, TShock, Palworld, Project Zomboid, CS2, Rust) are not offered a Docker network
 * field, and the panel's 400 on the field is shown at it.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api, ApiError } from "@/lib/api";
import { customNetworkAllowed } from "@/lib/games";
import { qk } from "@/lib/queries";
import type { GameInfo, Instance, PermissionCode } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { InstanceLayout } from "../InstanceLayout";
import Network from "../Network";

const PERMS: PermissionCode[] = ["instance.view", "instance.configure"];
const MINECRAFT = (over: Partial<GameInfo> = {}): GameInfo => ({
  id: "minecraft",
  label: "Minecraft",
  variants: [],
  capabilities: [],
  memory_model: "jvm",
  default_memory_mb: 2048,
  notes: [],
  ...over,
});

let gets: Record<string, unknown>;
beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});
beforeEach(() => {
  gets = { "/system/ports": { range: [25565, 40000], in_use: [], suggested: 25566 } };
  vi.spyOn(api, "get").mockImplementation(((path: string) =>
    path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
});
afterEach(() => vi.restoreAllMocks());

function renderPage(instance: Instance) {
  gets[`/instances/${instance.id}`] = instance;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(qk.instance(instance.id), instance);
  qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
  const router = createMemoryRouter(
    [{ path: "/servers/:id", element: <InstanceLayout />, children: [{ path: "network", element: <Network /> }] }],
    { initialEntries: [`/servers/${instance.id}/network`] },
  );
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <RouterProvider router={router} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("custom Docker networks", () => {
  it("are allowed unless the instance or its catalog entry says custom_network: false", () => {
    expect(customNetworkAllowed({}, undefined)).toBe(true);
    expect(customNetworkAllowed({}, { custom_network: true })).toBe(true);
    expect(customNetworkAllowed({}, { custom_network: false })).toBe(false);
    expect(customNetworkAllowed({ custom_network: false }, null)).toBe(false);
  });

  it("no network field when the game cannot have one", async () => {
    gets["/catalog/games"] = [MINECRAFT({ custom_network: false })];
    renderPage(makeInstance({ network: "", permissions: PERMS }));
    expect(await screen.findByText("Game port & bind address")).toBeInTheDocument();
    await vi.waitFor(() => expect(api.get).toHaveBeenCalledWith("/catalog/games"));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole("button", { name: /Advanced/ })).not.toBeInTheDocument();
  });

  it("a server that has one anyway keeps the field, to clear it", async () => {
    gets["/catalog/games"] = [MINECRAFT({ custom_network: false })];
    const user = renderPage(makeInstance({ network: "br0", permissions: PERMS }));
    await user.click(await screen.findByRole("button", { name: /Advanced/ }));
    expect(await screen.findByText(/servers run on the panel's own network only: clear the field/)).toBeInTheDocument();
  });

  it("the panel's 400 on the field is shown at it", async () => {
    gets["/catalog/games"] = [MINECRAFT()];
    const message = "Minecraft servers run on the panel's instance network only";
    vi.spyOn(api, "patch").mockRejectedValue(new ApiError(400, "validation_error", "Some fields are invalid.", { fields: { network: [message] } }));
    const user = renderPage(makeInstance({ network: "", host_ip: "0.0.0.0", permissions: PERMS }));
    await user.click(await screen.findByRole("button", { name: /Advanced/ }));
    await user.type(screen.getByLabelText(/Docker network/), "proxy_net");
    await user.click(screen.getByRole("button", { name: /Save changes/ }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByLabelText(/Docker network/)).toHaveAttribute("aria-invalid", "true");
  });
});
