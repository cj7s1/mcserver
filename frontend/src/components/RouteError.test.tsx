import { render, screen } from "@testing-library/react";
import { lazy, Suspense } from "react";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isChunkLoadError, reloadOnceForChunkError } from "@/lib/chunk-reload";
import { PageErrorBoundary, RouteErrorScreen } from "./RouteError";

const chunkError = () => new TypeError("Failed to fetch dynamically imported module: http://panel/assets/Backups-old.js");

beforeEach(() => sessionStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("chunk-load errors", () => {
  it("recognises the browsers' messages for a missing lazy chunk", () => {
    expect(isChunkLoadError(chunkError())).toBe(true);
    expect(isChunkLoadError(new TypeError("error loading dynamically imported module"))).toBe(true); // Firefox
    expect(isChunkLoadError(new TypeError("Importing a module script failed."))).toBe(true); // Safari
    expect(isChunkLoadError(new Error("Cannot read properties of undefined"))).toBe(false);
    expect(isChunkLoadError(null)).toBe(false);
  });

  it("reloads once, then not again within the guard window", () => {
    const reload = vi.fn();
    expect(reloadOnceForChunkError(1_000_000, reload)).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(reloadOnceForChunkError(1_005_000, reload)).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
    // much later (e.g. the next upgrade) it may reload again
    expect(reloadOnceForChunkError(2_000_000, reload)).toBe(true);
  });
});

function Layout() {
  return (
    <div>
      <nav>Sidebar</nav>
      <Outlet />
    </div>
  );
}

describe("route error boundaries", () => {
  it("keeps the layout when a page's chunk is gone after an upgrade", async () => {
    // already reloaded a moment ago: the boundary shows the message instead of looping
    sessionStorage.setItem("mcpanel.chunk-reload-at", String(Date.now()));
    vi.spyOn(console, "error").mockImplementation(() => {});
    const Broken = lazy(() => Promise.reject(chunkError()));
    const router = createMemoryRouter(
      [
        {
          element: <Layout />,
          errorElement: <RouteErrorScreen />,
          children: [
            {
              index: true,
              element: (
                <PageErrorBoundary>
                  <Suspense fallback={<p>Loading</p>}>
                    <Broken />
                  </Suspense>
                </PageErrorBoundary>
              ),
            },
          ],
        },
      ],
      { initialEntries: ["/"] },
    );
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("The panel was updated")).toBeInTheDocument();
    expect(screen.getByText("Sidebar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
  });

  it("shows a render error inside the layout instead of react-router's default screen", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    function Crash(): never {
      throw new Error("Boom while rendering");
    }
    const router = createMemoryRouter(
      [{ element: <Layout />, errorElement: <RouteErrorScreen />, children: [{ index: true, element: <PageErrorBoundary><Crash /></PageErrorBoundary> }] }],
      { initialEntries: ["/"] },
    );
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("This page could not be shown")).toBeInTheDocument();
    expect(screen.getByText("Boom while rendering")).toBeInTheDocument();
    expect(screen.getByText("Sidebar")).toBeInTheDocument();
  });

  it("the root errorElement replaces react-router's default when a layout fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    function BrokenLayout(): never {
      throw new Error("Layout broke");
    }
    const router = createMemoryRouter([{ element: <BrokenLayout />, errorElement: <RouteErrorScreen />, children: [{ index: true, element: <p>x</p> }] }]);
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("Layout broke")).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
  });
});
