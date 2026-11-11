/**
 * "Game settings" for games with config files of their own (capability config_files; Factorio's
 * server-settings.json, map settings, ...): one form per file from the definition's field schema
 * (GET /instances/{id}/config, PATCH /instances/{id}/config/{file}), and the server's secrets
 * (GET/PUT /instances/{id}/secrets) when its game has any.
 */
import { useMutation } from "@tanstack/react-query";
import { KeyRound, Loader2, RotateCw, Save, Undo2, X } from "lucide-react";
import { useCallback, useState } from "react";
import { toast } from "sonner";
import { ConfigFieldsForm } from "@/components/ConfigFieldsForm";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage, ApiError } from "@/lib/api";
import { diffTexts, initialTexts } from "@/lib/game-config";
import { sendPower } from "@/lib/power";
import { useInvalidateInstance } from "@/lib/queries";
import type { ConfigFileState, Instance } from "@/lib/types";
import { useConfigFiles, useGameInfo, usePatchConfigFile, usePutSecrets, useSecrets } from "../game-api";
import { Notice, ReadOnlyNotice, useReportDirty, type TabProps } from "./settings-ui";

const LIVE = ["running", "starting", "unhealthy", "restarting"];

function RestartNotice({ instance, keys, canControl, onDismiss }: { instance: Instance; keys: string[]; canControl: boolean; onDismiss: () => void }) {
  const invalidate = useInvalidateInstance();
  const restart = useMutation({
    mutationFn: () => sendPower({ id: instance.id, name: instance.name }, { action: "restart" }),
    onSuccess: (outcome) => {
      if (outcome.status === "cancelled") return;
      toast.success("Restart requested");
      invalidate(instance.id);
      onDismiss();
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  return (
    <Notice
      tone="warning"
      title="Restart the server to apply these changes"
      action={
        <>
          {canControl && (
            <Button size="sm" variant="outline" onClick={() => restart.mutate()} disabled={restart.isPending}>
              {restart.isPending ? <Loader2 className="animate-spin" /> : <RotateCw />} Restart now
            </Button>
          )}
          <Button size="icon-sm" variant="ghost" aria-label="Dismiss" onClick={onDismiss}>
            <X />
          </Button>
        </>
      }
    >
      The running server reads these settings when it starts. Changed: <span className="font-mono">{keys.join(", ")}</span>
    </Notice>
  );
}

function FileForm({
  instance,
  file,
  canEdit,
  canControl,
  onDirty,
}: {
  instance: Instance;
  file: ConfigFileState;
  canEdit: boolean;
  canControl: boolean;
  onDirty: (file: string, dirty: boolean) => void;
}) {
  const managed = new Set(file.managed_keys);
  const fields = file.fields.map((f) => (file.redacted_keys.includes(f.key) ? { ...f, secret: true } : f));
  const [base, setBase] = useState(() => initialTexts(fields, file.values));
  const [texts, setTexts] = useState(base);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [restartKeys, setRestartKeys] = useState<string[]>([]);
  const patch = usePatchConfigFile(instance.id);
  const { changes, errors: parseErrors } = diffTexts(fields, base, texts);
  const count = Object.keys(changes).length + Object.keys(parseErrors).length;
  const dirty = count > 0;
  const report = useCallback((d: boolean) => onDirty(file.id, d), [onDirty, file.id]);
  useReportDirty(dirty, report);

  const save = async () => {
    if (Object.keys(parseErrors).length) {
      setErrors(parseErrors);
      return;
    }
    try {
      const res = await patch.mutateAsync({ file: file.id, values: changes });
      const next = initialTexts(fields, res.values);
      setBase(next);
      setTexts(next);
      setErrors({});
      toast.success(`${file.label || file.path} saved`);
      const restartable = res.changed.filter((k) => fields.find((f) => f.key === k)?.restart !== false);
      if (res.restart_required && restartable.length) setRestartKeys(restartable);
    } catch (err) {
      const fe = err instanceof ApiError ? err.fieldErrors : {};
      setErrors(Object.keys(fe).length ? { ...fe } : {});
      toast.error(errorMessage(err));
    }
  };

  return (
    <Section
      title={file.label || file.path}
      description={
        <>
          <span className="font-mono">{file.path}</span>
          {file.help ? ` · ${file.help}` : ""}
          {!file.exists ? " · not created yet: the values shown are the defaults" : ""}
        </>
      }
    >
      <div className="grid grid-cols-1 gap-4">
        {restartKeys.length > 0 && LIVE.includes(instance.status) && (
          <RestartNotice instance={instance} keys={restartKeys} canControl={canControl} onDismiss={() => setRestartKeys([])} />
        )}
        <ConfigFieldsForm
          fields={fields.filter((f) => !managed.has(f.key))}
          texts={texts}
          onChange={(k, t) => {
            setTexts((prev) => ({ ...prev, [k]: t }));
            setErrors((prev) => {
              if (!(k in prev)) return prev;
              const n = { ...prev };
              delete n[k];
              return n;
            });
          }}
          errors={{ ...errors, ...parseErrors }}
          disabled={!canEdit || patch.isPending}
          idPrefix={`cfg-${file.id}`}
          showKeys
        />
        {dirty && canEdit && (
          <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-3">
            <span className="mr-auto text-xs text-muted-foreground">
              {count} unsaved change{count === 1 ? "" : "s"}
            </span>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setTexts(base);
                setErrors({});
              }}
              disabled={patch.isPending}
            >
              <Undo2 /> Discard
            </Button>
            <Button size="sm" onClick={save} disabled={patch.isPending}>
              {patch.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
            </Button>
          </div>
        )}
      </div>
    </Section>
  );
}

/** The server's secrets (tokens the owner provides): set or clear; values are never shown. */
export function SecretsSection({ instance }: { instance: Instance }) {
  const query = useSecrets(instance.id);
  const put = usePutSecrets(instance.id);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  if (query.isLoading) return <LoadingState label="Loading secrets…" className="py-6" />;
  if (query.error) return <ErrorState error={query.error} title="Could not load secrets" onRetry={() => query.refetch()} />;
  const secrets = query.data?.secrets ?? [];
  if (!secrets.length) return null;
  const submit = async (values: Record<string, string | null>, what: string, access?: "open") => {
    try {
      await put.mutateAsync({ values, access });
      setDrafts((d) => {
        const n = { ...d };
        for (const k of Object.keys(values)) delete n[k];
        return n;
      });
      toast.success(what);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };
  return (
    <Section title="Secrets" description="Tokens and passwords this server needs. They are stored encrypted and never shown again.">
      <ul className="grid gap-4">
        {secrets.map((s) => {
          const id = `secret-${s.key}`;
          const draft = drafts[s.key] ?? "";
          return (
            <li key={s.key} className="grid gap-1.5">
              <Label htmlFor={id} className="flex items-center gap-1.5">
                <KeyRound className="size-3.5 text-muted-foreground" aria-hidden />
                {s.label}
                {s.required && <span className="text-xs font-normal text-muted-foreground">(required)</span>}
                <span className={s.set ? "text-xs font-normal text-emerald-700 dark:text-emerald-400" : "text-xs font-normal text-muted-foreground"}>
                  {s.set ? "· set" : "· not set"}
                </span>
              </Label>
              <div className="flex gap-2">
                <Input
                  id={id}
                  type="password"
                  autoComplete="new-password"
                  value={draft}
                  placeholder={s.set ? "Type a new value to replace it" : "Not set"}
                  onChange={(e) => setDrafts((d) => ({ ...d, [s.key]: e.target.value }))}
                  disabled={put.isPending}
                />
                <Button size="sm" onClick={() => submit({ [s.key]: draft }, `${s.label} saved`)} disabled={put.isPending || !draft.trim()}>
                  Save
                </Button>
                {s.set && !s.required && !s.clear_opens && (
                  <Button size="sm" variant="outline" onClick={() => submit({ [s.key]: null }, `${s.label} cleared`)} disabled={put.isPending}>
                    Clear
                  </Button>
                )}
                {s.set && !s.required && s.clear_opens && (
                  <ConfirmDialog
                    trigger={
                      <Button size="sm" variant="outline" disabled={put.isPending}>
                        Clear
                      </Button>
                    }
                    title={`Clear the ${s.label.toLowerCase()} and open the server?`}
                    description="Without it anyone who knows the server's address can join. The change applies while the server runs."
                    confirmLabel="Open the server"
                    destructive
                    onConfirm={() => submit({ [s.key]: null }, `${s.label} cleared: the server is open`, "open")}
                  />
                )}
              </div>
              {s.help && <p className="text-xs text-muted-foreground">{s.help}</p>}
              {s.link && (
                <a href={s.link} target="_blank" rel="noopener noreferrer" className="text-xs text-primary hover:underline">
                  Where to get it
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

export function GameSettingsTab({ instance, can, onDirtyChange }: TabProps) {
  const query = useConfigFiles(instance.id);
  const game = useGameInfo(instance);
  const canEdit = can("instance.configure") && !instance.archived_at;
  const [dirtyFiles, setDirtyFiles] = useState<Record<string, boolean>>({});
  const onDirty = useCallback((file: string, d: boolean) => setDirtyFiles((prev) => (!!prev[file] === d ? prev : { ...prev, [file]: d })), []);
  useReportDirty(Object.values(dirtyFiles).some(Boolean), onDirtyChange);

  return (
    <div className="grid grid-cols-1 gap-4">
      {!can("instance.configure") && <ReadOnlyNotice />}
      {query.isLoading ? (
        <LoadingState label="Loading game settings…" />
      ) : query.error ? (
        <ErrorState error={query.error} title="Could not load the game settings" onRetry={() => query.refetch()} />
      ) : !query.data?.files.length ? (
        <EmptyState title="No settings files" description="This game has no settings the panel can edit; use the Files page." />
      ) : (
        query.data.files.map((f) => (
          <FileForm
            key={`${f.id}:${JSON.stringify(f.values)}`}
            instance={instance}
            file={f}
            canEdit={canEdit}
            canControl={can("instance.control")}
            onDirty={onDirty}
          />
        ))
      )}
      {can("instance.configure") && (game?.secrets?.length ?? 0) > 0 && <SecretsSection instance={instance} />}
    </div>
  );
}
