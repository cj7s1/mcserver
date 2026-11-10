import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Loader2, Lock, Rocket, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { PageHeader, Section } from "@/components/PageHeader";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { ApiError, api, errorMessage } from "@/lib/api";
import { OvercommitDeclined, postOrDecline } from "@/lib/power";
import { formatMemoryMb } from "@/lib/format";
import { qk } from "@/lib/queries";
import type { Provider, ServerType, SystemSettings } from "@/lib/types";
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
import { ArchivePicker } from "./create-server/import/ArchivePicker";
import { importPermissionProblem, nameFromArchive } from "./create-server/import/importModel";
import { ImportProgress } from "./create-server/import/ImportProgress";
import { ImportSoftware } from "./create-server/import/ImportSoftware";
import { NodeSelect } from "./create-server/import/NodeSelect";
import { selectedNode, useNodePorts, useWizardNodes } from "./create-server/import/nodes";
import { useImportDeploy } from "./create-server/import/useImportDeploy";
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
  IMPORT_TYPES,
  initialState,
  nightlyBackupAllowed,
  SERVER_FIELD_MAP,
  type StandardCategory,
  STEPS,
  type StepId,
  stepForField,
  TYPE_LABELS,
  validateStep,
  type WizardState,
} from "./create-server/wizardModel";
import { describeCron } from "./instance/schedules/cron";
import { loaderLabel } from "./modpacks/modpackMeta";
import {
  mpk,
  useGameVersions,
  useJavaInfo,
  useLoaderVersions,
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
 *   /servers/new?mode=import                                  -> import an existing server folder
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
  if (sp.get("mode") === "import") {
    s.category = "import";
    // nothing preselected: an unchanged default could upgrade the imported world on first start
    s.serverType = null;
    return { state: s, step: "software" };
  }
  return { state: s, step: "type" };
}

function useDerived(state: WizardState, backupAllowed: boolean, panelDefaultMemoryMb: number | null): Derived {
  const isModpack = state.category === "modpack";
  const isImport = state.category === "import";
  const types = isImport
    ? IMPORT_TYPES
    : state.category && state.category in CATEGORY_TYPES
      ? CATEGORY_TYPES[state.category as StandardCategory]
      : [];
  // Import: no fallback software or version. The panel installs exactly what is chosen and does not
  // read it from the archive, so a silent "Vanilla, newest release" could upgrade an older or modded
  // world irreversibly on its first start; the software step stays invalid until both are picked.
  const serverType: ServerType | null = isModpack
    ? null
    : state.serverType && types.includes(state.serverType)
      ? state.serverType
      : isImport
        ? null
        : (types[0] ?? null);

  const serverTypes = useServerTypes();
  const versions = useGameVersions(serverType, state.showSnapshots);
  const listed = versions.data?.versions;
  const minecraftVersion = !serverType
    ? ""
    : state.minecraftVersion &&
        (!listed || listed.some((v) => v.version === state.minecraftVersion))
      ? state.minecraftVersion
      : isImport
        ? ""
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
  const ports = useNodePorts(state.node);
  const nodes = useWizardNodes();

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
          : isImport && state.importFile && nameFromArchive(state.importFile.name)
            ? nameFromArchive(state.importFile.name)
            : serverType && minecraftVersion
            ? `${TYPE_LABELS[serverType]} ${minecraftVersion}`
            : ""
    ).slice(0, 80);
    // Standard servers: the panel's "Default memory" setting when this user may read it; otherwise
    // the wizard leaves memory_mb out while the heap is untouched and the backend applies it.
    const defaultMemoryMb = isModpack
      ? (resolved?.recommended_memory_mb ?? DEFAULT_MEMORY_MODPACK)
      : (panelDefaultMemoryMb ?? DEFAULT_MEMORY_STANDARD);
    const memoryIsPanelDefault = !isModpack && state.memoryMb === null && panelDefaultMemoryMb === null;
    const softwareSummary = byUrl
      ? `CurseForge ${cfSlug || "modpack"}${state.cfMinecraftVersion.trim() ? ` · Minecraft ${state.cfMinecraftVersion.trim()}` : ""}`
      : isModpack
        ? resolved
          ? `${resolved.name} ${resolved.version_name} · Minecraft ${resolved.minecraft_version} · ${loaderLabel(resolved.loader)}`
          : "Modpack"
        : serverType
          ? `${isImport ? "Import · " : ""}${TYPE_LABELS[serverType]} ${minecraftVersion}${hasLoader ? ` · ${loaderVersion || "latest build"}` : ""}`
          : "";
    const nodeList = nodes.data ?? [];
    const nodeName =
      nodeList.length > 1
        ? (selectedNode(nodeList, state.node)?.name ?? `Node ${state.node}`)
        : null;
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
      memoryIsPanelDefault,
      gamePort:
        state.gamePort ??
        (ports.data?.suggested ? String(ports.data.suggested) : ""),
      ports: ports.data,
      resolved,
      resolveOk: isModpack && resolve.isSuccess,
      resolvePending: isModpack && resolve.isLoading,
      softwareSummary,
      nodeName,
      nightlyBackupAllowed: backupAllowed,
    };
  }, [
    backupAllowed,
    isModpack,
    isImport,
    state.importFile,
    state.node,
    nodes.data,
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
    panelDefaultMemoryMb,
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
  const { can, user } = useAuth();
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
  const panelSettings = useQuery({
    queryKey: qk.settings,
    queryFn: () => api.get<SystemSettings>("/system/settings"),
    enabled: can("system.settings"),
    staleTime: 60_000,
    retry: false,
  });
  const panelDefaultMb = Number(panelSettings.data?.values.default_memory_mb) || null;
  const d = useDerived(state, nightlyBackupAllowed(user), panelDefaultMb);
  const nodes = useWizardNodes();
  const importer = useImportDeploy();

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
    // a server that starts right away must fit into the node's memory: "Start anyway?" like a power start
    mutationFn: ({ payload }: { payload: CreatePayload; importFile?: File; overwrite?: boolean }) =>
      postOrDecline<CreateResponse>("/instances/", { id: "", name: payload.name }, { ...payload }),
    onSuccess: (res, vars) => {
      // which minute the nightly backup got (it differs per server), in the words of the Schedules page
      const backupNote = res.backup_schedule
        ? ` Nightly backup: ${describeCron(res.backup_schedule.cron)?.toLowerCase() ?? res.backup_schedule.cron} (panel time).`
        : "";
      if (vars.importFile) {
        toast.success(`Created ${res.instance.name}`, {
          description: `Preparing it for the import; the archive upload starts next.${backupNote}`,
        });
        for (const w of res.warnings ?? []) toast.warning(w);
        qc.setQueryData(qk.instance(res.instance.id), res.instance);
        qc.invalidateQueries({ queryKey: qk.instances });
        qc.invalidateQueries({ queryKey: qk.jobs });
        qc.invalidateQueries({ queryKey: mpk.ports });
        window.scrollTo({ top: 0, behavior: "smooth" });
        void importer.begin(res, vars.importFile, vars.overwrite ?? true);
        return;
      }
      toast.success(`Deploying ${res.instance.name}`, {
        description: `${
          state.start ? "Installing now; the server starts when the install finishes." : "Installing now; it will stay stopped."
        }${backupNote}`,
      });
      for (const w of res.warnings ?? []) toast.warning(w);
      qc.setQueryData(qk.instance(res.instance.id), res.instance);
      qc.invalidateQueries({ queryKey: qk.instances });
      qc.invalidateQueries({ queryKey: qk.jobs });
      qc.invalidateQueries({ queryKey: mpk.ports });
      navigate(`/servers/${res.instance.id}`);
    },
    onError: (err) => {
      if (err instanceof OvercommitDeclined) return; // nothing was created
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
    const importProblem = state.category === "import" ? importPermissionProblem(user) : null;
    if (importProblem) {
      toast.error("This server could not be imported", { description: importProblem });
      return;
    }
    for (const s of STEPS) {
      const errs = validateStep(s.id, state, d);
      if (Object.keys(errs).length) {
        setAttempted((a) => new Set(a).add(s.id));
        if (s.id !== step) setStep(s.id);
        return;
      }
    }
    create.mutate({
      payload: buildPayload(state, d),
      importFile: state.category === "import" ? (state.importFile ?? undefined) : undefined,
      overwrite: state.importOverwrite,
    });
  };

  const onCategory = (c: Category) => {
    if (c === state.category) return;
    patch({
      category: c,
      serverType:
        c in CATEGORY_TYPES
          ? CATEGORY_TYPES[c as StandardCategory][0]
          : null, // import: nothing preselected (see useDerived)
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
  const showSummary = !wide && step !== "review" && !!state.category;
  const stepLabels = {
    software: d.isModpack ? "Modpack" : importMode ? "Software & files" : "Software",
  };
  const nodeList = nodes.data ?? [];
  const multiNode = nodeList.length > 1;

  if (importer.run) {
    return (
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 pb-2">
        <PageHeader
          title="Create server"
          description="The server exists now; its files are being imported."
          className="pb-0"
        />
        <ImportProgress run={importer.run} onRetry={(file) => void importer.retry(file)} />
      </div>
    );
  }
  const stepTitle =
    d.isModpack && step === "software" ? "Modpack & release" : STEPS[idx].label;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 pb-2">
      <PageHeader
        title="Create server"
        description={
          importMode
            ? "Bring an existing server: pick its software, choose an archive of its folder, size it and deploy."
            : "Pick the software, size it, and deploy. The install runs in the background and you can watch it on the server page."
        }
        actions={
          <Button asChild variant="ghost" size="sm">
            <Link to="/servers">Cancel</Link>
          </Button>
        }
        className="pb-0"
      />
      <Stepper
        current={step}
        maxReached={maxReached}
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
          {step === "software" && importMode && (
            <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
              <ImportSoftware patch={patch} d={d} errors={errors} />
              <StepSoftware state={state} patch={patch} d={d} errors={errors} />
              <ArchivePicker state={state} patch={patch} errors={errors} />
            </div>
          )}
          {step === "software" && !importMode && (
            <StepSoftware state={state} patch={patch} d={d} errors={errors} />
          )}
          {step === "resources" && (
            <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
              {multiNode && (
                <NodeSelect nodes={nodeList} state={state} patch={patch} errors={errors} />
              )}
              <StepResources state={state} patch={patch} d={d} errors={errors} />
            </div>
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
                        ...(d.nodeName ? [["Node", d.nodeName]] : []),
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
          {importMode && step === "review"
            ? "The server is created first, then the archive is uploaded."
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
            ) : importMode ? (
              <Upload />
            ) : (
              <Rocket />
            )}
            {create.isPending
              ? importMode
                ? "Creating…"
                : "Deploying…"
              : importMode
                ? "Create & import"
                : "Deploy server"}
          </Button>
        ) : (
          <Button type="button" onClick={next}>
            {step === "gameplay" ? "Review" : "Next"} <ArrowRight />
          </Button>
        )}
      </div>
    </div>
  );
}
