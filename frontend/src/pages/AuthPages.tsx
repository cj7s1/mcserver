import { useQueryClient } from "@tanstack/react-query";
import { Boxes, Loader2 } from "lucide-react";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { z } from "zod";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/hooks/use-auth";
import { api, errorMessage, throttledMessage } from "@/lib/api";
import { qk } from "@/lib/queries";

function AuthCard({ title, subtitle, children }: { title: string; subtitle?: ReactNode; children: ReactNode }) {
  return (
    <div className="grid min-h-svh place-items-center bg-[radial-gradient(ellipse_at_top,var(--color-primary)/8%,transparent_60%)] px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <div className="grid size-11 place-items-center rounded-xl bg-primary/15 text-primary">
            <Boxes className="size-5" />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
            {subtitle && <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>}
          </div>
        </div>
        <div className="rounded-xl border bg-card p-5 shadow-sm">{children}</div>
      </div>
    </div>
  );
}

const passwordSchema = z.string().min(10, "Use at least 10 characters");

export function LoginPage() {
  const qc = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/login", { username, password });
      await qc.invalidateQueries({ queryKey: qk.auth });
    } catch (err) {
      // 429 throttled (too many failed sign-ins for this account or from this address): with the wait
      setError(throttledMessage(err) ?? errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Sign in to MCPanel" subtitle="Manage your Minecraft servers">
      <form className="grid gap-4" onSubmit={submit}>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <div className="grid gap-1.5">
          <Label htmlFor="username">Username</Label>
          <Input id="username" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="password">Password</Label>
          <Input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <Button type="submit" disabled={busy}>
          {busy && <Loader2 className="animate-spin" />} Sign in
        </Button>
        <p className="text-center text-xs text-muted-foreground">
          Forgot your password? Ask an administrator for a reset link, or run{" "}
          <code className="rounded bg-muted px-1">manage.py changepassword</code> on the server.
        </p>
      </form>
    </AuthCard>
  );
}

export function SetupPage() {
  const qc = useQueryClient();
  const { state } = useAuth();
  const tokenRequired = !!state?.setup_token_required;
  const [form, setForm] = useState({ username: "", email: "", password: "", confirm: "", setupToken: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const pw = passwordSchema.safeParse(form.password);
    if (!pw.success) return setError(pw.error.issues[0].message);
    if (form.password !== form.confirm) return setError("Passwords do not match");
    setBusy(true);
    try {
      await api.post("/auth/setup", {
        username: form.username,
        email: form.email,
        password: form.password,
        ...(tokenRequired ? { setup_token: form.setupToken.trim() } : {}),
      });
      await qc.invalidateQueries({ queryKey: qk.auth });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Welcome to MCPanel" subtitle="Create the owner account. This account has full control of the panel.">
      <form className="grid gap-4" onSubmit={submit}>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {tokenRequired && (
          <div className="grid gap-1.5">
            <Label htmlFor="su-token">Setup token</Label>
            <Input
              id="su-token"
              type="password"
              autoComplete="off"
              value={form.setupToken}
              onChange={(e) => setForm({ ...form, setupToken: e.target.value })}
              required
            />
            <p className="text-xs text-muted-foreground">The value of MCPANEL_SETUP_TOKEN from the server's .env file.</p>
          </div>
        )}
        <div className="grid gap-1.5">
          <Label htmlFor="su-username">Username</Label>
          <Input id="su-username" autoComplete="username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required autoFocus />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="su-email">Email (optional)</Label>
          <Input id="su-email" type="email" autoComplete="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="su-password">Password</Label>
          <Input id="su-password" type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
          <p className="text-xs text-muted-foreground">At least 10 characters; avoid common passwords.</p>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="su-confirm">Confirm password</Label>
          <Input id="su-confirm" type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} required />
        </div>
        <Button type="submit" disabled={busy}>
          {busy && <Loader2 className="animate-spin" />} Create owner account
        </Button>
      </form>
    </AuthCard>
  );
}

export function ResetPasswordPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  // Reset links carry the token in the URL fragment (#token=…) so it never reaches server or proxy logs;
  // ?token= is still read for links issued by older versions. Strip it from the address bar right away.
  const [token] = useState(() => readResetToken(window.location.hash, params.get("token")));
  useEffect(() => {
    if (window.location.hash || params.has("token")) {
      window.history.replaceState(window.history.state, "", window.location.pathname);
    }
  }, [params]);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (password !== confirm) return setError("Passwords do not match");
    setBusy(true);
    try {
      await api.post("/auth/reset", { token, new_password: password });
      setDone(true);
      setTimeout(() => navigate("/"), 1500);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Reset password" subtitle="Choose a new password for your account.">
      {done ? (
        <p className="text-sm">Password updated. Redirecting to sign in…</p>
      ) : !token ? (
        <p className="text-sm text-muted-foreground">
          This page needs a reset link from an administrator. <Link className="text-primary underline" to="/">Back to sign in</Link>
        </p>
      ) : (
        <form className="grid gap-4" onSubmit={submit}>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <div className="grid gap-1.5">
            <Label htmlFor="rp-password">New password</Label>
            <Input id="rp-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="rp-confirm">Confirm</Label>
            <Input id="rp-confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
          </div>
          <Button type="submit" disabled={busy}>
            {busy && <Loader2 className="animate-spin" />} Set password
          </Button>
        </form>
      )}
    </AuthCard>
  );
}

/** Token from a reset link: the URL fragment (#token=…) first, then the legacy ?token= query. */
export function readResetToken(hash: string, query: string | null): string {
  const fromHash = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash).get("token");
  return (fromHash || query || "").trim();
}
