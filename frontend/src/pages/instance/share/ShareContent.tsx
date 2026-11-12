/**
 * The Share card's content (B1): what a friend needs to join, in the order the owner uses it: the address to
 * copy, the ports to forward, what keeps strangers out, the join steps, the checklist with what is missing and
 * where to fix it, and "Copy invite" (a ready message for Discord). Shown on the Overview (ShareCard) and in the
 * dialog of the header's Share button (ShareButton).
 *
 * The join password is only offered to users who may configure the server; it is read when a copy button is
 * pressed and not kept. Users who may not configure (a friend) see the card without password and fix-it links.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, CircleHelp, Copy, KeyRound, Loader2, Lock, LockOpen, Send, UserPlus, X } from "lucide-react";
import { useId, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/hooks/use-auth";
import { api, errorMessage } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { isMinecraft } from "@/lib/games";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { playerNameError, usePlayerAction } from "../players/player-utils";
import { fetchWithPassword, shareKey, useShareCard, type ShareAnswer, type ShareChecklistItem } from "./share-api";

async function copy(text: string, what: string) {
  if (await copyText(text)) toast.success(`${what} copied`);
  else toast.error("Could not access the clipboard. Select the text and copy it manually.");
}

function CopyAction({ label, onCopy, disabled }: { label: string; onCopy: () => Promise<void>; disabled?: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      disabled={disabled || busy}
      onClick={async () => {
        setBusy(true);
        try {
          await onCopy();
        } catch (err) {
          toast.error(errorMessage(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? <Loader2 className="animate-spin" /> : <Copy />} {label}
    </Button>
  );
}

const MARK = {
  pass: { icon: Check, label: "OK", className: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300" },
  fail: { icon: X, label: "Missing", className: "bg-destructive/15 text-destructive" },
  unknown: { icon: CircleHelp, label: "Check yourself", className: "bg-muted text-muted-foreground" },
} as const;

/** Where a failing checklist item is fixed, for users who may fix it there. */
function fixLink(item: ShareChecklistItem, data: ShareAnswer, instance: Instance, mayConfigure: boolean, maySystem: boolean): ReactNode {
  if (item.ok === true) return null;
  const link = (to: string, text: string) => (
    <Link to={to} className="font-medium text-primary hover:underline">
      {text}
    </Link>
  );
  if (item.id === "public_address") return maySystem ? link("/admin/system", "Set it in System settings") : null;
  if (!mayConfigure) return null;
  if (item.id === "protection") {
    if (data.protection.kind === "password" || data.protection.kind === "accounts")
      return link(`/servers/${instance.id}/settings?tab=game-settings`, data.protection.kind === "password" ? "Set a password in Secrets" : "Open Game settings");
    if (data.protection.kind === "whitelist") return link(`/servers/${instance.id}/players`, "Open the Players page");
    return null;
  }
  if (item.id === "online_mode") return link(`/servers/${instance.id}/settings?tab=properties`, "Turn online mode on in Properties");
  if (item.id === "ports") return link(`/servers/${instance.id}/network`, "See the Network page");
  return null;
}

function Checklist({ data, instance, mayConfigure, maySystem }: { data: ShareAnswer; instance: Instance; mayConfigure: boolean; maySystem: boolean }) {
  return (
    <ul className="grid gap-2" aria-label="Checklist">
      {data.checklist.map((item) => {
        const mark = item.ok === true ? MARK.pass : item.ok === false ? MARK.fail : MARK.unknown;
        const Icon = mark.icon;
        const fix = fixLink(item, data, instance, mayConfigure, maySystem);
        return (
          <li key={item.id} className="flex items-start gap-2.5 text-sm">
            <span className={cn("mt-0.5 grid size-5 shrink-0 place-items-center rounded-full [&_svg]:size-3.5", mark.className)} title={mark.label}>
              <Icon aria-hidden />
              <span className="sr-only">{mark.label}:</span>
            </span>
            <span className="grid min-w-0 gap-0.5">
              <span className={cn(item.ok === false && "font-medium")}>{item.label}</span>
              {item.ok !== true && item.hint && <span className="text-xs text-muted-foreground">{item.hint}</span>}
              {fix && <span className="text-xs">{fix}</span>}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function ProtectionLine({ data }: { data: ShareAnswer }) {
  const p = data.protection;
  // a password game is protected when a join password is set (`password_set`, null = cannot be read now)
  const on = p.kind === "password" && data.password_set !== undefined ? data.password_set : p.on;
  if (p.kind === "none" || on === false)
    return (
      <p className="flex items-start gap-2 text-sm text-amber-800 dark:text-amber-200">
        <LockOpen className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>
          <span className="font-medium">Open: </span>
          {data.open_warning || "anyone who knows the address can join."}
        </span>
      </p>
    );
  if (on === null)
    return (
      <p className="flex items-start gap-2 text-sm text-muted-foreground">
        <CircleHelp className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>Protected by {p.label}? The panel could not check it right now.</span>
      </p>
    );
  return (
    <p className="flex items-start gap-2 text-sm">
      <Lock className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" aria-hidden />
      <span>Protected by {p.label}.</span>
    </p>
  );
}

/** Minecraft: whitelist a friend by their Minecraft name; offer to turn the whitelist on when it is off. */
function AddFriend({ instance, data, mayConfigure }: { instance: Instance; data: ShareAnswer; mayConfigure: boolean }) {
  const id = useId();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [touched, setTouched] = useState(false);
  const act = usePlayerAction(instance.id);
  // protection.on is false also when the whitelist is on but online mode is off (any name can be used): then the
  // whitelist is not what is missing
  const onlineModeOff = data.checklist.some((c) => c.id === "online_mode" && c.ok === false);
  const whitelistOff = data.protection.kind === "whitelist" && data.protection.on === false && !onlineModeOff;
  const turnOn = useMutation({
    mutationFn: () => api.patch(`/instances/${instance.id}/properties`, { values: { "white-list": "true" } }),
    onSuccess: () => {
      toast.success("Whitelist turned on", {
        description: instance.status === "running" ? "Restart the server to apply it." : "Applies the next time the server starts.",
      });
      void qc.invalidateQueries({ queryKey: shareKey(instance.id) });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const error = touched ? playerNameError(name) : null;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (playerNameError(name)) return;
    act.mutate(
      { action: "whitelist_add", name },
      {
        onSuccess: () => {
          setName("");
          setTouched(false);
          void qc.invalidateQueries({ queryKey: shareKey(instance.id) });
        },
      },
    );
  };
  return (
    <div className="grid gap-2 rounded-lg border bg-muted/30 p-3">
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
        <div className="grid min-w-44 flex-1 gap-1">
          <Label htmlFor={`${id}-name`}>Add a friend (their Minecraft name)</Label>
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={!!error || undefined}
          />
        </div>
        <Button type="submit" size="sm" disabled={act.isPending}>
          {act.isPending ? <Loader2 className="animate-spin" /> : <UserPlus />} Add to whitelist
        </Button>
      </form>
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
      {onlineModeOff && (
        <p className="text-xs text-muted-foreground">
          Online mode is off: anyone can join under any name, also a name on the whitelist.
        </p>
      )}
      {whitelistOff && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>The whitelist is off, so anyone can join.</span>
          {mayConfigure && (
            <Button type="button" size="xs" variant="outline" onClick={() => turnOn.mutate()} disabled={turnOn.isPending}>
              {turnOn.isPending ? <Loader2 className="animate-spin" /> : <Lock />} Turn the whitelist on
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

export function ShareContent({ instance, enabled = true }: { instance: Instance; enabled?: boolean }) {
  const { can: canGlobal } = useAuth();
  const query = useShareCard(instance.id, enabled);
  const [withPassword, setWithPassword] = useState(false);
  const checkboxId = useId();
  if (query.isLoading) return <LoadingState label="Reading how friends join…" className="py-6" />;
  if (query.error || !query.data)
    return <ErrorState error={query.error ?? new Error("No data")} title="Could not read the share details" onRetry={() => query.refetch()} />;
  const data = query.data;
  const limited = !!instance.limited;
  const mayConfigure = !limited && instance.permissions.includes("instance.configure");
  const maySystem = !limited && canGlobal("system.settings");
  const passwordGame = data.protection.kind === "password";
  const mayCopyPassword = mayConfigure && passwordGame && data.password_set === true;
  const mayAddFriend = !limited && isMinecraft(instance) && instance.permissions.includes("instance.players");

  return (
    <div className="grid gap-5">
      <div className="grid gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Address</h3>
        {data.connect ? (
          <div className="flex flex-wrap items-center gap-2">
            <code className="rounded-md border bg-background px-2.5 py-1.5 font-mono text-base select-all" aria-label="Address friends type">
              {data.connect}
            </code>
            <CopyAction label="Copy address" onCopy={() => copy(data.connect!, "Address")} />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">{data.address.note || "No public address is set yet."}</p>
        )}
        {data.ports.length > 0 && (
          <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            Ports:
            {data.ports.map((p) => (
              <span key={`${p.protocol}-${p.port}`} className="rounded border bg-background px-1.5 py-0.5 font-mono text-foreground">
                {p.protocol.toUpperCase()} {p.port}
              </span>
            ))}
          </p>
        )}
      </div>

      <div className="grid gap-2">
        <ProtectionLine data={data} />
        {mayConfigure && passwordGame && data.password_set !== undefined && (
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <KeyRound className="size-4 text-muted-foreground" aria-hidden />
            {data.password_set === true ? (
              <>
                <span>Join password: ••••••••</span>
                <CopyAction
                  label="Copy password"
                  onCopy={async () => {
                    const { password } = await fetchWithPassword(instance.id);
                    if (!password) throw new Error("No join password is set");
                    await copy(password, "Password");
                  }}
                />
              </>
            ) : data.password_set === false ? (
              <span className="text-muted-foreground">No join password is set.</span>
            ) : (
              <span className="text-muted-foreground">The join password could not be read right now.</span>
            )}
          </div>
        )}
      </div>

      <div className="grid gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">How to join</h3>
        {data.requirements.length > 0 && <p className="text-sm">You need: {data.requirements.join("; ")}.</p>}
        <ol className="grid list-decimal gap-1 pl-5 text-sm" aria-label="Join steps">
          {data.steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
      </div>

      <div className="grid gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Checklist</h3>
        <Checklist data={data} instance={instance} mayConfigure={mayConfigure} maySystem={maySystem} />
      </div>

      {mayAddFriend && <AddFriend instance={instance} data={data} mayConfigure={mayConfigure} />}

      <div className="flex flex-wrap items-center gap-3 border-t pt-4">
        <Button
          type="button"
          onClick={async () => {
            try {
              const text = withPassword && mayCopyPassword ? (await fetchWithPassword(instance.id)).inviteText : data.invite_text;
              await copy(text, "Invite");
            } catch (err) {
              toast.error(errorMessage(err));
            }
          }}
        >
          <Send /> Copy invite
        </Button>
        {mayCopyPassword && (
          <div className="flex items-center gap-2">
            <Checkbox id={checkboxId} checked={withPassword} onCheckedChange={(v) => setWithPassword(v === true)} />
            <Label htmlFor={checkboxId} className="text-sm font-normal">
              Include the password
            </Label>
          </div>
        )}
        <span className="text-xs text-muted-foreground">A ready message to paste into Discord or a chat.</span>
      </div>
    </div>
  );
}
