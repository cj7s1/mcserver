/** Extra container environment variables (instance.env -> PATCH /instances/{id} {env}). */
import { ExternalLink, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { errorMessage } from "@/lib/api";
import { FieldError, fieldErrorsOf, Notice, ReadOnlyNotice, SaveBar, usePatchInstance, useReportDirty, type TabProps } from "./settings-ui";

interface Row {
  id: number;
  key: string;
  value: string;
}

const KEY_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
let rowSeq = 0;

function toRows(env: Record<string, string>): Row[] {
  return Object.entries(env).map(([key, value]) => ({ id: ++rowSeq, key, value }));
}

function toEnv(rows: Row[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) if (r.key.trim()) out[r.key.trim()] = r.value;
  return out;
}

function sameEnv(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => k in b && a[k] === b[k]);
}

export function EnvTab({ instance, can, onDirtyChange }: TabProps) {
  const canEdit = can("instance.configure");
  const patch = usePatchInstance(instance.id);
  const [rows, setRows] = useState<Row[] | null>(null);
  // Serialised so background refetches (new object, same content) do not rebuild the rows.
  const envKey = JSON.stringify(instance.env ?? {});
  const shown = useMemo(() => rows ?? toRows(JSON.parse(envKey) as Record<string, string>), [rows, envKey]);
  const next = useMemo(() => toEnv(shown), [shown]);
  const current = instance.env ?? {};
  const dirty = rows !== null && !sameEnv(next, current);
  const changeCount = dirty ? new Set([...Object.keys(next), ...Object.keys(current)].filter((k) => next[k] !== current[k])).size : 0;
  useReportDirty(dirty, onDirtyChange);

  const rowErrors = useMemo(() => {
    const seen = new Map<string, number>();
    const out: Record<number, string> = {};
    for (const r of shown) {
      const k = r.key.trim();
      if (!k) {
        if (r.value) out[r.id] = "Enter a variable name";
        continue;
      }
      if (!KEY_RE.test(k)) out[r.id] = "Letters, digits and _ only; cannot start with a digit";
      else if (seen.has(k)) out[r.id] = "Duplicate name";
      else if (r.value.length > 8192) out[r.id] = "Value too long (max 8192 characters)";
      seen.set(k, r.id);
    }
    return out;
  }, [shown]);
  const hasErrors = Object.keys(rowErrors).length > 0;
  const serverError = fieldErrorsOf(patch.error).env ?? null;

  const edit = (fn: (list: Row[]) => Row[]) => {
    setRows(fn(shown));
    if (patch.error) patch.reset();
  };

  return (
    <div className="grid grid-cols-1 gap-4">
      {!canEdit && <ReadOnlyNotice what="edit environment variables" />}
      <Section
        title="Environment variables"
        description="Advanced: extra variables passed to the itzg/minecraft-server container. Applied when the container is recreated (restart)."
        actions={
          <a
            href="https://docker-minecraft-server.readthedocs.io/en/latest/variables/"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
          >
            Variable reference <ExternalLink className="size-3" aria-hidden />
          </a>
        }
      >
        <div className="grid grid-cols-1 gap-3">
          <Notice>
            Settings MCPanel manages itself (server type, version, memory, JVM flags, EULA, ports and RCON) are rejected here; use
            the dedicated settings instead. Values are visible to everyone who can configure this server.
          </Notice>
          {serverError && (
            <Notice tone="danger" title="The server rejected these variables">
              {serverError}
            </Notice>
          )}
          {shown.length === 0 ? (
            <p className="py-2 text-sm text-muted-foreground">No extra variables are set.</p>
          ) : (
            <div className="-mx-4 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-y bg-muted/30 text-xs text-muted-foreground">
                  <tr>
                    <th scope="col" className="h-8 w-2/5 px-4 text-left font-medium">
                      Name
                    </th>
                    <th scope="col" className="h-8 px-2 text-left font-medium">
                      Value
                    </th>
                    {canEdit && (
                      <th scope="col" className="h-8 w-12 px-2">
                        <span className="sr-only">Remove</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r, i) => (
                    <tr key={r.id} className="border-b align-top last:border-b-0">
                      <td className="px-4 py-1.5">
                        <Input
                          aria-label={`Variable ${i + 1} name`}
                          value={r.key}
                          onChange={(e) => edit((list) => list.map((x) => (x.id === r.id ? { ...x, key: e.target.value } : x)))}
                          placeholder="NAME"
                          className="h-7 min-w-36 font-mono text-xs"
                          disabled={!canEdit || patch.isPending}
                          aria-invalid={!!rowErrors[r.id] || undefined}
                          spellCheck={false}
                          autoComplete="off"
                        />
                        <FieldError message={rowErrors[r.id]} />
                      </td>
                      <td className="px-2 py-1.5">
                        <Input
                          aria-label={`Variable ${r.key || i + 1} value`}
                          value={r.value}
                          onChange={(e) => edit((list) => list.map((x) => (x.id === r.id ? { ...x, value: e.target.value } : x)))}
                          placeholder="value"
                          className="h-7 min-w-44 font-mono text-xs"
                          disabled={!canEdit || patch.isPending}
                          spellCheck={false}
                          autoComplete="off"
                        />
                      </td>
                      {canEdit && (
                        <td className="px-2 py-1.5 text-right">
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`Remove ${r.key || `variable ${i + 1}`}`}
                            onClick={() => edit((list) => list.filter((x) => x.id !== r.id))}
                            disabled={patch.isPending}
                          >
                            <Trash2 />
                          </Button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {canEdit && (
            <Button
              variant="outline"
              size="sm"
              className="justify-self-start"
              onClick={() => edit((list) => [...list, { id: ++rowSeq, key: "", value: "" }])}
              disabled={patch.isPending || shown.length >= 100}
            >
              <Plus /> Add variable
            </Button>
          )}
        </div>
      </Section>
      {canEdit && (
        <SaveBar
          count={changeCount}
          saving={patch.isPending}
          disabled={hasErrors}
          note={hasErrors ? "Fix the highlighted names first" : "Applied when the server restarts"}
          onDiscard={() => {
            setRows(null);
            patch.reset();
          }}
          onSave={() =>
            patch.mutate(
              { env: next },
              {
                onSuccess: () => {
                  setRows(null);
                  toast.success("Environment variables saved. Restart the server to apply them.");
                },
                onError: (e) => toast.error(errorMessage(e)),
              },
            )
          }
        />
      )}
    </div>
  );
}
