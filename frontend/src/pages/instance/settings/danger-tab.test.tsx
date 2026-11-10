import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { toast } from "sonner";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, Instance, Job, PermissionCode } from "@/lib/types";
import { makeInstance, makeResetPlan } from "@/test/fixtures";
import { DangerTab } from "./danger-tab";

const RESET: PermissionCode[] = ["instance.view", "instance.restore", "instance.files.write"];
const ID = makeInstance().id;
const JOB = {
  id: "job-1",
  kind: "world_reset",
  title: "Reset world of Vanilla Test",
  instance: ID,
  status: "running",
  progress: 0.1,
  message: "Creating a safety backup of the current state",
  error: "",
  error_code: "",
  result: {},
  log: [],
} as unknown as Job;

const AUTH: AuthState = {
  setup_required: false,
  authenticated: true,
  user: {
    id: 1,
    username: "tester",
    email: "",
    first_name: "",
    last_name: "",
    is_owner: false,
    role_name: "Custom",
    permissions: [],
    must_change_password: false,
    preferences: {},
  },
};

function setup(over: Partial<Instance> = {}) {
  const instance = makeInstance({ status: "stopped", permissions: RESET, ...over });
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false }, mutations: { retry: false } } });
  qc.setQueryData(qk.auth, AUTH);
  const user = userEvent.setup();
  const view = (i: Instance) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <DangerTab instance={i} can={(code) => i.permissions.includes(code)} onDirtyChange={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const utils = render(view(instance));
  return { user, qc, instance, rerender: (next: Instance) => utils.rerender(view(next)) };
}

const resetButton = () => screen.getByRole("button", { name: "Reset world…" });
const rowTitles = () => screen.getAllByText(/^(Archive server|Unarchive server|Reset world|Delete permanently)$/).map((el) => el.textContent);

let post: ReturnType<typeof vi.spyOn>;
/** What GET /jobs/{id} returns: the server's record of the job. */
let serverJob: Job;

beforeAll(() => {
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

beforeEach(() => {
  serverJob = JOB;
  vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    if (path === `/instances/${ID}/world/reset`) return makeResetPlan();
    if (path === `/jobs/${JOB.id}`) return serverJob;
    throw new Error(`unexpected GET ${path}`);
  }) as typeof api.get);
  post = vi.spyOn(api, "post").mockResolvedValue(JOB as never);
  vi.spyOn(toast, "success").mockImplementation(() => "t");
  vi.spyOn(toast, "error").mockImplementation(() => "t");
  vi.spyOn(toast, "warning").mockImplementation(() => "t");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("DangerTab: Reset world row", () => {
  it("shows the Danger zone with only the reset row for a user who may restore and write files", () => {
    setup();
    expect(screen.getByRole("heading", { name: "Danger zone" })).toBeInTheDocument();
    expect(rowTitles()).toEqual(["Reset world"]);
    expect(resetButton()).toBeEnabled();
    expect(
      screen.getByText(
        "Deletes the world so the server generates a new one the next time it starts. Settings, mods, plugins, the whitelist, operators and backups are kept. The next step lets you take a safety backup first.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("No actions available")).not.toBeInTheDocument();
  });

  it("sits between Archive and Delete for a user who may also delete", () => {
    setup({ permissions: [...RESET, "instance.delete"] });
    expect(rowTitles()).toEqual(["Archive server", "Reset world", "Delete permanently"]);
  });

  it.each([
    ["instance.restore alone", ["instance.view", "instance.restore"]],
    ["instance.files.write alone", ["instance.view", "instance.files.write"]],
    ["instance.delete alone", ["instance.view", "instance.delete", "instance.backup", "instance.files.read"]],
  ] as [string, PermissionCode[]][])("needs both permissions (%s)", (_label, permissions) => {
    setup({ permissions });
    expect(screen.queryByRole("button", { name: "Reset world…" })).not.toBeInTheDocument();
    if (permissions.includes("instance.delete")) {
      expect(rowTitles()).toEqual(["Archive server", "Delete permanently"]);
    } else {
      expect(screen.queryByRole("heading", { name: "Danger zone" })).not.toBeInTheDocument();
      expect(screen.getByText("No actions available")).toBeInTheDocument();
      expect(screen.getByText(/Resetting the world needs the Restore backups and Write files permissions\./)).toBeInTheDocument();
    }
  });

  it("is disabled while another job runs", () => {
    setup({ active_job: { id: "j9", kind: "backup", title: "Backup", progress: 0.3, message: "", status: "running" } });
    expect(resetButton()).toBeDisabled();
  });

  it("is not available for an archived server", () => {
    setup({ archived_at: "2026-11-01T00:00:00Z", status: "archived", permissions: [...RESET, "instance.delete"] });
    expect(rowTitles()).toEqual(["Unarchive server", "Reset world", "Delete permanently"]);
    expect(resetButton()).toBeDisabled();
    expect(screen.getByText("Not available while the server is archived. Unarchive it first.")).toBeInTheDocument();
  });

  it("opens the dialog and gets focus back when it is closed", async () => {
    const { user } = setup();
    await user.click(resetButton());
    expect(await screen.findByRole("dialog", { name: "Reset world" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(resetButton()).toHaveFocus());
  });

  it("tracks the job it started and reports the outcome once", async () => {
    const { user, qc, instance, rerender } = setup();
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await user.click(resetButton());
    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const alert = await screen.findByRole("alertdialog");
    await user.type(within(alert).getByRole("textbox"), instance.name);
    await user.click(within(alert).getByRole("button", { name: "Reset world" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(`/instances/${ID}/world/reset`, expect.objectContaining({ confirm_name: instance.name })));

    // the progress block of the tab shows the queued job
    expect(await screen.findByText("Reset world of Vanilla Test")).toBeInTheDocument();
    expect(screen.getByText("Creating a safety backup of the current state")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Resetting the world…");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());

    // The job finishes. A live event only carries the status (lib/events.ts merges it into the cached
    // job), so the result must come from the server's record, not from the stale cached one.
    invalidate.mockClear();
    serverJob = {
      ...JOB,
      status: "succeeded",
      progress: 1,
      result: { deleted: ["world"], kept: [], warnings: [], leftover: null, safety_backup: "b1", restarted: true },
    } as Job;
    act(() => {
      qc.setQueryData(qk.job(JOB.id), { ...JOB, status: "succeeded", progress: 1 });
    });
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("The world was reset", {
        description:
          "The server was started again and is generating a new world. The old world is in the “Before restore” safety backup on the Backups page.",
        duration: undefined,
      }),
    );
    expect(api.get).toHaveBeenCalledWith(`/jobs/${JOB.id}`);
    const invalidated = invalidate.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
    for (const key of [qk.instance(ID), qk.world(ID), qk.worldReset(ID), qk.backups(ID), qk.properties(ID)]) {
      expect(invalidated).toContain(JSON.stringify(key));
    }
    // reported once, also when the tab renders again
    rerender({ ...instance, status: "stopped" });
    expect(vi.mocked(toast.success).mock.calls.filter(([title]) => title === "The world was reset")).toHaveLength(1);
  });

  it("shows the log of a failed reset and says the world may need a check", async () => {
    const { user, qc, instance } = setup();
    await user.click(resetButton());
    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const alert = await screen.findByRole("alertdialog");
    await user.type(within(alert).getByRole("textbox"), instance.name);
    await user.click(within(alert).getByRole("button", { name: "Reset world" }));
    await screen.findByText("Reset world of Vanilla Test");

    serverJob = {
      ...JOB,
      status: "failed",
      error: "The node agent did not answer",
      error_code: "agent_unavailable",
      log: ["World folder: world", "The reset may be incomplete, so the server was left stopped."],
    } as Job;
    // the live event has the error, but not the last log lines
    act(() => {
      qc.setQueryData(qk.job(JOB.id), { ...JOB, status: "failed", error: serverJob.error, error_code: serverJob.error_code });
    });
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Reset world failed",
        expect.objectContaining({ description: expect.stringMatching(/^The node agent did not answer\. The reset may be incomplete\./), duration: 15_000 }),
      ),
    );
    // the final log is read and shown in the tab's progress block
    expect(await screen.findByText(/The reset may be incomplete, so the server was left stopped\./)).toBeInTheDocument();
  });

  it("reports a finished job when its final record cannot be read", async () => {
    const { user, qc, instance } = setup();
    await user.click(resetButton());
    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Continue" })).toBeEnabled());
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));
    const alert = await screen.findByRole("alertdialog");
    await user.type(within(alert).getByRole("textbox"), instance.name);
    await user.click(within(alert).getByRole("button", { name: "Reset world" }));
    await screen.findByText("Reset world of Vanilla Test");

    vi.mocked(api.get).mockRejectedValue(new Error("offline"));
    act(() => {
      qc.setQueryData(qk.job(JOB.id), { ...JOB, status: "succeeded", progress: 1 });
    });
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("The world was reset", {
        description: "The job log on the Activity tab says what was deleted and kept.",
        duration: undefined,
      }),
    );
  });
});
