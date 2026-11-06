import { describe, expect, it } from "vitest";
import { buildPayload, type Derived, initialState, isModded, validateStep } from "../wizardModel";
import {
  archiveSuffix,
  importPermissionProblem,
  nameFromArchive,
  readyForImport,
  stageStates,
  validateImportArchive,
} from "./importModel";

describe("archive validation", () => {
  it("accepts the suffixes the backend accepts, case-insensitively", () => {
    for (const name of ["a.zip", "a.tar", "a.tar.gz", "a.tgz", "a.tar.xz", "a.txz", "Old Server.ZIP"]) {
      expect(validateImportArchive({ name, size: 10 })).toBeNull();
    }
    expect(archiveSuffix("world.tar.gz")).toBe(".tar.gz");
    expect(archiveSuffix("world.gz")).toBeNull();
  });

  it("rejects missing, unsupported, bare-suffix and empty files", () => {
    expect(validateImportArchive(null)).toMatch(/Choose an archive/);
    expect(validateImportArchive({ name: "world.rar", size: 10 })).toMatch(/not a supported archive/);
    expect(validateImportArchive({ name: "world.7z", size: 10 })).toMatch(/not a supported archive/);
    expect(validateImportArchive({ name: ".zip", size: 10 })).toMatch(/not a supported archive/);
    expect(validateImportArchive({ name: "world.zip", size: 0 })).toMatch(/empty/);
  });

  it("suggests a server name from the archive name", () => {
    expect(nameFromArchive("My_Survival__World.tar.gz")).toBe("My Survival World");
    expect(nameFromArchive("old-server.zip")).toBe("old-server");
    expect(nameFromArchive("x".repeat(100) + ".zip")).toHaveLength(80);
  });
});

describe("permissions and readiness", () => {
  it("flags roles that see all servers but cannot write files", () => {
    expect(importPermissionProblem({ is_owner: true, permissions: [] })).toBeNull();
    expect(importPermissionProblem({ is_owner: false, permissions: ["instance.create"] })).toBeNull();
    expect(importPermissionProblem({ is_owner: false, permissions: ["instance.create", "instance.view"] })).toMatch(/Write files/);
    expect(
      importPermissionProblem({ is_owner: false, permissions: ["instance.create", "instance.view", "instance.files.write"] }),
    ).toBeNull();
  });

  it("waits while a job holds the server or it is installing", () => {
    const job = { id: "j", kind: "install", title: "Install", progress: 0.5, message: "", status: "running" as const };
    expect(readyForImport({ status: "created", active_job: null })).toBe(true);
    expect(readyForImport({ status: "stopped", active_job: null })).toBe(true);
    expect(readyForImport({ status: "installing", active_job: null })).toBe(false);
    expect(readyForImport({ status: "created", active_job: job })).toBe(false);
  });

  it("maps phases to stage states", () => {
    expect(stageStates("preparing", null)).toEqual({ create: "done", prepare: "active", upload: "pending", extract: "pending" });
    expect(stageStates("uploading", null)).toEqual({ create: "done", prepare: "done", upload: "active", extract: "pending" });
    expect(stageStates("failed", "upload")).toEqual({ create: "done", prepare: "done", upload: "failed", extract: "pending" });
    expect(stageStates("failed", "prepare")).toMatchObject({ prepare: "failed", upload: "pending" });
    expect(stageStates("done", null)).toMatchObject({ upload: "done", extract: "active" });
  });
});

describe("wizard model in import mode", () => {
  const d = {
    isModpack: false,
    serverType: "FABRIC",
    minecraftVersion: "1.21.1",
    loaderVersion: "",
    name: "Imported",
    memoryMb: 4096,
    gamePort: "25570",
    resolveOk: false,
    resolvePending: false,
  } as unknown as Derived;
  const base = { ...initialState(), category: "import" as const, acceptEula: true };

  it("requires an archive on the software step and allows the type step", () => {
    expect(validateStep("type", base, d)).toEqual({});
    expect(validateStep("software", base, d)).toHaveProperty("importFile");
    const file = new File(["x"], "server.zip");
    expect(validateStep("software", { ...base, importFile: file }, d)).toEqual({});
  });

  it("always creates the server stopped, as a standard server, on the chosen node", () => {
    const payload = buildPayload({ ...base, start: true, node: 2 }, d);
    expect(payload).toMatchObject({ mode: "standard", server_type: "FABRIC", minecraft_version: "1.21.1", start: false, node: 2 });
    expect(buildPayload({ ...initialState(), category: "vanilla" }, d)).not.toHaveProperty("node");
  });

  it("uses the modded memory overhead for mod loaders", () => {
    expect(isModded(base, d)).toBe(true);
    expect(isModded(base, { ...d, serverType: "PAPER" } as Derived)).toBe(false);
  });
});
