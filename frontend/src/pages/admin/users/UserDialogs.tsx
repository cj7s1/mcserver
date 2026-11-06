import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, KeyRound, Link2, Loader2, Wand2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { z } from "zod";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { ApiError, api, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Me, PanelUser, Role } from "@/lib/types";
import { AdminCallout, AdminCopyButton, AdminFieldError, AdminHint } from "../admin-shared";
import { canAssignRole, generatePassword, OWNER_GRANTED_CODES, sortRoles, USERNAME_RE } from "./users-model";

const NO_ROLE = "none";

const emailSchema = z.union([z.literal(""), z.email("Enter a valid email address")]);

function useInvalidateUsers() {
  const qc = useQueryClient();
  return (touchesSelf = false) => {
    qc.invalidateQueries({ queryKey: qk.users });
    qc.invalidateQueries({ queryKey: qk.roles });
    if (touchesSelf) qc.invalidateQueries({ queryKey: qk.auth });
  };
}

/** Global role picker; roles the current user may not hand out are disabled (backend enforces this too). */
function UserRoleSelect({
  id,
  value,
  onChange,
  roles,
  me,
  disabled,
  invalid,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  roles: readonly Role[];
  me: Me;
  disabled?: boolean;
  invalid?: boolean;
}) {
  const sorted = sortRoles(roles);
  const selected = roles.find((r) => String(r.id) === value);
  return (
    <div className="grid content-start gap-1.5">
      <Select value={value} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger id={id} className="w-full" aria-invalid={invalid || undefined}>
          <SelectValue placeholder="Select a role" />
        </SelectTrigger>
        <SelectContent position="popper">
          <SelectItem value={NO_ROLE}>No global role (per-server access only)</SelectItem>
          <SelectSeparator />
          {sorted.map((r) => {
            const allowed = canAssignRole(me, r);
            return (
              <SelectItem key={r.id} value={String(r.id)} disabled={!allowed && String(r.id) !== value}>
                {r.name}
                {!r.is_system && <span className="text-xs text-muted-foreground">custom</span>}
                {!allowed && (
                  <span className="text-xs text-muted-foreground">
                    {!me.is_owner && (r.key === "administrator" || r.permissions.some((p) => OWNER_GRANTED_CODES.has(p)))
                      ? "only the owner can assign it"
                      : "exceeds your permissions"}
                  </span>
                )}
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>
      <AdminHint>
        {selected
          ? `${selected.description || selected.name} · ${selected.permissions.length} permissions, applied on every server.`
          : "The user only sees servers where they are granted access on the server's Access tab."}
      </AdminHint>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
export function CreateUserDialog({
  open,
  onOpenChange,
  roles,
  me,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  roles: readonly Role[];
  me: Me;
}) {
  const empty = {
    username: "",
    email: "",
    first_name: "",
    last_name: "",
    password: "",
    role: NO_ROLE,
    must_change_password: true,
  };
  const [form, setForm] = useState(empty);
  const [showPw, setShowPw] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const invalidate = useInvalidateUsers();

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<PanelUser>("/users", body),
    onSuccess: (u) => {
      toast.success(`User ${u.username} created`);
      invalidate();
      reset();
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof ApiError) setErrors(err.fieldErrors);
      toast.error(errorMessage(err));
    },
  });

  function reset() {
    setForm(empty);
    setErrors({});
    setShowPw(false);
  }

  function set<K extends keyof typeof empty>(key: K, value: (typeof empty)[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    if (errors[key]) setErrors((e) => ({ ...e, [key]: "" }));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    const username = form.username.trim();
    if (!username) errs.username = "Username is required";
    else if (username.length > 150) errs.username = "At most 150 characters";
    else if (!USERNAME_RE.test(username)) errs.username = "Use letters, digits and @ . + - _ only";
    const email = emailSchema.safeParse(form.email.trim());
    if (!email.success) errs.email = email.error.issues[0].message;
    if (form.password.length < 10) errs.password = "At least 10 characters";
    else if (form.password.length > 256) errs.password = "At most 256 characters";
    setErrors(errs);
    if (Object.values(errs).some(Boolean)) return;
    create.mutate({
      username,
      email: form.email.trim(),
      first_name: form.first_name.trim(),
      last_name: form.last_name.trim(),
      password: form.password,
      role: form.role === NO_ROLE ? null : Number(form.role),
      must_change_password: form.must_change_password,
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o && !create.isPending) reset();
        if (!create.isPending) onOpenChange(o);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>New panel user</DialogTitle>
            <DialogDescription>
              Panel users sign in to manage servers. Minecraft players are managed on each server's Players tab.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid content-start gap-1.5">
              <Label htmlFor="cu-username">Username</Label>
              <Input
                id="cu-username"
                value={form.username}
                onChange={(e) => set("username", e.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                required
                aria-invalid={!!errors.username || undefined}
                aria-describedby="cu-username-err"
              />
              <AdminFieldError id="cu-username-err" message={errors.username} />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="cu-email">
                Email <span className="font-normal text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="cu-email"
                type="email"
                value={form.email}
                onChange={(e) => set("email", e.target.value)}
                autoComplete="off"
                aria-invalid={!!errors.email || undefined}
                aria-describedby="cu-email-err"
              />
              <AdminFieldError id="cu-email-err" message={errors.email} />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="cu-first">
                First name <span className="font-normal text-muted-foreground">(optional)</span>
              </Label>
              <Input id="cu-first" value={form.first_name} onChange={(e) => set("first_name", e.target.value)} autoComplete="off" />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="cu-last">
                Last name <span className="font-normal text-muted-foreground">(optional)</span>
              </Label>
              <Input id="cu-last" value={form.last_name} onChange={(e) => set("last_name", e.target.value)} autoComplete="off" />
            </div>
          </div>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="cu-password">Initial password</Label>
            <div className="flex flex-wrap gap-2">
              <InputGroup className="min-w-48 flex-1">
                <InputGroupInput
                  id="cu-password"
                  type={showPw ? "text" : "password"}
                  value={form.password}
                  onChange={(e) => set("password", e.target.value)}
                  autoComplete="new-password"
                  spellCheck={false}
                  className="font-mono"
                  aria-invalid={!!errors.password || undefined}
                  aria-describedby="cu-password-hint cu-password-err"
                />
                <InputGroupAddon align="inline-end">
                  <InputGroupButton
                    size="icon-xs"
                    aria-label={showPw ? "Hide password" : "Show password"}
                    aria-pressed={showPw}
                    onClick={() => setShowPw((v) => !v)}
                  >
                    {showPw ? <EyeOff /> : <Eye />}
                  </InputGroupButton>
                </InputGroupAddon>
              </InputGroup>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  set("password", generatePassword());
                  setShowPw(true);
                }}
              >
                <Wand2 /> Generate
              </Button>
              {form.password && <AdminCopyButton value={form.password} label="Copy" size="default" />}
            </div>
            <AdminHint>
              <span id="cu-password-hint">
                At least 10 characters. Common passwords and ones similar to the username are rejected.
              </span>
            </AdminHint>
            <AdminFieldError id="cu-password-err" message={errors.password} />
          </div>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="cu-role">Global role</Label>
            <UserRoleSelect
              id="cu-role"
              value={form.role}
              onChange={(v) => set("role", v)}
              roles={roles}
              me={me}
              invalid={!!errors.role}
            />
            <AdminFieldError id="cu-role-err" message={errors.role} />
          </div>

          <div className="flex items-start gap-2.5">
            <Checkbox
              id="cu-must-change"
              checked={form.must_change_password}
              onCheckedChange={(c) => set("must_change_password", c === true)}
              className="mt-0.5"
            />
            <div className="grid gap-0.5">
              <Label htmlFor="cu-must-change">Ask for a new password after first sign-in</Label>
              <AdminHint>Recommended when you share the initial password with the user.</AdminHint>
            </div>
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                reset();
                onOpenChange(false);
              }}
              disabled={create.isPending}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending && <Loader2 className="animate-spin" />}
              Create user
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------------------------
export function EditUserDialog({
  user,
  open,
  onOpenChange,
  roles,
  me,
}: {
  user: PanelUser;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  roles: readonly Role[];
  me: Me;
}) {
  const isSelf = user.id === me.id;
  const initial = {
    email: user.email,
    first_name: user.first_name,
    last_name: user.last_name,
    role: user.role === null ? NO_ROLE : String(user.role),
    is_active: user.is_active,
  };
  const [form, setForm] = useState(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const invalidate = useInvalidateUsers();

  const roleLocked = isSelf && !user.is_owner;
  const activeLocked = isSelf || user.is_owner;

  const changes: Record<string, unknown> = {};
  if (form.email.trim() !== initial.email) changes.email = form.email.trim();
  if (form.first_name.trim() !== initial.first_name) changes.first_name = form.first_name.trim();
  if (form.last_name.trim() !== initial.last_name) changes.last_name = form.last_name.trim();
  if (!user.is_owner && !roleLocked && form.role !== initial.role)
    changes.role = form.role === NO_ROLE ? null : Number(form.role);
  if (!activeLocked && form.is_active !== initial.is_active) changes.is_active = form.is_active;
  const dirty = Object.keys(changes).length > 0;

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.patch<PanelUser>(`/users/${user.id}`, body),
    onSuccess: (u) => {
      toast.success(`Saved ${u.username}`);
      invalidate(isSelf);
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof ApiError) setErrors(err.fieldErrors);
      toast.error(errorMessage(err));
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const email = emailSchema.safeParse(form.email.trim());
    if (!email.success) {
      setErrors({ email: email.error.issues[0].message });
      return;
    }
    setErrors({});
    if (dirty) save.mutate(changes);
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !save.isPending && onOpenChange(o)}>
      <DialogContent className="sm:max-w-lg">
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>
              Edit <span className="font-mono">{user.username}</span>
            </DialogTitle>
            <DialogDescription>Usernames can't be changed. Use a reset link to set a new password.</DialogDescription>
          </DialogHeader>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid gap-1.5 sm:col-span-2">
              <Label htmlFor="eu-email">Email</Label>
              <Input
                id="eu-email"
                type="email"
                value={form.email}
                onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                autoComplete="off"
                aria-invalid={!!errors.email || undefined}
                aria-describedby="eu-email-err"
              />
              <AdminFieldError id="eu-email-err" message={errors.email} />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="eu-first">First name</Label>
              <Input
                id="eu-first"
                value={form.first_name}
                onChange={(e) => setForm((f) => ({ ...f, first_name: e.target.value }))}
                autoComplete="off"
              />
            </div>
            <div className="grid content-start gap-1.5">
              <Label htmlFor="eu-last">Last name</Label>
              <Input
                id="eu-last"
                value={form.last_name}
                onChange={(e) => setForm((f) => ({ ...f, last_name: e.target.value }))}
                autoComplete="off"
              />
            </div>
          </div>

          {user.is_owner ? (
            <AdminCallout tone="info" icon={<KeyRound />} title="Owner account">
              The owner holds every permission implicitly. It has no role and can't be disabled or deleted.
            </AdminCallout>
          ) : (
            <div className="grid content-start gap-1.5">
              <Label htmlFor="eu-role">Global role</Label>
              <UserRoleSelect
                id="eu-role"
                value={form.role}
                onChange={(v) => setForm((f) => ({ ...f, role: v }))}
                roles={roles}
                me={me}
                disabled={roleLocked}
                invalid={!!errors.role}
              />
              {roleLocked && <AdminHint>You can't change your own role.</AdminHint>}
              <AdminFieldError id="eu-role-err" message={errors.role} />
            </div>
          )}

          {!user.is_owner && (
            <div className="flex items-start justify-between gap-4 rounded-lg border p-3">
              <div className="grid gap-0.5">
                <Label htmlFor="eu-active">Account enabled</Label>
                <AdminHint>
                  {isSelf
                    ? "You can't disable your own account."
                    : "Disabled users can't sign in and lose access immediately. Their server grants are kept."}
                </AdminHint>
              </div>
              <Switch
                id="eu-active"
                checked={form.is_active}
                onCheckedChange={(c) => setForm((f) => ({ ...f, is_active: c }))}
                disabled={activeLocked}
              />
            </div>
          )}
          <AdminFieldError id="eu-active-err" message={errors.is_active} />

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={!dirty || save.isPending}>
              {save.isPending && <Loader2 className="animate-spin" />}
              Save changes
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------------------------
interface ResetLinkResponse {
  token: string;
  path: string;
  expires_in_hours: number;
}

export function ResetLinkDialog({
  user,
  open,
  onOpenChange,
}: {
  user: PanelUser;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [link, setLink] = useState<{ url: string; hours: number } | null>(null);
  const issue = useMutation({
    mutationFn: () => api.post<ResetLinkResponse>(`/users/${user.id}/reset-link`),
    onSuccess: (r) => {
      setLink({ url: `${window.location.origin}${r.path}`, hours: r.expires_in_hours });
      toast.success("Reset link created");
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (issue.isPending) return;
        if (!o) setLink(null);
        onOpenChange(o);
      }}
    >
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            Password reset link for <span className="font-mono">{user.username}</span>
          </DialogTitle>
          <DialogDescription>
            A one-time link the user opens to choose a new password. Their current password keeps working until the link
            is used. MCPanel doesn't send email, so pass the link on yourself.
          </DialogDescription>
        </DialogHeader>

        {!user.is_active && (
          <AdminCallout tone="warning" title="This account is disabled">
            Reset links only work for enabled accounts. Enable the account first, or the link will be rejected.
          </AdminCallout>
        )}

        {link ? (
          <div className="grid gap-3">
            <div className="grid content-start gap-1.5">
              <Label htmlFor="reset-link-url">Reset link</Label>
              <div className="flex gap-2">
                <Input
                  id="reset-link-url"
                  readOnly
                  value={link.url}
                  className="font-mono text-xs"
                  onFocus={(e) => e.currentTarget.select()}
                />
                <AdminCopyButton value={link.url} size="default" />
              </div>
            </div>
            <AdminCallout tone="warning" icon={<Link2 />} title="Shown only once">
              This link expires in {link.hours} hours and can be used once. Anyone who has it can set this account's
              password, so send it over a private channel. Close this dialog and create a new link if it gets lost.
            </AdminCallout>
          </div>
        ) : null}

        <DialogFooter>
          {link ? (
            <Button type="button" onClick={() => onOpenChange(false)}>
              Done
            </Button>
          ) : (
            <>
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={issue.isPending}>
                Cancel
              </Button>
              <Button type="button" onClick={() => issue.mutate()} disabled={issue.isPending}>
                {issue.isPending ? <Loader2 className="animate-spin" /> : <Link2 />}
                Create link
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
