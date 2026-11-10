import { AlertTriangle, ExternalLink, Pencil, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { useId } from "react";
import { Section } from "@/components/PageHeader";
import { ServerIcon } from "@/components/ServerIcon";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { formatBytes, formatMemoryMb } from "@/lib/format";
import { loaderLabel, providerLabel } from "../modpacks/modpackMeta";
import { SummaryList } from "./WizardBits";
import {
  containerLimitMb,
  isModded,
  type Derived,
  envObject,
  type FieldErrors,
  serverProperties,
  type StepId,
  TYPE_LABELS,
  type WizardState,
} from "./wizardModel";

const CATEGORY_LABELS: Record<string, string> = {
  vanilla: "Vanilla",
  plugins: "Plugin server",
  mods: "Modded",
  modpack: "Modpack",
  import: "Import",
};

const PRESET_LABELS: Record<string, string> = {
  aikar: "Aikar's flags",
  meowice: "MeowIce flags",
  none: "None",
};

/** Rows describing the software selection; shared by the sidebar summary and the review step. */
export function softwareRows(
  state: WizardState,
  d: Derived,
): [ReactNode, ReactNode][] {
  if (!state.category) return [["Type", "Not chosen"]];
  if (d.isModpack && state.modpackSource === "url") {
    return [
      ["Type", "Modpack · CurseForge (by page URL)"],
      ["Modpack", state.cfRef.trim() || "Not entered"],
      [
        "File",
        state.cfFileId.trim() ||
          "Newest server-compatible file (pinned after install)",
      ],
      ["Minecraft", state.cfMinecraftVersion.trim() || "—"],
      [
        "Java",
        state.javaOverride ? `Java ${state.javaOverride} (override)` : "Auto",
      ],
    ];
  }
  if (d.isModpack) {
    const r = d.resolved;
    return [
      ["Type", `Modpack · ${providerLabel(state.provider)}`],
      [
        "Modpack",
        r?.name ?? state.projectHit?.title ?? (state.projectId || "Not chosen"),
      ],
      [
        "Release",
        r ? r.version_name || r.version_id : state.versionId || "Not chosen",
      ],
      ["Minecraft", r?.minecraft_version ?? "—"],
      [
        "Loader",
        r
          ? `${loaderLabel(r.loader)}${r.loader_version ? ` ${r.loader_version}` : ""}`
          : "—",
      ],
      [
        "Java",
        d.javaVersion
          ? `Java ${d.javaVersion}${state.javaOverride ? " (override)" : ""}`
          : "Auto",
      ],
    ];
  }
  const rows: [ReactNode, ReactNode][] = [
    [
      "Type",
      `${CATEGORY_LABELS[state.category]}${d.serverType && state.category !== "vanilla" ? ` · ${TYPE_LABELS[d.serverType]}` : ""}`,
    ],
    ["Minecraft", d.minecraftVersion || "Not chosen"],
  ];
  if (d.serverType && d.serverType !== "VANILLA")
    rows.push([d.loaderLabel ?? "Build", d.loaderVersion || "Latest (auto)"]);
  rows.push([
    "Java",
    d.javaVersion
      ? `Java ${d.javaVersion}${state.javaOverride ? " (override)" : ""}`
      : "Auto",
  ]);
  if (state.category === "import")
    rows.push([
      "Archive",
      state.importFile ? (
        <span key="archive" className="break-all">
          {state.importFile.name}{" "}
          <span className="text-muted-foreground">
            · {formatBytes(state.importFile.size)}
          </span>
        </span>
      ) : (
        "Not chosen"
      ),
    ]);
  return rows;
}

function Group({
  title,
  step,
  onEdit,
  children,
}: {
  title: string;
  step: StepId;
  onEdit: (s: StepId) => void;
  children: ReactNode;
}) {
  return (
    <div className="grid content-start gap-2 rounded-lg border p-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {title}
        </h3>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          onClick={() => onEdit(step)}
          aria-label={`Edit ${title.toLowerCase()}`}
        >
          <Pencil /> Edit
        </Button>
      </div>
      {children}
    </div>
  );
}

export function StepReview({
  state,
  patch,
  d,
  errors,
  onEdit,
}: {
  state: WizardState;
  patch: (p: Partial<WizardState>) => void;
  d: Derived;
  errors: FieldErrors;
  onEdit: (s: StepId) => void;
}) {
  const uid = useId();
  const props = serverProperties(state);
  const env = envObject(state.env);
  const limit = Number.isFinite(d.memoryMb)
    ? containerLimitMb(d.memoryMb, isModded(state, d))
    : null;
  const warnings: string[] = [...(d.resolved?.warnings ?? [])];
  if (!state.onlineMode)
    warnings.push("Online mode is off: anyone can join with any username.");
  const importing = state.category === "import";
  if (importing) {
    warnings.push(
      "Keep this browser tab open until the archive is uploaded. The server is created stopped, so you can check the files before its first start.",
    );
    if (state.importOverwrite && Object.keys(props).length)
      warnings.push(
        "The archive's server.properties (if it has one) replaces the gameplay settings chosen here.",
      );
  } else if (!state.start)
    warnings.push("The server will be installed but not started.");
  if (d.nightlyBackupAllowed && !state.nightlyBackup)
    warnings.push(
      "No backup schedule will be created. Add one later on the server's Schedules page; until then it is only backed up by hand.",
    );
  const icon = state.iconUrl.trim() || d.resolved?.icon_url || null;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4">
      <Section
        title="Review"
        description="Check everything before deploying. Most settings can be changed later."
      >
        <div className="grid gap-4">
          <div className="flex items-center gap-3">
            <ServerIcon
              key={icon ?? "none"}
              url={icon}
              name={d.name || "?"}
              type={d.serverType ?? undefined}
              className="size-12"
            />
            <div className="min-w-0">
              <div className="truncate text-base font-semibold">
                {d.name || "Unnamed server"}
              </div>
              <div className="truncate text-sm text-muted-foreground">
                {d.softwareSummary}
              </div>
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            <Group title="Software" step="software" onEdit={onEdit}>
              <SummaryList rows={softwareRows(state, d)} />
            </Group>
            <Group title="Resources & network" step="resources" onEdit={onEdit}>
              <SummaryList
                rows={[
                  [
                    "Memory",
                    Number.isFinite(d.memoryMb)
                      ? `${formatMemoryMb(d.memoryMb)} heap${limit ? ` · ${formatMemoryMb(limit)} container limit` : ""}`
                      : "—",
                  ],
                  ...(d.nodeName
                    ? ([["Node", d.nodeName]] as [ReactNode, ReactNode][])
                    : []),
                  ["Port", d.gamePort ? `${d.gamePort}/tcp` : "—"],
                  ["Bind address", state.hostIp || "Panel default"],
                  [
                    "After install",
                    importing
                      ? `Upload and extract the archive${state.importOverwrite ? " (its files win)" : " (keep new defaults)"}, leave stopped`
                      : state.start
                        ? "Start the server"
                        : "Leave stopped",
                  ],
                  ...(d.nightlyBackupAllowed
                    ? ([
                        ["Backups", state.nightlyBackup ? "Nightly backup schedule (03:00–05:00, panel time)" : "No schedule"],
                      ] as [ReactNode, ReactNode][])
                    : []),
                ]}
              />
            </Group>
            <Group title="Gameplay" step="gameplay" onEdit={onEdit}>
              {Object.keys(props).length ? (
                <SummaryList
                  rows={Object.entries(props).map(([k, v]) => [
                    <span key={k} className="font-mono text-xs">
                      {k}
                    </span>,
                    String(v),
                  ])}
                />
              ) : (
                <p className="text-sm text-muted-foreground">
                  Minecraft defaults (MOTD = server name)
                </p>
              )}
            </Group>
            <Group title="Advanced" step="gameplay" onEdit={onEdit}>
              <SummaryList
                rows={[
                  ["JVM preset", PRESET_LABELS[state.jvmPreset]],
                  [
                    "JVM args",
                    state.jvmArgs.trim() ? (
                      <span key="jvm" className="font-mono text-xs">
                        {state.jvmArgs.trim()}
                      </span>
                    ) : (
                      "None"
                    ),
                  ],
                  [
                    "Environment",
                    Object.keys(env).length ? (
                      <span key="env" className="font-mono text-xs">
                        {Object.keys(env).join(", ")}
                      </span>
                    ) : (
                      "None"
                    ),
                  ],
                  ["Group", state.group.trim() || "None"],
                  ...(state.description.trim()
                    ? ([
                        [
                          "Description",
                          <span key="desc" className="line-clamp-2">
                            {state.description.trim()}
                          </span>,
                        ],
                      ] as [ReactNode, ReactNode][])
                    : []),
                ]}
              />
            </Group>
          </div>
          {warnings.length > 0 && (
            <ul className="grid gap-1 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200">
              {warnings.map((w) => (
                <li key={w} className="flex gap-1.5">
                  <AlertTriangle
                    className="mt-0.5 size-3.5 shrink-0"
                    aria-hidden
                  />
                  {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      </Section>

      <Section>
        <div className="grid gap-2">
          <div className="flex items-start gap-3">
            <Checkbox
              id={`${uid}-eula`}
              checked={state.acceptEula}
              onCheckedChange={(v) => patch({ acceptEula: v === true })}
              aria-invalid={!!errors.acceptEula || undefined}
              aria-describedby={
                errors.acceptEula ? `${uid}-eula-error` : undefined
              }
              className="mt-0.5"
            />
            <div className="grid gap-1">
              <Label htmlFor={`${uid}-eula`} className="leading-snug">
                I have read and accept the Minecraft End User License Agreement
              </Label>
              <a
                href="https://aka.ms/MinecraftEULA"
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex w-fit items-center gap-1 text-xs text-primary underline-offset-4 hover:underline"
              >
                Read the Minecraft EULA{" "}
                <ExternalLink className="size-3" aria-hidden />
              </a>
            </div>
          </div>
          {errors.acceptEula && (
            <p
              id={`${uid}-eula-error`}
              className="text-xs text-destructive"
              role="alert"
            >
              {errors.acceptEula}
            </p>
          )}
        </div>
      </Section>

      {errors.form && (
        <div
          className="flex gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
          role="alert"
        >
          <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <p>{errors.form}</p>
        </div>
      )}
    </div>
  );
}
