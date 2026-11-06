import { Slider as SliderPrimitive } from "radix-ui";
import { AlertTriangle, Loader2 } from "lucide-react";
import { useId } from "react";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/api";
import { formatBytes, formatMemoryMb } from "@/lib/format";
import { useSystemInfo } from "@/lib/queries";
import { useNodePorts } from "./import/nodes";
import { Advanced, describedBy, Field } from "./WizardBits";
import {
  containerLimitMb,
  isModded,
  type Derived,
  type FieldErrors,
  type WizardState,
} from "./wizardModel";

type Patch = (p: Partial<WizardState>) => void;

const PRESETS = [2048, 4096, 6144, 8192, 12288, 16384];

function MemorySlider({ value, onChange, max, labelledBy }: { value: number; onChange: (v: number) => void; max: number; labelledBy: string }) {
  const safe = Number.isFinite(value) ? Math.min(Math.max(value, 1024), max) : 1024;
  return (
    <SliderPrimitive.Root
      value={[safe]}
      onValueChange={([v]) => onChange(v)}
      min={1024}
      max={max}
      step={256}
      className="relative flex w-full touch-none items-center py-2 select-none"
    >
      <SliderPrimitive.Track className="relative h-1.5 grow overflow-hidden rounded-full bg-muted">
        <SliderPrimitive.Range className="absolute h-full bg-primary" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb
        aria-labelledby={labelledBy}
        aria-valuetext={formatMemoryMb(safe)}
        className="block size-4 rounded-full border border-primary bg-background shadow-sm ring-ring/50 transition-[box-shadow] outline-none hover:ring-3 focus-visible:ring-3"
      />
    </SliderPrimitive.Root>
  );
}

export function StepResources({ state, patch, d, errors }: { state: WizardState; patch: Patch; d: Derived; errors: FieldErrors }) {
  const uid = useId();
  const ports = useNodePorts(state.node);
  const system = useSystemInfo();
  // /system/info describes the default node only; host checks are skipped for other nodes
  const defaultNode = state.node === null;
  const hostBytes = (defaultNode && Number(system.data?.agent.docker?.memory_bytes ?? 0)) || null;
  const policyMax =
    (defaultNode && Number((system.data?.agent.policy as { max_memory_mb?: number } | undefined)?.max_memory_mb ?? 0)) || null;
  const hostMb = hostBytes ? Math.floor(hostBytes / 1024 / 1024) : null;
  const sliderMax = Math.max(
    4096,
    Math.floor(Math.min(policyMax ?? 65536, hostMb ?? 32768) / 1024) * 1024,
    Number.isFinite(d.memoryMb) ? d.memoryMb : 0,
  );
  const mem = d.memoryMb;
  const limit = Number.isFinite(mem) ? containerLimitMb(mem, isModded(state, d)) : null;
  const overHost = limit !== null && hostMb !== null && limit > hostMb;
  const overPolicy = limit !== null && policyMax !== null && limit > policyMax;

  const range = ports.data?.range;
  const portNum = Number(d.gamePort);
  const inUse = ports.data?.in_use ?? [];
  const liveConflict = /^\d+$/.test(d.gamePort) ? inUse.find((p) => p.port === portNum) : undefined;
  const portError = errors.gamePort ?? (liveConflict ? `Port ${liveConflict.port} is already used by ${liveConflict.used_by}` : undefined);
  const outsideRange = range && /^\d+$/.test(d.gamePort) && (portNum < range[0] || portNum > range[1]);
  const portHint = ports.isLoading ? (
    <span className="inline-flex items-center gap-1">
      <Loader2 className="size-3 animate-spin" /> Checking free ports…
    </span>
  ) : ports.isError ? (
    `Could not check ports (${errorMessage(ports.error)}); the backend still rejects conflicts.`
  ) : outsideRange ? (
    `Outside the panel's port range (${range[0]}–${range[1]}). Make sure nothing else on the host uses it.`
  ) : ports.data?.suggested ? (
    `Players connect to this TCP port on the host. First free port in ${range?.[0]}–${range?.[1]}: ${ports.data.suggested}.`
  ) : (
    "No free port left in the panel's range; enter one manually."
  );

  const nameHint = "Shown in the panel and used as the server list MOTD unless you set one.";
  const memHint = d.isModpack
    ? d.resolved?.recommended_memory_mb
      ? `The pack recommends ${formatMemoryMb(d.resolved.recommended_memory_mb)}.`
      : "Modpacks usually need 6 GB or more; check the pack page for its recommendation."
    : "4 GB suits a small vanilla or plugin server; add more for many players or mods.";
  const hostIpHint = "Bind the game port to one host address (e.g. a LAN IP). Leave empty for the panel default.";

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
      <Section title="Identity">
        <Field id={`${uid}-name`} label="Server name" error={errors.name} hint={nameHint}>
          <Input
            id={`${uid}-name`}
            value={d.name}
            onChange={(e) => patch({ name: e.target.value })}
            maxLength={80}
            placeholder={d.suggestedName || "My server"}
            aria-invalid={!!errors.name || undefined}
            aria-describedby={describedBy(`${uid}-name`, errors.name, nameHint)}
            autoComplete="off"
            required
          />
        </Field>
      </Section>

      <Section title="Memory" description="Java heap size. The container gets extra headroom for the JVM itself.">
        <div className="grid gap-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="grid min-w-0 flex-1 basis-64 gap-1.5">
              <span id={`${uid}-mem-label`} className="text-sm font-medium">
                Heap: <span className="tabular-nums">{Number.isFinite(mem) ? formatMemoryMb(mem) : "—"}</span>
              </span>
              <MemorySlider
                value={mem}
                onChange={(v) => patch({ memoryMb: v })}
                max={sliderMax}
                labelledBy={`${uid}-mem-label`}
              />
              <div className="flex justify-between text-[11px] text-muted-foreground tabular-nums" aria-hidden>
                <span>1 GB</span>
                <span>{formatMemoryMb(sliderMax)}</span>
              </div>
            </div>
            <div className="grid w-32 gap-1.5">
              <Label htmlFor={`${uid}-mem`}>Heap (MB)</Label>
              <Input
                id={`${uid}-mem`}
                type="number"
                inputMode="numeric"
                min={512}
                max={262144}
                step={256}
                value={Number.isFinite(mem) ? mem : ""}
                onChange={(e) => patch({ memoryMb: e.target.value === "" ? Number.NaN : Math.round(e.target.valueAsNumber) })}
                aria-invalid={!!errors.memoryMb || undefined}
                aria-describedby={describedBy(`${uid}-mem`, errors.memoryMb, memHint)}
              />
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Memory presets">
            {PRESETS.filter((p) => p <= sliderMax).map((p) => (
              <Button
                key={p}
                type="button"
                size="xs"
                variant={mem === p ? "secondary" : "outline"}
                aria-pressed={mem === p}
                onClick={() => patch({ memoryMb: p })}
              >
                {formatMemoryMb(p)}
              </Button>
            ))}
            {d.defaultMemoryMb !== mem && (
              <Button type="button" size="xs" variant="ghost" onClick={() => patch({ memoryMb: null })}>
                Reset to {formatMemoryMb(d.defaultMemoryMb)}
              </Button>
            )}
          </div>
          {errors.memoryMb ? (
            <p id={`${uid}-mem-error`} className="text-xs text-destructive" role="alert">
              {errors.memoryMb}
            </p>
          ) : (
            <p id={`${uid}-mem-hint`} className="text-xs text-muted-foreground">
              {memHint}
            </p>
          )}
          {limit !== null && (
            <div className="grid gap-1 rounded-lg border bg-muted/20 px-3 py-2 text-xs sm:grid-cols-3">
              <div>
                <span className="text-muted-foreground">Container limit </span>
                <span className="font-medium tabular-nums">{formatMemoryMb(limit)}</span>
              </div>
              <div className="text-muted-foreground">
                heap + {formatMemoryMb(limit - mem)} for the JVM itself ({isModded(state, d) ? "mods: 40%, at least 1.5 GB" : "30%, at least 768 MB"})
              </div>
              {hostBytes && (
                <div className="sm:text-right">
                  <span className="text-muted-foreground">Host memory </span>
                  <span className="font-medium tabular-nums">{formatBytes(hostBytes)}</span>
                </div>
              )}
            </div>
          )}
          {(overHost || overPolicy) && (
            <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300" role="alert">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {overPolicy
                ? `The container limit exceeds the node's ${formatMemoryMb(policyMax!)} per-server maximum; the agent will refuse to create it.`
                : "The container limit is larger than the host's total memory; the server may be killed when memory runs out."}
            </p>
          )}
        </div>
      </Section>

      <Section title="Network">
        <div className="grid gap-4">
          <div className="grid gap-4 sm:grid-cols-[12rem_minmax(0,1fr)]">
            <Field id={`${uid}-port`} label="Game port" error={portError} hint={portHint}>
              <div className="flex gap-2">
                <Input
                  id={`${uid}-port`}
                  inputMode="numeric"
                  value={d.gamePort}
                  onChange={(e) => patch({ gamePort: e.target.value.replace(/[^\d]/g, "").slice(0, 5) })}
                  placeholder={ports.data?.suggested ? String(ports.data.suggested) : "25565"}
                  aria-invalid={!!portError || undefined}
                  aria-describedby={describedBy(`${uid}-port`, portError, portHint)}
                  className="tabular-nums"
                />
                {ports.data?.suggested && d.gamePort !== String(ports.data.suggested) && (
                  <Button type="button" variant="outline" size="default" onClick={() => patch({ gamePort: null })}>
                    Use {ports.data.suggested}
                  </Button>
                )}
              </div>
            </Field>
            <div className="grid content-start gap-1.5">
              <span className="text-sm font-medium">Ports in use</span>
              {inUse.length ? (
                <ul className="flex flex-wrap gap-1.5 text-xs">
                  {inUse.map((p) => (
                    <li
                      key={p.port}
                      className={
                        p.port === portNum
                          ? "rounded-md border border-destructive/40 bg-destructive/10 px-1.5 py-0.5 text-destructive"
                          : "rounded-md border bg-muted/40 px-1.5 py-0.5 text-muted-foreground"
                      }
                    >
                      <span className="font-medium tabular-nums">{p.port}</span> · {p.used_by}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-muted-foreground">{ports.isLoading ? "Loading…" : "No ports are used by other servers."}</p>
              )}
            </div>
          </div>
          <Advanced description="Bind address" hasError={!!errors.hostIp}>
            <Field id={`${uid}-ip`} label="Bind IP address" optional error={errors.hostIp} hint={hostIpHint} className="max-w-sm">
              <Input
                id={`${uid}-ip`}
                value={state.hostIp}
                onChange={(e) => patch({ hostIp: e.target.value.trim() })}
                placeholder="Panel default"
                aria-invalid={!!errors.hostIp || undefined}
                aria-describedby={describedBy(`${uid}-ip`, errors.hostIp, hostIpHint)}
                className="font-mono"
                autoComplete="off"
              />
            </Field>
          </Advanced>
        </div>
      </Section>

      {state.category !== "import" && (
      <Section>
        <div className="flex items-start justify-between gap-4">
          <div>
            <Label htmlFor={`${uid}-start`}>Start after creation</Label>
            <p id={`${uid}-start-hint`} className="mt-0.5 text-xs text-muted-foreground">
              {d.isModpack
                ? "Starts the server as soon as the modpack is installed. The first start of a large pack can take several minutes."
                : "Starts the server once the software is installed. Turn off to upload files (e.g. an existing world) first."}
            </p>
          </div>
          <Switch
            id={`${uid}-start`}
            checked={state.start}
            onCheckedChange={(v) => patch({ start: v })}
            aria-describedby={`${uid}-start-hint`}
          />
        </div>
      </Section>
      )}
    </div>
  );
}
