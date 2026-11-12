/**
 * B2 Stop when empty: the setting (0 = off, 5..1440 minutes) with its explanation, the reason instead of the field
 * when the panel cannot read the server's player count, the panel's 400 at the field, and the Overview's line.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { InstanceLayout } from "../InstanceLayout";
import Overview from "../Overview";
import { IdleStopSection, idleStopError, idleStopText } from "./idle-stop";

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});
beforeEach(() => {
  vi.spyOn(api, "get").mockImplementation((() => new Promise(() => {})) as never);
});
afterEach(() => vi.restoreAllMocks());

function renderSection(instance: Instance, canEdit = true) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <IdleStopSection instance={instance} canEdit={canEdit} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("stop when empty", () => {
  it("accepts 0 (off) or 5 to 1440 whole minutes and says what it does", () => {
    expect(idleStopError("0")).toBeNull();
    expect(idleStopError("5")).toBeNull();
    expect(idleStopError("1440")).toBeNull();
    expect(idleStopError("4")).toBe("0 (off) or 5 to 1440 minutes");
    expect(idleStopError("1441")).toBe("0 (off) or 5 to 1440 minutes");
    expect(idleStopError("")).toBe("A whole number of minutes");
    expect(idleStopError("2.5")).toBe("A whole number of minutes");
    expect(idleStopText(30)).toMatch(/^Stops the server after 30 minutes with nobody on it; it saves the world first/);
    expect(idleStopText(0)).toMatch(/^Off/);
  });

  it("saves the minutes on their own", async () => {
    const patch = vi.spyOn(api, "patch").mockResolvedValue(makeInstance({ idle_stop_minutes: 30 }) as never);
    const user = renderSection(makeInstance({ idle_stop_minutes: 0, idle_stop_unavailable: null }));
    const field = screen.getByLabelText("Minutes without players (0 = off)");
    expect(screen.getByText(/^Off: the server keeps running/)).toBeInTheDocument();
    await user.clear(field);
    await user.type(field, "3");
    expect(screen.getByText("0 (off) or 5 to 1440 minutes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save/ })).toBeDisabled();
    await user.type(field, "0");
    expect(screen.getByText(/^Stops the server after 30 minutes/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(patch).toHaveBeenCalledWith(`/instances/${makeInstance().id}`, { idle_stop_minutes: 30 });
  });

  it("shows the panel's 400 at the field", async () => {
    vi.spyOn(api, "patch").mockRejectedValue(
      new ApiError(400, "validation_error", "Some fields are invalid.", { fields: { idle_stop_minutes: ["Not available for this server: no count"] } }),
    );
    const user = renderSection(makeInstance({ idle_stop_minutes: 0, idle_stop_unavailable: null }));
    const field = screen.getByLabelText("Minutes without players (0 = off)");
    await user.clear(field);
    await user.type(field, "15");
    await user.click(screen.getByRole("button", { name: /Save/ }));
    expect(await screen.findByText("Not available for this server: no count")).toBeInTheDocument();
  });

  it("shows why it is unavailable instead of the field; nothing for an older panel", () => {
    renderSection(makeInstance({ game: "valheim", idle_stop_minutes: 0, idle_stop_unavailable: "Valheim has no way to tell how many players are online" }));
    expect(screen.getByText("Valheim has no way to tell how many players are online")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Minutes without players/)).not.toBeInTheDocument();
  });

  it("an answer without the keys (older panel) shows nothing", () => {
    renderSection(makeInstance());
    expect(screen.queryByText("Stop when empty")).not.toBeInTheDocument();
  });

  it("the Overview says the setting is on, in one small line", async () => {
    const instance = makeInstance({ idle_stop_minutes: 45, idle_stop_unavailable: null, permissions: ["instance.view", "instance.configure"] });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(qk.instance(instance.id), instance);
    qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
    const router = createMemoryRouter([{ path: "/servers/:id", element: <InstanceLayout />, children: [{ index: true, element: <Overview /> }] }], {
      initialEntries: [`/servers/${instance.id}`],
    });
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/Stops by itself after 45 minutes with nobody on it/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Change" })).toHaveAttribute("href", `/servers/${instance.id}/settings?tab=resources`);
  });
});
