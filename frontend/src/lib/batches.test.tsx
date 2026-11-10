/** Client-side batching of >100-file requests: bulk mod updates and multi-file uploads. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { type UpdateResponse, updateInBatches } from "@/pages/instance/content/content-api";
import { reportFromBatchedUpdate } from "@/pages/instance/content/content-report";
import { batchLabel, chunk } from "./batches";

const uploads = vi.hoisted(() => ({ fn: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/api")>();
  return { ...mod, uploadWithProgress: (...args: unknown[]) => uploads.fn(...args) };
});

// imported after the mock is registered (vi.mock is hoisted)
const { useFileUploads, UploadList } = await import("@/pages/instance/files/uploads");

const ID = "11111111-1111-1111-1111-111111111111";
const names = (n: number) => Array.from({ length: n }, (_, i) => `mod-${String(i + 1).padStart(3, "0")}.jar`);

function okResponse(files: string[]): UpdateResponse {
  return {
    results: files.map((f) => ({ filename: f, status: "updated", new_filename: f.replace(".jar", "-new.jar") })),
    updated: files.length,
    failed: 0,
    missing_dependencies: files.length ? [{ project_id: "P1", version_id: null, file_name: null, required_by: "x", reason: "not_installed", message: null }] : [],
    restart_required: true,
  };
}

afterEach(() => vi.restoreAllMocks());

describe("chunk", () => {
  it("splits into batches of 100 without empty batches", () => {
    expect(chunk(names(250)).map((b) => b.length)).toEqual([100, 100, 50]);
    expect(chunk(names(100)).map((b) => b.length)).toEqual([100]);
    expect(chunk([])).toEqual([]);
    expect(batchLabel({ batch: 2, batches: 3, done: 100, total: 250 })).toBe("batch 2 of 3 (files 101–200 of 250)");
    expect(batchLabel({ batch: 1, batches: 1, done: 0, total: 40 })).toBe("");
  });
});

describe("bulk mod update in batches", () => {
  it("stops sending batches once the server answers that it became busy part-way (per-file results)", async () => {
    const busy = "Another operation started on this server: Backup";
    let call = 0;
    const post = vi.spyOn(api, "post").mockImplementation((async (_path: string, body: { files: string[] }) => {
      call += 1;
      if (call === 1) return okResponse(body.files);
      // the backend processed 50 and reports the rest of this request as skipped (instance_busy)
      const [done, rest] = [body.files.slice(0, 50), body.files.slice(50)];
      return {
        ...okResponse(done),
        results: [
          ...okResponse(done).results,
          ...rest.map((filename) => ({ filename, status: "skipped" as const, reason: "instance_busy", message: busy })),
        ],
        skipped: rest.length,
        up_to_date: 0,
        stopped: busy,
      };
    }) as never);
    const run = await updateInBatches(ID, names(250));
    expect(post).toHaveBeenCalledTimes(2); // batch 3 is not sent
    expect(run.error).toBeNull();
    expect(run.response.results).toHaveLength(250);
    expect(run.response.updated).toBe(150);
    expect(run.response.skipped).toBe(100);
    expect(run.response.stopped).toBe(busy);
    const report = reportFromBatchedUpdate(run);
    expect(report.title).toBe("Update finished: 150 updated, 100 skipped");
    expect(report.warnings[0]).toBe(`Stopped part-way, the remaining files were skipped: ${busy}`);
    expect(report.tone).toBe("warning");
  });

  it("sends at most 100 names per request, one batch after another, and merges the results", async () => {
    const post = vi.spyOn(api, "post").mockImplementation((async (_path: string, body: { files: string[] }) => okResponse(body.files)) as never);
    const progress: number[] = [];
    const run = await updateInBatches(ID, names(250), (p) => progress.push(p.batch));
    expect(post).toHaveBeenCalledTimes(3);
    expect(post.mock.calls.map((c) => (c[1] as { files: string[] }).files.length)).toEqual([100, 100, 50]);
    expect(post.mock.calls[0][0]).toBe(`/instances/${ID}/content/update`);
    expect((post.mock.calls[2][1] as { files: string[] }).files[0]).toBe("mod-201.jar");
    expect(progress).toEqual([1, 2, 3]);
    expect(run.error).toBeNull();
    expect(run.response.updated).toBe(250);
    expect(run.response.results).toHaveLength(250);
    expect(run.response.missing_dependencies).toHaveLength(1); // the same missing dependency is listed once
    expect(run.response.restart_required).toBe(true);
  });

  it("stops at a batch that fails as a whole and reports what was not attempted", async () => {
    let call = 0;
    vi.spyOn(api, "post").mockImplementation((async (_path: string, body: { files: string[] }) => {
      call += 1;
      if (call === 2) throw new ApiError(409, "instance_busy", "Another operation is in progress: Backup");
      return okResponse(body.files);
    }) as never);
    const run = await updateInBatches(ID, names(250));
    expect(call).toBe(2); // batch 3 is not sent
    expect(run.failedBatch).toBe(2);
    expect(run.response.updated).toBe(100);
    expect(run.notAttempted).toHaveLength(150);
    expect(run.notAttempted[0]).toBe("mod-101.jar");
    const report = reportFromBatchedUpdate(run);
    expect(report.title).toMatch(/100 updated.*stopped at batch 2 of 3/);
    expect(report.tone).toBe("warning");
    expect(report.lines.filter((l) => l.tone === "error")).toHaveLength(150);
    expect(report.lines.at(-1)?.detail).toMatch(/batch 2 of 3 failed \(Another operation is in progress: Backup\)/);
  });

  it("a failing first batch rejects like the single request did", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(400, "invalid_files", "Update at most 100 files at once"));
    await expect(updateInBatches(ID, names(30))).rejects.toMatchObject({ code: "invalid_files" });
  });
});

describe("multi-file upload in batches", () => {
  const files = (n: number) => names(n).map((name) => new File(["x".repeat(10)], name));

  function wrapper({ children }: { children: ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  }

  beforeEach(() => {
    uploads.fn.mockReset();
  });

  it("uploads 250 files as three requests of at most 100 and shows the batch", async () => {
    const release: (() => void)[] = [];
    uploads.fn.mockImplementation(
      (_path: string, form: FormData) =>
        new Promise((resolve) => release.push(() => resolve({ uploaded: form.getAll("file").map((f) => ({ path: (f as File).name, size: 10 })) }))),
    );
    const { result } = renderHook(() => useFileUploads(ID), { wrapper });
    act(() => result.current.start("mods", files(250), new Set()));
    await waitFor(() => expect(uploads.fn).toHaveBeenCalledTimes(1));
    expect((uploads.fn.mock.calls[0][1] as FormData).getAll("file")).toHaveLength(100);
    expect(uploads.fn.mock.calls[0][2]).toEqual({ path: "mods", overwrite: false });

    const { rerender } = render(<UploadList items={result.current.items} onDismiss={() => {}} />);
    expect(screen.getByText("Sending batch 1 of 3 (files 1–100 of 250)")).toBeInTheDocument();

    await act(async () => release.shift()!());
    await waitFor(() => expect(uploads.fn).toHaveBeenCalledTimes(2));
    rerender(<UploadList items={result.current.items} onDismiss={() => {}} />);
    expect(screen.getByText("Sending batch 2 of 3 (files 101–200 of 250)")).toBeInTheDocument();

    await act(async () => release.shift()!());
    await waitFor(() => expect(uploads.fn).toHaveBeenCalledTimes(3));
    expect((uploads.fn.mock.calls[2][1] as FormData).getAll("file")).toHaveLength(50);
    await act(async () => release.shift()!());
    await waitFor(() => expect(result.current.items[0]?.status).toBe("done"));
  });

  it("reports a failed batch with how many files made it, and sends no further batch", async () => {
    let call = 0;
    uploads.fn.mockImplementation(async (_path: unknown, form: FormData) => {
      call += 1;
      if (call === 2) throw new ApiError(507, "no_space", "Not enough free space on the server volume");
      return stored(form);
    });
    const { result } = renderHook(() => useFileUploads(ID), { wrapper });
    act(() => result.current.start("", files(250), new Set()));
    await waitFor(() => expect(result.current.items[0]?.status).toBe("error"));
    expect(call).toBe(2);
    expect(result.current.items[0].error).toBe(
      "Batch 2 of 3 failed: Not enough free space on the server volume; 100 of 250 files were uploaded, the other 150 were not",
    );
  });

  it("asks about the files not written yet when a later batch reports an existing name", async () => {
    let call = 0;
    uploads.fn.mockImplementation(async () => {
      call += 1;
      if (call === 2) throw new ApiError(409, "exists", "mod-150.jar already exists");
      return { uploaded: [] };
    });
    // (the first batch's answer lists nothing: the count of written files then comes from the batch)
    const { result } = renderHook(() => useFileUploads(ID), { wrapper });
    act(() => result.current.start("mods", files(150), new Set()));
    await waitFor(() => expect(result.current.conflict).not.toBeNull());
    expect(result.current.conflict?.files).toHaveLength(50);
    expect(result.current.conflict?.files[0].name).toBe("mod-101.jar");
  });

  it("a small upload is still one request", async () => {
    uploads.fn.mockResolvedValue({ uploaded: [] });
    const { result } = renderHook(() => useFileUploads(ID), { wrapper });
    act(() => result.current.start("", files(3), new Set()));
    await waitFor(() => expect(result.current.items[0]?.status).toBe("done"));
    expect(uploads.fn).toHaveBeenCalledTimes(1);
    expect(result.current.items[0].batch).toBeUndefined();
  });

  it("lists each file the server could not store and offers to overwrite the ones that exist", async () => {
    // 201 {uploaded, failed}: one file with a bad name, one that exists; the others were written
    uploads.fn.mockImplementation(async (_path: unknown, form: FormData) => {
      const names = form.getAll("file").map((f) => (f as File).name);
      return {
        uploaded: names.filter((n) => n !== "mod-002.jar" && n !== "mod-003.jar").map((n) => ({ path: `mods/${n}`, size: 10 })),
        failed: [
          { name: "mod-002.jar", path: "mods/mod-002.jar", code: "invalid_path", message: "Invalid file name" },
          { name: "mod-003.jar", path: "mods/mod-003.jar", code: "exists", message: "Already exists" },
        ],
      };
    });
    const { result } = renderHook(() => useFileUploads(ID), { wrapper });
    act(() => result.current.start("mods", files(4), new Set()));
    await waitFor(() => expect(result.current.items[0]?.status).toBe("error"));
    const item = result.current.items[0];
    expect(item.error).toBe("2 of 4 files uploaded; 1 could not be stored:");
    expect(item.failures).toEqual([{ name: "mod-002.jar", path: "mods/mod-002.jar", code: "invalid_path", message: "Invalid file name" }]);
    expect(result.current.conflict?.files.map((f) => f.name)).toEqual(["mod-003.jar"]);
    expect(result.current.conflict?.existing).toEqual(["mod-003.jar"]);
  });
});

function stored(form: FormData) {
  return { uploaded: form.getAll("file").map((f) => ({ path: (f as File).name, size: 10 })), failed: [] };
}
