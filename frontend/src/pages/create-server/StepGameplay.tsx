import { AlertTriangle, Plus, Trash2 } from "lucide-react";
import { useId } from "react";
import { Section } from "@/components/PageHeader";
import { ServerIcon } from "@/components/ServerIcon";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Advanced, ChoiceCard, describedBy, Field } from "./WizardBits";
import type { Derived, EnvRow, FieldErrors, JvmPreset, WizardState } from "./wizardModel";

type Patch = (p: Partial<WizardState>) => void;

const DIFFICULTIES = ["peaceful", "easy", "normal", "hard"];
const GAMEMODES = ["survival", "creative", "adventure", "spectator"];

const PRESETS: { id: JvmPreset; title: string; description: string }[] = [
  { id: "aikar", title: "Aikar's flags", description: "Tuned G1GC settings; the long-standing default for most servers." },
  { id: "meowice", title: "MeowIce flags", description: "Newer tuned flag set for Java 17+ (ignored on older Java)." },
  { id: "none", title: "None", description: "Only the heap size and your extra arguments." },
];

let envSeq = 1;

function EnvEditor({ rows, onChange, errors }: { rows: EnvRow[]; onChange: (rows: EnvRow[]) => void; errors: FieldErrors }) {
  const uid = useId();
  const update = (id: number, p: Partial<EnvRow>) => onChange(rows.map((r) => (r.id === id ? { ...r, ...p } : r)));
  return (
    <div className="grid gap-2">
      <div className="flex items-center justify-between gap-2">
        <span id={`${uid}-title`} className="text-sm font-medium">
          Environment variables <span className="font-normal text-muted-foreground">(optional)</span>
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => onChange([...rows, { id: envSeq++, key: "", value: "" }])}
          disabled={rows.length >= 100}
        >
          <Plus /> Add variable
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Extra settings for the itzg/minecraft-server runtime (e.g. <code className="font-mono">MOTD</code>,{" "}
        <code className="font-mono">VIEW_DISTANCE</code>, <code className="font-mono">TZ</code>). Settings MCPanel manages
        itself (type, version, memory, ports, RCON) are rejected.
      </p>
      {errors.env && (
        <p className="text-xs text-destructive" role="alert">
          {errors.env}
        </p>
      )}
      {rows.length > 0 && (
        <ul className="grid gap-2" aria-labelledby={`${uid}-title`}>
          {rows.map((r, i) => {
            const err = errors[`env.${i}`];
            return (
              <li key={r.id} className="grid gap-1">
                <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
                  <Label htmlFor={`${uid}-k-${r.id}`} className="sr-only">
                    Variable {i + 1} name
                  </Label>
                  <Input
                    id={`${uid}-k-${r.id}`}
                    value={r.key}
                    onChange={(e) => update(r.id, { key: e.target.value.replace(/\s/g, "") })}
                    placeholder="NAME"
                    className="min-w-0 flex-1 font-mono sm:max-w-56"
                    aria-invalid={!!err || undefined}
                    aria-describedby={err ? `${uid}-e-${r.id}` : undefined}
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <span className="hidden text-muted-foreground sm:inline" aria-hidden>
                    =
                  </span>
                  <Label htmlFor={`${uid}-v-${r.id}`} className="sr-only">
                    Variable {i + 1} value
                  </Label>
                  <Input
                    id={`${uid}-v-${r.id}`}
                    value={r.value}
                    onChange={(e) => update(r.id, { value: e.target.value })}
                    placeholder="value"
                    className="min-w-0 flex-[2] basis-40 font-mono"
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Remove variable ${r.key || i + 1}`}
                    onClick={() => onChange(rows.filter((x) => x.id !== r.id))}
                  >
                    <Trash2 />
                  </Button>
                </div>
                {err && (
                  <p id={`${uid}-e-${r.id}`} className="text-xs text-destructive" role="alert">
                    {err}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function StepGameplay({ state, patch, d, errors }: { state: WizardState; patch: Patch; d: Derived; errors: FieldErrors }) {
  const uid = useId();
  const advancedError = ["jvmArgs", "env", "description", "group", "iconUrl", "jvmPreset"].some(
    (k) => errors[k] || Object.keys(errors).some((e) => e.startsWith(`${k}.`)),
  );
  const motdHint = `Shown in the multiplayer server list. Defaults to the server name (“${d.name.slice(0, 59) || "…"}”).`;
  const seedHint = "Only used when the world is first generated. Leave empty for a random world.";
  const jvmHint = "Space-separated options starting with '-'. Heap size comes from the memory setting.";
  const meowiceOld = state.jvmPreset === "meowice" && d.javaVersion !== null && d.javaVersion < 17;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
      <Section
        title="Gameplay basics"
        description="Optional. These become the initial server.properties; everything can be changed later in Settings."
      >
        <div className="grid gap-4">
          <Field id={`${uid}-motd`} label="Message of the day" optional error={errors.motd} hint={motdHint}>
            <Input
              id={`${uid}-motd`}
              value={state.motd}
              onChange={(e) => patch({ motd: e.target.value })}
              placeholder={d.name.slice(0, 59) || "A Minecraft Server"}
              maxLength={512}
              aria-invalid={!!errors.motd || undefined}
              aria-describedby={describedBy(`${uid}-motd`, errors.motd, motdHint)}
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-3">
            <Field id={`${uid}-diff`} label="Difficulty">
              <Select value={state.difficulty} onValueChange={(v) => patch({ difficulty: v })}>
                <SelectTrigger id={`${uid}-diff`} className="w-full capitalize">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DIFFICULTIES.map((v) => (
                    <SelectItem key={v} value={v} className="capitalize">
                      {v}
                      {v === "easy" ? " (default)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field id={`${uid}-mode`} label="Game mode">
              <Select value={state.gamemode} onValueChange={(v) => patch({ gamemode: v })}>
                <SelectTrigger id={`${uid}-mode`} className="w-full capitalize">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {GAMEMODES.map((v) => (
                    <SelectItem key={v} value={v} className="capitalize">
                      {v}
                      {v === "survival" ? " (default)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field id={`${uid}-max`} label="Max players" error={errors.maxPlayers}>
              <Input
                id={`${uid}-max`}
                type="number"
                inputMode="numeric"
                min={1}
                max={10000}
                value={state.maxPlayers}
                onChange={(e) => patch({ maxPlayers: e.target.value })}
                aria-invalid={!!errors.maxPlayers || undefined}
                aria-describedby={describedBy(`${uid}-max`, errors.maxPlayers)}
              />
            </Field>
          </div>
          <Field id={`${uid}-seed`} label="World seed" optional error={errors.seed} hint={seedHint}>
            <Input
              id={`${uid}-seed`}
              value={state.seed}
              onChange={(e) => patch({ seed: e.target.value })}
              placeholder="Random"
              className="font-mono"
              aria-invalid={!!errors.seed || undefined}
              aria-describedby={describedBy(`${uid}-seed`, errors.seed, seedHint)}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="flex items-start justify-between gap-3 rounded-lg border p-3">
              <div>
                <Label htmlFor={`${uid}-online`}>Online mode</Label>
                <p id={`${uid}-online-hint`} className="mt-0.5 text-xs text-muted-foreground">
                  Verify player accounts with Mojang/Microsoft.
                </p>
              </div>
              <Switch
                id={`${uid}-online`}
                checked={state.onlineMode}
                onCheckedChange={(v) => patch({ onlineMode: v })}
                aria-describedby={`${uid}-online-hint`}
              />
            </div>
            <div className="flex items-start justify-between gap-3 rounded-lg border p-3">
              <div>
                <Label htmlFor={`${uid}-wl`}>Whitelist</Label>
                <p id={`${uid}-wl-hint`} className="mt-0.5 text-xs text-muted-foreground">
                  Only listed players can join. Add them in the Players tab.
                </p>
              </div>
              <Switch
                id={`${uid}-wl`}
                checked={state.whitelist}
                onCheckedChange={(v) => patch({ whitelist: v })}
                aria-describedby={`${uid}-wl-hint`}
              />
            </div>
          </div>
          {!state.onlineMode && (
            <div className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200" role="alert">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <p>
                <span className="font-medium">Offline mode lets anyone join with any username</span>, including impersonating
                operators. Only disable it behind a proxy that authenticates players (Velocity/BungeeCord), and enable the
                whitelist if the server is reachable from the internet.
              </p>
            </div>
          )}
          {errors.serverProperties && (
            <p className="text-xs text-destructive" role="alert">
              {errors.serverProperties}
            </p>
          )}
        </div>
      </Section>

      <Advanced title="Advanced" description="JVM flags, environment, description, group & icon" hasError={advancedError}>
        <div className="grid gap-5">
          <div className="grid gap-2">
            <span id={`${uid}-preset`} className="text-sm font-medium">
              JVM flag preset
            </span>
            <div role="radiogroup" aria-labelledby={`${uid}-preset`} className="grid gap-2 sm:grid-cols-3">
              {PRESETS.map((p) => (
                <ChoiceCard
                  key={p.id}
                  name={`${uid}-preset-r`}
                  value={p.id}
                  checked={state.jvmPreset === p.id}
                  onChange={(v) => patch({ jvmPreset: v as JvmPreset })}
                  title={p.title}
                  description={p.description}
                  className="p-2.5"
                />
              ))}
            </div>
            {meowiceOld && (
              <p className="text-xs text-amber-700 dark:text-amber-300">
                MeowIce flags need Java 17+; this server uses Java {d.javaVersion}, so the preset will be ignored.
              </p>
            )}
          </div>
          <Field id={`${uid}-jvm`} label="Extra JVM arguments" optional error={errors.jvmArgs} hint={jvmHint}>
            <Input
              id={`${uid}-jvm`}
              value={state.jvmArgs}
              onChange={(e) => patch({ jvmArgs: e.target.value })}
              placeholder="-Dfile.encoding=UTF-8"
              className="font-mono"
              aria-invalid={!!errors.jvmArgs || undefined}
              aria-describedby={describedBy(`${uid}-jvm`, errors.jvmArgs, jvmHint)}
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <EnvEditor rows={state.env} onChange={(env) => patch({ env })} errors={errors} />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id={`${uid}-group`} label="Group" optional error={errors.group} hint="Groups servers together in lists.">
              <Input
                id={`${uid}-group`}
                value={state.group}
                onChange={(e) => patch({ group: e.target.value })}
                maxLength={60}
                placeholder="e.g. Friends"
                aria-invalid={!!errors.group || undefined}
                aria-describedby={describedBy(`${uid}-group`, errors.group, "x")}
              />
            </Field>
            <Field
              id={`${uid}-icon`}
              label="Icon URL"
              optional
              error={errors.iconUrl}
              hint={d.isModpack ? "Defaults to the modpack's icon." : "An http(s) image URL shown in the panel."}
            >
              <div className="flex items-center gap-2">
                <ServerIcon
                  key={state.iconUrl.trim() || d.resolved?.icon_url || "none"}
                  url={state.iconUrl.trim() || d.resolved?.icon_url || null} name={d.name || "?"} type={d.serverType ?? undefined} className="size-8" />
                <Input
                  id={`${uid}-icon`}
                  type="url"
                  value={state.iconUrl}
                  onChange={(e) => patch({ iconUrl: e.target.value })}
                  placeholder="https://…"
                  aria-invalid={!!errors.iconUrl || undefined}
                  aria-describedby={describedBy(`${uid}-icon`, errors.iconUrl, "x")}
                />
              </div>
            </Field>
          </div>
          <Field id={`${uid}-desc`} label="Description" optional error={errors.description}>
            <Textarea
              id={`${uid}-desc`}
              value={state.description}
              onChange={(e) => patch({ description: e.target.value })}
              maxLength={2000}
              rows={3}
              placeholder="Notes for other admins"
              aria-invalid={!!errors.description || undefined}
              aria-describedby={describedBy(`${uid}-desc`, errors.description)}
            />
          </Field>
        </div>
      </Advanced>
    </div>
  );
}
