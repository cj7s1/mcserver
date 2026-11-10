import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { MemoryRouter } from "react-router";
import { toast } from "sonner";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, Job, WorldResetPlan } from "@/lib/types";
import { makeBukkitResetPlan, makeInstance, makeResetPlan } from "@/test/fixtures";
import { ResetWorldDialog } from "./reset-world-dialog";

const INSTANCE = makeInstance({ name: "Vanilla Test", status: "stopped" });
const RESET_URL = `/instances/${INSTANCE.id}/world/reset`;
const JOB = { id: "job-1", kind: "world_reset", title: "Reset world of Vanilla Test", status: "queued" } as Job;

/** The dialog opened from a button, the way the Danger zone row opens it. */
function Harness({ instance, onStarted }: { instance: Instance; onStarted: (job: Job) => void }) {
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button ref={opener} type="button" onClick={() => setOpen(true)}>
        Open reset
      </button>
      {open && <ResetWorldDialog instance={instance} returnFocusRef={opener} onClose={() => setOpen(false)} onStarted={onStarted} />}
    </>
  );
}

let get: ReturnType<typeof vi.spyOn>;
let post: ReturnType<typeof vi.spyOn>;
let toastError: ReturnType<typeof vi.spyOn>;
let toastSuccess: ReturnType<typeof vi.spyOn>;

/** Render the harness and open the dialog; `plans` are the answers of successive GET /world/reset requests (the last one repeats). */
async function setup(plans: (WorldResetPlan | Error)[] = [makeResetPlan()], instance: Instance = INSTANCE) {
  const answers = [...plans];
  get = vi.spyOn(api, "get").mockImplementation((async (path: string) => {
    if (path !== RESET_URL) throw new Error(`unexpected GET ${path}`);
    const answer = answers.length > 1 ? answers.shift()! : answers[0];
    if (answer instanceof Error) throw answer;
    return answer;
  }) as typeof api.get);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onStarted = vi.fn();
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Harness instance={instance} onStarted={onStarted} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Open reset" }));
  const dialog = await screen.findByRole("dialog", { name: "Reset world" });
  return { user, dialog, onStarted, qc };
}

const continueButton = () => screen.getByRole("button", { name: "Continue" });

/** Step 1 -> step 2; returns the confirmation dialog. */
async function toConfirmation(user: ReturnType<typeof userEvent.setup>) {
  await waitFor(() => expect(continueButton()).toBeEnabled());
  await user.click(continueButton());
  return screen.findByRole("alertdialog");
}

async function confirm(user: ReturnType<typeof userEvent.setup>, alert: HTMLElement, name = INSTANCE.name) {
  await user.type(within(alert).getByRole("textbox"), name);
  await user.click(within(alert).getByRole("button", { name: "Reset world" }));
}

beforeAll(() => {
  // jsdom lacks it; Radix measures checkboxes and radios that sit inside a form.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

beforeEach(() => {
  post = vi.spyOn(api, "post").mockResolvedValue(JOB as never);
  toastError = vi.spyOn(toast, "error").mockImplementation(() => "t");
  toastSuccess = vi.spyOn(toast, "success").mockImplementation(() => "t");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ResetWorldDialog: options", () => {
  it("reads the plan when it opens and shows what is deleted and kept", async () => {
    const { dialog } = await setup([makeBukkitResetPlan()]);
    expect(get).toHaveBeenCalledWith(RESET_URL);
    const deleted = await within(dialog).findByRole("list", { name: "Deleted" });
    const rows = within(deleted).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector("code")?.textContent)).toEqual(["bruh", "bruh_nether", "bruh_the_end"]);
    expect(rows[0]).toHaveTextContent("Overworld");
    expect(rows[0]).toHaveTextContent("1.0 GB · 42 files · last changed");
    expect(rows[1]).toHaveTextContent("Nether");
    expect(dialog).toHaveTextContent("Player data goes with the world");
    expect(dialog).toHaveTextContent("the Nether and the End have their own folders");
    expect(dialog).toHaveTextContent("the whitelist, operators, bans, and all backups");
    expect(dialog).toHaveTextContent("keeps pointing at places in the old world");
    expect(dialog).toHaveTextContent("Other worlds on this server, for example Multiverse worlds, are not touched.");
  });

  it("shows the loading state, then a failed plan with a retry", async () => {
    const { user, dialog } = await setup([new ApiError(503, "agent_unavailable", "The node agent is not reachable."), makeResetPlan()]);
    expect(await within(dialog).findByText("Could not check the world")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("The node agent is not reachable.");
    expect(continueButton()).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Retry" }));
    expect(await within(dialog).findByRole("list", { name: "Deleted" })).toHaveTextContent("world");
    await waitFor(() => expect(continueButton()).toBeEnabled());
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("keeps Continue disabled while the plan loads", async () => {
    let resolve!: (plan: WorldResetPlan) => void;
    get = vi.spyOn(api, "get").mockImplementation((() => new Promise<WorldResetPlan>((r) => (resolve = r))) as unknown as typeof api.get);
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <Harness instance={INSTANCE} onStarted={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Open reset" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    expect(within(dialog).getByRole("status")).toHaveTextContent("Checking the world folder…");
    expect(continueButton()).toBeDisabled();
    resolve(makeResetPlan());
    await waitFor(() => expect(continueButton()).toBeEnabled());
  });

  it("shows blockers as an error and disables Continue", async () => {
    const blocked = makeResetPlan({
      can_reset: false,
      delete: [],
      blockers: [{ code: "not_a_world", message: "'bluemap' does not look like a Minecraft world, so it is not deleted." }],
    });
    const { user, dialog } = await setup([blocked, makeResetPlan()]);
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("The world cannot be reset");
    expect(alert).toHaveTextContent("'bluemap' does not look like a Minecraft world, so it is not deleted.");
    expect(continueButton()).toBeDisabled();
    // nothing to choose while it is blocked
    expect(within(dialog).queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    // fixed elsewhere (e.g. level-name corrected): the plan can be read again without reopening
    await user.click(within(dialog).getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(continueButton()).toBeEnabled());
    expect(within(dialog).queryByText("The world cannot be reset")).not.toBeInTheDocument();
  });

  it("offers a random or a custom seed when none is set, with a hint how to find the current one", async () => {
    const { dialog } = await setup();
    const seeds = await within(dialog).findByRole("radiogroup", { name: "Seed of the new world" });
    expect(within(seeds).getAllByRole("radio").map((r) => r.getAttribute("value"))).toEqual(["random", "custom"]);
    expect(within(seeds).getByRole("radio", { name: /Random seed/ })).toBeChecked();
    expect(dialog).toHaveTextContent("No seed is set in server.properties.");
    expect(dialog).toHaveTextContent("run seed in the console while the server is running");
  });

  it("preselects keeping a configured seed", async () => {
    const { dialog } = await setup([makeBukkitResetPlan()]);
    const seeds = await within(dialog).findByRole("radiogroup", { name: "Seed of the new world" });
    expect(within(seeds).getAllByRole("radio").map((r) => r.getAttribute("value"))).toEqual(["random", "keep", "custom"]);
    expect(within(seeds).getByRole("radio", { name: /Keep the current seed setting \(level-seed: -4172144997902289642\)/ })).toBeChecked();
    expect(dialog).not.toHaveTextContent("No seed is set");
  });

  it("disables the seed choices when SEED is in the environment and points to Settings → Environment", async () => {
    const { user, dialog } = await setup([makeResetPlan({ seed: { value: "20261109", source: "env" } })]);
    const seeds = await within(dialog).findByRole("radiogroup", { name: "Seed of the new world" });
    const radios = within(seeds).getAllByRole("radio");
    expect(radios).toHaveLength(3);
    for (const radio of radios) expect(radio).toBeDisabled();
    expect(within(seeds).getByRole("radio", { name: /Keep the current seed setting \(SEED: 20261109\)/ })).toBeChecked();
    expect(dialog).toHaveTextContent("The seed is set by the SEED environment variable");
    expect(within(dialog).getByRole("link", { name: "Settings → Environment" })).toHaveAttribute(
      "href",
      `/servers/${INSTANCE.id}/settings?tab=environment`,
    );
    const alert = await toConfirmation(user);
    expect(alert).toHaveTextContent("The seed stays as the SEED environment variable sets it (20261109)");
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toMatchObject({ seed_mode: "keep" });
    expect(post.mock.calls[0][1]).not.toHaveProperty("seed");
  });

  it("treats an empty SEED variable as set: the seed cannot be chosen", async () => {
    const { dialog } = await setup([makeResetPlan({ seed: { value: "", source: "env" } })]);
    const seeds = await within(dialog).findByRole("radiogroup", { name: "Seed of the new world" });
    for (const radio of within(seeds).getAllByRole("radio")) expect(radio).toBeDisabled();
    expect(within(seeds).getByRole("radio", { name: /Keep the current seed setting \(SEED: empty\)/ })).toBeChecked();
    expect(dialog).toHaveTextContent("An empty seed setting gives the new world a random seed.");
    expect(dialog).not.toHaveTextContent("No seed is set in server.properties");
  });

  it("needs a valid custom seed before continuing", async () => {
    const { user, dialog } = await setup();
    await user.click(await within(dialog).findByRole("radio", { name: "Custom seed" }));
    const field = within(dialog).getByRole("textbox", { name: "Seed" });
    expect(continueButton()).toBeDisabled();
    // no error while the field was not touched yet
    expect(within(dialog).queryByText(/Enter a seed/)).not.toBeInTheDocument();
    expect(field).not.toBeInvalid();
    await user.click(field);
    await user.tab();
    expect(await within(dialog).findByText("Enter a seed, or choose another option.")).toBeInTheDocument();
    expect(field).toBeInvalid();
    expect(field).toHaveAccessibleDescription("A number or any text, up to 200 characters. Enter a seed, or choose another option.");

    await user.click(field);
    await user.paste("x".repeat(201));
    expect(within(dialog).getByText(/at most 200 characters; this one has 201/)).toBeInTheDocument();
    expect(continueButton()).toBeDisabled();

    await user.clear(field);
    await user.type(field, "  Glacier 12 ");
    expect(field).not.toBeInvalid();
    expect(field).toHaveAccessibleDescription("A number or any text, up to 200 characters.");
    expect(continueButton()).toBeEnabled();
    // Enter in the field continues
    await user.keyboard("{Enter}");
    const alert = await screen.findByRole("alertdialog");
    expect(alert).toHaveTextContent("level-seed is set to Glacier 12");
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toMatchObject({ seed_mode: "custom", seed: "Glacier 12" });
  });

  it("offers only the carry-overs that exist", async () => {
    const { dialog } = await setup();
    await within(dialog).findByRole("list", { name: "Deleted" });
    expect(within(dialog).queryByRole("group", { name: "Carry over into the new world" })).not.toBeInTheDocument();
    // only the safety backup
    expect(within(dialog).getAllByRole("checkbox")).toHaveLength(1);
  });

  it("sends the carry-overs that stay ticked", async () => {
    const { user, dialog } = await setup([makeBukkitResetPlan()]);
    const group = await within(dialog).findByRole("group", { name: "Carry over into the new world" });
    const datapacks = within(group).getByRole("checkbox", { name: /Datapacks/ });
    const config = within(group).getByRole("checkbox", { name: /World configuration/ });
    expect(datapacks).toBeChecked();
    expect(config).toBeChecked();
    expect(group).toHaveTextContent("bruh/datapacks");
    expect(group).toHaveTextContent("bruh/paper-world.yml, bruh_nether/paper-world.yml");
    await user.click(datapacks);
    const alert = await toConfirmation(user);
    expect(alert).toHaveTextContent("Carried over into the new world: bruh/paper-world.yml and bruh_nether/paper-world.yml.");
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toMatchObject({ keep: ["config"] });
  });

  it("offers the safety backup ticked, with how long it is kept", async () => {
    const first = await setup();
    const backup = await within(first.dialog).findByRole("checkbox", { name: /Take a safety backup of the server folder first \(recommended\)/ });
    expect(backup).toBeChecked();
    expect(backup).toHaveAccessibleDescription("If it fails or does not contain the world, nothing is deleted. Kept 14 days.");
  });

  it.each([
    [{ safety_backup_keep_days: 0 }, "Safety backups are deleted by the next backup; pin it to keep it."],
    [{ safety_backup_keep_days: 1 }, "Kept 1 day."],
    [{ backup_exclusions: 3 }, "Kept 14 days. This backup ignores the server's 3 backup exclusions, so it can be larger than usual."],
    [
      { backup_exclusions: 1, safety_backup_keep_days: 0 },
      "Safety backups are deleted by the next backup; pin it to keep it. This backup ignores the server's 1 backup exclusion, so it can be larger than usual.",
    ],
  ])("safety backup note for %j", async (over, note) => {
    const { dialog } = await setup([makeResetPlan(over)]);
    const backup = await within(dialog).findByRole("checkbox", { name: /Take a safety backup/ });
    expect(backup).toHaveAccessibleDescription(`If it fails or does not contain the world, nothing is deleted. ${note}`);
  });

  it("follows the panel setting for the safety backup default", async () => {
    const { dialog } = await setup([makeResetPlan({ pre_restore_backup_default: false })]);
    expect(await within(dialog).findByRole("checkbox", { name: /Take a safety backup/ })).not.toBeChecked();
    expect(dialog).toHaveTextContent("Without a safety backup the world cannot be recovered after the reset.");
  });

  it("warns in both steps when the safety backup is unticked", async () => {
    const { user, dialog } = await setup([makeBukkitResetPlan()]);
    const backup = await within(dialog).findByRole("checkbox", { name: /Take a safety backup/ });
    expect(dialog).not.toHaveTextContent("cannot be recovered");
    await user.click(backup);
    expect(dialog).toHaveTextContent("Without a safety backup the world cannot be recovered after the reset.");
    // the notes about the backup go with it
    expect(backup).toHaveAccessibleDescription("If it fails or does not contain the world, nothing is deleted.");
    const alert = await toConfirmation(user);
    expect(alert).toHaveTextContent("No safety backup is taken: the world cannot be recovered afterwards.");
    expect(alert).not.toHaveTextContent("stays offline during the backup");
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0][1]).toMatchObject({ pre_restore_backup: false });
  });

  it("does not continue while another job runs on the server", async () => {
    const busy = makeInstance({
      ...INSTANCE,
      active_job: { id: "j9", kind: "backup", title: "Backup of Vanilla Test", progress: 0.2, message: "", status: "running" },
    });
    const { dialog } = await setup([makeResetPlan()], busy);
    await within(dialog).findByRole("list", { name: "Deleted" });
    expect(dialog).toHaveTextContent("Another operation is running on this server (Backup of Vanilla Test).");
    expect(continueButton()).toBeDisabled();
  });
});

describe("ResetWorldDialog: confirmation", () => {
  it("lists what happens and stays disabled until the exact server name is typed", async () => {
    const { user } = await setup([makeBukkitResetPlan()]);
    const alert = await toConfirmation(user);
    expect(within(alert).getByRole("heading", { name: "Reset the world of Vanilla Test?" })).toBeInTheDocument();
    expect(within(alert).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "The server is stopped (players are disconnected) and stays offline during the backup.",
      "A safety backup of the server folder is taken and checked (“Before restore”); like every backup it leaves out old logs, crash reports and caches. If it fails or does not contain the world, nothing is deleted.",
      "The world folders bruh, bruh_nether and bruh_the_end (2.0 GB in total) are deleted, with all player data in them. " +
        "Carried over into the new world: bruh/datapacks, bruh/paper-world.yml and bruh_nether/paper-world.yml.",
      "The seed setting is kept (level-seed: -4172144997902289642): the new world is generated from this seed.",
      "The server is started again and generates the new world.",
    ]);
    const action = within(alert).getByRole("button", { name: "Reset world" });
    const field = within(alert).getByRole("textbox");
    expect(action).toBeDisabled();
    await user.type(field, "vanilla test");
    expect(action).toBeDisabled();
    await user.clear(field);
    await user.type(field, "Vanilla Tes");
    expect(action).toBeDisabled();
    await user.type(field, "t ");
    expect(action).toBeDisabled();
    await user.clear(field);
    await user.type(field, "Vanilla Test");
    expect(action).toBeEnabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("sends the default request for a running Paper server", async () => {
    const { user, onStarted } = await setup([makeBukkitResetPlan()]);
    const alert = await toConfirmation(user);
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(RESET_URL, {
      confirm_name: "Vanilla Test",
      folders: ["bruh", "bruh_nether", "bruh_the_end"],
      stop_if_running: true,
      pre_restore_backup: true,
      seed_mode: "keep",
      keep: ["datapacks", "config"],
    });
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(JOB));
    expect(toastSuccess).toHaveBeenCalledWith("Resetting the world…");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("sends the default request for a stopped vanilla server", async () => {
    const { user } = await setup();
    const alert = await toConfirmation(user);
    expect(alert).toHaveTextContent("The server stays stopped meanwhile.");
    expect(alert).toHaveTextContent("The server is left stopped.");
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(RESET_URL, {
      confirm_name: "Vanilla Test",
      folders: ["world"],
      stop_if_running: false,
      pre_restore_backup: true,
      seed_mode: "random",
      keep: [],
    });
  });

  it("keeps the confirmation open when the request fails, and can be retried", async () => {
    post.mockRejectedValueOnce(new ApiError(409, "instance_busy", "Another job is running on this server."));
    const { user, onStarted } = await setup();
    const alert = await toConfirmation(user);
    await confirm(user, alert);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Another job is running on this server."));
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    // the reason stays readable after the toast is gone
    const problem = within(screen.getByRole("alertdialog")).getByRole("alert");
    expect(problem).toHaveTextContent("The reset was not started");
    expect(problem).toHaveTextContent("Another job is running on this server.");
    expect(onStarted).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledTimes(1);
    // the name is still typed in: confirm again
    const action = within(screen.getByRole("alertdialog")).getByRole("button", { name: "Reset world" });
    await waitFor(() => expect(action).toBeEnabled());
    await user.click(action);
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(JOB));
    expect(post).toHaveBeenCalledTimes(2);
    expect(post.mock.calls[1]).toEqual(post.mock.calls[0]);
  });

  it.each([
    ["world_changed", "The world folders changed since you confirmed."],
    ["must_stop", "The server is running. Confirm that it may be stopped to reset the world."],
    ["seed_env_set", "The SEED environment variable is set, so a seed chosen here would not be used."],
  ])("returns to the options with a fresh plan on %s", async (code, message) => {
    post.mockRejectedValueOnce(new ApiError(409, code, message));
    const changed = makeResetPlan({
      level_name: "survival",
      folders: [{ ...makeResetPlan().folders[0], path: "survival" }],
      delete: ["survival"],
      running: true,
    });
    const { user, onStarted } = await setup([makeResetPlan(), changed]);
    const alert = await toConfirmation(user);
    expect(alert).toHaveTextContent("The world folder world (3.0 MB) is deleted");
    await confirm(user, alert);

    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(toastError).toHaveBeenCalledWith(message);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    const deleted = await within(dialog).findByRole("list", { name: "Deleted" });
    expect(deleted).toHaveTextContent("survival");
    expect(dialog).toHaveTextContent("Nothing was reset");
    expect(dialog).toHaveTextContent(`${message} The details below were read again.`);
    expect(onStarted).not.toHaveBeenCalled();

    // confirming again sends what the new plan says
    const again = await toConfirmation(user);
    expect(again).toHaveTextContent("The world folder survival (3.0 MB) is deleted");
    expect(again).toHaveTextContent("The server is stopped (players are disconnected)");
    expect(within(again).getByRole("textbox")).toHaveValue("");
    await confirm(user, again);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post.mock.calls[1][1]).toMatchObject({ folders: ["survival"], stop_if_running: true });
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(JOB));
  });
});

describe("ResetWorldDialog: a plan that no longer holds", () => {
  it("shows the blocker when the request is refused because the folder is not a world any more", async () => {
    const message = "'world' does not look like a Minecraft world, so it is not deleted.";
    post.mockRejectedValueOnce(new ApiError(409, "not_a_world", message));
    const blocked = makeResetPlan({ can_reset: false, delete: [], blockers: [{ code: "not_a_world", message }] });
    const { user, onStarted } = await setup([makeResetPlan(), blocked]);
    await confirm(user, await toConfirmation(user));

    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    expect(toastError).toHaveBeenCalledWith(message);
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("The world cannot be reset");
    expect(alert).toHaveTextContent(message);
    // said once: the blocker itself explains why nothing was reset
    expect(dialog).not.toHaveTextContent("Nothing was reset");
    expect(continueButton()).toBeDisabled();
    expect(onStarted).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("sends exactly what was confirmed, even if the cached plan changed after Continue", async () => {
    const { user, qc } = await setup();
    const alert = await toConfirmation(user);
    // e.g. a job that finished meanwhile made every query of the server refetch
    const other = makeResetPlan({ folders: [{ ...makeResetPlan().folders[0], path: "other" }], delete: ["other"], running: true });
    act(() => {
      qc.setQueryData(qk.worldReset(INSTANCE.id), other);
    });
    expect(alert).toHaveTextContent("The world folder world (3.0 MB) is deleted");
    expect(alert).not.toHaveTextContent("other");
    await confirm(user, alert);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    // the server compares this with its own fresh plan and answers world_changed if it differs
    expect(post.mock.calls[0][1]).toMatchObject({ folders: ["world"], stop_if_running: false });
  });

  it("shows the reading error when the plan cannot be read again", async () => {
    post.mockRejectedValueOnce(new ApiError(409, "world_changed", "The world folders changed."));
    const { user } = await setup([makeResetPlan(), new ApiError(503, "agent_unavailable", "The node agent is not reachable.")]);
    await confirm(user, await toConfirmation(user));
    const dialog = await screen.findByRole("dialog", { name: "Reset world" });
    expect(await within(dialog).findByText("Could not check the world")).toBeInTheDocument();
    expect(within(dialog).queryByRole("list", { name: "Deleted" })).not.toBeInTheDocument();
    expect(continueButton()).toBeDisabled();
  });
});

describe("ResetWorldDialog: keyboard and focus", () => {
  const opener = () => screen.getByRole("button", { name: "Open reset" });

  it("focuses the dialog when it opens and returns focus to the opener on Escape", async () => {
    const { user, dialog } = await setup();
    await waitFor(() => expect(dialog).toHaveFocus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener()).toHaveFocus());
  });

  it("returns focus to the opener when the options are cancelled", async () => {
    const { user, dialog } = await setup();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener()).toHaveFocus());
  });

  it("keeps focus in the confirmation, and returns it to the opener when that is cancelled", async () => {
    const { user } = await setup();
    const alert = await toConfirmation(user);
    const cancel = within(alert).getByRole("button", { name: "Cancel" });
    await waitFor(() => expect(cancel).toHaveFocus());
    // the options dialog that was replaced must not pull focus back to the opener
    await new Promise((r) => setTimeout(r, 30));
    expect(alert).toContainElement(document.activeElement as HTMLElement);
    await user.click(cancel);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(opener()).toHaveFocus());
  });

  it("can be completed with the keyboard alone", async () => {
    const { user, dialog, onStarted } = await setup([makeBukkitResetPlan()]);
    await within(dialog).findByRole("list", { name: "Deleted" });
    await waitFor(() => expect(continueButton()).toBeEnabled());
    // seed radios: arrow keys move the choice
    await user.tab();
    expect(within(dialog).getByRole("radio", { name: /Keep the current seed setting/ })).toHaveFocus();
    // (Radix moves focus a tick after keydown and selects only while the key is still down)
    await user.keyboard("{ArrowUp>}");
    await waitFor(() => expect(within(dialog).getByRole("radio", { name: /Random seed/ })).toBeChecked());
    await user.keyboard("{/ArrowUp}");
    // carry-overs and the safety backup: Space toggles
    await user.tab();
    expect(within(dialog).getByRole("checkbox", { name: /Datapacks/ })).toHaveFocus();
    await user.keyboard(" ");
    expect(within(dialog).getByRole("checkbox", { name: /Datapacks/ })).not.toBeChecked();
    await user.tab();
    await user.tab();
    expect(within(dialog).getByRole("checkbox", { name: /Take a safety backup/ })).toHaveFocus();
    await user.tab();
    await user.tab();
    expect(continueButton()).toHaveFocus();
    await user.keyboard("{Enter}");
    const alert = await screen.findByRole("alertdialog");
    await waitFor(() => expect(within(alert).getByRole("button", { name: "Cancel" })).toHaveFocus());
    await user.tab({ shift: true });
    expect(within(alert).getByRole("textbox")).toHaveFocus();
    await user.keyboard("Vanilla Test");
    await user.tab();
    await user.tab();
    expect(within(alert).getByRole("button", { name: "Reset world" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith(JOB));
    expect(post.mock.calls[0][1]).toMatchObject({ seed_mode: "random", keep: ["config"] });
  });
});
