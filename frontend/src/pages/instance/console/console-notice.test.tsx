import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { qk } from "@/lib/queries";
import { CommandInput } from "./CommandInput";
import { ConnectionIndicator } from "./ConnectionIndicator";
import { entryText, makeEntry, matches } from "./console-ansi";
import { PendingCommands } from "./pending-commands";

describe("panel notices in the console", () => {
  it("are a distinct line kind, kept under the Warnings filter and marked in copies", () => {
    const e = makeEntry("notice", "“stop” was turned into a Stop of the server (players are warned first)");
    expect(e.kind).toBe("notice");
    expect(e.rendered).toContain("panel:");
    expect(entryText(e)).toBe("[panel] “stop” was turned into a Stop of the server (players are warned first)");
    expect(matches(e, { text: "", level: "warn" })).toBe(true);
    expect(matches(e, { text: "", level: "error" })).toBe(false);
    // untrusted text never reaches the terminal with control sequences
    expect(makeEntry("notice", "a\x1b]0;title\x07b").plain).toBe("ab");
  });
});

describe("console input", () => {
  it("explains that stop goes through the panel", () => {
    const qc = new QueryClient();
    qc.setQueryData(qk.auth, { setup_required: false, authenticated: true, user: null });
    render(
      <QueryClientProvider client={qc}>
        <CommandInput instanceId="abc" disabledReason={null} pending={0} onSubmit={() => true} />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("textbox", { name: "Console command" })).toHaveAccessibleDescription(/stop stops the server through the panel/);
  });
});

describe("ConnectionIndicator", () => {
  beforeAll(() => {
    // jsdom lacks what the tooltip's popper measures with
    globalThis.ResizeObserver ??= class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  });

  it("offers a reconnect while live, in case the stream went quiet", async () => {
    const onRetry = vi.fn();
    render(
      <TooltipProvider>
        <ConnectionIndicator state="connected" agentMessage={null} retryAt={null} onRetry={onRetry} />
      </TooltipProvider>,
    );
    await userEvent.setup().click(screen.getByRole("button", { name: "Reconnect the console" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe("pending console commands", () => {
  it("a typed stop: the notice ends the wait once and the command_result after it is not shown again", () => {
    const p = new PendingCommands();
    p.add("c1", "stop");
    expect(p.size).toBe(1);
    // notice{level, message, id, job_id} first (the backend sends the command's id with it)
    expect(p.notice("c1")).toBe(true);
    expect(p.size).toBe(0);
    expect(p.notice("c1")).toBe(false); // a second notice changes nothing
    // then command_result with the same id and text
    expect(p.result("c1")).toBe("skip");
    expect(p.size).toBe(0);
  });

  it("other commands, notices without an id and an older backend's lone command_result are shown", () => {
    const p = new PendingCommands();
    p.add("c2", "list");
    expect(p.notice(undefined)).toBe(false);
    expect(p.size).toBe(1);
    expect(p.result("c2")).toBe("show");
    expect(p.size).toBe(0);
    // numeric ids (as the backend echoes what it got) match too
    p.add("7", "stop");
    expect(p.notice(7)).toBe(true);
    expect(p.result(7)).toBe("skip");
  });

  it("forgets everything when the connection drops", () => {
    const p = new PendingCommands();
    p.add("a", "say hi");
    p.add("b", "stop");
    p.notice("b");
    expect(p.drop()).toEqual(["say hi"]);
    expect(p.size).toBe(0);
    expect(p.result("b")).toBe("show");
  });
});
