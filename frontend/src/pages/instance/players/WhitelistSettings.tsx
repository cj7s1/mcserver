/** white-list / enforce-whitelist from server.properties, editable with instance.configure. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info } from "lucide-react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { ErrorState, LoadingState } from "@/components/States";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, PropertiesResponse } from "@/lib/types";

interface PatchResult {
  values: Record<string, string>;
  raw: string;
  changed: string[];
  restart_required: boolean;
}

/** itzg env vars that rewrite whitelist/ops state on every start (docs/reference/itzg-runtime.md §10). */
const OVERRIDING_ENV = ["ENABLE_WHITELIST", "WHITELIST", "WHITELIST_FILE", "ENFORCE_WHITELIST", "OPS", "OPS_FILE"];

const isTrue = (v: string | undefined) => (v ?? "").trim().toLowerCase() === "true";

export function WhitelistSettings({
  instance,
  canConfigure,
  running,
  whitelistCount,
}: {
  instance: Instance;
  canConfigure: boolean;
  running: boolean;
  whitelistCount: number;
}) {
  const qc = useQueryClient();
  const key = qk.properties(instance.id);
  const props = useQuery({
    queryKey: key,
    queryFn: () => api.get<PropertiesResponse>(`/instances/${instance.id}/properties`),
    staleTime: 30_000,
    retry: false,
  });
  const save = useMutation({
    mutationFn: (values: Record<string, string>) => api.patch<PatchResult>(`/instances/${instance.id}/properties`, { values }),
    onSuccess: (res, values) => {
      qc.setQueryData<PropertiesResponse>(key, (old) => (old ? { ...old, exists: true, values: res.values, raw: res.raw } : old));
      qc.invalidateQueries({ queryKey: key });
      const [k, v] = Object.entries(values)[0] ?? [];
      const what = k === "white-list" ? "Whitelist" : "Whitelist enforcement";
      toast.success(`${what} ${v === "true" ? "turned on" : "turned off"}`, {
        description: running ? "Saved to server.properties. Restart the server to apply it." : "Applies the next time the server starts.",
      });
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  const envKeys = OVERRIDING_ENV.filter((k) => k in (instance.env ?? {}));
  const editable = canConfigure && !instance.archived_at;

  let body;
  if (props.isLoading) body = <LoadingState label="Reading server.properties…" className="py-6" />;
  else if (props.error || !props.data)
    body = <ErrorState error={props.error ?? new Error("No data")} title="Could not read server.properties" onRetry={() => props.refetch()} className="py-6" />;
  else {
    const values = props.data.values;
    const pending = save.isPending ? (save.variables ?? {}) : {};
    const whitelist = "white-list" in pending ? pending["white-list"] === "true" : isTrue(values["white-list"]);
    const enforce = "enforce-whitelist" in pending ? pending["enforce-whitelist"] === "true" : isTrue(values["enforce-whitelist"]);
    body = (
      <div className="grid gap-4">
        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-0.5">
            <Label htmlFor="players-white-list">Whitelist</Label>
            <p className="text-xs text-muted-foreground">Only players on the whitelist (and operators) can join.</p>
          </div>
          <Switch
            id="players-white-list"
            checked={whitelist}
            disabled={!editable || save.isPending}
            onCheckedChange={(c) => save.mutate({ "white-list": c ? "true" : "false" })}
          />
        </div>
        <div className="flex items-start justify-between gap-4">
          <div className="grid gap-0.5">
            <Label htmlFor="players-enforce-whitelist">Enforce whitelist</Label>
            <p className="text-xs text-muted-foreground">
              Also disconnect players who are online but not on the whitelist when it is turned on or reloaded.
            </p>
          </div>
          <Switch
            id="players-enforce-whitelist"
            checked={enforce}
            disabled={!editable || save.isPending}
            onCheckedChange={(c) => save.mutate({ "enforce-whitelist": c ? "true" : "false" })}
          />
        </div>

        {!whitelist && whitelistCount > 0 && (
          <p className="flex gap-2 text-xs text-amber-700 dark:text-amber-300">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            The whitelist has {whitelistCount} {whitelistCount === 1 ? "player" : "players"} but is turned off, so anyone can join.
          </p>
        )}
        {whitelist && whitelistCount === 0 && (
          <p className="flex gap-2 text-xs text-amber-700 dark:text-amber-300">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            The whitelist is on but empty: only operators can join until you add players.
          </p>
        )}
        {(running || !props.data.exists || envKeys.length > 0 || !canConfigure) && (
          <ul className="grid gap-1.5 text-xs text-muted-foreground">
            {running && (
              <li className="flex gap-2">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                The running server reads these settings at startup; a change here applies after a restart.
              </li>
            )}
            {!props.data.exists && (
              <li className="flex gap-2">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                server.properties does not exist yet; it is created on the first start (saving here creates it early).
              </li>
            )}
            {envKeys.length > 0 && (
              <li className="flex gap-2">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
                <span>
                  Environment variable{envKeys.length === 1 ? "" : "s"} <span className="font-mono">{envKeys.join(", ")}</span>{" "}
                  {envKeys.length === 1 ? "is" : "are"} set in Settings and applied on every start, which can override these settings and
                  the lists.
                </span>
              </li>
            )}
            {!canConfigure && (
              <li className="flex gap-2">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                Changing these needs the “Configure server” permission.
              </li>
            )}
          </ul>
        )}
      </div>
    );
  }

  return (
    <Section title="Whitelist enforcement" description="From server.properties">
      {body}
    </Section>
  );
}
