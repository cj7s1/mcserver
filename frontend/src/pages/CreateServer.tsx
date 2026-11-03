import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Loader2, Lock, Rocket } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { PageHeader, Section } from "@/components/PageHeader";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { ApiError, api, errorMessage } from "@/lib/api";
import { formatMemoryMb } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { Provider, ServerType } from "@/lib/types";
import { cn } from "@/lib/utils";
import { StepGameplay } from "./create-server/StepGameplay";
import { StepResources } from "./create-server/StepResources";
import { softwareRows, StepReview } from "./create-server/StepReview";
import {
  defaultLoaderVersion,
  defaultMinecraftVersion,
  StepSoftware,
} from "./create-server/StepSoftware";
import { StepType } from "./create-server/StepType";
import { Stepper, SummaryList } from "./create-server/WizardBits";
import {
  buildPayload,
  type Category,
  CATEGORY_TYPES,
  type CreatePayload,
  type CreateResponse,
  DEFAULT_MEMORY_MODPACK,
  DEFAULT_MEMORY_STANDARD,
  type Derived,
  type FieldErrors,
  initialState,
  SERVER_FIELD_MAP,
  type StandardCategory,
  STEPS,
  type StepId,
  stepForField,
  TYPE_LABELS,
  validateStep,
  type WizardState,
} from "./create-server/wizardModel";
import { loaderLabel } from "./modpacks/modpackMeta";
import {
  mpk,
  useGameVersions,
  useJavaInfo,
  useLoaderVersions,
  usePorts,
  useResolveRelease,
  useServerTypes,
} from "./modpacks/modpackQueries";

const PROVIDERS: Provider[] = ["modrinth", "curseforge", "ftb"];
const GAMEPLAY_KEYS = [
  "motd",
  "difficulty",
  "gamemode",
  "maxPlayers",
  "onlineMode",
  "seed",
  "whitelist",
];

/**
 * Deep links:
 *   /servers/new?provider=modrinth&project=<id>&version=<id>  -> modpack mode with that project/release
 *   /servers/new?type=PAPER                                   -> standard mode with that software
 */
function stateFromParams(sp: URLSearchParams): {
  state: WizardState;
  step: StepId;
} {
  const s = initialState();
  const provider = sp.get("provider") as Provider | null;
  const project = sp.get("project");
  if (project && provider && PROVIDERS.includes(provider)) {
    s.category = "modpack";
    s.provider = provider;
    s.projectId = project;
    s.versionId = sp.get("version") ?? "";
    s.browse = { ...s.browse, provider };
    return { state: s, step: "software" };
  }
  const type = sp.get("type")?.toUpperCase() as ServerType | undefined;
  if (type) {
    const cat = (Object.keys(CATEGORY_TYPES) as StandardCategory[]).find((c) =>
      CATEGORY_TYPES[c].includes(type),
    );
    if (cat) {
      s.category = cat;
      s.serverType = type;
      return { state: s, step: "software" };
    }
  }
  if (sp.get("mode") === "modpack") {
    s.category = "modpack";
    return { state: s, step: "software" };
  }
  return { state: s, step: "type" };
}

function useDerived(state: WizardState): Derived {
  const isModpack = state.category === "modpack";
  const types =
    state.category && state.category in CATEGORY_TYPES
      ? CATEGORY_TYPES[state.category as StandardCategory]
      : [];
  const serverType: ServerType | null = isModpack
    ? null
    : state.serverType && types.includes(state.serverType)
      ? state.serverType
      : (types[0] ?? null);

  const serverTypes = useServerTypes();
  const versions = useGameVersions(serverType, state.showSnapshots);
  const listed = versions.data?.versions;
  const minecraftVersion = !serverType
    ? ""
    : state.minecraftVersion &&
        (!listed || listed.some((v) => v.version === state.minecraftVersion))
      ? state.minecraftVersion
      : defaultMinecraftVersion(versions.data);
  const hasLoader = !!serverType && serverType !== "VANILLA";
  const loaders = useLoaderVersions(
    hasLoader ? serverType : null,
    minecraftVersion || null,
  );
  const loaderVersion = !hasLoader
    ? ""
    : state.loaderVersion !== null
      ? state.loaderVersion
      : loaders.isSuccess
        ? defaultLoaderVersion(loaders.data)
        : "";
  const javaQ = useJavaInfo(
    !isModpack && minecraftVersion ? minecraftVersion : null,
    serverType,
  );
  const resolve = useResolveRelease(
    state.provider,
    isModpack ? state.projectId : null,
    isModpack ? state.versionId : null,
  );
  const ports = usePorts();

  return useMemo(() => {
    const resolved = isModpack ? resolve.data : undefined;
    const java = isModpack ? (resolved?.java ?? null) : (javaQ.data ?? null);
    const byUrl = isModpack && state.modpackSource === "url";
    const cfSlug = byUrl
      ? (state.cfRef.trim().match(/modpacks\/([a-z0-9-]+)/)?.[1] ??
        state.cfRef.trim())
      : "";
    const suggestedName = (
      byUrl
        ? cfSlug
            .split("-")
            .filter(Boolean)
            // short words and words with digits are usually acronyms/editions: "better-mc-forge-bmc4" -> "Better MC Forge BMC4"
            .map((w) => (w.length <= 3 || /\d/.test(w) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)))
            .join(" ")
        : isModpack
          ? (resolved?.name ?? state.projectHit?.title ?? "")
          : serverType && minecraftVersion
            ? `${TYPE_LABELS[serverType]} ${minecraftVersion}`
            : ""
    ).slice(0, 80);
    const defaultMemoryMb = isModpack
      ? (resolved?.recommended_memory_mb ?? DEFAULT_MEMORY_MODPACK)
      : DEFAULT_MEMORY_STANDARD;
    const softwareSummary = byUrl
      ? `CurseForge ${cfSlug || "modpack"}${state.cfMinecraftVersion.trim() ? ` · Minecraft ${state.cfMinecraftVersion.trim()}` : ""}`
      : isModpack
        ? resolved
          ? `${resolved.name} ${resolved.version_name} · Minecraft ${resolved.minecraft_version} · ${loaderLabel(resolved.loader)}`
          : "Modpack"
        : serverType
          ? `${TYPE_LABELS[serverType]} ${minecraftVersion}${hasLoader ? ` · ${loaderVersion || "latest build"}` : ""}`
          : "";
    return {
      isModpack,
      serverType,
      minecraftVersion,
      loaderVersion,
      loaderLabel:
        serverTypes.data?.find((t) => t.id === serverType)?.loader_label ??
        null,
      java,
      javaVersion: state.javaOverride
        ? Number(state.javaOverride)
        : (java?.java_version ?? null),
      name: state.name ?? suggestedName,
      suggestedName,
      memoryMb: state.memoryMb ?? defaultMemoryMb,
      defaultMemoryMb,
      gamePort:
        state.gamePort ??
        (ports.data?.suggested ? String(ports.data.suggested) : ""),
      ports: ports.data,
      resolved,
      resolveOk: isModpack && resolve.isSuccess,
      resolvePending: isModpack && resolve.isLoading,
      softwareSummary,
    };
  }, [
    isModpack,
    resolve.data,
    resolve.isSuccess,
    resolve.isLoading,
    javaQ.data,
    state.projectHit,
    state.modpackSource,
    state.cfRef,
    state.cfMinecraftVersion,
    state.javaOverride,
    state.name,
    state.memoryMb,
    state.gamePort,
    serverType,
    minecraftVersion,
    hasLoader,
    loaderVersion,
    serverTypes.data,
    ports.data,
  ]);
}

/** Map a failed POST /instances/ to wizard field errors. */
function serverErrorsFrom(err: unknown): FieldErrors {
  if (!(err instanceof ApiError)) return { form: errorMessage(err) };
  if (err.code === "port_conflict" || err.code === "no_free_ports")
    return { gamePort: err.message };
  const out: FieldErrors = {};
  for (const [field, msg] of Object.entries(err.fieldErrors)) {
    out[SERVER_FIELD_MAP[field] ?? "form"] = out[
      SERVER_FIELD_MAP[field] ?? "form"
    ]
      ? `${out[SERVER_FIELD_MAP[field] ?? "form"]} ${msg}`
      : msg;
  }
  if (!Object.keys(out).length) out.form = err.display;
  return out;
}

function stepIndex(step: StepId): number {
  return STEPS.findIndex((s) => s.id === step);
}

export default function CreateServer() {
  const { can } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [searchParams] = useSearchParams();
  const [init] = useState(() => stateFromParams(searchParams));
  const [state, setState] = useState<WizardState>(init.state);
  const [step, setStep] = useState<StepId>(init.step);
  const [maxReached, setMaxReached] = useState(stepIndex(init.step));
  const [attempted, setAttempted] = useState<Set<StepId>>(() => new Set());
  const [serverErrors, setServerErrors] = useState<FieldErrors>({});
  const headingRef = useRef<HTMLHeadingElement>(null);
  const firstRender = useRef(true);
  const d = useDerived(state);

  const patch = useCallback((p: Partial<WizardState>) => {
    setState((s) => ({ ...s, ...p }));
    setServerErrors((errs) => {
      const keys = Object.keys(p);
      if (!Object.keys(errs).length) return errs;
      const next = { ...errs };
      for (const k of Object.keys(next)) {
        const base = k.split(".")[0];
        if (keys.includes(base)) delete next[k];
        if (
          k === "serverProperties" &&
          keys.some((x) => GAMEPLAY_KEYS.includes(x))
        )
          delete next[k];
        if (base === "versionId" && keys.includes("projectId")) delete next[k];
      }
      delete next.form;
      return next;
    });
  }, []);

  const clientErrors = attempted.has(step) ? validateStep(step, state, d) : {};
  const errors: FieldErrors = { ...serverErrors, ...clientErrors };

  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
    headingRef.current?.focus({ preventScroll: true });
  }, [step]);

  const goTo = (next: StepId) => {
    setStep(next);
    setMaxReached((m) => Math.max(m, stepIndex(next)));
  };

  const next = () => {
    const errs = validateStep(step, state, d);
    if (Object.keys(errs).length) {
      setAttempted((a) => new Set(a).add(step));
      return;
    }
    const idx = stepIndex(step);
    if (idx < STEPS.length - 1) goTo(STEPS[idx + 1].id);
  };

  const back = () => {
    const idx = stepIndex(step);
    if (idx > 0) setStep(STEPS[idx - 1].id);
  };

  const create = useMutation({
    mutationFn: (payload: CreatePayload) =>
      api.post<CreateResponse>("/instances/", payload),
    onSuccess: (res) => {
      toast.success(`Deploying ${res.instance.name}`, {
        description: state.start
          ? "Installing now; the server starts when the install finishes."
          : "Installing now; it will stay stopped.",
      });
      for (const w of res.warnings ?? []) toast.warning(w);
      qc.setQueryData(qk.instance(res.instance.id), res.instance);
      qc.invalidateQueries({ queryKey: qk.instances });
      qc.invalidateQueries({ queryKey: qk.jobs });
      qc.invalidateQueries({ queryKey: mpk.ports });
      navigate(`/servers/${res.instance.id}`);
    },
    onError: (err) => {
      toast.error(errorMessage(err));
      const errs = serverErrorsFrom(err);
      if (
        err instanceof ApiError &&
        (err.code === "port_conflict" || err.code === "no_free_ports")
      ) {
        qc.invalidateQueries({ queryKey: mpk.ports });
      }
      setServerErrors(errs);
      const first = STEPS.find((s) =>
        Object.keys(errs).some((k) => stepForField(k) === s.id),
      );
      if (first && first.id !== step) setStep(first.id);
    },
  });

  const deploy = () => {
    for (const s of STEPS) {
      const errs = validateStep(s.id, state, d);
      if (Object.keys(errs).length) {
        setAttempted((a) => new Set(a).add(s.id));
        if (s.id !== step) setStep(s.id);
        return;
      }
    }
    create.mutate(buildPayload(state, d));
  };

  const onCategory = (c: Category) => {
    if (c === state.category) return;
    patch({
      category: c,
      serverType:
        c in CATEGORY_TYPES ? CATEGORY_TYPES[c as StandardCategory][0] : null,
      minecraftVersion: null,
      loaderVersion: null,
      javaOverride: "",
    });
    setMaxReached(0);
    setAttempted(new Set());
  };

  if (!can("instance.create")) {
    return (
      <div className="grid gap-4">
        <PageHeader title="Create server" />
        <EmptyState
          icon={<Lock className="size-5" />}
          title="You can't create servers"
          description="Your role does not include the “Create servers” permission. Ask the panel owner for access."
          action={
            <Button asChild variant="outline" size="sm">
              <Link to="/servers">Back to servers</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const idx = stepIndex(step);
  const isLast = idx === STEPS.length - 1;
  const importMode = state.category === "import";
  const wide = step === "software" && d.isModpack;
  const showSummary =
    !wide && step !== "review" && !!state.category && !importMode;
  const stepLabels = { software: d.isModpack ? "Modpack" : "Software" };
  const stepTitle =
    d.isModpack && step === "software" ? "Modpack & release" : STEPS[idx].label;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 pb-2">
      <PageHeader
        title="Create server"
        description="Pick the software, size it, and deploy. The install runs in the background and you can watch it on the server page."
        actions={
          <Button asChild variant="ghost" size="sm">
            <Link to="/servers">Cancel</Link>
          </Button>
        }
        className="pb-0"
      />
      <Stepper
        current={step}
        maxReached={importMode ? 0 : maxReached}
        onGo={goTo}
        labels={stepLabels}
      />
      <h2 ref={headingRef} tabIndex={-1} className="sr-only">
        Step {idx + 1} of {STEPS.length}: {stepTitle}
      </h2>

      <div
        className={cn(
          "grid items-start gap-4",
          showSummary && "lg:grid-cols-[minmax(0,1fr)_17rem]",
        )}
      >
        <div className="min-w-0">
          {step === "type" && (
            <StepType
              category={state.category}
              onChange={onCategory}
              error={errors.category}
            />
          )}
          {step === "software" && (
            <StepSoftware state={state} patch={patch} d={d} errors={errors} />
          )}
          {step === "resources" && (
            <StepResources state={state} patch={patch} d={d} errors={errors} />
          )}
          {step === "gameplay" && (
            <StepGameplay state={state} patch={patch} d={d} errors={errors} />
          )}
          {step === "review" && (
            <StepReview
              state={state}
              patch={patch}
              d={d}
              errors={errors}
              onEdit={goTo}
            />
          )}
        </div>
        {showSummary && (
          <aside className="sticky top-16 hidden lg:block" aria-label="Summary">
            <Section title="Summary">
              <SummaryList
                className="text-xs"
                rows={[
                  ...softwareRows(state, d),
                  ...(idx >= 2
                    ? ([
                        ["Name", d.name || "—"],
                        [
                          "Memory",
                          Number.isFinite(d.memoryMb)
                            ? formatMemoryMb(d.memoryMb)
                            : "—",
                        ],
                        ["Port", d.gamePort || "—"],
                      ] as [string, string][])
                    : []),
                ]}
              />
            </Section>
          </aside>
        )}
      </div>

      <div className="sticky bottom-0 z-20 -mx-3 flex items-center gap-2 border-t bg-background/90 px-3 py-3 backdrop-blur md:-mx-6 md:px-6">
        <Button
          type="button"
          variant="outline"
          onClick={back}
          disabled={idx === 0 || create.isPending}
        >
          <ArrowLeft /> Back
        </Button>
        <div className="min-w-0 flex-1 truncate text-center text-xs text-muted-foreground">
          {importMode
            ? "Import and restore happen from an existing server — see the steps above."
            : step === "software" && d.isModpack && d.resolvePending
              ? "Checking the selected release…"
              : step === "gameplay"
                ? "Everything on this step is optional."
                : ""}
        </div>
        {isLast ? (
          <Button
            type="button"
            onClick={deploy}
            disabled={create.isPending}
            className="min-w-28"
          >
            {create.isPending ? (
              <Loader2 className="animate-spin" />
            ) : (
              <Rocket />
            )}
            {create.isPending ? "Deploying…" : "Deploy server"}
          </Button>
        ) : (
          <Button type="button" onClick={next} disabled={importMode}>
            {step === "gameplay" ? "Review" : "Next"} <ArrowRight />
          </Button>
        )}
      </div>
    </div>
  );
}
