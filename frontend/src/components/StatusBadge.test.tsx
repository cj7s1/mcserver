import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge, statusLabel } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders human labels with a status role", () => {
    render(<StatusBadge status="backing_up" title="Running restic" />);
    const el = screen.getByRole("status");
    expect(el).toHaveTextContent("Backing up");
    expect(el).toHaveAttribute("title", "Running restic");
  });

  it("distinguishes installing/starting from running", () => {
    expect(statusLabel("installing")).toBe("Installing");
    expect(statusLabel("unhealthy")).toBe("Unresponsive");
    expect(statusLabel("created")).toBe("Not installed");
  });
});
