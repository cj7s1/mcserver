/**
 * Answers of the panel's round 2 (security review 2026-11-11) shown where the user meets them:
 * E-3: a world reset refused with 400 `validation_error` names the seed (`details.fields.seed`): the message
 * appears at the seed field; C-2: a rename of a secret-bearing file by a user without configure answers 403
 * `managed_file`: the rename dialog shows the panel's message.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import type { ConfigField } from "@/lib/types";
import { makeInstance } from "@/test/fixtures";
import { NameDialog } from "../files/name-dialog";
import { SavesResetDialog } from "./SavesDialogs";
import { resetFieldErrors } from "./saves-model";

afterEach(() => vi.restoreAllMocks());

const FIELDS: ConfigField[] = [
  { key: "seed", type: "int", label: "Map seed", nullable: true },
  { key: "world_size", type: "int", label: "World size", min: 1000, max: 6000, default: 3500 },
];

describe("a refused world reset", () => {
  it("maps the field errors of the answer to the form's fields", () => {
    expect(resetFieldErrors(FIELDS, { fields: { seed: ["Must be at most 2147483647"] } })).toEqual({ seed: "Must be at most 2147483647" });
    expect(resetFieldErrors(FIELDS, { fields: { settings: { world_size: ["Too small"] }, other: ["x"] } })).toEqual({ world_size: "Too small" });
    expect(resetFieldErrors(FIELDS, {})).toEqual({});
    expect(resetFieldErrors(FIELDS, undefined)).toEqual({});
  });

  it("shows the seed error at the seed field", async () => {
    vi.spyOn(api, "post").mockRejectedValue(
      new ApiError(400, "validation_error", "Some fields are invalid.", { fields: { seed: ["Must be at most 2147483647"] } }),
    );
    const instance = makeInstance({ name: "Rusty", game: "rust", status: "stopped" });
    render(<SavesResetDialog instance={instance} fields={FIELDS} folder={{}} onClose={() => {}} onStarted={() => {}} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/Map seed/), "4000000000");
    await user.type(screen.getByLabelText(/to confirm/), "Rusty");
    await user.click(screen.getByRole("button", { name: "Start a new world" }));
    await waitFor(() => expect(screen.getByText("Must be at most 2147483647")).toBeInTheDocument());
    expect(screen.getByLabelText(/Map seed/)).toHaveAttribute("aria-invalid", "true");
  });
});

describe("a refused rename", () => {
  it("shows the panel's message for 403 managed_file", async () => {
    const message = "server.properties holds the RCON password: renaming it needs the permission to configure the server";
    render(
      <NameDialog
        title="Rename file"
        label="New name"
        initial="server.properties"
        confirmLabel="Rename"
        onClose={() => {}}
        onSubmit={async () => {
          throw new ApiError(403, "managed_file", message);
        }}
      />,
    );
    const user = userEvent.setup();
    const input = screen.getByLabelText("New name");
    await user.clear(input);
    await user.type(input, "old.properties");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
  });
});
