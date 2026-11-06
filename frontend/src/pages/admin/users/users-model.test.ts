import { describe, expect, it } from "vitest";
import type { Me, PermissionCode, Role } from "@/lib/types";
import { canAssignRole, canGrantPermission, grantBlockedReason, roleEditBlockedReason } from "./users-model";

function me(permissions: PermissionCode[], is_owner = false): Me {
  return {
    id: 2,
    username: "manager",
    email: "",
    first_name: "",
    last_name: "",
    is_owner,
    role_name: "Custom",
    permissions,
    must_change_password: false,
    preferences: {},
  };
}

function role(key: string, permissions: PermissionCode[]): Role {
  return { id: key.length, key, name: key, description: "", permissions, is_system: false, user_count: 0 };
}

describe("owner-granted permissions (backend OWNER_GRANTED_CODES)", () => {
  const userManager = me(["system.users", "system.settings", "instance.view", "instance.control"]);

  it("only the owner grants user and settings administration", () => {
    expect(canGrantPermission(userManager, "system.users")).toBe(false);
    expect(canGrantPermission(userManager, "system.settings")).toBe(false);
    expect(grantBlockedReason(userManager, "system.users")).toMatch(/Only the owner/);
    expect(canGrantPermission(userManager, "instance.control")).toBe(true);
    expect(grantBlockedReason(userManager, "instance.delete")).toMatch(/don't hold/);
    expect(canGrantPermission(me([], true), "system.users")).toBe(true);
  });

  it("only the owner assigns or edits roles that include them", () => {
    const admins = role("helpers", ["system.users", "instance.view"]);
    const operators = role("ops", ["instance.view", "instance.control"]);
    expect(canAssignRole(userManager, admins)).toBe(false);
    expect(canAssignRole(userManager, operators)).toBe(true);
    expect(roleEditBlockedReason(userManager, admins)).toMatch(/Only the owner/);
    expect(roleEditBlockedReason(userManager, operators)).toBeNull();
    expect(canAssignRole(me([], true), admins)).toBe(true);
    expect(roleEditBlockedReason(me([], true), admins)).toBeNull();
  });
});
