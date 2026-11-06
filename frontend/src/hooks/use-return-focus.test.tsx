import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

/** A dialog opened from state (no DialogTrigger), the way most panel dialogs are opened. */
function StateDialog({ autoFocusField = false }: { autoFocusField?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      {open && (
        <Dialog open onOpenChange={setOpen}>
          <DialogContent>
            <DialogTitle>Example</DialogTitle>
            <DialogDescription>Body</DialogDescription>
            <input aria-label="Name" autoFocus={autoFocusField} />
          </DialogContent>
        </Dialog>
      )}
    </>
  );
}

describe("dialog focus return", () => {
  it.each([false, true])("returns focus to the opener after Escape (autoFocus field: %s)", async (autoFocusField) => {
    const user = userEvent.setup();
    render(<StateDialog autoFocusField={autoFocusField} />);
    const opener = screen.getByRole("button", { name: "Open" });
    await user.click(opener);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
