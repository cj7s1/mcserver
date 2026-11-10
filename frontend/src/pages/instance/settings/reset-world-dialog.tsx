/**
 * "Reset world…" (Settings → Danger zone): step 1 shows what is deleted and kept and takes the options
 * (seed, carry-overs, safety backup); step 2 lists what happens and asks for the server name.
 */
import { Eraser, RefreshCw } from "lucide-react";
import { useId, useRef, useState, type FormEvent, type RefObject } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { LoadingState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { ApiError, errorMessage } from "@/lib/api";
import { formatBytes, formatDateTime, relativeTime } from "@/lib/format";
import type { Instance, Job, WorldResetKeep, WorldResetPlan, WorldResetSeedMode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ROLE_LABEL } from "../backups/world-utils";
import { requestWorldReset, useWorldResetPlan } from "./reset-world-api";
import {
  buildResetBody,
  deletedFolders,
  initialForm,
  keepChoices,
  PLAN_OUTDATED_CODES,
  resolveForm,
  seedError,
  seedFromEnv,
  seedIsSet,
  summaryLines,
  type ResetForm,
  type SummaryPart,
} from "./reset-world-model";
import { FieldError, Notice } from "./settings-ui";

const KEEP_TEXT: Record<WorldResetKeep, { label: string; hint: string }> = {
  datapacks: { label: "Datapacks", hint: "The new world is generated with the datapacks of the old one." },
  config: { label: "World configuration", hint: "Per-world settings of mods and Paper stay as they are." },
};

const OPTION_CARD =
  "rounded-lg border p-3 has-data-checked:border-primary/50 has-data-checked:bg-primary/5 has-data-disabled:opacity-60";

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

function Code({ children }: { children: string }) {
  return <code className="font-mono text-foreground [overflow-wrap:anywhere]">{children}</code>;
}

function Parts({ parts }: { parts: SummaryPart[] }) {
  return parts.map((part, i) => (typeof part === "string" ? part : <Code key={i}>{part.code}</Code>));
}

/** Step 1 once the plan allows a reset: what is deleted and kept, then the options. */
function ResetOptions({
  instance,
  plan,
  form,
  onChange,
  seedProblem,
  onSeedBlur,
  onNavigate,
}: {
  instance: Instance;
  plan: WorldResetPlan;
  form: ResetForm;
  onChange: (patch: Partial<ResetForm>) => void;
  /** shown under the custom seed field (null: nothing to show yet) */
  seedProblem: string | null;
  onSeedBlur: () => void;
  onNavigate: () => void;
}) {
  const uid = useId();
  const deleted = deletedFolders(plan);
  const choices = keepChoices(plan);
  const envSeed = seedFromEnv(plan);
  const hasSeed = seedIsSet(plan);
  const several = deleted.length > 1;

  return (
    <>
      <div className="grid gap-2">
        <h3 id={`${uid}-deleted`} className="text-sm font-medium text-destructive">
          Deleted
        </h3>
        <ul className="divide-y rounded-lg border border-destructive/30 px-3" aria-labelledby={`${uid}-deleted`}>
          {deleted.map(({ path, info }) => (
            <li key={path} className="grid gap-0.5 py-2">
              <div className="flex min-w-0 items-baseline gap-2">
                {plan.layout === "bukkit" && info && (
                  <span className="w-16 shrink-0 text-xs text-muted-foreground">{ROLE_LABEL[info.role]}</span>
                )}
                <code className="min-w-0 font-mono text-sm [overflow-wrap:anywhere]">{path}</code>
              </div>
              {info && (
                <p className="text-xs text-muted-foreground tabular-nums">
                  {formatBytes(info.bytes)} · {info.files.toLocaleString()} {info.files === 1 ? "file" : "files"}
                  {info.modified != null && (
                    <>
                      {" · last changed "}
                      <span title={formatDateTime(info.modified)}>{relativeTime(info.modified)}</span>
                    </>
                  )}
                </p>
              )}
            </li>
          ))}
        </ul>
        <ul className="list-disc space-y-1 pl-5 text-xs text-muted-foreground">
          <li>
            {plan.layout === "bukkit"
              ? "Paper-style server: the Nether and the End have their own folders, and each one that exists is listed above."
              : "All dimensions are inside this folder: the Overworld, the Nether, the End and any that mods or datapacks added."}
          </li>
          <li>Player data goes with the world: inventories, ender chests, positions, advancements and statistics.</li>
          <li>So does everything that mods, plugins and datapacks store inside the world {several ? "folders" : "folder"}.</li>
        </ul>
      </div>

      <div className="grid gap-1">
        <h3 className="text-sm font-medium">Kept</h3>
        <p className="text-xs text-muted-foreground">
          Everything outside the world {several ? "folders" : "folder"}: the server settings, mods, plugins and their configuration,
          the whitelist, operators, bans, and all backups.
        </p>
      </div>

      <Notice tone="warning" title="Not reset with the world">
        <ul className="list-disc space-y-1 pl-4">
          <li>
            Plugin and mod data stored outside the world (live maps, land claims, homes, block logs) keeps pointing at places
            in the old world.
          </li>
          <li>Other worlds on this server, for example Multiverse worlds, are not touched.</li>
          {plan.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      </Notice>

      <fieldset className="grid min-w-0 gap-2">
        <legend id={`${uid}-seed`} className="mb-2 text-sm font-medium">
          Seed of the new world
        </legend>
        {envSeed && (
          <p id={`${uid}-seed-env`} className="text-xs text-muted-foreground">
            The seed is set by the <Code>SEED</Code> environment variable, which is applied on every start. To choose another
            seed, change or remove it under{" "}
            <Link
              to={`/servers/${instance.id}/settings?tab=environment`}
              onClick={onNavigate}
              className="font-medium text-foreground underline underline-offset-3"
            >
              Settings → Environment
            </Link>{" "}
            first.
          </p>
        )}
        <RadioGroup
          value={form.seedMode}
          onValueChange={(v) => onChange({ seedMode: v as WorldResetSeedMode })}
          disabled={envSeed}
          aria-labelledby={`${uid}-seed`}
          aria-describedby={envSeed ? `${uid}-seed-env` : undefined}
          className="gap-2"
        >
          <label htmlFor={`${uid}-seed-random`} className={cn(OPTION_CARD, "flex items-start gap-3", !envSeed && "cursor-pointer")}>
            <RadioGroupItem id={`${uid}-seed-random`} value="random" className="mt-0.5" />
            <span className="grid min-w-0 gap-0.5">
              <span className="text-sm font-medium">Random seed</span>
              <span className="text-xs text-muted-foreground">Minecraft picks a new seed, so the terrain is new.</span>
            </span>
          </label>
          {(hasSeed || envSeed) && (
            <label htmlFor={`${uid}-seed-keep`} className={cn(OPTION_CARD, "flex items-start gap-3", !envSeed && "cursor-pointer")}>
              <RadioGroupItem id={`${uid}-seed-keep`} value="keep" className="mt-0.5" />
              <span className="grid min-w-0 gap-0.5">
                <span className="text-sm font-medium">
                  Keep the current seed setting (
                  <Code>{`${envSeed ? "SEED" : "level-seed"}: ${plan.seed.value || "empty"}`}</Code>)
                </span>
                <span className="text-xs text-muted-foreground">
                  {hasSeed ? "The same seed generates the same terrain again." : "An empty seed setting gives the new world a random seed."}
                </span>
              </span>
            </label>
          )}
          <div className={OPTION_CARD}>
            <label htmlFor={`${uid}-seed-custom`} className={cn("flex items-start gap-3", !envSeed && "cursor-pointer")}>
              <RadioGroupItem id={`${uid}-seed-custom`} value="custom" className="mt-0.5" />
              <span className="text-sm font-medium">Custom seed</span>
            </label>
            {form.seedMode === "custom" && (
              <div className="mt-2 grid gap-1.5 pl-7">
                <Input
                  id={`${uid}-seed-value`}
                  aria-label="Seed"
                  value={form.seed}
                  onChange={(e) => onChange({ seed: e.target.value })}
                  onBlur={onSeedBlur}
                  autoComplete="off"
                  autoCapitalize="none"
                  spellCheck={false}
                  aria-invalid={!!seedProblem}
                  aria-describedby={seedProblem ? `${uid}-seed-hint ${uid}-seed-error` : `${uid}-seed-hint`}
                  className="font-mono"
                />
                <p id={`${uid}-seed-hint`} className="text-xs text-muted-foreground">
                  A number or any text, up to 200 characters.
                </p>
                <FieldError id={`${uid}-seed-error`} message={seedProblem} />
              </div>
            )}
          </div>
        </RadioGroup>
        {!hasSeed && !envSeed && (
          <p className="text-xs text-muted-foreground">
            No seed is set in server.properties. To generate the same terrain again, run <Code>seed</Code> in the console while
            the server is running and enter the number it prints as a custom seed.
          </p>
        )}
        {plan.level_type && (
          <p className="text-xs text-muted-foreground">
            The world type is not changed (<Code>{`level-type: ${plan.level_type}`}</Code>).
          </p>
        )}
      </fieldset>

      {choices.length > 0 && (
        <fieldset className="grid min-w-0 gap-2">
          <legend className="mb-2 text-sm font-medium">Carry over into the new world</legend>
          {choices.map(({ option, paths }) => (
            <label key={option} htmlFor={`${uid}-keep-${option}`} className="flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 text-sm">
              <Checkbox
                id={`${uid}-keep-${option}`}
                checked={form.keep[option]}
                onCheckedChange={(v) => onChange({ keep: { ...form.keep, [option]: v === true } })}
                className="mt-0.5"
              />
              <span className="grid min-w-0 gap-0.5">
                <span className="font-medium">{KEEP_TEXT[option].label}</span>
                <span className="text-xs text-muted-foreground">
                  {KEEP_TEXT[option].hint}{" "}
                  {paths.map((path, i) => (
                    <span key={path}>
                      {i > 0 && ", "}
                      <Code>{path}</Code>
                    </span>
                  ))}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
      )}

      <div className="grid gap-2">
        <label htmlFor={`${uid}-backup`} className="flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 text-sm">
          <Checkbox
            id={`${uid}-backup`}
            checked={form.backup}
            onCheckedChange={(v) => onChange({ backup: v === true })}
            aria-describedby={`${uid}-backup-hint`}
            className="mt-0.5"
          />
          <span className="grid min-w-0 gap-0.5">
            <span className="font-medium">Take a safety backup of the whole server folder first (recommended)</span>
            <span id={`${uid}-backup-hint`} className="text-xs text-muted-foreground">
              If it fails or does not contain the world, nothing is deleted.
              {form.backup && (
                <>
                  {" "}
                  {plan.safety_backup_keep_days === 0
                    ? "Safety backups are deleted by the next backup; pin it to keep it."
                    : `Kept ${plural(plan.safety_backup_keep_days, "day")}.`}
                  {plan.backup_exclusions > 0 &&
                    ` This backup ignores the server's ${plural(plan.backup_exclusions, "backup exclusion")}, so it can be larger than usual.`}
                </>
              )}
            </span>
          </span>
        </label>
        {!form.backup && (
          <p className="text-xs font-medium text-destructive" role="alert">
            Without a safety backup the world cannot be recovered after the reset.
          </p>
        )}
      </div>
    </>
  );
}

export function ResetWorldDialog({
  instance,
  onClose,
  onStarted,
  returnFocusRef,
}: {
  instance: Instance;
  onClose: () => void;
  onStarted: (job: Job) => void;
  /** The button that opened the dialog; focus goes back to it when the dialog closes. */
  returnFocusRef?: RefObject<HTMLElement | null>;
}) {
  const query = useWorldResetPlan(instance.id);
  const { refetch } = query;
  const plan = query.data;
  // null until the user changes an option: the defaults then follow the plan
  const [edited, setEdited] = useState<ResetForm | null>(null);
  // the plan and options as they were when the user pressed Continue: exactly this is confirmed and sent
  const [confirming, setConfirming] = useState<{ plan: WorldResetPlan; form: ResetForm } | null>(null);
  const [seedTouched, setSeedTouched] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  // why the confirmation was refused and the options are shown again
  const [outdated, setOutdated] = useState<string | null>(null);
  // why the request failed and the confirmation is still open (the toast is gone after a few seconds)
  const [submitError, setSubmitError] = useState<string | null>(null);
  const closing = useRef(false);

  const form = plan ? resolveForm(plan, edited ?? initialForm(plan)) : null;
  const seedInvalid = form ? seedError(form) : null;
  // "enter a seed" only once the field was left empty; other problems as soon as they are typed
  const seedProblem = seedInvalid && (seedTouched || form?.seed.trim()) ? seedInvalid : null;
  const busy = instance.active_job;
  const loading = query.isLoading || rechecking;
  const failed = !loading && query.isError;
  const ready = !loading && !failed && !!plan && !!form;
  const canContinue = ready && plan.can_reset && plan.delete.length > 0 && !query.isFetching && !seedInvalid && !busy;

  const close = () => {
    closing.current = true;
    onClose();
  };

  // Both steps are dialogs of their own, so each "closes" when the other replaces it: focus only
  // moves back to the opener when the whole flow ends.
  const handleCloseAutoFocus = (event: Event) => {
    if (!closing.current) {
      event.preventDefault();
      return;
    }
    const opener = returnFocusRef?.current;
    if (opener?.isConnected) {
      event.preventDefault();
      opener.focus({ preventScroll: true });
    }
  };

  const recheck = () => {
    setRechecking(true);
    void refetch().finally(() => setRechecking(false));
  };

  const submit = async () => {
    if (!confirming) return;
    setSubmitError(null);
    try {
      const job = await requestWorldReset(instance.id, {
        ...buildResetBody(confirming.plan, confirming.form),
        confirm_name: instance.name,
      });
      toast.success("Resetting the world…");
      onStarted(job);
    } catch (err) {
      toast.error(errorMessage(err));
      if (err instanceof ApiError && PLAN_OUTDATED_CODES.includes(err.code)) {
        // what was confirmed no longer matches the server: read it again and start over at the options
        setOutdated(errorMessage(err));
        setConfirming(null);
        recheck();
      } else {
        setSubmitError(errorMessage(err));
      }
      throw err; // keeps the confirmation open
    }
  };

  if (confirming) {
    return (
      <ConfirmDialog
        open
        onOpenChange={(o) => !o && close()}
        onCloseAutoFocus={handleCloseAutoFocus}
        contentClassName="max-h-[90svh] overflow-y-auto [overflow-wrap:anywhere]"
        title={`Reset the world of ${instance.name}?`}
        destructive
        confirmLabel="Reset world"
        confirmText={instance.name}
        description={
          <ol className="grid list-decimal gap-1.5 pl-5 text-sm">
            {summaryLines(confirming.plan, confirming.form).map((line) => (
              <li key={line.id} className={line.danger ? "font-medium text-destructive" : undefined}>
                <Parts parts={line.parts} />
              </li>
            ))}
          </ol>
        }
        onConfirm={submit}
      >
        {submitError && (
          <Notice tone="danger" title="The reset was not started">
            {submitError}
          </Notice>
        )}
      </ConfirmDialog>
    );
  }

  const next = (e: FormEvent) => {
    e.preventDefault();
    if (!canContinue || !plan || !form) return;
    setOutdated(null);
    setSubmitError(null);
    setConfirming({ plan, form });
  };

  return (
    <Dialog open onOpenChange={(o) => !o && close()}>
      <DialogContent
        className="max-h-[90svh] overflow-y-auto sm:max-w-lg"
        // the options arrive after the dialog opened: focus the dialog itself, so Tab starts at its first control
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          (event.target as HTMLElement | null)?.focus({ preventScroll: true });
        }}
        onCloseAutoFocus={handleCloseAutoFocus}
      >
        <form onSubmit={next} className="grid min-w-0 gap-4 [overflow-wrap:anywhere]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Eraser className="size-4 text-muted-foreground" aria-hidden /> Reset world
            </DialogTitle>
            <DialogDescription>
              Deletes the world of <span className="font-medium text-foreground">{instance.name}</span> so the server generates a
              new one the next time it starts.
            </DialogDescription>
          </DialogHeader>

          {loading ? (
            <LoadingState label="Checking the world folder…" className="py-8" />
          ) : failed || !plan || !form ? (
            <Notice tone="danger" title="Could not check the world">
              <div className="grid justify-items-start gap-2">
                <p>{errorMessage(query.error ?? "The server did not return a plan.")}</p>
                <Button type="button" variant="outline" size="sm" onClick={recheck}>
                  <RefreshCw /> Retry
                </Button>
              </div>
            </Notice>
          ) : (
            <>
              {outdated && plan.can_reset && (
                <Notice tone="warning" title="Nothing was reset">
                  {outdated} The details below were read again.
                </Notice>
              )}
              {!plan.can_reset || plan.delete.length === 0 ? (
                <Notice tone="danger" title="The world cannot be reset">
                  <div className="grid justify-items-start gap-2">
                    {plan.blockers.length > 0 ? (
                      <ul className={cn("space-y-1", plan.blockers.length > 1 && "list-disc pl-4")}>
                        {plan.blockers.map((blocker, i) => (
                          // two folders can be refused for the same reason
                          <li key={`${blocker.code}-${i}`}>{blocker.message}</li>
                        ))}
                      </ul>
                    ) : (
                      <p>There is no world folder to delete.</p>
                    )}
                    <Button type="button" variant="outline" size="sm" onClick={recheck}>
                      <RefreshCw /> Check again
                    </Button>
                  </div>
                </Notice>
              ) : (
                <ResetOptions
                  instance={instance}
                  plan={plan}
                  form={form}
                  onChange={(patch) => setEdited({ ...form, ...patch })}
                  seedProblem={seedProblem}
                  onSeedBlur={() => setSeedTouched(true)}
                  onNavigate={close}
                />
              )}
              {busy && (
                <Notice>
                  Another operation is running on this server ({busy.title}). The world can be reset when it finishes.
                </Notice>
              )}
            </>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" variant="destructive" disabled={!canContinue}>
              Continue
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
