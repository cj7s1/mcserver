import { AlertTriangle, Coffee, PackageSearch } from "lucide-react";
import { useCallback, useId, useMemo, useRef } from "react";
import { Section } from "@/components/PageHeader";
import { EmptyState, ErrorState } from "@/components/States";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { errorMessage } from "@/lib/api";
import { variantLabel } from "@/lib/games";
import type { Project, ServerType } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  ModpackBrowser,
  type ModpackFilters,
} from "../modpacks/ModpackBrowser";
import {
  type CatalogVersions,
  type LoaderEntry,
  type ReleaseData,
  useGameVersions,
  useJavaInfo,
  useLoaderVersions,
  useProviders,
  useServerTypes,
} from "../modpacks/modpackQueries";
import { ProjectDetail } from "../modpacks/ProjectDetail";
import { ReleasePicker } from "../modpacks/ReleasePicker";
import { ResolvePreview } from "../modpacks/ResolvePreview";
import {
  VersionCombobox,
  type VersionOption,
} from "../modpacks/VersionCombobox";
import { Advanced, ChoiceCard, describedBy, Field } from "./WizardBits";
import {
  CATEGORY_TYPES,
  CF_LOADER_LABELS,
  CF_LOADERS,
  type CfLoader,
  type Derived,
  type FieldErrors,
  JAVA_VERSIONS,
  type StandardCategory,
  type WizardState,
} from "./wizardModel";

export const AUTO_LOADER = "__auto__";
const AUTO_JAVA = "__auto__";

type Patch = (p: Partial<WizardState>) => void;

/** Newest stable Minecraft version from a catalog list (prefers Mojang's latest release). */
export function defaultMinecraftVersion(
  data: CatalogVersions | undefined,
): string {
  const list = data?.versions ?? [];
  if (!list.length) return "";
  const latest = list.find(
    (v) => v.version === data?.latest_release && v.stable,
  );
  return (latest ?? list.find((v) => v.stable) ?? list[0]).version;
}

/** Recommended build if the upstream marks one, else the newest stable build, else auto. */
export function defaultLoaderVersion(
  builds: LoaderEntry[] | undefined,
): string {
  if (!builds?.length) return "";
  return (
    builds.find((b) => b.recommended) ??
    builds.find((b) => b.stable) ??
    builds[0]
  ).version;
}

function versionTags(
  serverType: ServerType,
  v: CatalogVersions["versions"][number],
  latest: string | undefined,
): string[] {
  const tags: string[] = [];
  if (v.version === latest) tags.push("latest");
  if (serverType === "VANILLA") {
    if (v.type && v.type !== "release") tags.push(v.type.replace("old_", ""));
  } else if (v.support) {
    if (v.support !== "SUPPORTED") tags.push(v.support.toLowerCase());
  } else if (!v.stable) {
    tags.push("unstable");
  }
  return tags;
}

function loaderOptionLabel(b: LoaderEntry): string {
  const notes: string[] = [];
  if (b.recommended) notes.push("recommended");
  if (b.latest) notes.push("latest");
  if (b.channel && b.channel !== "STABLE") notes.push(b.channel.toLowerCase());
  else if (!b.stable && !b.recommended && !b.channel) notes.push("beta");
  return notes.length ? `${b.version} · ${notes.join(", ")}` : b.version;
}

export function JavaOverride({
  state,
  patch,
  d,
  error,
}: {
  state: WizardState;
  patch: Patch;
  d: Derived;
  error?: string;
}) {
  const id = useId();
  const autoLabel = d.java
    ? `Automatic (Java ${d.java.java_version})`
    : "Automatic";
  const override = state.javaOverride ? Number(state.javaOverride) : null;
  const tooOld = override !== null && d.java && override < d.java.required;
  const hint = tooOld
    ? undefined
    : "Only override when a pack or plugin needs a specific Java runtime. The runtime image is chosen from this.";
  return (
    <Advanced description="Java runtime" hasError={!!error}>
      <Field
        id={id}
        label="Java version"
        hint={hint}
        error={error}
        className="max-w-sm"
      >
        <Select
          value={state.javaOverride || AUTO_JAVA}
          onValueChange={(v) =>
            patch({ javaOverride: v === AUTO_JAVA ? "" : v })
          }
        >
          <SelectTrigger
            id={id}
            className="w-full"
            aria-invalid={!!error || undefined}
            aria-describedby={describedBy(id, error, hint)}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={AUTO_JAVA}>{autoLabel}</SelectItem>
            {JAVA_VERSIONS.map((j) => (
              <SelectItem key={j} value={String(j)}>
                Java {j}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      {tooOld && d.java && (
        <p
          className="mt-2 flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300"
          role="alert"
        >
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
          Java {override} is older than the Java {d.java.required} that
          Minecraft {d.minecraftVersion} requires; the server will probably fail
          to start.
        </p>
      )}
    </Advanced>
  );
}

function JavaLine({
  d,
  loading,
  error,
}: {
  d: Derived;
  loading: boolean;
  error: unknown;
}) {
  return (
    <div className="flex items-start gap-2 rounded-lg border bg-muted/20 px-3 py-2 text-sm">
      <Coffee
        className="mt-0.5 size-4 shrink-0 text-muted-foreground"
        aria-hidden
      />
      <div className="min-w-0">
        {loading ? (
          <span className="text-muted-foreground">Resolving Java version…</span>
        ) : error ? (
          <span className="text-muted-foreground">
            Java could not be resolved ({errorMessage(error)}); the backend
            picks it at install.
          </span>
        ) : d.java ? (
          <>
            <span className="font-medium">Java {d.javaVersion}</span>
            <span className="text-muted-foreground">
              {" "}
              ·{" "}
              {d.javaVersion !== d.java.java_version
                ? "manual override"
                : d.java.reason}
              {d.java.required
                ? ` (Minecraft ${d.minecraftVersion} requires Java ${d.java.required}+)`
                : ""}
            </span>
          </>
        ) : (
          <span className="text-muted-foreground">
            Choose a Minecraft version to resolve Java.
          </span>
        )}
      </div>
    </div>
  );
}

function StandardSoftware({
  state,
  patch,
  d,
  errors,
}: {
  state: WizardState;
  patch: Patch;
  d: Derived;
  errors: FieldErrors;
}) {
  const uid = useId();
  const category = state.category as StandardCategory;
  const types = CATEGORY_TYPES[category] ?? ["VANILLA"];
  const serverTypes = useServerTypes();
  const versions = useGameVersions(d.serverType, state.showSnapshots);
  const loaders = useLoaderVersions(d.serverType, d.minecraftVersion);
  const java = useJavaInfo(d.minecraftVersion, d.serverType);
  const typeInfo = serverTypes.data?.find((t) => t.id === d.serverType);

  const versionOptions: VersionOption[] = useMemo(
    () =>
      (versions.data?.versions ?? []).map((v) => ({
        version: v.version,
        tags: versionTags(
          d.serverType ?? "VANILLA",
          v,
          versions.data?.latest_release,
        ),
        muted: !v.stable,
      })),
    [versions.data, d.serverType],
  );
  const hasLoader = d.serverType !== null && d.serverType !== "VANILLA";
  const loaderLabel =
    typeInfo?.loader_label ??
    (category === "plugins" ? "Build" : "Loader version");

  if (state.category === "import" && !d.serverType) {
    return (
      <Section title="Minecraft version">
        <p className="text-sm text-muted-foreground">Choose the software of the imported server first.</p>
      </Section>
    );
  }

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
      {types.length > 1 && (
        <Section
          title="Software"
          description={
            category === "plugins"
              ? "Plugins are installed from the Content tab after creation."
              : "Mods are installed from the Content tab after creation."
          }
        >
          <div
            role="radiogroup"
            aria-label="Server software"
            className="grid gap-2 sm:grid-cols-2"
          >
            {types.map((t) => {
              const info = serverTypes.data?.find((s) => s.id === t);
              return (
                <ChoiceCard
                  key={t}
                  name={`${uid}-type`}
                  value={t}
                  checked={d.serverType === t}
                  onChange={(v) =>
                    patch({
                      serverType: v as ServerType,
                      minecraftVersion: null,
                      loaderVersion: null,
                      // a Java picked for the previous choice may not run the new one
                      javaOverride: "",
                    })
                  }
                  title={info?.label ?? variantLabel(t)}
                  description={info?.description}
                />
              );
            })}
          </div>
          {errors.serverType && (
            <p className="mt-2 text-xs text-destructive" role="alert">
              {errors.serverType}
            </p>
          )}
        </Section>
      )}

      <Section
        title={`${variantLabel(d.serverType ?? "VANILLA")} version`}
        description={types.length === 1 ? typeInfo?.description : undefined}
      >
        <div className="grid gap-4">
          {versions.isError ? (
            <ErrorState
              error={versions.error}
              onRetry={() => versions.refetch()}
              title="Could not load Minecraft versions"
              className="py-6"
            />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                id={`${uid}-mc`}
                label="Minecraft version"
                error={errors.minecraftVersion}
                hint={
                  versions.data
                    ? `${versions.data.versions.length} versions from upstream metadata`
                    : undefined
                }
              >
                <VersionCombobox
                  id={`${uid}-mc`}
                  value={d.minecraftVersion}
                  onChange={(v) =>
                    patch({ minecraftVersion: v, loaderVersion: null, javaOverride: "" })
                  }
                  options={versionOptions}
                  loading={versions.isLoading}
                  invalid={!!errors.minecraftVersion}
                  aria-describedby={describedBy(
                    `${uid}-mc`,
                    errors.minecraftVersion,
                    versions.data ? "x" : undefined,
                  )}
                />
                <div className="flex items-center gap-2 pt-0.5">
                  <Switch
                    id={`${uid}-snap`}
                    size="sm"
                    checked={state.showSnapshots}
                    onCheckedChange={(v) => patch({ showSnapshots: v })}
                  />
                  <Label
                    htmlFor={`${uid}-snap`}
                    className="text-xs font-normal text-muted-foreground"
                  >
                    Show snapshots &amp; pre-releases
                  </Label>
                </div>
              </Field>

              {hasLoader && (
                <Field
                  id={`${uid}-loader`}
                  label={loaderLabel}
                  error={errors.loaderVersion}
                  hint={
                    loaders.isError
                      ? `Could not list builds (${errorMessage(loaders.error)}). "Latest (auto)" lets the runtime pick one.`
                      : d.loaderVersion === ""
                        ? "The runtime picks the newest recommended/stable build at install time."
                        : "Pinned build — reinstalls use exactly this version."
                  }
                >
                  <Select
                    value={
                      d.loaderVersion === "" ? AUTO_LOADER : d.loaderVersion
                    }
                    onValueChange={(v) =>
                      patch({ loaderVersion: v === AUTO_LOADER ? "" : v })
                    }
                    disabled={!d.minecraftVersion || loaders.isLoading}
                  >
                    <SelectTrigger
                      id={`${uid}-loader`}
                      className="w-full"
                      aria-invalid={!!errors.loaderVersion || undefined}
                      aria-describedby={`${uid}-loader-${errors.loaderVersion ? "error" : "hint"}`}
                    >
                      <SelectValue
                        placeholder={
                          loaders.isLoading
                            ? "Loading builds…"
                            : "Select a build"
                        }
                      />
                    </SelectTrigger>
                    <SelectContent position="popper" className="max-h-72">
                      <SelectItem value={AUTO_LOADER}>Latest (auto)</SelectItem>
                      {(loaders.data ?? []).map((b) => (
                        <SelectItem key={b.version} value={b.version}>
                          {loaderOptionLabel(b)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {loaders.isSuccess && !loaders.data.length && (
                    <p className="text-xs text-amber-700 dark:text-amber-300">
                      Upstream lists no builds for Minecraft{" "}
                      {d.minecraftVersion}; installation will likely fail. Pick
                      another version.
                    </p>
                  )}
                </Field>
              )}
            </div>
          )}
          <JavaLine d={d} loading={java.isLoading} error={java.error} />
          <JavaOverride
            state={state}
            patch={patch}
            d={d}
            error={errors.javaOverride}
          />
        </div>
      </Section>
    </div>
  );
}

/** Deploy a CurseForge pack by its page URL: works without a CurseForge API key (the runtime installs it). */
function CurseForgeByUrl({
  state,
  patch,
  errors,
}: {
  state: WizardState;
  patch: Patch;
  errors: FieldErrors;
}) {
  const base = useId();
  return (
    <Section
      title="CurseForge modpack by page URL"
      description="No CurseForge API key is needed: the server runtime downloads the pack itself, and the installed file is pinned after the first install."
      className="max-w-2xl"
    >
      <div className="grid gap-4">
        <Field
          id={`${base}-ref`}
          label="Modpack page URL or slug"
          hint={
            <>
              For example{" "}
              <span className="font-mono">
                https://www.curseforge.com/minecraft/modpacks/better-mc-forge-bmc4
              </span>
              . Add <span className="font-mono">/files/&lt;id&gt;</span> to
              install one exact file.
            </>
          }
          error={errors.cfRef ?? errors.projectId}
        >
          <Input
            id={`${base}-ref`}
            value={state.cfRef}
            placeholder="https://www.curseforge.com/minecraft/modpacks/…"
            onChange={(e) => patch({ cfRef: e.target.value })}
            aria-invalid={!!(errors.cfRef ?? errors.projectId)}
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field
            id={`${base}-mc`}
            label="Minecraft version"
            hint="Shown on the pack's CurseForge page; picks the Java version."
            error={errors.cfMinecraftVersion ?? errors.minecraftVersion}
          >
            <Input
              id={`${base}-mc`}
              value={state.cfMinecraftVersion}
              placeholder="1.20.1"
              inputMode="decimal"
              onChange={(e) => patch({ cfMinecraftVersion: e.target.value })}
              aria-invalid={
                !!(errors.cfMinecraftVersion ?? errors.minecraftVersion)
              }
            />
          </Field>
          <Field
            id={`${base}-file`}
            label="File id"
            optional
            hint="Empty = the newest server-compatible file."
            error={errors.cfFileId ?? errors.versionId}
          >
            <Input
              id={`${base}-file`}
              value={state.cfFileId}
              placeholder="e.g. 9104992"
              inputMode="numeric"
              onChange={(e) => patch({ cfFileId: e.target.value })}
              aria-invalid={!!(errors.cfFileId ?? errors.versionId)}
            />
          </Field>
        </div>
        <Field
          id={`${base}-loader`}
          label="Mod loader"
          hint="Shown on the pack's CurseForge page. The Mods tab uses it to find compatible mods."
          error={errors.cfLoader}
          className="max-w-xs"
        >
          <Select value={state.cfLoader} onValueChange={(v) => patch({ cfLoader: v as CfLoader })}>
            <SelectTrigger id={`${base}-loader`} className="w-full" aria-invalid={!!errors.cfLoader || undefined}>
              <SelectValue placeholder="Choose the loader" />
            </SelectTrigger>
            <SelectContent>
              {CF_LOADERS.map((l) => (
                <SelectItem key={l} value={l}>
                  {CF_LOADER_LABELS[l]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <p className="text-xs text-muted-foreground">
          Better MC: use the CurseForge editions (BMC4 Forge, BMC2 Fabric). The
          Modrinth editions need dozens of files that can only be downloaded by
          hand.
        </p>
      </div>
    </Section>
  );
}

function ModpackSoftware(props: {
  state: WizardState;
  patch: Patch;
  d: Derived;
  errors: FieldErrors;
}) {
  const { state, patch } = props;
  const { data: providers } = useProviders();
  const cf = providers?.find((p) => p.id === "curseforge");
  const offerUrl = !!cf && !cf.available;
  const source = offerUrl ? state.modpackSource : "browse";
  return (
    <div className="grid gap-4">
      {offerUrl && (
        <ToggleGroup
          type="single"
          variant="outline"
          value={source}
          onValueChange={(v) =>
            v && patch({ modpackSource: v as WizardState["modpackSource"] })
          }
          aria-label="How to choose the modpack"
          className="justify-start"
        >
          <ToggleGroupItem value="browse">
            Search Modrinth / FTB
          </ToggleGroupItem>
          <ToggleGroupItem value="url">CurseForge page URL</ToggleGroupItem>
        </ToggleGroup>
      )}
      {source === "url" ? (
        <CurseForgeByUrl state={state} patch={patch} errors={props.errors} />
      ) : (
        <ModpackBrowse {...props} />
      )}
    </div>
  );
}

function ModpackBrowse({
  state,
  patch,
  d,
  errors,
}: {
  state: WizardState;
  patch: Patch;
  d: Derived;
  errors: FieldErrors;
}) {
  const detailRef = useRef<HTMLDivElement>(null);
  const onBrowse = useCallback(
    (browse: ModpackFilters) => patch({ browse }),
    [patch],
  );
  const onProject = useCallback(
    (p: Project) => {
      if (p.id !== state.projectId) {
        patch({
          provider: p.provider,
          projectId: p.id,
          projectHit: p,
          versionId: "",
          javaOverride: "",
        });
      }
      if (window.matchMedia("(max-width: 1023px)").matches) {
        requestAnimationFrame(() =>
          detailRef.current?.scrollIntoView({
            behavior: "smooth",
            block: "start",
          }),
        );
      }
    },
    [patch, state.projectId],
  );
  const onRelease = useCallback(
    (r: ReleaseData) => patch({ versionId: r.id, javaOverride: "" }),
    [patch],
  );

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
      <Section
        title="Find a modpack"
        description="Search by name, then pick a pack to see its releases."
        className="min-w-0 self-start"
      >
        <ModpackBrowser
          filters={state.browse}
          onFiltersChange={onBrowse}
          selectedId={state.projectId}
          onSelect={onProject}
          layout="list"
          pageSize={10}
        />
      </Section>
      <div
        ref={detailRef}
        className="grid min-w-0 scroll-mt-16 content-start gap-4"
      >
        {!state.projectId ? (
          <EmptyState
            icon={<PackageSearch className="size-5" />}
            title="No modpack selected"
            description={
              <>
                Search on the left — for example{" "}
                <span className="font-medium text-foreground">Better MC</span> —
                and choose a pack.
              </>
            }
            className={cn(errors.projectId && "border-destructive/50")}
          />
        ) : (
          <>
            <Section title="Modpack">
              <ProjectDetail
                provider={state.provider}
                projectId={state.projectId}
                fallback={state.projectHit}
              />
            </Section>
            <Section
              title="Release"
              description="Pick the exact release to install. The server stays on it until you change it."
            >
              <div className="grid gap-3">
                <ReleasePicker
                  provider={state.provider}
                  projectId={state.projectId}
                  selectedId={state.versionId}
                  onSelect={onRelease}
                  autoSelectLatest
                />
                {state.versionId && (
                  <ResolvePreview
                    provider={state.provider}
                    projectId={state.projectId}
                    versionId={state.versionId}
                  />
                )}
                {errors.versionId && !d.resolvePending && (
                  <p className="text-xs text-destructive" role="alert">
                    {errors.versionId}
                  </p>
                )}
                {d.resolveOk && (
                  <JavaOverride
                    state={state}
                    patch={patch}
                    d={d}
                    error={errors.javaOverride}
                  />
                )}
              </div>
            </Section>
          </>
        )}
        {errors.projectId && (
          <p className="text-xs text-destructive" role="alert">
            {errors.projectId}
          </p>
        )}
      </div>
    </div>
  );
}

export function StepSoftware(props: {
  state: WizardState;
  patch: Patch;
  d: Derived;
  errors: FieldErrors;
}) {
  return props.d.isModpack ? (
    <ModpackSoftware {...props} />
  ) : (
    <StandardSoftware {...props} />
  );
}
