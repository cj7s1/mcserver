import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Me, Role } from "@/lib/types";
import { AdminFieldError, AdminHint } from "../admin-shared";
import { canGrantPermission, ROLE_KEY_RE, slugify, sortRoles } from "./users-model";

const BLANK = "blank";

export function CreateRoleDialog({
  open,
  onOpenChange,
  roles,
  me,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  roles: readonly Role[];
  me: Me;
  onCreated?: (role: Role) => void;
}) {
  const qc = useQueryClient();
  const empty = { name: "", key: "", keyTouched: false, description: "", template: BLANK };
  const [form, setForm] = useState(empty);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const template = roles.find((r) => String(r.id) === form.template);
  const templatePerms = template?.permissions ?? [];
  const grantable = templatePerms.filter((p) => canGrantPermission(me, p));
  const dropped = templatePerms.length - grantable.length;

  const create = useMutation({
    mutationFn: (body: { key: string; name: string; description: string; permissions: string[] }) =>
      api.post<Role>("/roles", body),
    onSuccess: (role) => {
      toast.success(`Role ${role.name} created`);
      qc.invalidateQueries({ queryKey: qk.roles });
      setForm(empty);
      setErrors({});
      onOpenChange(false);
      onCreated?.(role);
    },
    onError: (err) => {
      if (err instanceof ApiError) setErrors(err.fieldErrors);
      toast.error(errorMessage(err));
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs: Record<string, string> = {};
    const name = form.name.trim();
    const key = form.key.trim();
    if (!name) errs.name = "Name is required";
    else if (name.length > 100) errs.name = "At most 100 characters";
    if (!key) errs.key = "Key is required";
    else if (key.length > 64) errs.key = "At most 64 characters";
    else if (!ROLE_KEY_RE.test(key)) errs.key = "Use letters, digits, hyphens and underscores only";
    else if (roles.some((r) => r.key === key)) errs.key = "A role with this key already exists";
    if (form.description.length > 300) errs.description = "At most 300 characters";
    setErrors(errs);
    if (Object.keys(errs).length) return;
    create.mutate({ key, name, description: form.description.trim(), permissions: grantable });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (create.isPending) return;
        if (!o) {
          setForm(empty);
          setErrors({});
        }
        onOpenChange(o);
      }}
    >
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>New custom role</DialogTitle>
            <DialogDescription>
              Start from an existing role, then fine-tune its permissions in the matrix.
            </DialogDescription>
          </DialogHeader>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="cr-name">Name</Label>
            <Input
              id="cr-name"
              value={form.name}
              placeholder="e.g. Moderator"
              onChange={(e) => {
                const name = e.target.value;
                setForm((f) => ({ ...f, name, key: f.keyTouched ? f.key : slugify(name) }));
              }}
              aria-invalid={!!errors.name || undefined}
              aria-describedby="cr-name-err"
              autoComplete="off"
            />
            <AdminFieldError id="cr-name-err" message={errors.name} />
          </div>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="cr-key">Key</Label>
            <Input
              id="cr-key"
              value={form.key}
              onChange={(e) => setForm((f) => ({ ...f, key: e.target.value, keyTouched: true }))}
              className="font-mono"
              spellCheck={false}
              autoCapitalize="none"
              autoComplete="off"
              aria-invalid={!!errors.key || undefined}
              aria-describedby="cr-key-hint cr-key-err"
            />
            <AdminHint>
              <span id="cr-key-hint">A unique identifier (slug). It can't be changed later.</span>
            </AdminHint>
            <AdminFieldError id="cr-key-err" message={errors.key} />
          </div>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="cr-desc">
              Description <span className="font-normal text-muted-foreground">(optional)</span>
            </Label>
            <Textarea
              id="cr-desc"
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              rows={2}
              maxLength={300}
              aria-invalid={!!errors.description || undefined}
              aria-describedby="cr-desc-err"
            />
            <AdminFieldError id="cr-desc-err" message={errors.description} />
          </div>

          <div className="grid content-start gap-1.5">
            <Label htmlFor="cr-template">Start from</Label>
            <Select value={form.template} onValueChange={(v) => setForm((f) => ({ ...f, template: v }))}>
              <SelectTrigger id="cr-template" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper">
                <SelectItem value={BLANK}>Blank (no permissions)</SelectItem>
                <SelectSeparator />
                {sortRoles(roles).map((r) => (
                  <SelectItem key={r.id} value={String(r.id)}>
                    {r.name}
                    <span className="text-xs text-muted-foreground">{r.permissions.length} permissions</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <AdminHint>
              {template
                ? `Copies ${grantable.length} permission${grantable.length === 1 ? "" : "s"} from ${template.name}.`
                : "The role starts empty."}
              {dropped > 0 &&
                ` ${dropped} permission${dropped === 1 ? "" : "s"} you can't grant (ones you don't hold, and user or settings administration, which only the owner grants) will be left out.`}
            </AdminHint>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending && <Loader2 className="animate-spin" />}
              Create role
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function EditRoleDialog({
  role,
  open,
  onOpenChange,
}: {
  role: Role;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(role.name);
  const [description, setDescription] = useState(role.description);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const dirty = name.trim() !== role.name || description.trim() !== role.description;

  const save = useMutation({
    mutationFn: (body: { name: string; description: string }) => api.patch<Role>(`/roles/${role.id}`, body),
    onSuccess: (r) => {
      toast.success(`Saved ${r.name}`);
      qc.invalidateQueries({ queryKey: qk.roles });
      qc.invalidateQueries({ queryKey: qk.users });
      onOpenChange(false);
    },
    onError: (err) => {
      if (err instanceof ApiError) setErrors(err.fieldErrors);
      toast.error(errorMessage(err));
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!name.trim()) {
      setErrors({ name: "Name is required" });
      return;
    }
    setErrors({});
    save.mutate({ name: name.trim(), description: description.trim() });
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !save.isPending && onOpenChange(o)}>
      <DialogContent className="sm:max-w-md">
        <form onSubmit={submit} className="grid gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>Edit role</DialogTitle>
            <DialogDescription>
              Key <span className="font-mono">{role.key}</span>. Permissions are edited in the matrix.
            </DialogDescription>
          </DialogHeader>
          <div className="grid content-start gap-1.5">
            <Label htmlFor="er-name">Name</Label>
            <Input
              id="er-name"
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={!!errors.name || undefined}
              aria-describedby="er-name-err"
              autoComplete="off"
            />
            <AdminFieldError id="er-name-err" message={errors.name} />
          </div>
          <div className="grid content-start gap-1.5">
            <Label htmlFor="er-desc">Description</Label>
            <Textarea
              id="er-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              maxLength={300}
              aria-describedby="er-desc-err"
            />
            <AdminFieldError id="er-desc-err" message={errors.description} />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
              Cancel
            </Button>
            <Button type="submit" disabled={!dirty || save.isPending}>
              {save.isPending && <Loader2 className="animate-spin" />}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
