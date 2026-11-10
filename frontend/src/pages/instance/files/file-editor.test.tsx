import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { makeInstance } from "@/test/fixtures";
import { FileEditorDialog } from "./file-editor";
import { isNonUtf8 } from "./file-utils";

const SERVER = makeInstance({ status: "stopped" });

afterEach(() => vi.restoreAllMocks());

function open(file: Record<string, unknown>) {
  vi.spyOn(api, "get").mockResolvedValue({ path: "config/legacy.cfg", content: "caf�", size: 4, modified: 1_700_000_000, ...file } as never);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([
    {
      path: "/",
      element: <FileEditorDialog instance={SERVER} target={{ path: "config/legacy.cfg", key: 1 }} canWrite onClose={() => {}} />,
    },
  ]);
  render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("text files that are not valid UTF-8", () => {
  it("reads either flag name the backend may use", () => {
    expect(isNonUtf8({ binary_or_non_utf8: true })).toBe(true);
    expect(isNonUtf8({ non_utf8: true })).toBe(true);
    expect(isNonUtf8({ non_utf8: false })).toBe(false);
    expect(isNonUtf8({})).toBe(false);
    expect(isNonUtf8(undefined)).toBe(false);
  });

  it("open read-only with a notice and offer download instead of save", async () => {
    open({ binary_or_non_utf8: true });
    const editor = await screen.findByRole("textbox", { name: "Contents of legacy.cfg" });
    expect(editor).toHaveAttribute("readonly");
    expect(screen.getByText(/not valid UTF-8 text/)).toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Save/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Download/ })).toHaveAttribute("href", expect.stringContaining("files/download"));
  });

  it("stay editable when the flag is absent (older panel) or false", async () => {
    open({ non_utf8: false });
    const editor = await screen.findByRole("textbox", { name: "Contents of legacy.cfg" });
    expect(editor).not.toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: /Save/ })).toBeInTheDocument();
    expect(screen.queryByText(/not valid UTF-8 text/)).toBeNull();
  });
});
