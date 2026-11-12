import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Circle,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  LogOut,
  Monitor,
  Moon,
  ShieldCheck,
  Sun,
  TriangleAlert,
  UserRound,
} from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { z } from "zod";
import { PageHeader, Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { useAuth } from "@/hooks/use-auth";
import { useTheme } from "@/hooks/use-theme";
import { ApiError, api, errorMessage, throttledMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { AuthState, Me, PermissionDef } from "@/lib/types";
import { cn } from "@/lib/utils";

const meKey = [...qk.auth, "me"] as const;
const permissionDefsKey = ["permissions", "defs"] as const;

const emailSchema = z.union([z.literal(""), z.email("Enter a valid email address")]);

export default function AccountPage() {
  const me = useQuery({ queryKey: meKey, queryFn: () => api.get<Me>("/auth/me") });

  if (me.isLoading) return <LoadingState label="Loading account…" />;
  if (me.error || !me.data)
    return <ErrorState error={me.error ?? new Error("Account unavailable")} onRetry={() => me.refetch()} />;
  const user = me.data;
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(" ");

  return (
    <div className="grid gap-4">
      <PageHeader
        className="pb-1"
        icon={
          <span
            className="grid size-11 place-items-center rounded-full bg-primary/15 text-lg font-semibold text-primary"
            aria-hidden
          >
            {user.username.slice(0, 1).toUpperCase()}
          </span>
        }
        title={fullName || user.username}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs">@{user.username}</span>
            {user.is_owner ? (
              <Badge>Owner</Badge>
            ) : user.role_name ? (
              <Badge variant="secondary">{user.role_name}</Badge>
            ) : (
              <Badge variant="outline">No global role</Badge>
            )}
            {user.email && <span>{user.email}</span>}
          </span>
        }
      />

      {user.must_change_password && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-800 dark:text-amber-200"
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <div>
            <p className="font-medium">Please choose a new password</p>
            <p className="mt-0.5 text-amber-800/90 dark:text-amber-200/80">
              An administrator set a temporary password for this account. Change it below to continue securely.
            </p>
          </div>
        </div>
      )}

      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div className="grid gap-4">
          <ProfileForm key={`${user.email}|${user.first_name}|${user.last_name}`} me={user} />
          <PasswordForm me={user} />
        </div>
        <div className="grid gap-4">
          <AppearanceSection />
          <PermissionsSection me={user} />
        </div>
      </div>
    </div>
  );
}

/**
 * Focused screen the auth gate (router.tsx) shows in place of every page while the account has
 * `must_change_password` set (the backend refuses everything else until then). After a successful
 * change every query is refreshed and the page the user originally asked for renders.
 */
export function ForcedPasswordChange() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <main className="grid min-h-svh place-items-center bg-[radial-gradient(ellipse_at_top,var(--color-primary)/8%,transparent_60%)] px-4 py-10">
      <div className="grid w-full max-w-md gap-4">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="grid size-11 place-items-center rounded-xl bg-primary/15 text-primary">
            <KeyRound className="size-5" aria-hidden />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Set a new password</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              An administrator set a temporary password for <span className="font-mono">@{user.username}</span>. Choose your own
              password to continue.
            </p>
          </div>
        </div>
        <PasswordForm me={user} required />
        <div className="flex justify-center">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => logout().catch((err: unknown) => toast.error(errorMessage(err)))}
          >
            <LogOut /> Sign out
          </Button>
        </div>
      </div>
    </main>
  );
}

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="text-xs text-destructive">
      {message}
    </p>
  );
}

function ProfileForm({ me }: { me: Me }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({ email: me.email, first_name: me.first_name, last_name: me.last_name });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const dirty = form.email !== me.email || form.first_name !== me.first_name || form.last_name !== me.last_name;

  const save = useMutation({
    mutationFn: (body: typeof form) => api.patch<Me>("/auth/me", body),
    onSuccess: (data) => {
      qc.setQueryData(meKey, data);
      qc.setQueryData<AuthState>(qk.auth, (old) => (old ? { ...old, user: data } : old));
      toast.success("Profile saved");
    },
    onError: (err) => {
      if (err instanceof ApiError) setErrors(err.fieldErrors);
      toast.error(errorMessage(err));
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const body = { email: form.email.trim(), first_name: form.first_name.trim(), last_name: form.last_name.trim() };
    const email = emailSchema.safeParse(body.email);
    if (!email.success) {
      setErrors({ email: email.error.issues[0].message });
      return;
    }
    setErrors({});
    save.mutate(body);
  }

  const field = (key: keyof typeof form, label: string, props: { type?: string; autoComplete: string; placeholder?: string }) => (
    <div className="grid gap-1.5">
      <Label htmlFor={`profile-${key}`}>{label}</Label>
      <Input
        id={`profile-${key}`}
        maxLength={key === "email" ? 254 : 150}
        value={form[key]}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
        aria-invalid={!!errors[key] || undefined}
        aria-describedby={errors[key] ? `profile-${key}-error` : undefined}
        {...props}
      />
      <FieldError id={`profile-${key}-error`} message={errors[key]} />
    </div>
  );

  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <UserRound className="size-4 text-muted-foreground" aria-hidden /> Profile
        </span>
      }
      description="Your name and contact address. The username cannot be changed."
    >
      <form className="grid gap-4" onSubmit={submit} noValidate>
        <div className="grid gap-1.5">
          <Label htmlFor="profile-username">Username</Label>
          <Input id="profile-username" value={me.username} readOnly disabled className="font-mono" />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          {field("first_name", "First name", { autoComplete: "given-name" })}
          {field("last_name", "Last name", { autoComplete: "family-name" })}
        </div>
        {field("email", "Email", { type: "email", autoComplete: "email", placeholder: "you@example.com" })}
        {errors.non_field_errors && <FieldError id="profile-form-error" message={errors.non_field_errors} />}
        <div className="flex items-center justify-end gap-2">
          {dirty && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={save.isPending}
              onClick={() => {
                setForm({ email: me.email, first_name: me.first_name, last_name: me.last_name });
                setErrors({});
              }}
            >
              Discard
            </Button>
          )}
          <Button type="submit" size="sm" disabled={!dirty || save.isPending}>
            {save.isPending && <Loader2 className="animate-spin" />} Save profile
          </Button>
        </div>
      </form>
    </Section>
  );
}

const EMPTY_PASSWORDS = { old_password: "", new_password: "", confirm: "" };

/** `required`: shown by the forced password-change screen; success refreshes every query. */
function PasswordForm({ me, required = false }: { me: Me; required?: boolean }) {
  const qc = useQueryClient();
  const [form, setForm] = useState(EMPTY_PASSWORDS);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [show, setShow] = useState(false);

  const pw = form.new_password;
  const lowered = pw.toLowerCase();
  const personal = [me.username, me.email.split("@")[0], me.first_name, me.last_name]
    .map((s) => s.trim().toLowerCase())
    .filter((s) => s.length >= 3);
  const checks = [
    { ok: pw.length >= 10, label: "At least 10 characters" },
    { ok: pw.length > 0 && !/^\d+$/.test(pw), label: "Not only numbers" },
    { ok: pw.length > 0 && !personal.some((s) => lowered.includes(s)), label: "Does not contain your username or name" },
    {
      ok: pw.length > 0 && pw !== form.old_password,
      label: required ? "Different from the temporary password" : "Different from the current password",
    },
  ];

  const change = useMutation({
    mutationFn: (body: { old_password: string; new_password: string }) => api.post<void>("/auth/password", body),
    onSuccess: () => {
      toast.success("Password changed");
      setForm(EMPTY_PASSWORDS);
      setErrors({});
      // Refresh /auth/state and /auth/me; after a forced change also everything that was refused.
      qc.invalidateQueries(required ? undefined : { queryKey: qk.auth });
    },
    onError: (err) => {
      // 429 throttled: too many wrong current passwords for this account; the form says until when (D-3)
      const throttled = throttledMessage(err);
      if (throttled) setErrors({ non_field_errors: throttled });
      else if (err instanceof ApiError) setErrors(err.fieldErrors);
      toast.error(throttled ?? errorMessage(err));
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (!form.old_password) next.old_password = "Enter your current password";
    const failed = checks.find((c) => !c.ok);
    if (failed) next.new_password = `New password: ${failed.label.toLowerCase()}`;
    if (form.confirm !== form.new_password) next.confirm = "Passwords do not match";
    setErrors(next);
    if (Object.keys(next).length) return;
    change.mutate({ old_password: form.old_password, new_password: form.new_password });
  }

  const input = (key: keyof typeof form, label: string, autoComplete: string, extra?: ReactNode) => (
    <div className="grid gap-1.5">
      <Label htmlFor={`pw-${key}`}>{label}</Label>
      <Input
        id={`pw-${key}`}
        type={show ? "text" : "password"}
        autoComplete={autoComplete}
        value={form[key]}
        maxLength={256}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
        aria-invalid={!!errors[key] || undefined}
        aria-describedby={
          [errors[key] ? `pw-${key}-error` : "", key === "new_password" ? "pw-rules" : ""].filter(Boolean).join(" ") || undefined
        }
      />
      <FieldError id={`pw-${key}-error`} message={errors[key]} />
      {extra}
    </div>
  );

  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <KeyRound className="size-4 text-muted-foreground" aria-hidden /> {required ? "New password" : "Change password"}
        </span>
      }
      description={
        required
          ? "Enter the temporary password once more, then your new one. You stay signed in on this device."
          : "You stay signed in on this device after changing it."
      }
      actions={
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={() => setShow((s) => !s)}
          aria-label={show ? "Hide passwords" : "Show passwords"}
          aria-pressed={show}
        >
          {show ? <EyeOff /> : <Eye />}
        </Button>
      }
    >
      <form className="grid gap-4" onSubmit={submit} noValidate>
        {/* hidden username helps password managers associate the change */}
        <input type="text" name="username" autoComplete="username" value={me.username} readOnly hidden />
        {input("old_password", required ? "Temporary password" : "Current password", "current-password")}
        {input(
          "new_password",
          "New password",
          "new-password",
          <ul id="pw-rules" className="grid gap-0.5 text-xs" aria-label="Password requirements">
            {checks.map((c) => (
              <li
                key={c.label}
                className={cn(
                  "flex items-center gap-1.5",
                  c.ok ? "text-emerald-700 dark:text-emerald-400" : "text-muted-foreground",
                )}
              >
                {c.ok ? <Check className="size-3" aria-hidden /> : <Circle className="size-3" aria-hidden />}
                {c.label}
                <span className="sr-only">{c.ok ? "(met)" : "(not met)"}</span>
              </li>
            ))}
            <li className="pl-[1.125rem] text-muted-foreground">Common passwords are rejected by the server.</li>
          </ul>,
        )}
        {input("confirm", "Confirm new password", "new-password")}
        {errors.non_field_errors && <FieldError id="pw-form-error" message={errors.non_field_errors} />}
        <div className="flex justify-end">
          <Button
            type="submit"
            size="sm"
            disabled={change.isPending || !form.old_password || !form.new_password || !form.confirm}
          >
            {change.isPending && <Loader2 className="animate-spin" />} {required ? "Set password and continue" : "Change password"}
          </Button>
        </div>
      </form>
    </Section>
  );
}

const THEMES: { value: "dark" | "light" | "system"; label: string; description: string; icon: ReactNode }[] = [
  { value: "dark", label: "Dark", description: "The default panel theme.", icon: <Moon /> },
  { value: "light", label: "Light", description: "Bright surfaces for well-lit rooms.", icon: <Sun /> },
  { value: "system", label: "System", description: "Match your operating system.", icon: <Monitor /> },
];

function AppearanceSection() {
  const { theme, resolved, setTheme } = useTheme();
  return (
    <Section title="Appearance" description="Saved in this browser only.">
      <RadioGroup
        value={theme}
        onValueChange={(v) => setTheme(v as "dark" | "light" | "system")}
        className="grid gap-2 sm:grid-cols-3"
        aria-label="Theme"
      >
        {THEMES.map((t) => (
          <Label
            key={t.value}
            htmlFor={`theme-${t.value}`}
            className={cn(
              "flex cursor-pointer flex-col items-start gap-2 rounded-lg border p-3 font-normal transition-colors hover:bg-muted/50",
              theme === t.value && "border-primary bg-primary/5",
            )}
          >
            <span className="flex w-full items-center gap-2">
              <RadioGroupItem id={`theme-${t.value}`} value={t.value} />
              <span className="text-sm font-medium">{t.label}</span>
              <span className="ml-auto text-muted-foreground [&_svg]:size-4" aria-hidden>
                {t.icon}
              </span>
            </span>
            <span className="text-xs leading-snug text-muted-foreground">
              {t.description}
              {t.value === "system" && theme === "system" ? ` Currently ${resolved}.` : ""}
            </span>
          </Label>
        ))}
      </RadioGroup>
    </Section>
  );
}

function PermissionsSection({ me }: { me: Me }) {
  const defs = useQuery({
    queryKey: permissionDefsKey,
    queryFn: () => api.get<{ permissions: PermissionDef[] }>("/permissions"),
    staleTime: 5 * 60_000,
  });
  const held = new Set<string>(me.permissions);
  const known = defs.data?.permissions ?? [];
  const mine = known.filter((p) => held.has(p.code));
  const unknownCodes = me.permissions.filter((c) => !known.some((p) => p.code === c));
  const system = mine.filter((p) => p.scope === "system");
  const instance = mine.filter((p) => p.scope === "instance");

  return (
    <Section
      title={
        <span className="flex items-center gap-2">
          <ShieldCheck className="size-4 text-muted-foreground" aria-hidden /> Global permissions
        </span>
      }
      description={
        me.is_owner
          ? "As the owner you implicitly hold every permission."
          : me.role_name
            ? `Granted by your global role “${me.role_name}”.`
            : "You have no global role. Access to individual servers can still be granted per server."
      }
    >
      {defs.isLoading ? (
        <LoadingState className="py-6" />
      ) : me.permissions.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No panel-wide permissions. You can only see servers that an administrator has shared with you, with the role chosen for
          each server.
        </p>
      ) : (
        <div className="grid gap-4">
          {defs.error && (
            <p className="text-xs text-muted-foreground">Permission descriptions could not be loaded; showing codes only.</p>
          )}
          {system.length > 0 && <PermissionGroup title="Panel" items={system} />}
          {instance.length > 0 && <PermissionGroup title="On every server" items={instance} />}
          {unknownCodes.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {unknownCodes.map((c) => (
                <Badge key={c} variant="outline" className="font-mono">
                  {c}
                </Badge>
              ))}
            </div>
          )}
          {!me.is_owner && instance.length === 0 && (
            <p className="text-xs text-muted-foreground">
              Server-level permissions come from per-server access grants and are shown on each server.
            </p>
          )}
        </div>
      )}
    </Section>
  );
}

function PermissionGroup({ title, items }: { title: string; items: PermissionDef[] }) {
  return (
    <div className="grid gap-2">
      <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{title}</h3>
      <ul className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
        {items.map((p) => (
          <li key={p.code} className="flex min-w-0 items-start gap-2">
            <Check className="mt-0.5 size-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden />
            <div className="min-w-0">
              <p className="text-sm leading-tight">{p.label}</p>
              <p className="text-xs text-muted-foreground">{p.description}</p>
              <p className="truncate font-mono text-[10px] text-muted-foreground">{p.code}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
