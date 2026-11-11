/**
 * The create wizard after the Game step for a game other than Minecraft (gameWizardModel.ts): every
 * step comes from the game's catalog entry, so the next games need no wizard code of their own.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, KeyRound, Loader2, Rocket } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";
import { ConfigFieldsForm } from "@/components/ConfigFieldsForm";
import { PageHeader, Section } from "@/components/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, errorMessage } from "@/lib/api";
import { formatMemoryMb } from "@/lib/format";
import { displayValue, fromText } from "@/lib/game-config";
import { memoryWording } from "@/lib/games";
import { OvercommitDeclined, postOrDecline } from "@/lib/power";
import { qk } from "@/lib/queries";
import type { GameInfo } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useGameVersionCatalog } from "../instance/game-api";
import { mpk } from "../modpacks/modpackQueries";
import { useNodePorts, useWizardNodes } from "./import/nodes";
import {
  buildGamePayload,
  defaultName,
  effectiveVersion,
  GAME_WIZARD_STEPS,
  gameServerErrors,
  gameStepForField,
  initialFields,
  initialGameState,
  memoryMb,
  secretRequiredNow,
  validateGameStep,
  variantView,
  wizardVariants,
  type GameWizardState,
} from "./gameWizardModel";
import { FixedVersion } from "./FixedVersion";
import { sharedPortsLabel } from "./shared-ports";
import { Field, Stepper, SummaryList } from "./WizardBits";
import type { CreateResponse, FieldErrors, StepId } from "./wizardModel";

const STEP_IDS = GAME_WIZARD_STEPS.map((s) => s.id);

function notesText(game: GameInfo): string {
  return Array.isArray(game.notes) ? game.notes.join(" ") : game.notes || "";
}

/** Errors of one config file's fields (keys "settings.<file>.<key>" -> "<key>"). */
function fileErrors(errors: FieldErrors, file: string): Record<string, string> {
  const prefix = `settings.${file}.`;
  return Object.fromEntries(Object.entries(errors).filter(([k]) => k.startsWith(prefix)).map(([k, v]) => [k.slice(prefix.length), v]));
}

export function GameWizard({
  game: catalogGame,
  nightlyBackupAllowed,
  onBackToGame,
}: {
  game: GameInfo;
  nightlyBackupAllowed: boolean;
  onBackToGame: () => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [state, setState] = useState<GameWizardState>(() => initialGameState(catalogGame));
  // a game with variants (Terraria): everything below is what the chosen variant has
  const variants = wizardVariants(catalogGame);
  const variantInfo = variants.find((v) => v.id === state.variant);
  const game = variantView(catalogGame, state.variant);
  const [step, setStep] = useState<StepId>("software");
  const [reached, setReached] = useState(1);
  const [attempted, setAttempted] = useState<Set<StepId>>(() => new Set());
  const [serverErrors, setServerErrors] = useState<FieldErrors>({});
  const headingRef = useRef<HTMLHeadingElement>(null);
  const versions = useGameVersionCatalog(game.id, game.has_versions !== false && !game.fixed_version, state.variant);
  // a game without versions to choose (Satisfactory follows Steam's stable branch) sends its fixed id
  const version = game.fixed_version?.id ?? effectiveVersion(state, versions.data);
  const nodes = useWizardNodes();
  const ports = useNodePorts(state.node);
  const idx = STEP_IDS.indexOf(step);
  const isLast = idx === STEP_IDS.length - 1;
  const wording = memoryWording(game.memory_model);

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
  }, [step]);

  const patch = (p: Partial<GameWizardState>) => {
    setState((s) => ({ ...s, ...p }));
    setServerErrors({});
  };
  const chooseVariant = (variant: string) => {
    // another variant has other versions, options, settings and secrets: start those over
    const fresh = initialGameState(catalogGame, variant);
    setState((s) => ({ ...fresh, name: s.name, hostIp: s.hostIp, ports: s.ports, node: s.node, description: s.description, group: s.group, start: s.start, nightlyBackup: s.nightlyBackup }));
    setServerErrors({});
  };
  const clientErrors = attempted.has(step) ? validateGameStep(step, game, state, version) : {};
  const errors: FieldErrors = { ...serverErrors, ...clientErrors };

  const goTo = (s: StepId) => {
    if (s === "game") return onBackToGame();
    setStep(s);
    setReached((r) => Math.max(r, STEP_IDS.indexOf(s)));
  };
  const next = () => {
    if (Object.keys(validateGameStep(step, game, state, version)).length) {
      setAttempted((a) => new Set(a).add(step));
      return;
    }
    goTo(STEP_IDS[idx + 1]);
  };
  const back = () => (idx <= 1 ? onBackToGame() : setStep(STEP_IDS[idx - 1]));

  const create = useMutation({
    mutationFn: () =>
      postOrDecline<CreateResponse>("/instances/", { id: "", name: state.name || defaultName(game, version) }, {
        ...buildGamePayload(game, state, version, nightlyBackupAllowed),
      }),
    onSuccess: (res) => {
      toast.success(`Deploying ${res.instance.name}`, {
        description: state.start ? "Installing now; the server starts when the install finishes." : "Installing now; it will stay stopped.",
      });
      for (const w of res.warnings ?? []) toast.warning(w);
      qc.setQueryData(qk.instance(res.instance.id), res.instance);
      qc.invalidateQueries({ queryKey: qk.instances });
      qc.invalidateQueries({ queryKey: qk.jobs });
      qc.invalidateQueries({ queryKey: mpk.ports });
      navigate(`/servers/${res.instance.id}`);
    },
    onError: (err) => {
      if (err instanceof OvercommitDeclined) return;
      toast.error(errorMessage(err));
      let errs: FieldErrors = {};
      if (err instanceof ApiError && (err.code === "port_conflict" || err.code === "no_free_ports")) {
        const name = (err.details?.port_name as string | undefined) ?? game.ports?.[0]?.name;
        errs = { [name ? `ports.${name}` : "form"]: err.message };
        qc.invalidateQueries({ queryKey: mpk.ports });
      } else if (err instanceof ApiError && Object.keys(err.fieldErrors).length) {
        errs = gameServerErrors(err.fieldErrors);
      } else {
        errs = { form: errorMessage(err) };
      }
      setServerErrors(errs);
      const first = STEP_IDS.find((s) => Object.keys(errs).some((k) => gameStepForField(k) === s));
      if (first && first !== step && first !== "game") setStep(first);
    },
  });

  const deploy = () => {
    for (const s of STEP_IDS.slice(1)) {
      if (Object.keys(validateGameStep(s, game, state, version)).length) {
        setAttempted((a) => new Set(a).add(s));
        setStep(s);
        return;
      }
    }
    create.mutate();
  };

  const settingsFiles = initialFields(game);
  const nodeList = nodes.data ?? [];
  const mem = memoryMb(game, state);

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 pb-2">
      <PageHeader
        title="Create server"
        description={`A ${game.label} server: pick the version, size it, set it up and deploy. The install runs in the background.`}
        actions={
          <Button asChild variant="ghost" size="sm">
            <Link to="/servers">Cancel</Link>
          </Button>
        }
        className="pb-0"
      />
      <Stepper steps={GAME_WIZARD_STEPS} current={step} maxReached={reached} onGo={goTo} />
      <h2 ref={headingRef} tabIndex={-1} className="sr-only">
        Step {idx + 1} of {STEP_IDS.length}: {GAME_WIZARD_STEPS[idx].label}
      </h2>

      {step === "software" && (
        <Section
          title={variants.length ? `${game.label} variant and version` : `${game.label} version`}
          description={variants.length ? "Players need exactly the same game version as the server." : notesText(game) || "Players need exactly the same version as the server."}
        >
          <div className="grid gap-5">
            {variants.length > 0 && (
              <div className="grid gap-2">
                <Field id="game-variant" label="Variant" error={errors.variant} hint="Fixed once the server exists.">
                  <Select value={state.variant ?? ""} onValueChange={chooseVariant}>
                    <SelectTrigger id="game-variant" className="w-full sm:max-w-xs">
                      <SelectValue placeholder="Choose a variant" />
                    </SelectTrigger>
                    <SelectContent>
                      {variants.map((v) => (
                        <SelectItem key={v.id} value={v.id}>
                          {v.label}
                          {v.game_version ? ` (${catalogGame.label} ${v.game_version})` : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                {variantInfo?.game_version && (
                  <p className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200" role="note">
                    {variantInfo.label} runs on {catalogGame.label} {variantInfo.game_version}: players need the {variantInfo.label} client, and
                    vanilla clients on newer {catalogGame.label} versions cannot join.
                  </p>
                )}
                {(variantInfo?.notes ?? []).length > 0 && (
                  <ul className="grid list-disc gap-1 pl-5 text-xs text-muted-foreground" aria-label={`About ${variantInfo?.label}`}>
                    {(variantInfo?.notes ?? []).map((n) => (
                      <li key={n}>{n}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {game.fixed_version ? (
              <FixedVersion info={game.fixed_version} />
            ) : game.has_versions === false || versions.isError ? (
              <Field
                id="game-version"
                label="Version"
                error={errors.version}
                hint={versions.isError ? `The version list could not be loaded (${errorMessage(versions.error)}); enter an exact version.` : "The exact version, e.g. 2.1.24"}
              >
                <Input id="game-version" value={state.version ?? ""} onChange={(e) => patch({ version: e.target.value })} />
              </Field>
            ) : (
              <Field id="game-version" label="Version" error={errors.version} hint="Pinned: the server stays on it until you update it in Settings → Software.">
                <Select value={version} onValueChange={(v) => patch({ version: v })} disabled={versions.isLoading}>
                  <SelectTrigger id="game-version" className="w-full sm:max-w-xs">
                    <SelectValue placeholder={versions.isLoading ? "Loading versions…" : "Choose a version"} />
                  </SelectTrigger>
                  <SelectContent>
                    {(versions.data?.versions ?? []).map((v) => (
                      <SelectItem key={v.id} value={v.id}>
                        {v.label || v.id}
                        {v.latest ? " (latest)" : !v.stable ? " (experimental)" : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            )}
            {(game.options ?? []).length > 0 && (
              <div className="grid gap-2">
                <ConfigFieldsForm
                  fields={game.options ?? []}
                  texts={state.options}
                  onChange={(k, t) => patch({ options: { ...state.options, [k]: t } })}
                  errors={Object.fromEntries(Object.entries(errors).filter(([k]) => k.startsWith("options.")).map(([k, v]) => [k.slice(8), v]))}
                  idPrefix="game-option"
                />
                {(game.options ?? []).some((o) => o.pinned) && (
                  <p className="text-xs text-muted-foreground">These choices are fixed once the server exists: changing them later could damage its saves.</p>
                )}
              </div>
            )}
          </div>
        </Section>
      )}

      {step === "resources" && (
        <div className="grid gap-4">
          <Section title="Server">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field id="game-name" label="Name" error={errors.name}>
                <Input id="game-name" value={state.name} placeholder={defaultName(game, version)} onChange={(e) => patch({ name: e.target.value })} maxLength={80} />
              </Field>
              {nodeList.length > 1 && (
                <Field id="game-node" label="Node">
                  <Select value={state.node === null ? "default" : String(state.node)} onValueChange={(v) => patch({ node: v === "default" ? null : Number(v) })}>
                    <SelectTrigger id="game-node" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="default">Default node</SelectItem>
                      {nodeList.map((n) => (
                        <SelectItem key={n.id} value={String(n.id)}>
                          {n.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
              )}
            </div>
          </Section>
          <Section title={wording.fieldLabel} description={wording.wizardDescription}>
            <Field id="game-memory" label={wording.inputLabel} error={errors.memoryMb} hint={`Default for ${game.label}: ${formatMemoryMb(game.default_memory_mb)}.`}>
              <Input
                id="game-memory"
                inputMode="numeric"
                className="sm:max-w-40"
                value={state.memory ?? String(game.default_memory_mb)}
                onChange={(e) => patch({ memory: e.target.value })}
              />
            </Field>
          </Section>
          <Section title="Network" description="Host ports players connect to. Empty = the game's usual port when it is free, else the next free one.">
            <div className="grid gap-4">
              {/* a port that always has another one's number (Satisfactory's API on TCP) is not asked for */}
              {(game.ports ?? []).filter((p) => !p.same_port_as).map((p) => {
                const id = `game-port-${p.name}`;
                const t = (state.ports[p.name] ?? "").trim();
                const used = t ? ports.data?.in_use.find((u) => u.port === Number(t)) : undefined;
                return (
                  <Field
                    key={p.name}
                    id={id}
                    label={
                      <span className="flex items-center gap-2">
                        {p.label}
                        <Badge variant="outline" className="font-mono uppercase">
                          {sharedPortsLabel(game.ports ?? [], p)}
                        </Badge>
                      </span>
                    }
                    error={errors[`ports.${p.name}`]}
                    hint={used ? `Port ${t} is in use by ${used.used_by} (fine if that is the other protocol).` : p.help || `Container port ${p.container_port}/${p.protocol}; usually ${p.default_host_port}.`}
                  >
                    <Input
                      id={id}
                      inputMode="numeric"
                      className="sm:max-w-40"
                      placeholder={`Auto (${p.default_host_port})`}
                      value={state.ports[p.name] ?? ""}
                      onChange={(e) => patch({ ports: { ...state.ports, [p.name]: e.target.value } })}
                    />
                  </Field>
                );
              })}
              <Field id="game-bind" label="Bind address" error={errors.hostIp} hint="Empty = the panel's default (usually every interface).">
                <Input id="game-bind" className="sm:max-w-60" value={state.hostIp} onChange={(e) => patch({ hostIp: e.target.value })} placeholder="0.0.0.0" />
              </Field>
            </div>
          </Section>
        </div>
      )}

      {step === "settings" && (
        <div className="grid gap-4">
          {settingsFiles.map((f) => (
            <Section key={f.file} title={f.label} description="Everything else can be changed later in Settings → Game settings.">
              <ConfigFieldsForm
                fields={f.fields}
                texts={state.settings[f.file] ?? {}}
                onChange={(k, t) => patch({ settings: { ...state.settings, [f.file]: { ...state.settings[f.file], [k]: t } } })}
                errors={fileErrors(errors, f.file)}
                idPrefix={`game-setting-${f.file}`}
              />
            </Section>
          ))}
          {errors.settings && (
            <p className="text-xs text-destructive" role="alert">
              {errors.settings}
            </p>
          )}
          <Section title="Description">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field id="game-description" label="Description" optional error={errors.description}>
                <Textarea id="game-description" value={state.description} onChange={(e) => patch({ description: e.target.value })} rows={2} />
              </Field>
              <Field id="game-group" label="Group" optional error={errors.group}>
                <Input id="game-group" value={state.group} onChange={(e) => patch({ group: e.target.value })} maxLength={60} />
              </Field>
            </div>
          </Section>
        </div>
      )}

      {step === "review" && (
        <div className="grid gap-4">
          <Section title="Summary">
            <SummaryList
              rows={[
                ["Game", game.label],
                ...(variantInfo ? [["Variant", variantInfo.label] as [string, string]] : []),
                ["Version", (versions.data?.versions.find((v) => v.id === version)?.label ?? version) || "—"],
                ...(game.options ?? []).map((o): [string, string] => {
                  const parsed = fromText(o, state.options[o.key] ?? "");
                  return [o.label, "error" in parsed ? "—" : displayValue(o, parsed.value)];
                }),
                ["Name", state.name.trim() || defaultName(game, version)],
                ["Memory", Number.isFinite(mem) ? formatMemoryMb(mem) : "—"],
                ...(game.ports ?? []).filter((p) => !p.same_port_as).map((p): [string, string] => [
                  `${p.label} (${sharedPortsLabel(game.ports ?? [], p).toUpperCase()})`,
                  (state.ports[p.name] ?? "").trim() || `automatic (${p.default_host_port} when free)`,
                ]),
                ["Bind address", state.hostIp.trim() || "Panel default"],
              ]}
            />
          </Section>
          {(game.secrets ?? []).length > 0 && (
            <Section title="Secrets" description="Stored encrypted; never shown again. You can also enter them later in Settings → Game settings.">
              <div className="grid gap-4">
                {(game.secrets ?? []).map((s) => (
                  <Field
                    key={s.key}
                    id={`game-secret-${s.key}`}
                    label={
                      <span className="flex items-center gap-1.5">
                        <KeyRound className="size-3.5" aria-hidden /> {s.label}
                      </span>
                    }
                    optional={!s.required && !secretRequiredNow(s, state)}
                    error={errors[`secrets.${s.key}`]}
                    hint={s.help}
                  >
                    <Input
                      id={`game-secret-${s.key}`}
                      type="password"
                      autoComplete="new-password"
                      value={state.secrets[s.key] ?? ""}
                      onChange={(e) => patch({ secrets: { ...state.secrets, [s.key]: e.target.value } })}
                    />
                  </Field>
                ))}
              </div>
            </Section>
          )}
          {(game.setup_steps ?? []).length > 0 && (
            <Section title="Setup after the install">
              <ol className="grid list-decimal gap-1 pl-5 text-sm">
                {(game.setup_steps ?? []).map((s) => (
                  <li key={s.id}>
                    <span className="font-medium">{s.label}</span>
                    {s.help ? <span className="text-muted-foreground"> — {s.help}</span> : null}
                  </li>
                ))}
              </ol>
              <p className="mt-2 text-xs text-muted-foreground">The Overview shows “Setup needed” until these are done.</p>
            </Section>
          )}
          <Section title="Deploy">
            <div className="grid gap-3">
              <div className="flex items-center justify-between gap-4">
                <Label htmlFor="game-start">Start the server after the install</Label>
                <Switch id="game-start" checked={state.start} onCheckedChange={(v) => patch({ start: v })} />
              </div>
              {nightlyBackupAllowed && (
                <div className="flex items-center justify-between gap-4">
                  <Label htmlFor="game-nightly">Nightly backup</Label>
                  <Switch id="game-nightly" checked={state.nightlyBackup} onCheckedChange={(v) => patch({ nightlyBackup: v })} />
                </div>
              )}
              {notesText(game) && <p className="text-xs text-muted-foreground">{notesText(game)}</p>}
            </div>
          </Section>
        </div>
      )}

      {errors.form && (
        <p className="text-sm text-destructive" role="alert">
          {errors.form}
        </p>
      )}

      <div className="sticky bottom-0 z-20 -mx-3 flex items-center gap-2 border-t bg-background/90 px-3 py-3 backdrop-blur md:-mx-6 md:px-6">
        <Button type="button" variant="outline" onClick={back} disabled={create.isPending}>
          <ArrowLeft /> Back
        </Button>
        <div className={cn("min-w-0 flex-1 truncate text-center text-xs text-muted-foreground")} />
        {isLast ? (
          <Button type="button" onClick={deploy} disabled={create.isPending} className="min-w-28">
            {create.isPending ? <Loader2 className="animate-spin" /> : <Rocket />}
            {create.isPending ? "Deploying…" : "Deploy server"}
          </Button>
        ) : (
          <Button type="button" onClick={next}>
            {STEP_IDS[idx + 1] === "review" ? "Review" : "Next"} <ArrowRight />
          </Button>
        )}
      </div>
    </div>
  );
}
