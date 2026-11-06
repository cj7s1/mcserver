import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { applyLiveEvent, isInstanceListKey, isJobListKey } from "./events";

const ID = "11111111-1111-1111-1111-111111111111";

function client() {
  const qc = new QueryClient();
  qc.setQueryData(["instances", { archived: false }], []);
  qc.setQueryData(["instances", ID], { id: ID, active_job: { id: "j1", progress: 0.1 } });
  qc.setQueryData(["instances", ID, "files", ""], { entries: [] });
  qc.setQueryData(["instances", ID, "metrics", "1h"], { samples: [] });
  qc.setQueryData(["jobs", { limit: 20 }], []);
  qc.setQueryData(["jobs", "j1"], { id: "j1", progress: 0.1 });
  return qc;
}
const stale = (qc: QueryClient) =>
  qc.getQueryCache().getAll().filter((q) => q.state.isInvalidated).map((q) => JSON.stringify(q.queryKey));

describe("live events", () => {
  it("classifies list keys", () => {
    expect(isInstanceListKey(["instances", { archived: false }])).toBe(true);
    expect(isInstanceListKey(["instances", ID])).toBe(false);
    expect(isJobListKey(["jobs"])).toBe(true);
    expect(isJobListKey(["jobs", "j1"])).toBe(false);
  });

  it("progress ticks update caches without invalidating anything", () => {
    const qc = client();
    applyLiveEvent(qc, { type: "job.progress", instance_id: ID, payload: { id: "j1", progress: 0.5 } });
    expect(stale(qc)).toEqual([]);
    expect((qc.getQueryData(["instances", ID]) as any).active_job.progress).toBe(0.5);
    expect((qc.getQueryData(["jobs", "j1"]) as any).progress).toBe(0.5);
  });

  it("status changes refresh only the server summary and lists", () => {
    const qc = client();
    applyLiveEvent(qc, { type: "instance.status", instance_id: ID, payload: {} });
    expect(stale(qc).sort()).toEqual([JSON.stringify(["instances", ID]), JSON.stringify(["instances", { archived: false }])].sort());
  });

  it("a finished job refreshes the server's queries", () => {
    const qc = client();
    applyLiveEvent(qc, { type: "job.finished", instance_id: ID, payload: { id: "j1" } });
    const s = stale(qc);
    expect(s).toContain(JSON.stringify(["instances", ID, "files", ""]));
    expect(s).toContain(JSON.stringify(["jobs", { limit: 20 }]));
    expect(s).not.toContain(JSON.stringify(["jobs", "j1"]));
  });
});
