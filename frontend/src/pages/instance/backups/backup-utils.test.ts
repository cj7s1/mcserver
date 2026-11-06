import { describe, expect, it } from "vitest";
import { backupReadable, onOtherNode, otherNodeHint } from "./backup-utils";

describe("backups stored on another node", () => {
  it("only flags backups the API reports as not on the current node", () => {
    expect(onOtherNode({ on_current_node: false })).toBe(true);
    expect(onOtherNode({ on_current_node: true })).toBe(false);
    expect(onOtherNode({})).toBe(false); // older API without the field: treat as local
  });

  it("explains where the backup is and what still works", () => {
    expect(otherNodeHint({ node_id: 3, node_name: "node-b" })).toMatch(/node “node-b”.*restored as a new server.*not restored over/);
    expect(otherNodeHint({ node_id: null, node_name: "" })).toMatch(/a node that no longer exists/);
    expect(otherNodeHint({ node_id: null, node_name: "retired" })).toMatch(/node “retired”, which no longer exists/);
    expect(backupReadable({ on_current_node: false, node_id: null })).toBe(false);
    expect(backupReadable({ on_current_node: false, node_id: 3 })).toBe(true);
    expect(backupReadable({ on_current_node: true, node_id: 1 })).toBe(true);
  });
});
