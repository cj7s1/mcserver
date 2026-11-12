/**
 * Users → Invite a friend (B4): `POST /api/users/invite` creates an account with the Friend role on the chosen
 * servers (see, start, stop and the Share card) and answers a one-time link where the friend picks a password.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link2, Loader2, UserPlus } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, api, errorMessage } from "@/lib/api";
import { qk, useInstances } from "@/lib/queries";
import type { PanelUser } from "@/lib/types";
import { AdminCallout, AdminCopyButton, AdminFieldError, AdminHint } from "../admin-shared";
import { USERNAME_RE } from "./users-model";

interface InviteAnswer {
  user: PanelUser;
  servers: { id: string; name: string }[];
  token: string;
  path: string;
  expires_in_hours: number;
}

export function InviteFriendDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const qc = useQueryClient();
  const uid = useId();
  const instances = useInstances();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [servers, setServers] = useState<string[]>([]);
  const [touched, setTouched] = useState(false);
  const [done, setDone] = useState<{ url: string; hours: number; username: string; servers: string[] } | null>(null);

  const invite = useMutation({
    mutationFn: () => api.post<InviteAnswer>("/users/invite", { username: username.trim(), email: email.trim(), servers }),
    onSuccess: (r) => {
      setDone({ url: `${window.location.origin}${r.path}`, hours: r.expires_in_hours, username: r.user.username, servers: r.servers.map((s) => s.name) });
      qc.invalidateQueries({ queryKey: qk.users });
      toast.success(`${r.user.username} invited`);
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.code === "validation_error")) toast.error(errorMessage(err));
    },
  });
  const server = invite.error instanceof ApiError ? invite.error.fieldErrors : {};
  const errors: Record<string, string> = {};
  if (!username.trim()) errors.username = "Enter a user name";
  else if (!USERNAME_RE.test(username.trim())) errors.username = "Letters, digits and @ . + - _ only";
  if (!servers.length) errors.servers = "Choose at least one server";
  const err = (k: string) => (touched ? errors[k] : undefined) ?? server[k];
  const list = (instances.data ?? []).filter((i) => !i.archived_at && !i.limited);

  const close = (o: boolean) => {
    if (invite.isPending) return;
    if (!o) {
      setDone(null);
      setUsername("");
      setEmail("");
      setServers([]);
      setTouched(false);
      invite.reset();
    }
    onOpenChange(o);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(errors).length) return;
    invite.mutate();
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90svh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Invite a friend</DialogTitle>
          <DialogDescription>
            A friend can see the servers you choose, start and stop them and read their Share card (never the password).
            Nothing else.
          </DialogDescription>
        </DialogHeader>
        {done ? (
          <div className="grid gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor={`${uid}-link`}>Link for {done.username}</Label>
              <div className="flex gap-2">
                <Input id={`${uid}-link`} readOnly value={done.url} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
                <AdminCopyButton value={done.url} size="default" />
              </div>
            </div>
            <AdminCallout tone="warning" icon={<Link2 />} title="Send it privately">
              Your friend opens the link and picks a password there. It works once and for {done.hours} hours; after that, create a
              new link from the user's menu (Reset link). Servers: {done.servers.join(", ")}.
            </AdminCallout>
            <DialogFooter>
              <Button type="button" onClick={() => close(false)}>
                Done
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <form className="grid gap-4" onSubmit={submit} noValidate>
            <div className="grid gap-1.5">
              <Label htmlFor={`${uid}-user`}>User name</Label>
              <Input id={`${uid}-user`} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" aria-invalid={!!err("username") || undefined} />
              <AdminFieldError id={`${uid}-user-err`} message={err("username")} />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor={`${uid}-email`}>E-mail (optional)</Label>
              <Input id={`${uid}-email`} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" aria-invalid={!!server.email || undefined} />
              <AdminFieldError id={`${uid}-email-err`} message={server.email} />
            </div>
            <fieldset className="grid gap-1.5">
              <legend className="text-sm font-medium">Servers</legend>
              {list.length ? (
                <div className="grid max-h-56 gap-1.5 overflow-y-auto rounded-md border p-2">
                  {list.map((i) => {
                    const id = `${uid}-srv-${i.id}`;
                    return (
                      <div key={i.id} className="flex items-center gap-2">
                        <Checkbox
                          id={id}
                          checked={servers.includes(i.id)}
                          onCheckedChange={(v) => setServers((s) => (v === true ? [...s, i.id] : s.filter((x) => x !== i.id)))}
                        />
                        <Label htmlFor={id} className="font-normal">
                          {i.name}
                        </Label>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <AdminHint>{instances.isLoading ? "Loading servers…" : "There is no server to share yet."}</AdminHint>
              )}
              <AdminFieldError id={`${uid}-servers-err`} message={err("servers")} />
            </fieldset>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => close(false)} disabled={invite.isPending}>
                Cancel
              </Button>
              <Button type="submit" disabled={invite.isPending}>
                {invite.isPending ? <Loader2 className="animate-spin" /> : <UserPlus />} Invite
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
