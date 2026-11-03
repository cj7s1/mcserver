/** Retention policy + exclude patterns (PUT /instances/{id}/backups/policy). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Lock, Plus, Save, Undo2, X } from "lucide-react";
import { useState, type KeyboardEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, ApiError, errorMessage } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Backup, RetentionPolicy } from "@/lib/types";
import { BUILTIN_EXCLUDES, type BackupsResponse } from "./backup-utils";

type NumKey = Exclude<keyof RetentionPolicy, "excludes">;

const NUM_FIELDS: { key: NumKey; label: string; hint: string; min: number }[] = [
  { key: "keep_last", label: "Keep last", hint: "Most recent backups always kept", min: 1 },
  { key: "keep_daily", label: "Daily", hint: "Newest backup of each of the last N days", min: 0 },
  { key: "keep_weekly", label: "Weekly", hint: "Newest backup of each of the last N weeks", min: 0 },
  { key: "keep_monthly", label: "Monthly", hint: "Newest backup of each of the last N months", min: 0 },
  { key: "keep_safety_days", label: "Safety backups (days)", hint: "How long before-restore/update backups are kept", min: 0 },
];

const PATTERN = /^[A-Za-z0-9_.:/*@-]{1,200}$/;

function patternError(p: string, existing: string[]): string | null {
  if (!p) return null;
  if (!PATTERN.test(p)) return "Use letters, digits and _ . : / * @ - only (no spaces)";
  if (p.includes("..")) return "Patterns may not contain ..";
  if (existing.includes(p)) return "Already in the list";
  if (existing.length >= 50) return "Up to 50 patterns";
  return null;
}

/** ISO week key in UTC, mirroring Python's date.isocalendar()[:2] on the UTC timestamp. */
function isoWeekKey(d: Date): string {
  const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const yearStart = Date.UTC(t.getUTCFullYear(), 0, 1);
  const week = Math.ceil(((t.getTime() - yearStart) / 86_400_000 + 1) / 7);
  return `${t.getUTCFullYear()}-W${week}`;
}

/** Port of backend/apps/backups/retention.py select_to_delete (UTC buckets, like the backend). */
function selectToDelete(backups: Backup[], p: RetentionPolicy, now = Date.now()): Backup[] {
  const ordered = backups
    .filter((b) => b.status === "succeeded")
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  const keep = new Set<string>();
  const regular: Backup[] = [];
  for (const b of ordered) {
    if (b.pinned) keep.add(b.id);
    else if (b.kind === "pre_restore" || b.kind === "pre_update") {
      if (now - new Date(b.created_at).getTime() <= p.keep_safety_days * 86_400_000) keep.add(b.id);
    } else regular.push(b);
  }
  regular.slice(0, p.keep_last).forEach((b) => keep.add(b.id));
  const bucket = (count: number, key: (d: Date) => string) => {
    const seen = new Set<string>();
    for (const b of regular) {
      const k = key(new Date(b.created_at));
      if (seen.has(k)) continue;
      if (seen.size >= count) break;
      seen.add(k);
      keep.add(b.id);
    }
  };
  if (p.keep_daily) bucket(p.keep_daily, (d) => d.toISOString().slice(0, 10));
  if (p.keep_weekly) bucket(p.keep_weekly, isoWeekKey);
  if (p.keep_monthly) bucket(p.keep_monthly, (d) => d.toISOString().slice(0, 7));
  return ordered.filter((b) => !keep.has(b.id));
}

export function RetentionPolicyForm({
  instanceId,
  policy,
  backups,
  canEdit,
}: {
  instanceId: string;
  policy: RetentionPolicy;
  backups: Backup[];
  canEdit: boolean;
}) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Partial<Record<NumKey, string>>>({});
  const [excludes, setExcludes] = useState<string[] | null>(null);
  const [pattern, setPattern] = useState("");

  const currentExcludes = excludes ?? policy.excludes;
  const value = (k: NumKey) => draft[k] ?? String(policy[k]);
  const parsed: RetentionPolicy = {
    keep_last: Number(value("keep_last")),
    keep_daily: Number(value("keep_daily")),
    keep_weekly: Number(value("keep_weekly")),
    keep_monthly: Number(value("keep_monthly")),
    keep_safety_days: Number(value("keep_safety_days")),
    excludes: currentExcludes,
  };
  const errors: Partial<Record<NumKey, string>> = {};
  for (const f of NUM_FIELDS) {
    const raw = value(f.key).trim();
    const n = Number(raw);
    if (raw === "" || !Number.isInteger(n)) errors[f.key] = "Whole number required";
    else if (n < f.min) errors[f.key] = f.min === 1 ? "Keep at least one backup" : "Cannot be negative";
    else if (n > 100_000) errors[f.key] = "Too large";
  }
  const dirtyNums = NUM_FIELDS.filter((f) => draft[f.key] !== undefined && Number(draft[f.key]) !== policy[f.key]);
  const dirtyExcludes = excludes !== null && JSON.stringify(excludes) !== JSON.stringify(policy.excludes);
  const dirty = dirtyNums.length > 0 || dirtyExcludes;
  const valid = Object.keys(errors).length === 0;

  const save = useMutation({
    mutationFn: () => api.put<RetentionPolicy>(`/instances/${instanceId}/backups/policy`, parsed),
    onSuccess: (p) => {
      qc.setQueryData<BackupsResponse>(qk.backups(instanceId), (old) => (old ? { ...old, policy: p } : old));
      setDraft({});
      setExcludes(null);
      toast.success("Retention policy saved");
    },
    onError: (err) => {
      if (!(err instanceof ApiError && err.code === "validation_error")) toast.error(errorMessage(err));
    },
  });
  const serverErrors = save.error instanceof ApiError ? save.error.fieldErrors : {};

  const preview = valid ? selectToDelete(backups, parsed) : [];
  const pinned = backups.filter((b) => b.pinned && b.status === "succeeded").length;
  const pError = patternError(pattern.trim(), currentExcludes);

  const addPattern = () => {
    const p = pattern.trim();
    if (!p || pError) return;
    setExcludes([...currentExcludes, p]);
    setPattern("");
  };
  const onPatternKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addPattern();
    }
  };
  const discard = () => {
    setDraft({});
    setExcludes(null);
    setPattern("");
    save.reset();
  };

  return (
    <div className="grid gap-5">
      {!canEdit && (
        <p className="flex items-center gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <Lock className="size-3.5 shrink-0" aria-hidden /> Read-only: changing retention needs the “Restore backups” permission.
        </p>
      )}

      <div className="grid gap-3">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {NUM_FIELDS.map((f) => {
            const err = errors[f.key] ?? serverErrors[f.key];
            const id = `retention-${f.key}`;
            return (
              <div key={f.key} className="grid content-start gap-1.5">
                <Label htmlFor={id}>{f.label}</Label>
                <Input
                  id={id}
                  type="number"
                  inputMode="numeric"
                  min={f.min}
                  step={1}
                  value={value(f.key)}
                  disabled={!canEdit || save.isPending}
                  onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))}
                  aria-invalid={!!err || undefined}
                  aria-describedby={`${id}-hint`}
                  className="tabular-nums"
                />
                <p id={`${id}-hint`} className={err ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                  {err ?? f.hint}
                </p>
              </div>
            );
          })}
        </div>
        <div className="rounded-lg border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          <p>
            Retention runs after every successful manual or scheduled backup. A backup is kept if <em>any</em> rule matches it.{" "}
            <strong className="text-foreground">Pinned backups are never removed</strong>
            {pinned ? ` (${pinned} pinned now)` : ""}, and safety backups taken before a restore or update are kept for{" "}
            {valid ? parsed.keep_safety_days : "N"} days without counting toward the rules above. Day, week and month boundaries
            are in UTC.
          </p>
          {valid && (
            <p className="mt-1.5" aria-live="polite">
              {preview.length === 0 ? (
                <>If retention ran now, all {backups.filter((b) => b.status === "succeeded").length} current backups would be kept.</>
              ) : (
                <span className="text-amber-700 dark:text-amber-300">
                  If retention ran now, {preview.length} current backup{preview.length === 1 ? "" : "s"} would be removed:{" "}
                  {preview
                    .slice(0, 5)
                    .map((b) => `“${b.name}”`)
                    .join(", ")}
                  {preview.length > 5 ? ` and ${preview.length - 5} more` : ""}. Pin any you want to keep.
                </span>
              )}
            </p>
          )}
        </div>
      </div>

      <div className="grid gap-2">
        <div>
          <Label htmlFor="exclude-pattern">Excluded paths</Label>
          <p className="mt-0.5 text-xs text-muted-foreground">
            restic exclude patterns, relative to the server folder (e.g. <code className="font-mono">bluemap/web/maps</code>,{" "}
            <code className="font-mono">*.log</code>, <code className="font-mono">dynmap/web/tiles</code>). Excluded files are not
            backed up and are missing after a full restore.
          </p>
        </div>
        <ul className="flex flex-wrap gap-1.5" aria-label="Exclude patterns">
          {currentExcludes.map((p) => (
            <li key={p} className="inline-flex items-center gap-1 rounded-md border bg-background py-0.5 pr-0.5 pl-2 font-mono text-xs">
              {p}
              {canEdit && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="size-5"
                  aria-label={`Remove exclude pattern ${p}`}
                  onClick={() => setExcludes(currentExcludes.filter((x) => x !== p))}
                  disabled={save.isPending}
                >
                  <X />
                </Button>
              )}
            </li>
          ))}
          {currentExcludes.length === 0 && <li className="text-xs text-muted-foreground">No custom excludes.</li>}
        </ul>
        {canEdit && (
          <div className="flex max-w-lg gap-2">
            <div className="grid min-w-0 flex-1 gap-1">
              <Input
                id="exclude-pattern"
                value={pattern}
                onChange={(e) => setPattern(e.target.value)}
                onKeyDown={onPatternKey}
                placeholder="path/or/*.pattern"
                className="font-mono"
                aria-invalid={!!pError || undefined}
                aria-describedby={pError ? "exclude-pattern-err" : undefined}
                autoComplete="off"
                spellCheck={false}
                disabled={save.isPending}
              />
              {(pError || serverErrors.excludes) && (
                <p id="exclude-pattern-err" className="text-xs text-destructive">
                  {pError ?? serverErrors.excludes}
                </p>
              )}
            </div>
            <Button type="button" variant="outline" onClick={addPattern} disabled={!pattern.trim() || !!pError || save.isPending}>
              <Plus /> Add
            </Button>
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          Always excluded:{" "}
          {BUILTIN_EXCLUDES.map((p, i) => (
            <span key={p}>
              <code className="font-mono">{p}</code>
              {i < BUILTIN_EXCLUDES.length - 1 ? ", " : ""}
            </span>
          ))}
          .
        </p>
      </div>

      {canEdit && (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t pt-3">
          {dirty && <span className="mr-auto text-xs text-muted-foreground">Unsaved changes</span>}
          <Button variant="outline" size="sm" onClick={discard} disabled={!dirty || save.isPending}>
            <Undo2 /> Discard
          </Button>
          <Button size="sm" onClick={() => save.mutate()} disabled={!dirty || !valid || save.isPending}>
            {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save policy
          </Button>
        </div>
      )}
    </div>
  );
}
