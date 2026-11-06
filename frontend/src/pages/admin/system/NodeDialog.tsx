/** Add / edit a node (POST /nodes, PATCH /nodes/{id}) with the TLS certificate reader (POST /nodes/tls-probe). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleX, Eye, EyeOff, FileKey2, Info, Loader2, ShieldAlert, TriangleAlert } from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import { Label } from "@/components/ui/label";
import { ApiError, api, errorMessage } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { AdminCallout, AdminCopyButton, AdminFieldError, AdminHint } from "../admin-shared";
import {
  agentUrlError,
  certValidity,
  checkSummary,
  EMPTY_NODE_FORM,
  isHttpsUrl,
  MIN_TOKEN_LENGTH,
  NODE_COMPOSE_FINGERPRINT_CMD,
  nodePayload,
  nodesKey,
  nodeToForm,
  normalizeFingerprint,
  sameFingerprint,
  validateNodeForm,
  type NodeCheckResult,
  type NodeFormState,
  type PanelNodeRecord,
  type TlsProbeResult,
} from "./nodes-model";

/** Form order, with the input each field error belongs to. */
const FIELD_IDS: Record<keyof NodeFormState, string> = {
  name: "nd-name",
  agent_url: "nd-url",
  agent_token: "nd-token",
  tls_fingerprint: "nd-fp",
  lan_address: "nd-lan",
  public_address: "nd-public",
};
const FIELD_KEYS = Object.keys(FIELD_IDS) as (keyof NodeFormState)[];

/** Move focus (and the dialog's scroll position) to the first field with an error. */
function focusFirstError(errs: Record<string, string>) {
  const key = FIELD_KEYS.find((k) => errs[k]);
  if (key) requestAnimationFrame(() => document.getElementById(FIELD_IDS[key])?.focus());
}

export function NodeDialog({
  open,
  onOpenChange,
  node,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The node to edit; omitted to add a new one. */
  node?: PanelNodeRecord;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open && <NodeDialogBody key={node?.id ?? "new"} node={node} onClose={() => onOpenChange(false)} />}
    </Dialog>
  );
}

function NodeDialogBody({ node, onClose }: { node?: PanelNodeRecord; onClose: () => void }) {
  const qc = useQueryClient();
  const editing = !!node;
  const managed = !!node?.use_settings_token;
  const [form, setForm] = useState<NodeFormState>(() => (node ? nodeToForm(node) : EMPTY_NODE_FORM));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [showToken, setShowToken] = useState(false);
  const [probe, setProbe] = useState<{ url: string; result: TlsProbeResult } | null>(null);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [compared, setCompared] = useState(false);

  const set = <K extends keyof NodeFormState>(key: K, value: NodeFormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    if (errors[key]) setErrors((e) => ({ ...e, [key]: "" }));
    if (key === "agent_url") {
      setProbe(null);
      setProbeError(null);
      setCompared(false);
    }
  };

  const save = useMutation({
    mutationFn: (body: Record<string, string>) =>
      node ? api.patch<PanelNodeRecord>(`/nodes/${node.id}`, body) : api.post<PanelNodeRecord>("/nodes", body),
    onSuccess: async (saved) => {
      qc.invalidateQueries({ queryKey: nodesKey });
      onClose();
      if (editing) {
        toast.success(`Node “${saved.name}” saved`);
        return;
      }
      // a new node: check right away that the agent answers with this token
      const id = toast.loading(`Node “${saved.name}” added. Checking the agent…`);
      try {
        const r = await api.post<NodeCheckResult>(`/nodes/${saved.id}/check`);
        if (r.ok) toast.success(`Node “${saved.name}” added and reachable`, { id, description: checkSummary(r.info) });
        else toast.error(`Node “${saved.name}” added, but the agent did not answer`, { id, description: r.error });
      } catch (err) {
        toast.error(`Node “${saved.name}” added, but the check failed`, { id, description: errorMessage(err) });
      } finally {
        qc.invalidateQueries({ queryKey: nodesKey });
      }
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        const entries = Object.entries(err.fieldErrors);
        const isField = (k: string) => FIELD_KEYS.includes(k as keyof NodeFormState);
        const onFields = Object.fromEntries(entries.filter(([k]) => isField(k)));
        const rest = entries.filter(([k]) => !isField(k)).map(([, v]) => v);
        setErrors(onFields);
        focusFirstError(onFields);
        if (rest.length) setFormError(rest.join(" "));
        else setFormError(Object.keys(onFields).length ? null : err.display);
        toast.error(Object.keys(onFields).length ? "Check the highlighted fields" : err.display);
      } else {
        setFormError(errorMessage(err));
        toast.error(errorMessage(err));
      }
    },
  });

  const readCert = useMutation({
    mutationFn: (url: string) => api.post<TlsProbeResult>("/nodes/tls-probe", { agent_url: url }),
    onMutate: () => {
      setProbe(null);
      setProbeError(null);
      setCompared(false);
    },
    onSuccess: (result, url) => setProbe({ url, result }),
    onError: (err) => setProbeError(errorMessage(err)),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    const errs = validateNodeForm(form, node);
    setErrors(errs);
    setFormError(null);
    if (Object.values(errs).some(Boolean)) {
      focusFirstError(errs);
      return;
    }
    const body = nodePayload(form, node);
    if (editing && Object.keys(body).length === 0) {
      toast.info("Nothing changed");
      onClose();
      return;
    }
    save.mutate(body);
  }

  const urlValid = agentUrlError(form.agent_url) === null;
  const https = isHttpsUrl(form.agent_url);
  const plainHttp = urlValid && !https;
  const busy = save.isPending;
  const err = (k: string) => errors[k] || undefined;

  return (
    <DialogContent
      className="max-h-[90svh] overflow-y-auto sm:max-w-2xl"
      onInteractOutside={(e) => busy && e.preventDefault()}
      onEscapeKeyDown={(e) => busy && e.preventDefault()}
    >
      <form onSubmit={submit} className="grid min-w-0 gap-4" noValidate>
        <DialogHeader>
          <DialogTitle>{editing ? `Edit node “${node.name}”` : "Add a node"}</DialogTitle>
          <DialogDescription>
            A node is another Docker host that runs only the MCPanel agent, started from{" "}
            <span className="font-mono text-xs">deploy/docker-compose.node.yml</span>. The setup steps are in{" "}
            <span className="font-medium">DEPLOYMENT.md</span>, section “Additional nodes”.
          </DialogDescription>
        </DialogHeader>

        {managed && (
          <AdminCallout tone="info" icon={<Info />} title="This node is configured by the panel's environment">
            Its URL, token and certificate pin come from <span className="font-mono">MCPANEL_AGENT_URL</span>,{" "}
            <span className="font-mono">MCPANEL_AGENT_TOKEN</span> and{" "}
            <span className="font-mono">MCPANEL_AGENT_TLS_FINGERPRINT</span>. Change them there and restart the panel. Only
            the connection addresses can be edited here.
          </AdminCallout>
        )}

        <div className="grid gap-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <Field id="nd-name" label="Name" error={err("name")}>
            <Input
              id="nd-name"
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              disabled={managed}
              maxLength={100}
              placeholder="node2"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={!!err("name") || undefined}
              aria-describedby="nd-name-err"
            />
          </Field>
          <Field id="nd-url" label="Agent URL" error={err("agent_url")}>
            <Input
              id="nd-url"
              value={form.agent_url}
              onChange={(e) => set("agent_url", e.target.value)}
              disabled={managed}
              placeholder="https://192.168.1.30:8700"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              aria-invalid={!!err("agent_url") || undefined}
              aria-describedby="nd-url-err"
            />
          </Field>
        </div>

        {plainHttp && !node?.is_default && (
          <AdminCallout tone="warning" icon={<TriangleAlert />} title="Plain HTTP is not safe between hosts">
            The token and console traffic could be read on the network. Run the node with{" "}
            <span className="font-mono">AGENT_TLS=auto</span>, use an <span className="font-mono">https://</span> URL and pin
            its certificate fingerprint.
          </AdminCallout>
        )}

        {!managed && (
          <Field
            id="nd-token"
            label="Agent token"
            error={err("agent_token")}
            hint={
              <>
                <span className="font-mono">AGENT_TOKEN</span> from the node's <span className="font-mono">node.env</span>, at
                least {MIN_TOKEN_LENGTH} characters. Stored encrypted and never shown again.
                {editing && " Leave blank to keep the current token."}
              </>
            }
          >
            <InputGroup>
              <InputGroupInput
                id="nd-token"
                type={showToken ? "text" : "password"}
                value={form.agent_token}
                onChange={(e) => set("agent_token", e.target.value)}
                placeholder={editing ? "Unchanged" : "Paste the node's AGENT_TOKEN"}
                autoComplete="new-password"
                spellCheck={false}
                maxLength={500}
                className="font-mono"
                aria-invalid={!!err("agent_token") || undefined}
                aria-describedby="nd-token-hint nd-token-err"
              />
              <InputGroupAddon align="inline-end">
                <InputGroupButton
                  size="icon-xs"
                  aria-label={showToken ? "Hide token" : "Show token"}
                  aria-pressed={showToken}
                  onClick={() => setShowToken((v) => !v)}
                >
                  {showToken ? <EyeOff /> : <Eye />}
                </InputGroupButton>
              </InputGroupAddon>
            </InputGroup>
          </Field>
        )}

        {!managed && (
          <div className="grid min-w-0 gap-2">
            <Field
              id="nd-fp"
              label={
                <>
                  TLS certificate fingerprint <span className="font-normal text-muted-foreground">(SHA-256)</span>
                </>
              }
              error={err("tls_fingerprint")}
              hint={
                https ? (
                  <>
                    Printed by <span className="font-mono">python -m mcagent fingerprint</span> on the node. Leave it empty
                    only if the agent's certificate is issued by a CA the panel trusts.
                  </>
                ) : (
                  "Only for https:// agent URLs."
                )
              }
            >
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  id="nd-fp"
                  value={form.tls_fingerprint}
                  onChange={(e) => set("tls_fingerprint", e.target.value)}
                  placeholder="AB:CD:EF:…"
                  autoComplete="off"
                  spellCheck={false}
                  className="min-w-0 flex-1 font-mono text-xs"
                  aria-invalid={!!err("tls_fingerprint") || undefined}
                  aria-describedby="nd-fp-hint nd-fp-err"
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={!https || readCert.isPending}
                  onClick={() => readCert.mutate(form.agent_url.trim().replace(/\/+$/, ""))}
                  title={https ? "Read the certificate the agent presents" : "Enter an https:// agent URL first"}
                  className="shrink-0"
                >
                  {readCert.isPending ? <Loader2 className="animate-spin" /> : <FileKey2 />}
                  Read certificate
                </Button>
              </div>
            </Field>
            {probeError && (
              <AdminCallout tone="danger" icon={<CircleX />} title="Couldn't read the certificate">
                {probeError}
              </AdminCallout>
            )}
            {probe && (
              <ProbeResult
                probe={probe.result}
                url={probe.url}
                entered={form.tls_fingerprint}
                compared={compared}
                onComparedChange={setCompared}
                onUse={() => set("tls_fingerprint", probe.result.fingerprint)}
              />
            )}
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field
            id="nd-lan"
            label={
              <>
                LAN address <span className="font-normal text-muted-foreground">(optional)</span>
              </>
            }
            error={err("lan_address")}
            hint="Shown to players on your network for servers on this node."
          >
            <Input
              id="nd-lan"
              value={form.lan_address}
              onChange={(e) => set("lan_address", e.target.value)}
              placeholder="192.168.1.30"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={!!err("lan_address") || undefined}
              aria-describedby="nd-lan-hint nd-lan-err"
            />
          </Field>
          <Field
            id="nd-public"
            label={
              <>
                Public address <span className="font-normal text-muted-foreground">(optional)</span>
              </>
            }
            error={err("public_address")}
            hint="Hostname or IP players outside your network use."
          >
            <Input
              id="nd-public"
              value={form.public_address}
              onChange={(e) => set("public_address", e.target.value)}
              placeholder="mc2.example.com"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={!!err("public_address") || undefined}
              aria-describedby="nd-public-hint nd-public-err"
            />
          </Field>
        </div>

        {formError && (
          <AdminCallout tone="danger" icon={<CircleX />}>
            {formError}
          </AdminCallout>
        )}

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy && <Loader2 className="animate-spin" />}
            {editing ? "Save node" : "Add node"}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  );
}

function Field({
  id,
  label,
  error,
  hint,
  children,
}: {
  id: string;
  label: ReactNode;
  error?: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && (
        <AdminHint>
          <span id={`${id}-hint`}>{hint}</span>
        </AdminHint>
      )}
      <AdminFieldError id={`${id}-err`} message={error} />
    </div>
  );
}

function ProbeResult({
  probe,
  url,
  entered,
  compared,
  onComparedChange,
  onUse,
}: {
  probe: TlsProbeResult;
  url: string;
  entered: string;
  compared: boolean;
  onComparedChange: (v: boolean) => void;
  onUse: () => void;
}) {
  const validity = certValidity(probe);
  const hasEntered = normalizeFingerprint(entered) !== null;
  const matches = hasEntered && sameFingerprint(entered, probe.fingerprint);
  let host = url;
  try {
    host = new URL(url).host;
  } catch {
    /* keep the raw URL */
  }
  return (
    <div className="grid min-w-0 gap-3 rounded-lg border bg-muted/30 p-3" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">
          Certificate presented by <span className="font-mono text-xs break-all">{host}</span>
        </p>
        <div className="flex flex-wrap gap-1">
          <Badge variant="outline">{probe.self_signed ? "Self-signed" : "CA-issued"}</Badge>
          {validity === "expired" && <Badge variant="destructive">Expired</Badge>}
          {validity === "not_yet_valid" && <Badge variant="destructive">Not yet valid</Badge>}
        </div>
      </div>

      <dl className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-2 text-xs sm:grid-cols-[8rem_minmax(0,1fr)]">
        <dt className="text-muted-foreground">Fingerprint</dt>
        <dd className="min-w-0 font-mono text-[11px] leading-relaxed break-all">{probe.fingerprint}</dd>
        <dt className="text-muted-foreground">Subject</dt>
        <dd className="min-w-0 break-all">{probe.subject || "—"}</dd>
        {!probe.self_signed && (
          <>
            <dt className="text-muted-foreground">Issuer</dt>
            <dd className="min-w-0 break-all">{probe.issuer || "—"}</dd>
          </>
        )}
        <dt className="text-muted-foreground">Valid</dt>
        <dd className={cn("min-w-0", validity !== "valid" && validity !== "unknown" && "text-destructive")}>
          {formatDateTime(probe.not_before)} – {formatDateTime(probe.not_after)}
        </dd>
        <dt className="text-muted-foreground">Names</dt>
        <dd className="flex min-w-0 flex-wrap gap-1">
          {probe.names.length ? (
            probe.names.map((n) => (
              <Badge key={n} variant="secondary" className="h-auto max-w-full font-mono break-all whitespace-normal">
                {n}
              </Badge>
            ))
          ) : (
            <span className="text-muted-foreground">None</span>
          )}
        </dd>
      </dl>

      {hasEntered &&
        (matches ? (
          <p className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-700 dark:text-emerald-300">
            <CircleCheck className="size-3.5" aria-hidden /> Matches the fingerprint entered above.
          </p>
        ) : (
          <p className="inline-flex items-start gap-1.5 text-xs font-medium text-destructive">
            <CircleX className="mt-px size-3.5 shrink-0" aria-hidden /> Does not match the fingerprint entered above. Don't
            save until you know why.
          </p>
        ))}

      <AdminCallout tone="warning" icon={<ShieldAlert />} title="Compare it on the node before trusting it">
        <p>
          This is what the panel received over the network; someone in between could present their own certificate. On
          the node, in the <span className="font-mono">deploy</span> folder, run:
        </p>
        <div className="mt-1.5 flex min-w-0 items-start gap-2">
          <code className="min-w-0 flex-1 rounded bg-background/70 px-2 py-1 font-mono text-[11px] break-all text-foreground">
            {NODE_COMPOSE_FINGERPRINT_CMD}
          </code>
          <AdminCopyButton value={NODE_COMPOSE_FINGERPRINT_CMD} label="Copy" className="shrink-0" />
        </div>
        <p className="mt-1.5">and check that it prints exactly this fingerprint.</p>
      </AdminCallout>

      {!matches && (
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-2">
            <Checkbox
              id="nd-compared"
              checked={compared}
              onCheckedChange={(c) => onComparedChange(c === true)}
              className="mt-0.5"
            />
            <Label htmlFor="nd-compared" className="text-xs leading-snug font-normal">
              The node printed exactly this fingerprint
            </Label>
          </div>
          <Button type="button" size="sm" variant="outline" disabled={!compared} onClick={onUse} className="self-start sm:self-auto">
            Use this fingerprint
          </Button>
        </div>
      )}
    </div>
  );
}
