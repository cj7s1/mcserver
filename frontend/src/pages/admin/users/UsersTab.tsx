import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Crown,
  KeyRound,
  Link2,
  MoreHorizontal,
  Pencil,
  Search,
  ShieldOff,
  Trash2,
  UserCheck,
  UserPlus,
  UserX,
  Send,
} from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { EmptyState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { api, errorMessage } from "@/lib/api";
import { formatDateTime, relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { Me, PanelUser, Role } from "@/lib/types";
import { cn } from "@/lib/utils";
import { InviteFriendDialog } from "./InviteFriendDialog";
import { CreateUserDialog, EditUserDialog, ResetLinkDialog } from "./UserDialogs";
import { canManageUser, DISCORD_BOT_USER, fullName } from "./users-model";

type DialogState = { kind: "edit" | "reset" | "delete" | "disable"; user: PanelUser } | null;

export function UsersTab({ users, roles, me }: { users: PanelUser[]; roles: Role[]; me: Me }) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [dialog, setDialog] = useState<DialogState>(null);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return users;
    return users.filter((u) =>
      [u.username, u.email, u.first_name, u.last_name, u.role_name ?? ""].some((v) => v.toLowerCase().includes(q)),
    );
  }, [users, search]);

  const active = users.filter((u) => u.is_active).length;

  function invalidate() {
    qc.invalidateQueries({ queryKey: qk.users });
    qc.invalidateQueries({ queryKey: qk.roles });
  }

  const setActive = useMutation({
    mutationFn: ({ user, is_active }: { user: PanelUser; is_active: boolean }) =>
      api.patch<PanelUser>(`/users/${user.id}`, { is_active }),
    onSuccess: (u) => {
      toast.success(u.is_active ? `${u.username} enabled` : `${u.username} disabled`);
      invalidate();
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  const close = (o: boolean) => {
    if (!o) setDialog(null);
  };

  return (
    <Section
      title="Panel users"
      description={`${users.length} account${users.length === 1 ? "" : "s"} · ${active} enabled`}
      actions={
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => setInviteOpen(true)}>
            <Send /> Invite a friend
          </Button>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <UserPlus /> New user
          </Button>
        </div>
      }
    >
      <div className="grid grid-cols-1 gap-3">
        <InputGroup className="sm:max-w-xs">
          <InputGroupAddon>
            <Search />
          </InputGroupAddon>
          <InputGroupInput
            type="search"
            placeholder="Filter by name, email or role"
            aria-label="Filter users"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </InputGroup>

        {filtered.length === 0 ? (
          <EmptyState
            title={users.length === 0 ? "No users yet" : "No users match the filter"}
            description={users.length === 0 ? "Create an account for each person who manages servers." : undefined}
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="text-xs text-muted-foreground">User</TableHead>
                <TableHead className="hidden text-xs text-muted-foreground md:table-cell">Email</TableHead>
                <TableHead className="text-xs text-muted-foreground">Global role</TableHead>
                <TableHead className="hidden text-xs text-muted-foreground sm:table-cell">Status</TableHead>
                <TableHead className="hidden text-xs text-muted-foreground sm:table-cell">Last sign-in</TableHead>
                <TableHead className="w-10">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((u) => (
                <UserRow
                  key={u.id}
                  user={u}
                  me={me}
                  manageable={canManageUser(me, u, roles)}
                  onAction={(kind) => {
                    if (kind === "enable") setActive.mutate({ user: u, is_active: true });
                    else setDialog({ kind, user: u });
                  }}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <CreateUserDialog open={createOpen} onOpenChange={setCreateOpen} roles={roles} me={me} />
      <InviteFriendDialog open={inviteOpen} onOpenChange={setInviteOpen} />

      {dialog?.kind === "edit" && (
        <EditUserDialog key={dialog.user.id} user={dialog.user} open onOpenChange={close} roles={roles} me={me} />
      )}
      {dialog?.kind === "reset" && <ResetLinkDialog key={dialog.user.id} user={dialog.user} open onOpenChange={close} />}
      {dialog?.kind === "disable" && (
        <ConfirmDialog
          open
          onOpenChange={close}
          title={`Disable ${dialog.user.username}?`}
          description="They are signed out and can't sign in until the account is enabled again. Their role and server access grants are kept."
          confirmLabel="Disable account"
          destructive
          onConfirm={async () => {
            try {
              await setActive.mutateAsync({ user: dialog.user, is_active: false });
            } catch {
              /* toast shown by the mutation */
            }
          }}
        />
      )}
      {dialog?.kind === "delete" && (
        <ConfirmDialog
          open
          onOpenChange={close}
          title={`Delete ${dialog.user.username}?`}
          description={
            <div className="grid gap-2">
              <p>
                This permanently deletes the panel account and all of its per-server access grants. Audit log entries keep
                the username. This can't be undone.
              </p>
              <p>To keep the account but block sign-in, disable it instead.</p>
            </div>
          }
          confirmText={dialog.user.username}
          confirmLabel="Delete user"
          destructive
          onConfirm={async () => {
            try {
              await api.delete(`/users/${dialog.user.id}`);
              toast.success(`Deleted ${dialog.user.username}`);
              invalidate();
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        />
      )}
    </Section>
  );
}

function UserRow({
  user: u,
  me,
  manageable,
  onAction,
}: {
  user: PanelUser;
  me: Me;
  manageable: boolean;
  onAction: (kind: "edit" | "reset" | "delete" | "disable" | "enable") => void;
}) {
  const isSelf = u.id === me.id;
  const name = fullName(u);
  const statusEl = (
    <div className="flex flex-wrap items-center gap-1.5">
      {u.is_active ? (
        <span className="inline-flex items-center gap-1.5 text-xs text-emerald-700 dark:text-emerald-300">
          <span className="size-2 rounded-full bg-emerald-500" aria-hidden /> Enabled
        </span>
      ) : (
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="size-2 rounded-full bg-zinc-400" aria-hidden /> Disabled
        </span>
      )}
      {u.must_change_password && (
        <Badge
          variant="outline"
          className="border-amber-500/40 text-amber-700 dark:text-amber-300"
          title="Will be asked to choose a new password"
        >
          <KeyRound /> Password change pending
        </Badge>
      )}
    </div>
  );
  return (
    <TableRow className={cn(!u.is_active && "text-muted-foreground")}>
      <TableCell>
        <div className="flex min-w-0 items-center gap-2.5">
          <span
            className={cn(
              "grid size-8 shrink-0 place-items-center rounded-full text-sm font-semibold",
              u.is_owner ? "bg-amber-500/15 text-amber-700 dark:text-amber-300" : "bg-primary/10 text-primary",
            )}
            aria-hidden
          >
            {u.username.slice(0, 1).toUpperCase()}
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="max-w-48 truncate font-medium text-foreground">{u.username}</span>
              {isSelf && (
                <Badge variant="outline" className="h-4 px-1.5 text-[10px]">
                  You
                </Badge>
              )}
              {u.username === DISCORD_BOT_USER && !u.is_active && (
                <Badge variant="outline" className="h-4 px-1.5 text-[10px]" title="Starts and stops made through Discord are recorded as this user; it cannot sign in">
                  Discord bot (service user)
                </Badge>
              )}
            </div>
            {name && <div className="max-w-48 truncate text-xs text-muted-foreground">{name}</div>}
            {u.email && <div className="max-w-48 truncate text-xs text-muted-foreground md:hidden">{u.email}</div>}
            <div className="mt-1 sm:hidden">{statusEl}</div>
          </div>
        </div>
      </TableCell>
      <TableCell className="hidden max-w-56 truncate text-muted-foreground md:table-cell">{u.email || "—"}</TableCell>
      <TableCell>
        {u.is_owner ? (
          <Badge className="bg-amber-500/15 text-amber-800 dark:text-amber-200" title="Holds every permission implicitly">
            <Crown /> Owner
          </Badge>
        ) : u.role_name ? (
          <Badge variant="secondary">{u.role_name}</Badge>
        ) : (
          <span className="text-xs text-muted-foreground" title="Only sees servers they are granted access to">
            Per-server only
          </span>
        )}
      </TableCell>
      <TableCell className="hidden sm:table-cell">{statusEl}</TableCell>
      <TableCell className="hidden text-xs sm:table-cell" title={u.last_login ? formatDateTime(u.last_login) : undefined}>
        {u.last_login ? relativeTime(u.last_login) : <span className="text-muted-foreground">Never</span>}
      </TableCell>
      <TableCell className="text-right">
        {manageable ? (
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${u.username}`}>
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52">
              <DropdownMenuLabel className="truncate">{u.username}</DropdownMenuLabel>
              <DropdownMenuItem onSelect={() => onAction("edit")}>
                <Pencil /> Edit details & role
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => onAction("reset")}>
                <Link2 /> Create reset link
              </DropdownMenuItem>
              {!isSelf && !u.is_owner && (
                <>
                  <DropdownMenuSeparator />
                  {u.is_active ? (
                    <DropdownMenuItem onSelect={() => onAction("disable")}>
                      <UserX /> Disable account
                    </DropdownMenuItem>
                  ) : (
                    <DropdownMenuItem onSelect={() => onAction("enable")}>
                      <UserCheck /> Enable account
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuItem variant="destructive" onSelect={() => onAction("delete")}>
                    <Trash2 /> Delete user
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          <span
            className="inline-flex size-7 items-center justify-center text-muted-foreground"
            title={
              u.is_owner
                ? "Only the owner can modify the owner account"
                : "This user has permissions you don't have, so you can't manage them"
            }
          >
            <ShieldOff className="size-4" aria-hidden />
            <span className="sr-only">Not manageable</span>
          </span>
        )}
      </TableCell>
    </TableRow>
  );
}
