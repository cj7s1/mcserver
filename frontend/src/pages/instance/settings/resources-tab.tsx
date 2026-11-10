/** Memory, CPU and JVM settings (PATCH /instances/{id} with changed fields only). */
import { Cpu, MemoryStick } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/api";
import { formatMemoryMb } from "@/lib/format";
import { MemoryCommitBar } from "@/components/MemoryCommitBar";
import { useInstances, useSystemInfo } from "@/lib/queries";
import { memoryCommitment } from "@/pages/library/server-utils";
import type { Instance } from "@/lib/types";
import {
  Advanced,
  Field,
  fieldErrorsOf,
  InlineField,
  Notice,
  parseNum,
  ReadOnlyNotice,
  SaveBar,
  usePatchInstance,
  useReportDirty,
  type TabProps,
} from "./settings-ui";

type Fields = Pick<
  Instance,
  "memory_mb" | "memory_overhead_mb" | "cpu_limit" | "jvm_flags_preset" | "jvm_args" | "stop_timeout" | "restart_policy"
>;
const KEYS: (keyof Fields)[] = [
  "memory_mb",
  "memory_overhead_mb",
  "cpu_limit",
  "jvm_flags_preset",
  "jvm_args",
  "stop_timeout",
  "restart_policy",
];

const PRESETS: { value: Instance["jvm_flags_preset"]; label: string; hint: string }[] = [
  { value: "none", label: "None", hint: "JVM defaults plus the heap size. Use this if you supply your own GC flags." },
  { value: "aikar", label: "Aikar's flags", hint: "Well-tested G1GC tuning that reduces lag spikes on most servers." },
  { value: "meowice", label: "MeowIce flags", hint: "Newer G1GC tuning for Java 17+ (ignored on older Java)." },
];

const POLICIES: { value: Instance["restart_policy"]; label: string; hint: string }[] = [
  {
    value: "unless-stopped",
    label: "Unless stopped",
    hint: "Docker restarts the server after a crash and when the host or Docker restarts, unless you stopped it.",
  },
  { value: "on-failure", label: "On failure", hint: "Restart only when the server exits with an error." },
  { value: "no", label: "Never", hint: "Never restart automatically." },
];

/** Mirrors Instance.container_memory_mb on the backend, for previewing unsaved values. */
function estimateContainerMb(memory: number, overhead: number, contentKind: Instance["content_kind"]): number {
  if (overhead) return memory + overhead;
  return memory + (contentKind === "mods" ? Math.max(1536, Math.floor(memory * 0.4)) : Math.max(768, Math.floor(memory * 0.3)));
}

function validate(f: Fields): Partial<Record<keyof Fields, string>> {
  const e: Partial<Record<keyof Fields, string>> = {};
  if (!Number.isInteger(f.memory_mb) || f.memory_mb < 512 || f.memory_mb > 262144) e.memory_mb = "Between 512 MB and 256 GB";
  if (!Number.isInteger(f.memory_overhead_mb) || f.memory_overhead_mb < 0) e.memory_overhead_mb = "0 (auto) or a positive number of MB";
  if (f.cpu_limit !== null && (!(f.cpu_limit >= 0.1) || f.cpu_limit > 256)) e.cpu_limit = "Between 0.1 and 256 cores";
  if (!Number.isInteger(f.stop_timeout) || f.stop_timeout < 15 || f.stop_timeout > 900) e.stop_timeout = "Between 15 and 900 seconds";
  for (const arg of f.jvm_args.split(/\s+/).filter(Boolean)) {
    if (!arg.startsWith("-")) {
      e.jvm_args = `“${arg}” does not look like a JVM option (must start with -)`;
      break;
    }
    if (arg.startsWith("-Xmx") || arg.startsWith("-Xms")) {
      e.jvm_args = "Set the heap with the memory setting above, not -Xmx/-Xms";
      break;
    }
  }
  return e;
}

export function ResourcesTab({ instance, can, onDirtyChange }: TabProps) {
  const canEdit = can("instance.configure");
  const system = useSystemInfo();
  const patch = usePatchInstance(instance.id);
  const [draft, setDraft] = useState<Partial<Record<keyof Fields, unknown>>>({});
  // raw text of number inputs so partially typed values are not clobbered
  const [text, setText] = useState<Partial<Record<keyof Fields, string>>>({});

  const current = useMemo(() => {
    const out = {} as Fields;
    for (const k of KEYS) (out as Record<string, unknown>)[k] = k in draft ? draft[k] : instance[k];
    return out;
  }, [draft, instance]);
  const changed = useMemo(() => KEYS.filter((k) => k in draft && draft[k] !== instance[k]), [draft, instance]);
  const errors = validate(current);
  const serverErrors = fieldErrorsOf(patch.error);
  useReportDirty(changed.length > 0, onDirtyChange);

  const set = <K extends keyof Fields>(k: K, v: Fields[K]) => {
    setDraft((d) => ({ ...d, [k]: v }));
    if (patch.error) patch.reset();
  };
  const setNumber = (k: "memory_mb" | "memory_overhead_mb" | "stop_timeout", raw: string) => {
    setText((t) => ({ ...t, [k]: raw }));
    const n = parseNum(raw);
    set(k, n === null ? Number.NaN : n);
  };
  const numText = (k: "memory_mb" | "memory_overhead_mb" | "stop_timeout" | "cpu_limit") =>
    k in text ? (text[k] ?? "") : current[k] === null ? "" : String(current[k]);

  const hostMb = system.data?.agent.docker?.memory_bytes ? Math.floor(Number(system.data.agent.docker.memory_bytes) / 1048576) : null;
  const hostCpus = system.data?.agent.docker?.cpus ? Number(system.data.agent.docker.cpus) : null;
  const sliderMax = Math.max(instance.memory_mb, Number.isFinite(current.memory_mb) ? current.memory_mb : 0, hostMb ? Math.floor(hostMb / 512) * 512 : 32768);
  const memValid = Number.isFinite(current.memory_mb) && !errors.memory_mb;
  const containerMb = memValid
    ? estimateContainerMb(current.memory_mb, Number.isFinite(current.memory_overhead_mb) ? current.memory_overhead_mb : 0, instance.content_kind)
    : null;
  const memChanged = changed.includes("memory_mb") || changed.includes("memory_overhead_mb");
  // Other running servers on the same host (/system/info describes the default node only).
  const instances = useInstances();
  const sameHost = !system.data || system.data.node.id === instance.node;
  const others =
    instances.data && sameHost ? memoryCommitment(instances.data, { nodeId: instance.node, excludeId: instance.id }) : null;
  const preset = PRESETS.find((p) => p.value === current.jvm_flags_preset);
  const policy = POLICIES.find((p) => p.value === current.restart_policy);
  const err = (k: keyof Fields) => errors[k] ?? serverErrors[k] ?? null;
  const disabled = !canEdit || patch.isPending;

  const save = () => {
    const body: Partial<Instance> = {};
    for (const k of changed) (body as Record<string, unknown>)[k] = current[k];
    patch.mutate(body, {
      onSuccess: () => {
        setDraft({});
        setText({});
        toast.success("Resource settings saved. Restart the server to apply them.");
      },
      onError: (e) => toast.error(errorMessage(e)),
    });
  };

  return (
    <div className="grid grid-cols-1 gap-4">
      {!canEdit && <ReadOnlyNotice />}
      <Section
        title={
          <span className="inline-flex items-center gap-2">
            <MemoryStick className="size-4 text-muted-foreground" aria-hidden /> Memory
          </span>
        }
        description="Java heap for the server. The container limit adds headroom for off-heap memory so the server is not OOM-killed."
      >
        <div className="grid grid-cols-1 gap-5">
          <Field
            id="memory-mb"
            label="Heap size (-Xmx)"
            error={err("memory_mb")}
            aside={<span className="text-sm font-semibold tabular-nums">{memValid ? formatMemoryMb(current.memory_mb) : "—"}</span>}
          >
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
              <Slider
                aria-label="Heap size"
                className="flex-1"
                min={512}
                max={sliderMax}
                step={256}
                value={[memValid ? Math.min(current.memory_mb, sliderMax) : instance.memory_mb]}
                onValueChange={([v]) => {
                  setText((t) => ({ ...t, memory_mb: String(v) }));
                  set("memory_mb", v);
                }}
                disabled={disabled}
              />
              <div className="flex items-center gap-2">
                <Input
                  id="memory-mb"
                  type="number"
                  inputMode="numeric"
                  min={512}
                  max={262144}
                  step={256}
                  className="w-28 tabular-nums"
                  value={numText("memory_mb")}
                  onChange={(e) => setNumber("memory_mb", e.target.value)}
                  disabled={disabled}
                  aria-invalid={!!err("memory_mb") || undefined}
                />
                <span className="text-sm text-muted-foreground">MB</span>
              </div>
            </div>
          </Field>
          <div className="grid gap-2 rounded-lg bg-muted/40 p-3 text-xs sm:grid-cols-3">
            <div>
              <p className="text-muted-foreground">Container memory limit</p>
              <p className="mt-0.5 text-sm font-medium tabular-nums">
                {formatMemoryMb(instance.container_memory_mb)}
                {memChanged && containerMb !== null && containerMb !== instance.container_memory_mb && (
                  <span className="text-primary"> → ≈ {formatMemoryMb(containerMb)}</span>
                )}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">Off-heap headroom</p>
              <p className="mt-0.5 text-sm font-medium tabular-nums">
                {current.memory_overhead_mb ? formatMemoryMb(current.memory_overhead_mb) : "Automatic"}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">Host memory</p>
              <p className="mt-0.5 text-sm font-medium tabular-nums">{hostMb ? formatMemoryMb(hostMb) : system.isLoading ? "…" : "Unknown"}</p>
            </div>
          </div>
          {hostMb !== null && others && sameHost && !(containerMb !== null && containerMb > hostMb) && (
            <MemoryCommitBar
              otherRunningMb={others.runningMb}
              addMb={memChanged && containerMb !== null ? containerMb : instance.container_memory_mb}
              hostMb={hostMb}
            />
          )}
          {hostMb !== null && containerMb !== null && containerMb > hostMb && (
            <Notice tone="warning">
              The container limit ({formatMemoryMb(containerMb)}) is larger than the host's memory ({formatMemoryMb(hostMb)}). The
              server may be killed by the kernel when memory runs out.
            </Notice>
          )}
          <Advanced title="Advanced memory" description="Off-heap headroom">
            <Field
              id="memory-overhead"
              label="Extra container memory above heap (MB)"
              error={err("memory_overhead_mb")}
              hint={`0 = automatic (${instance.content_kind === "mods" ? "40% of heap, at least 1.5 GB for modded servers" : "30% of heap, at least 768 MB"}). Raise it if the server is OOM-killed while the heap is not full.`}
            >
              <Input
                id="memory-overhead"
                type="number"
                inputMode="numeric"
                min={0}
                step={128}
                className="w-36 tabular-nums"
                value={numText("memory_overhead_mb")}
                onChange={(e) => setNumber("memory_overhead_mb", e.target.value)}
                disabled={disabled}
                aria-invalid={!!err("memory_overhead_mb") || undefined}
              />
            </Field>
          </Advanced>
        </div>
      </Section>

      <Section
        title={
          <span className="inline-flex items-center gap-2">
            <Cpu className="size-4 text-muted-foreground" aria-hidden /> CPU
          </span>
        }
      >
        <div className="grid grid-cols-1 gap-3">
          <InlineField
            id="cpu-limit-toggle"
            label="Limit CPU usage"
            hint={`Caps the cores the container may use${hostCpus ? ` (host has ${hostCpus})` : ""}. Unlimited lets the server use idle cores.`}
          >
            <Switch
              id="cpu-limit-toggle"
              checked={current.cpu_limit !== null}
              onCheckedChange={(on) => {
                setText((t) => ({ ...t, cpu_limit: on ? String(instance.cpu_limit ?? Math.min(2, hostCpus ?? 2)) : "" }));
                set("cpu_limit", on ? (instance.cpu_limit ?? Math.min(2, hostCpus ?? 2)) : null);
              }}
              disabled={disabled}
            />
          </InlineField>
          {current.cpu_limit !== null && (
            <Field id="cpu-limit" label="Cores" error={err("cpu_limit")} hint="Fractions are allowed, e.g. 1.5.">
              <Input
                id="cpu-limit"
                type="number"
                inputMode="decimal"
                min={0.1}
                max={256}
                step={0.5}
                className="w-28 tabular-nums"
                value={numText("cpu_limit")}
                onChange={(e) => {
                  setText((t) => ({ ...t, cpu_limit: e.target.value }));
                  const n = parseNum(e.target.value);
                  set("cpu_limit", n === null ? Number.NaN : n);
                }}
                disabled={disabled}
                aria-invalid={!!err("cpu_limit") || undefined}
              />
            </Field>
          )}
        </div>
      </Section>

      <Section title="Java runtime" description="Garbage collector tuning and process behaviour.">
        <div className="grid grid-cols-1 gap-5">
          <Field id="jvm-preset" label="JVM flag preset" error={err("jvm_flags_preset")} hint={preset?.hint}>
            <Select value={current.jvm_flags_preset} onValueChange={(v) => set("jvm_flags_preset", v as Fields["jvm_flags_preset"])} disabled={disabled}>
              <SelectTrigger id="jvm-preset" className="w-full sm:w-72">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PRESETS.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {current.jvm_flags_preset === "meowice" && instance.java_version < 17 && (
            <Notice tone="warning">
              MeowIce flags need Java 17 or newer; this server uses Java {instance.java_version}, so no preset flags are applied.
            </Notice>
          )}
          <Advanced title="Advanced runtime" description="JVM arguments, shutdown and restart policy">
            <Field
              id="jvm-args"
              label="Extra JVM arguments"
              error={err("jvm_args")}
              hint="Space-separated options, e.g. -XX:+UseZGC -Dlog4j2.formatMsgNoLookups=true. Heap size comes from the memory setting."
            >
              <Input
                id="jvm-args"
                value={current.jvm_args}
                onChange={(e) => set("jvm_args", e.target.value)}
                className="font-mono text-xs"
                spellCheck={false}
                autoComplete="off"
                disabled={disabled}
                aria-invalid={!!err("jvm_args") || undefined}
              />
            </Field>
            <Field
              id="stop-timeout"
              label="Stop timeout (seconds)"
              error={err("stop_timeout")}
              hint="How long a stop waits for the world to save before the process is killed (15–900)."
            >
              <Input
                id="stop-timeout"
                type="number"
                inputMode="numeric"
                min={15}
                max={900}
                className="w-28 tabular-nums"
                value={numText("stop_timeout")}
                onChange={(e) => setNumber("stop_timeout", e.target.value)}
                disabled={disabled}
                aria-invalid={!!err("stop_timeout") || undefined}
              />
            </Field>
            <Field id="restart-policy" label="Automatic restart" error={err("restart_policy")} hint={policy?.hint}>
              <Select value={current.restart_policy} onValueChange={(v) => set("restart_policy", v as Fields["restart_policy"])} disabled={disabled}>
                <SelectTrigger id="restart-policy" className="w-full sm:w-72">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {POLICIES.map((p) => (
                    <SelectItem key={p.value} value={p.value}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </Advanced>
        </div>
      </Section>

      {canEdit && (
        <SaveBar
          count={changed.length}
          saving={patch.isPending}
          disabled={Object.keys(errors).length > 0}
          note={Object.keys(errors).length > 0 ? "Fix the highlighted values first" : "Applied when the server restarts"}
          onDiscard={() => {
            setDraft({});
            setText({});
            patch.reset();
          }}
          onSave={save}
        />
      )}
    </div>
  );
}
