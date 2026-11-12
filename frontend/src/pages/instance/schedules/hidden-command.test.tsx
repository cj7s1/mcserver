/**
 * D-13 (security review 2026-11-11): the schedules of a server list a command schedule's `payload.command`
 * and `last_result` as null to users without console access on that server. The page says they are
 * hidden, and such a schedule cannot be edited (its command would have to be typed anew, unseen); it can
 * still be paused or resumed.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PermissionCode, Schedule } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { InstanceLayout } from "../InstanceLayout";
import Schedules from "../Schedules";
import { commandHidden, draftFrom, editBlockedReason } from "./schedule-meta";

const schedule = (over: Partial<Schedule>): Schedule => ({
  id: 1,
  name: "Announce",
  action: "command",
  cron: "0 * * * *",
  payload: { command: "say hello" },
  enabled: true,
  only_when_running: true,
  last_run_at: "2026-11-11T10:00:00Z",
  next_run_at: "2026-11-11T11:00:00Z",
  last_result: "Done: hello",
  created_at: "2026-11-01T00:00:00Z",
  ...over,
});

const HIDDEN = schedule({ payload: { command: null }, last_result: null });

describe("commandHidden", () => {
  it("is true only for a command schedule whose command the panel sent as null", () => {
    expect(commandHidden(HIDDEN)).toBe(true);
    expect(commandHidden(schedule({}))).toBe(false);
    expect(commandHidden(schedule({ action: "backup", payload: { name: null } }))).toBe(false);
    expect(commandHidden(schedule({ payload: {} }))).toBe(false);
    expect(draftFrom(HIDDEN).command).toBe("");
  });

  it("blocks editing a hidden command, also for users who may send commands", () => {
    const can = (codes: PermissionCode[]) => (c: PermissionCode) => codes.includes(c);
    expect(editBlockedReason(HIDDEN, can(["instance.schedules", "instance.command"]))).toMatch(/hidden from you.*“View console” permission/);
    expect(editBlockedReason(schedule({}), can(["instance.schedules", "instance.command"]))).toBeNull();
  });
});

describe("the schedules page", () => {
  let gets: Record<string, unknown>;
  beforeAll(() => {
    globalThis.ResizeObserver ??= class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  });
  beforeEach(() => {
    gets = {};
    vi.spyOn(api, "get").mockImplementation(((path: string) =>
      path in gets ? Promise.resolve(structuredClone(gets[path])) : new Promise(() => {})) as never);
  });
  afterEach(() => vi.restoreAllMocks());

  function renderPage(instance: Instance, schedules: Schedule[]) {
    gets[`/instances/${instance.id}`] = instance;
    gets[`/instances/${instance.id}/schedules`] = schedules;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(qk.instance(instance.id), instance);
    const router = createMemoryRouter(
      [{ path: "/servers/:id", element: <InstanceLayout />, children: [{ path: "schedules", element: <Schedules /> }] }],
      { initialEntries: [`/servers/${instance.id}/schedules`] },
    );
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
  }

  it("says the command and the result are hidden; the schedule can be paused but not edited", async () => {
    const inst = makeInstance({ permissions: ["instance.view", "instance.schedules", "instance.command", "instance.control"] });
    renderPage(inst, [HIDDEN, schedule({ id: 2, name: "Nightly restart", action: "restart", payload: {}, last_result: "Done" })]);
    const row = (await screen.findByText("Announce")).closest("li")!;
    expect(within(row).getByText("Command hidden (needs console access)")).toBeInTheDocument();
    expect(within(row).getByText("Result hidden (needs console access)")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Edit schedule Announce" })).not.toBeInTheDocument();
    expect(within(row).getByRole("switch", { name: "Pause schedule Announce" })).toBeEnabled();
    // other schedules as before
    const restart = screen.getByText("Nightly restart").closest("li")!;
    expect(within(restart).getByText("Done")).toBeInTheDocument();
    expect(within(restart).getByRole("button", { name: "Edit schedule Nightly restart" })).toBeInTheDocument();
  });

  it("with console access the command and its result are shown as before", async () => {
    const inst = makeInstance({ permissions: ["instance.view", "instance.schedules", "instance.command", "instance.console"] });
    renderPage(inst, [schedule({})]);
    const row = (await screen.findByText("Announce")).closest("li")!;
    expect(within(row).getByText("/say hello")).toBeInTheDocument();
    expect(within(row).getByText("Done: hello")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Edit schedule Announce" })).toBeInTheDocument();
  });
});
