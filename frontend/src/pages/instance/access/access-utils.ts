import type { PermissionCode, Role } from "@/lib/types";

/** The instance-scoped permissions of a role (system permissions do not apply per server). */
export function instancePerms(role: Pick<Role, "permissions">, instanceCodes: Set<string>): PermissionCode[] {
  return role.permissions.filter((p) => instanceCodes.has(p));
}
