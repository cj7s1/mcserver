/** Per-server access grants: which panel users get which role on this server. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Crown, Info, Loader2, Lock, ShieldCheck, UserMinus, UserPlus, Users } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useAuth } from "@/hooks/use-auth";
import { api, ApiError, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { InstanceAccessGrant, PanelUser, PermissionDef, Role } from "@/lib/types";
import { useInstanceContext } from "./InstanceLayout";
import { PermissionChips, RoleReference } from "./access/access-ui";
import { instancePerms } from "./access/access-utils";

export default function Access() {
  const { instance, can } = useInstanceContext();
  const auth = useAuth();
  const qc = useQueryClient();
  const canManage = can("instance.access");
  const isUserAdmin = auth.can("system.users");

  const grants = useQuery({
    queryKey: qk.access(instance.id),
    queryFn: () => api.get<InstanceAccessGrant[]>(`/instances/${instance.id}/access`),
    enabled: canManage,
  });
  const roles = useQuery({ queryKey: qk.roles, queryFn: () => api.get<Role[]>("/roles"), enabled: canManage, staleTime: 60_000 });
  const permDefs = useQuery({
    queryKey: ["permissions"],
    queryFn: () => api.get<{ permissions: PermissionDef[] }>("/permissions"),
    enabled: canManage,
    staleTime: 10 * 60_000,
  });
  const users = useQuery({
    queryKey: qk.users,
    queryFn: () => api.get<PanelUser[]>("/users"),
    enabled: canManage && isUserAdmin,
    retry: (n, err) => !(err instanceof ApiError && err.status === 403) && n < 2,
  });

  const [newUser, setNewUser] = useState("");
  const [newRole, setNewRole] = useState("");
  const [revoke, setRevoke] = useState<InstanceAccessGrant | null>(null);

  const put = useMutation({
    mutationFn: (body: { user: number; role: number }) => api.put<InstanceAccessGrant>(`/instances/${instance.id}/access`, body),
    onSuccess: (g, vars) => {
      const existed = grants.data?.some((x) => x.user === vars.user);
      toast.success(existed ? `${g.username} is now ${g.role_name}` : `Gave ${g.username} the ${g.role_name} role`);
      qc.invalidateQueries({ queryKey: qk.access(instance.id) });
      if (!existed) {
        setNewUser("");
        setNewRole("");
      }
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const del = useMutation({
    mutationFn: (g: InstanceAccessGrant) => api.delete(`/instances/${instance.id}/access`, undefined, { user: g.user }),
    onSuccess: (_d, g) => {
      toast.success(`Removed ${g.username}'s access`);
      qc.invalidateQueries({ queryKey: qk.access(instance.id) });
      if (g.user === auth.user?.id) qc.invalidateQueries({ queryKey: qk.instance(instance.id) });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  if (!canManage) {
    return (
      <EmptyState
        icon={<Lock className="size-5" />}
        title="No access to this page"
        description="You need the “Manage access” permission on this server to see and change who can use it."
      />
    );
  }
  if (grants.isLoading || roles.isLoading || permDefs.isLoading) return <LoadingState label="Loading access…" />;
  const firstError = grants.error ?? roles.error ?? permDefs.error;
  if (firstError)
    return (
      <ErrorState
        error={firstError}
        title="Could not load access settings"
        onRetry={() => {
          grants.refetch();
          roles.refetch();
          permDefs.refetch();
        }}
      />
    );

  const defs = new Map((permDefs.data?.permissions ?? []).map((d) => [d.code as string, d]));
  const instanceCodes = new Set((permDefs.data?.permissions ?? []).filter((d) => d.scope === "instance").map((d) => d.code as string));
  const mine = new Set<string>(instance.permissions);
  const allRoles = roles.data ?? [];
  const roleById = new Map(allRoles.map((r) => [r.id, r]));
  const serverRoles = allRoles.filter((r) => instancePerms(r, instanceCodes).length > 0);

  /** Why the current user cannot hand out this role here (mirrors AccessListView.put), or null. */
  const grantBlock = (r: Role): string | null => {
    const perms = instancePerms(r, instanceCodes);
    if (perms.includes("instance.access") && !isUserAdmin) return "Only user administrators can grant access-management rights.";
    const missing = perms.filter((p) => !mine.has(p));
    if (missing.length) return `You don't have: ${missing.map((m) => defs.get(m)?.label ?? m).join(", ")}.`;
    return null;
  };
  const reasons = new Map(serverRoles.map((r) => [r.id, grantBlock(r)]));
  /** Grants carrying permissions you lack cannot be changed or revoked by you (backend _guard_existing_grant). */
  const grantLocked = (g: InstanceAccessGrant): boolean => {
    const r = roleById.get(g.role);
    return !!r && instancePerms(r, instanceCodes).some((p) => !mine.has(p));
  };

  const list = grants.data ?? [];
  const granted = new Set(list.map((g) => g.user));
  const userById = new Map((users.data ?? []).map((u) => [u.id, u]));
  const candidates = (users.data ?? []).filter((u) => u.is_active && !u.is_owner && !granted.has(u.id) && u.id !== auth.user?.id);
  const globalAccess = (users.data ?? []).filter(
    (u) => u.is_active && (u.is_owner || u.permissions.some((p) => instanceCodes.has(p))),
  );
  const usersForbidden = users.error instanceof ApiError && users.error.status === 403;
  const selectedRole = newRole ? roleById.get(Number(newRole)) : undefined;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!newUser || !newRole) return;
    put.mutate({ user: Number(newUser), role: Number(newRole) });
  };

  return (
    <div className="grid min-w-0 gap-4">
      <Section
        title="People with access"
        description={`Roles granted here apply only to “${instance.name}” and add to whatever the user's global role allows.`}
        className="min-w-0"
      >
        <div className="grid gap-4">
          {isUserAdmin && users.data ? (
            <form onSubmit={submit} className="grid gap-3 rounded-lg border bg-muted/30 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
              <div className="grid gap-1.5">
                <Label htmlFor="access-user">User</Label>
                <Select value={newUser} onValueChange={setNewUser} disabled={candidates.length === 0 || put.isPending}>
                  <SelectTrigger id="access-user" className="w-full">
                    <SelectValue placeholder={candidates.length ? "Choose a user" : "No other users to add"} />
                  </SelectTrigger>
                  <SelectContent>
                    {candidates.map((u) => (
                      <SelectItem key={u.id} value={String(u.id)}>
                        {u.username}
                        {u.role_name ? ` · ${u.role_name}` : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="access-role">Role on this server</Label>
                <Select value={newRole} onValueChange={setNewRole} disabled={put.isPending}>
                  <SelectTrigger id="access-role" className="w-full">
                    <SelectValue placeholder="Choose a role" />
                  </SelectTrigger>
                  <SelectContent>
                    {serverRoles.map((r) => (
                      <SelectItem key={r.id} value={String(r.id)} disabled={!!reasons.get(r.id)}>
                        {r.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <Button type="submit" disabled={!newUser || !newRole || put.isPending}>
                {put.isPending && !granted.has(Number(newUser)) ? <Loader2 className="animate-spin" /> : <UserPlus />} Grant access
              </Button>
              {candidates.length === 0 && (
                <p className="text-xs text-muted-foreground sm:col-span-3">
                  Every active user already has access, or there are no other users yet.{" "}
                  <Link to="/admin/users" className="font-medium text-primary hover:underline">
                    Create users in Users &amp; roles
                  </Link>
                  .
                </p>
              )}
              {selectedRole && (
                <div className="grid gap-1 sm:col-span-3">
                  <p className="text-xs text-muted-foreground">{selectedRole.name} can:</p>
                  <PermissionChips codes={instancePerms(selectedRole, instanceCodes)} defs={defs} />
                </div>
              )}
            </form>
          ) : (
            <p className="flex items-start gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
              <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {users.isLoading
                ? "Loading users…"
                : usersForbidden || !isUserAdmin
                  ? "Only user administrators can list panel users, so only they can add people here. You can still change the role of, or remove, the people below."
                  : `Could not load users: ${errorMessage(users.error)}`}
            </p>
          )}

          {list.length === 0 ? (
            <EmptyState
              icon={<Users className="size-5" />}
              title="No per-server grants"
              description="Only the owner and users whose global role covers servers can use this server."
            />
          ) : (
            <Table className="text-xs">
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>User</TableHead>
                  <TableHead className="w-48">Role here</TableHead>
                  <TableHead className="min-w-64">Permissions on this server</TableHead>
                  <TableHead>Granted</TableHead>
                  <TableHead>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {list.map((g) => {
                  const role = roleById.get(g.role);
                  const user = userById.get(g.user);
                  const locked = grantLocked(g);
                  const rolePerms = role ? instancePerms(role, instanceCodes) : [];
                  const viaGlobal = user ? user.permissions.filter((p) => instanceCodes.has(p) && !rolePerms.includes(p)) : [];
                  const self = g.user === auth.user?.id;
                  return (
                    <TableRow key={g.id}>
                      <TableCell className="whitespace-nowrap">
                        <div className="text-sm font-medium">
                          {g.username}
                          {self && <span className="ml-1.5 text-xs font-normal text-muted-foreground">(you)</span>}
                        </div>
                        {user && (
                          <div className="text-muted-foreground">
                            Global role: {user.is_owner ? "Owner" : (user.role_name ?? "none")}
                            {!user.is_active && " · disabled"}
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        {locked ? (
                          <span className="inline-flex items-center gap-1" title="This grant includes permissions you don't have, so you can't change it.">
                            <Lock className="size-3 text-muted-foreground" aria-hidden />
                            {g.role_name}
                          </span>
                        ) : (
                          <>
                            <Label htmlFor={`grant-role-${g.id}`} className="sr-only">
                              Role for {g.username}
                            </Label>
                            <Select
                              value={String(g.role)}
                              onValueChange={(v) => put.mutate({ user: g.user, role: Number(v) })}
                              disabled={put.isPending}
                            >
                              <SelectTrigger id={`grant-role-${g.id}`} size="sm" className="w-full">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {!serverRoles.some((r) => r.id === g.role) && <SelectItem value={String(g.role)}>{g.role_name}</SelectItem>}
                                {serverRoles.map((r) => (
                                  <SelectItem key={r.id} value={String(r.id)} disabled={r.id !== g.role && !!reasons.get(r.id)}>
                                    {r.name}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          </>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-normal">
                        <PermissionChips
                          codes={rolePerms}
                          defs={defs}
                          limit={4}
                          extra={
                            viaGlobal.length > 0 && (
                              <span className="inline-flex h-5 items-center text-[11px] text-muted-foreground" title={viaGlobal.map((p) => defs.get(p)?.label ?? p).join(", ")}>
                                +{viaGlobal.length} via global role
                              </span>
                            )
                          }
                        />
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-muted-foreground" title={formatDateTime(g.created_at)}>
                        {relativeTime(g.created_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        {!locked && (
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`Remove access for ${g.username}`}
                            disabled={del.isPending}
                            onClick={() => setRevoke(g)}
                          >
                            <UserMinus />
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </div>
      </Section>

      {users.data && globalAccess.length > 0 && (
        <Section
          title="Access through global roles"
          description="These users can use every server because of their panel-wide role. Change that in user management."
          actions={
            <Button variant="outline" size="sm" asChild>
              <Link to="/admin/users">Manage users</Link>
            </Button>
          }
        >
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {globalAccess.map((u) => {
              const codes = u.is_owner ? [...instanceCodes] : u.permissions.filter((p) => instanceCodes.has(p));
              return (
                <li key={u.id} className="flex items-center gap-2.5 rounded-lg border px-3 py-2">
                  {u.is_owner ? (
                    <Crown className="size-4 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
                  ) : (
                    <ShieldCheck className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                  )}
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">
                      {u.username}
                      {u.id === auth.user?.id && <span className="ml-1.5 text-xs font-normal text-muted-foreground">(you)</span>}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {u.is_owner ? "Owner" : u.role_name} · {codes.length}/{instanceCodes.size} server permissions
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        </Section>
      )}

      <Section title="Roles" description="What each role allows on a server. Roles are defined in user management.">
        {serverRoles.length ? (
          <RoleReference roles={serverRoles} defs={defs} instanceCodes={instanceCodes} reasons={reasons} />
        ) : (
          <p className="text-sm text-muted-foreground">No roles with server permissions exist yet.</p>
        )}
      </Section>

      {revoke && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setRevoke(null)}
          title={`Remove ${revoke.username}'s access?`}
          destructive
          confirmLabel="Remove access"
          description={
            <div className="grid gap-2">
              <p>
                {revoke.username} loses the {revoke.role_name} role on “{instance.name}”. Permissions from their global role, if any,
                still apply.
              </p>
              {revoke.user === auth.user?.id && (
                <p className="font-medium text-destructive">This is your own access. You may lose access to this page.</p>
              )}
            </div>
          }
          onConfirm={async () => {
            try {
              await del.mutateAsync(revoke);
            } catch {
              /* reported by onError */
            }
          }}
        />
      )}
    </div>
  );
}
