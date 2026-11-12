/**
 * A-6 (security review 2026-11-11): the Network page warns when a server is not on the agent's own
 * network (host networking, or another Docker network such as a macvlan br0): there its control port,
 * which the panel never publishes, is reachable directly.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PermissionCode } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { InstanceLayout } from "../InstanceLayout";
import Network from "../Network";
import { controlPorts, onAgentNetwork } from "./network-exposure";
import { NetworkExposureNotice } from "./NetworkExposureNotice";

describe("which networks are the agent's own", () => {
  it("empty (the default), the default instance network and a per-server network", () => {
    expect(onAgentNetwork("")).toBe(true);
    expect(onAgentNetwork(undefined)).toBe(true);
    expect(onAgentNetwork("mcpanel_instances")).toBe(true);
    expect(onAgentNetwork("mcpanel_i_0123456789ab")).toBe(true);
    expect(onAgentNetwork("host")).toBe(false);
    expect(onAgentNetwork("br0")).toBe(false);
    expect(onAgentNetwork("proxy_net")).toBe(false);
    expect(onAgentNetwork("mcpanel_i_xyz")).toBe(false);
  });

  it("uses the agent's own names when the instance answer has them (agent_networks)", () => {
    const agent = { shared: "panel_net", per_server_prefix: "mcpanel_i_" };
    expect(onAgentNetwork("", agent)).toBe(true);
    expect(onAgentNetwork("panel_net", agent)).toBe(true);
    expect(onAgentNetwork("mcpanel_i_7f3a", agent)).toBe(true);
    // the guessed default is not the agent's network when the agent names another one
    expect(onAgentNetwork("mcpanel_instances", agent)).toBe(false);
    expect(onAgentNetwork("br0", agent)).toBe(false);
    // an agent without per-server networks
    expect(onAgentNetwork("mcpanel_i_0123456789ab", { shared: "mcpanel_instances", per_server_prefix: null })).toBe(false);
    // null (older agent, or not asked yet): the guess
    expect(onAgentNetwork("mcpanel_instances", null)).toBe(true);
    expect(onAgentNetwork("mcpanel_i_0123456789ab", null)).toBe(true);
    const { rerender } = render(<NetworkExposureNotice instance={makeInstance({ network: "panel_net", agent_networks: agent })} />);
    expect(screen.queryByRole("note", { name: "Network exposure" })).not.toBeInTheDocument();
    rerender(<NetworkExposureNotice instance={makeInstance({ network: "mcpanel_instances", agent_networks: agent })} />);
    expect(screen.getByRole("note", { name: "Network exposure" })).toHaveTextContent("“mcpanel_instances”");
  });

  it("names the control port of each game", () => {
    expect(controlPorts({ game: "minecraft", server_type: "PAPER" })).toBe("RCON (TCP 25575)");
    expect(controlPorts({ server_type: "VANILLA" } as Instance)).toBe("RCON (TCP 25575)");
    expect(controlPorts({ game: "terraria", server_type: "tshock" as Instance["server_type"] })).toBe("the TShock REST API (TCP 7878)");
    expect(controlPorts({ game: "terraria", server_type: "vanilla" as Instance["server_type"] })).toBeNull();
    expect(controlPorts({ game: "hkmp", server_type: "" as Instance["server_type"] })).toBeNull();
    expect(controlPorts({ game: "rust", server_type: "" as Instance["server_type"] })).toMatch(/WebRCON/);
  });
});

describe("NetworkExposureNotice", () => {
  it("nothing on the agent's network; a warning naming the control port elsewhere", () => {
    const { rerender } = render(<NetworkExposureNotice instance={makeInstance({ network: "" })} />);
    expect(screen.queryByRole("note", { name: "Network exposure" })).not.toBeInTheDocument();
    rerender(<NetworkExposureNotice instance={makeInstance({ network: "br0" })} />);
    const note = screen.getByRole("note", { name: "Network exposure" });
    expect(note).toHaveTextContent("This server is on the Docker network “br0”, not on the panel's own network");
    expect(note).toHaveTextContent("RCON (TCP 25575) included");
    expect(note).toHaveTextContent("Unraid's br0");
    rerender(<NetworkExposureNotice instance={makeInstance({ network: "host" })} />);
    expect(screen.getByRole("note", { name: "Network exposure" })).toHaveTextContent(/uses the host's network.*every network interface of the host/);
    rerender(<NetworkExposureNotice instance={makeInstance({ game: "hkmp", network: "br0" })} />);
    expect(screen.getByRole("note", { name: "Network exposure" })).not.toHaveTextContent("control port");
  });
});

describe("the Minecraft Network page", () => {
  const PERMS: PermissionCode[] = ["instance.view", "instance.configure"];
  beforeAll(() => {
    globalThis.ResizeObserver ??= class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  });
  let gets: Record<string, unknown>;
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

  it("on the default network: no warning, RCON is described as before", async () => {
    renderPage(makeInstance({ network: "", permissions: PERMS }));
    expect(await screen.findByText(/The panel talks to it over the internal/)).toBeInTheDocument();
    expect(screen.queryByRole("note", { name: "Network exposure" })).not.toBeInTheDocument();
  });

  it("on br0: the warning, and the RCON line says it is reachable there", async () => {
    renderPage(makeInstance({ network: "br0", permissions: PERMS }));
    expect(await screen.findByRole("note", { name: "Network exposure" })).toHaveTextContent("“br0”");
    expect(screen.getByText(/there RCON is reachable directly/)).toBeInTheDocument();
  });

  it("typing another network into the form warns before it is saved", async () => {
    const user = renderPage(makeInstance({ network: "", permissions: PERMS }));
    await user.click(await screen.findByRole("button", { name: /Advanced/ }));
    await user.type(screen.getByLabelText(/Docker network/), "br0");
    const notes = screen.getAllByRole("note", { name: "Network exposure" });
    expect(notes).toHaveLength(1);
    expect(within(notes[0]).getByText(/Docker network “br0”/)).toBeInTheDocument();
  });
});
