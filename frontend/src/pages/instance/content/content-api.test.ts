import { describe, expect, it } from "vitest";
import { safeHttpUrl, testedSummary } from "./content-api";

describe("plugin source helpers", () => {
  it("shortens the author's tested Minecraft versions", () => {
    expect(testedSummary({ tested_versions: [] })).toBe("no versions listed");
    expect(testedSummary({ tested_versions: ["1.20", "1.21"] })).toBe("1.20, 1.21");
    expect(testedSummary({ tested_versions: ["1.8", "1.12", "1.16", "1.20", "1.21", "26.1"] })).toBe("1.8 … 1.20, 1.21, 26.1");
  });

  it("only turns http(s) provider links into links", () => {
    expect(safeHttpUrl("https://www.spigotmc.org/resources/81534/")).toBe("https://www.spigotmc.org/resources/81534/");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,<b>x</b>")).toBeNull();
    expect(safeHttpUrl("resources/chunky.81534/download")).toBeNull();
  });
});
