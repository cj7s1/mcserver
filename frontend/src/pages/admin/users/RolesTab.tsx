import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Crown, Info, Loader2, Lock, Minus, Pencil, Plus, Save, Trash2, Undo2 } from "lucide-react";
import { Fragment, useMemo, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Table, TableBody, TableCell, TableFooter, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Me, PermissionDef, Role } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AdminCallout } from "../admin-shared";
import { CreateRoleDialog, EditRoleDialog } from "./RoleDialogs";
import { canGrantPermission, groupPermissions, sameSet, sortRoles } from "./users-model";

export function RolesTab({ roles, defs, me }: { roles: Role[]; defs: PermissionDef[]; me: Me }) {
  const qc = useQueryClient();
  const sorted = useMemo(() => sortRoles(roles), [roles]);
  const groups = useMemo(() => groupPermissions(defs), [defs]);
  const systemCodes = useMemo(() => new Set(defs.filter((d) => d.scope === "system").map((d) => d.code)), [defs]);

  const [drafts, setDrafts] = useState<Record<number, string[]>>({});
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<Role | null>(null);
  const [deleting, setDeleting] = useState<Role | null>(null);

  const permsOf = (r: Role) => drafts[r.id] ?? r.permissions;
  const dirtyRoles = sorted.filter((r) => !r.is_system && drafts[r.id] && !sameSet(drafts[r.id], r.permissions));
  const customCount = sorted.filter((r) => !r.is_system).length;

  function toggle(role: Role, code: string, on: boolean) {
    setDrafts((d) => {
      const cur = new Set(d[role.id] ?? role.permissions);
      if (on) cur.add(code);
      else cur.delete(code);
      return { ...d, [role.id]: [...cur] };
    });
  }

  function discard() {
    setDrafts({});
  }

  const save = useMutation({
    mutationFn: async (targets: Role[]) => {
      const saved: Role[] = [];
      const failed: { role: Role; err: unknown }[] = [];
      for (const r of targets) {
        try {
          saved.push(await api.patch<Role>(`/roles/${r.id}`, { permissions: [...permsOf(r)].sort() }));
        } catch (err) {
          failed.push({ role: r, err });
        }
      }
      return { saved, failed };
    },
    onSuccess: ({ saved, failed }) => {
      if (saved.length) {
        qc.setQueryData<Role[]>(qk.roles, (old) => old?.map((r) => saved.find((s) => s.id === r.id) ?? r));
        setDrafts((d) => {
          const next = { ...d };
          for (const r of saved) delete next[r.id];
          return next;
        });
        toast.success(
          saved.length === 1 ? `Saved permissions for ${saved[0].name}` : `Saved permissions for ${saved.length} roles`,
        );
      }
      for (const f of failed) toast.error(`${f.role.name}: ${errorMessage(f.err)}`);
      qc.invalidateQueries({ queryKey: qk.roles });
      qc.invalidateQueries({ queryKey: qk.users });
      qc.invalidateQueries({ queryKey: qk.auth });
    },
  });

  return (
    <div className="grid grid-cols-1 gap-4">
      <AdminCallout tone="info" icon={<Info />} title="How access works">
        <ul className="grid list-disc gap-1 pl-4">
          <li>
            A user's <strong className="font-medium">global role</strong> applies to every server, including servers
            created later, plus any panel-wide (System) permissions it contains.
          </li>
          <li>
            <strong className="font-medium">Per-server access</strong> is granted on each server's{" "}
            <Link to="/servers" className="underline underline-offset-3">
              Access tab
            </Link>
            . Only the role's server-level permissions apply there, on top of the user's global role.
          </li>
          <li>
            The <strong className="font-medium">Owner</strong> account holds every permission implicitly and can't be
            restricted. Built-in roles are read-only; create a custom role to tailor permissions.
          </li>
        </ul>
      </AdminCallout>

      <Section
        title="Roles"
        description={`${sorted.length - customCount} built-in · ${customCount} custom`}
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus /> New role
          </Button>
        }
      >
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {sorted.map((r) => {
            const hasSystem = r.permissions.some((p) => systemCodes.has(p));
            return (
              <li key={r.id} className="flex flex-col gap-2 rounded-lg border bg-background/40 p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate font-medium">{r.name}</p>
                    <p className="truncate font-mono text-[11px] text-muted-foreground">{r.key}</p>
                  </div>
                  {r.is_system ? (
                    <Badge variant="secondary">
                      <Lock /> Built-in
                    </Badge>
                  ) : (
                    <Badge variant="outline">Custom</Badge>
                  )}
                </div>
                <p className="line-clamp-2 text-xs text-muted-foreground">{r.description || "No description."}</p>
                <div className="mt-auto flex items-center justify-between gap-2 pt-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    <span>{r.permissions.length} permissions</span>
                    <span aria-hidden>·</span>
                    <span title="Users with this as their global role">
                      {r.user_count} user{r.user_count === 1 ? "" : "s"}
                    </span>
                    {hasSystem && (
                      <Badge variant="outline" className="h-4 px-1.5 text-[10px]" title="Includes panel-wide permissions">
                        System
                      </Badge>
                    )}
                  </div>
                  {!r.is_system && (
                    <div className="flex shrink-0 items-center">
                      <Button variant="ghost" size="icon-sm" aria-label={`Edit ${r.name}`} onClick={() => setEditing(r)}>
                        <Pencil />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        className="text-destructive hover:text-destructive"
                        aria-label={`Delete ${r.name}`}
                        title={r.user_count > 0 ? "Reassign this role's users before deleting it" : undefined}
                        disabled={r.user_count > 0}
                        onClick={() => setDeleting(r)}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </Section>

      <Section
        title="Permission matrix"
        description={
          customCount > 0
            ? "Toggle cells in custom role columns, then save. Built-in roles are shown for reference."
            : "Built-in roles are read-only. Create a custom role to edit permissions here."
        }
      >
        <Table className="text-[13px]">
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="sticky left-0 z-10 min-w-36 bg-card text-xs text-muted-foreground sm:min-w-64">
                Permission
              </TableHead>
              <TableHead className="h-auto min-w-20 py-2 text-center">
                <div className="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
                  <Crown className="size-3.5" aria-hidden /> Owner
                </div>
                <div className="text-[10px] font-normal text-muted-foreground">implicit</div>
              </TableHead>
              {sorted.map((r) => (
                <TableHead key={r.id} className="h-auto min-w-24 py-2 text-center">
                  <div className="mx-auto max-w-32 truncate" title={r.name}>
                    {r.name}
                  </div>
                  <div className="text-[10px] font-normal text-muted-foreground">
                    {r.is_system ? "built-in" : dirtyRoles.includes(r) ? "unsaved" : "custom"}
                  </div>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {groups.map((g) => (
              <Fragment key={g.group}>
                <TableRow className="bg-muted/40 hover:bg-muted/40">
                  <TableCell colSpan={sorted.length + 2} className="py-1.5">
                    <span className="sticky left-2 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                      {g.group}
                      <span className="ml-2 font-normal tracking-normal normal-case">
                        {g.scope === "system" ? "panel-wide · global role only" : "server-level"}
                      </span>
                    </span>
                  </TableCell>
                </TableRow>
                {g.items.map((p) => (
                  <TableRow key={p.code}>
                    <TableHead
                      scope="row"
                      className="sticky left-0 z-10 h-auto bg-card py-1.5 font-normal whitespace-normal"
                      title={p.code}
                    >
                      <div className="font-medium">{p.label}</div>
                      <div className="hidden text-xs text-muted-foreground md:block">{p.description}</div>
                    </TableHead>
                    <TableCell className="text-center">
                      <Check className="mx-auto size-4 text-amber-600/60 dark:text-amber-300/60" aria-hidden />
                      <span className="sr-only">Granted implicitly</span>
                    </TableCell>
                    {sorted.map((r) => {
                      const checked = permsOf(r).includes(p.code);
                      if (r.is_system) {
                        return (
                          <TableCell key={r.id} className="text-center">
                            {checked ? (
                              <Check className="mx-auto size-4 text-primary" aria-hidden />
                            ) : (
                              <Minus className="mx-auto size-3.5 text-muted-foreground/40" aria-hidden />
                            )}
                            <span className="sr-only">{checked ? "Granted" : "Not granted"}</span>
                          </TableCell>
                        );
                      }
                      const changed = checked !== r.permissions.includes(p.code);
                      const allowed = canGrantPermission(me, p.code);
                      return (
                        <TableCell key={r.id} className={cn("text-center", changed && "bg-primary/10")}>
                          <Checkbox
                            className="mx-auto"
                            checked={checked}
                            disabled={!allowed || save.isPending}
                            title={allowed ? undefined : "You can't grant a permission you don't hold"}
                            aria-label={`${p.label} for ${r.name}`}
                            onCheckedChange={(c) => toggle(r, p.code, c === true)}
                          />
                        </TableCell>
                      );
                    })}
                  </TableRow>
                ))}
              </Fragment>
            ))}
          </TableBody>
          <TableFooter className="bg-transparent">
            <TableRow className="hover:bg-transparent">
              <TableHead scope="row" className="sticky left-0 z-10 bg-card text-xs text-muted-foreground">
                Total
              </TableHead>
              <TableCell className="text-center text-xs tabular-nums">{defs.length}</TableCell>
              {sorted.map((r) => (
                <TableCell key={r.id} className="text-center text-xs tabular-nums">
                  {permsOf(r).length}/{defs.length}
                </TableCell>
              ))}
            </TableRow>
          </TableFooter>
        </Table>

        {dirtyRoles.length > 0 && (
          <div
            className="sticky bottom-4 z-20 mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-popover p-2.5 shadow-lg"
            role="status"
          >
            <p className="text-sm">
              Unsaved changes to{" "}
              <span className="font-medium">{dirtyRoles.map((r) => r.name).join(", ")}</span>
            </p>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={discard} disabled={save.isPending}>
                <Undo2 /> Discard
              </Button>
              <Button size="sm" onClick={() => save.mutate(dirtyRoles)} disabled={save.isPending}>
                {save.isPending ? <Loader2 className="animate-spin" /> : <Save />}
                Save changes
              </Button>
            </div>
          </div>
        )}
      </Section>

      <CreateRoleDialog open={createOpen} onOpenChange={setCreateOpen} roles={roles} me={me} />
      {editing && (
        <EditRoleDialog key={editing.id} role={editing} open onOpenChange={(o) => !o && setEditing(null)} />
      )}
      {deleting && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDeleting(null)}
          title={`Delete role ${deleting.name}?`}
          description="The role is removed permanently. Roles still used as a global role or in a server's access grants can't be deleted."
          confirmText={deleting.key}
          confirmLabel="Delete role"
          destructive
          onConfirm={async () => {
            try {
              await api.delete(`/roles/${deleting.id}`);
              toast.success(`Deleted role ${deleting.name}`);
              setDrafts((d) => {
                const next = { ...d };
                delete next[deleting.id];
                return next;
              });
              qc.invalidateQueries({ queryKey: qk.roles });
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        />
      )}
    </div>
  );
}
