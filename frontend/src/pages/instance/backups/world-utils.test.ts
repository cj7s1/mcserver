import { describe, expect, it } from "vitest";
import type { WorldFolder } from "@/lib/types";
import { dimensionsSummary, isArchiveName, isWorldJob, replacedFolders } from "./world-utils";

const folder = (role: WorldFolder["role"], path: string, exists: boolean, extra: Partial<WorldFolder> = {}): WorldFolder => ({
  role,
  path,
  exists,
  type: exists ? "directory" : null,
  has_level_dat: exists,
  bytes: 0,
  files: 0,
  modified: null,
  ...extra,
});

describe("world helpers", () => {
  it("accepts the archive types the backend extracts", () => {
    for (const name of ["world.zip", "World.ZIP", "w.tar", "w.tar.gz", "w.tgz", "w.tar.xz", "w.txz", " uploads/w.zip "]) {
      expect(isArchiveName(name), name).toBe(true);
    }
    for (const name of ["world.rar", "world.7z", "level.dat", ".zip", "", "zip"]) {
      expect(isArchiveName(name), name).toBe(false);
    }
  });

  it("recognises world jobs", () => {
    expect(isWorldJob("world_replace")).toBe(true);
    expect(isWorldJob("restore")).toBe(false);
    expect(isWorldJob(undefined)).toBe(false);
  });

  it("summarises where the dimensions live", () => {
    expect(dimensionsSummary({ layout: "vanilla", folders: [folder("overworld", "world", true, { dimensions: ["nether"] })] })).toBe(
      "Nether inside the world folder",
    );
    expect(dimensionsSummary({ layout: "vanilla", folders: [folder("overworld", "world", true)] })).toMatch(/inside the world/);
    const bukkit = [folder("overworld", "world", true), folder("nether", "world_nether", true), folder("end", "world_the_end", false)];
    expect(dimensionsSummary({ layout: "bukkit", folders: bukkit })).toBe("Nether in separate folders (Paper/Bukkit layout)");
    expect(replacedFolders({ folders: bukkit })).toEqual(["world", "world_nether"]);
  });
});
