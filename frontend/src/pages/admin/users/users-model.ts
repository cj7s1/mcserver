/** Pure helpers for the Users & roles page (mirrors the guards in backend/apps/accounts/views.py). */
import type { Me, PanelUser, PermissionDef, Role } from "@/lib/types";

const BUILTIN_ORDER = ["administrator", "server_manager", "operator", "viewer"];

/** Built-in roles first (most to least privileged), then custom roles by name. */
export function sortRoles(roles: readonly Role[]): Role[] {
  return [...roles].sort((a, b) => {
    if (a.is_system !== b.is_system) return a.is_system ? -1 : 1;
    if (a.is_system) {
      const ia = BUILTIN_ORDER.indexOf(a.key);
      const ib = BUILTIN_ORDER.indexOf(b.key);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    }
    return a.name.localeCompare(b.name);
  });
}

/**
 * Permissions only the owner may hand out, in a role or by assigning a role that has them
 * (backend apps/accounts/permissions.py OWNER_GRANTED_CODES): otherwise a user manager could mint
 * more user managers or Administrator-equivalent accounts.
 */
export const OWNER_GRANTED_CODES: ReadonlySet<string> = new Set(["system.users", "system.settings"]);

/** Only the owner may hand out Administrator (or any role with owner-granted permissions); others can only assign roles within their own permissions. */
export function canAssignRole(me: Me, role: Role): boolean {
  if (me.is_owner) return true;
  if (role.key === "administrator") return false;
  if (role.permissions.some((p) => OWNER_GRANTED_CODES.has(p))) return false;
  return role.permissions.every((p) => me.permissions.includes(p));
}

/** Client-side hint of the backend's `_guard_owner` (per-server grants are still checked server side). */
export function canManageUser(me: Me, target: PanelUser, roles: readonly Role[]): boolean {
  if (target.is_owner) return me.is_owner;
  if (me.is_owner) return true;
  const rolePerms = roles.find((r) => r.id === target.role)?.permissions ?? [];
  return rolePerms.every((p) => me.permissions.includes(p));
}

/** Why `me` cannot put `code` into a role (null = allowed). Mirrors backend `_guard_permission_grant`. */
export function grantBlockedReason(me: Me, code: string): string | null {
  if (me.is_owner) return null;
  if (OWNER_GRANTED_CODES.has(code)) return "Only the owner can grant this permission";
  if (!me.permissions.includes(code as Me["permissions"][number])) return "You can't grant a permission you don't hold";
  return null;
}

export function canGrantPermission(me: Me, code: string): boolean {
  return grantBlockedReason(me, code) === null;
}

/**
 * Why `me` cannot change a custom role's permissions (null = allowed): saving re-checks every
 * permission the role keeps, so a role holding an owner-granted permission or one `me` lacks
 * is read-only for them (backend `_guard_role_change` + `_guard_permission_grant`).
 */
export function roleEditBlockedReason(me: Me, role: Role): string | null {
  if (me.is_owner) return null;
  if (role.permissions.some((p) => OWNER_GRANTED_CODES.has(p)))
    return "Only the owner can change roles that include user or settings administration";
  if (!role.permissions.every((p) => me.permissions.includes(p))) return "This role has permissions you don't hold";
  return null;
}

export interface PermissionGroup {
  group: string;
  scope: PermissionDef["scope"];
  items: PermissionDef[];
}

/** Group permission definitions, preserving the backend's order. */
export function groupPermissions(defs: readonly PermissionDef[]): PermissionGroup[] {
  const out: PermissionGroup[] = [];
  for (const d of defs) {
    let g = out.find((x) => x.group === d.group);
    if (!g) {
      g = { group: d.group, scope: d.scope, items: [] };
      out.push(g);
    }
    g.items.push(d);
  }
  return out;
}

export function sameSet(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false;
  const s = new Set(a);
  return b.every((x) => s.has(x));
}

/** Django's UnicodeUsernameValidator: letters, digits and @/./+/-/_ only. */
export const USERNAME_RE = /^[\p{L}\p{N}_.@+-]+$/u;
/** Django SlugField. */
export const ROLE_KEY_RE = /^[-a-zA-Z0-9_]+$/;

export function slugify(s: string): string {
  return s
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

/** Random password from the Web Crypto API (no ambiguous characters). */
export function generatePassword(length = 20): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_.!@#%+";
  const out: string[] = [];
  const buf = new Uint32Array(1);
  const limit = Math.floor(0x1_0000_0000 / alphabet.length) * alphabet.length;
  while (out.length < length) {
    crypto.getRandomValues(buf);
    if (buf[0] < limit) out.push(alphabet[buf[0] % alphabet.length]);
  }
  return out.join("");
}

export function fullName(u: { first_name: string; last_name: string }): string {
  return [u.first_name, u.last_name].filter(Boolean).join(" ");
}
