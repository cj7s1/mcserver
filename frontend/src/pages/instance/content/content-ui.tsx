/** Shared building blocks for the mods/plugins manager. */
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ChevronLeft, ChevronRight, Download, ExternalLink, Info, Package, X, XCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { api, errorMessage } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { contentPath, useInvalidateContent, type InstallResponse, type MissingDependency, type nouns } from "./content-api";
import { reportFromInstall, type ContentReport } from "./content-report";

/** Everything the tabs need from the page. */
export interface ContentEnv {
  instance: Instance;
  kind: "mods" | "plugins";
  noun: ReturnType<typeof nouns>;
  /** Folder inside the server directory, e.g. "mods". */
  directory: string;
  platform: string;
  minecraftVersion: string | null;
  /** User holds instance.content. */
  canEdit: boolean;
  /** Why mutations are blocked right now (archived, another job running), or null. */
  locked: string | null;
  /** Toast a successful change and remember that a restart is needed when it is. */
  notify: (message: string, restartRequired: boolean, description?: string) => void;
  showReport: (report: ContentReport | null) => void;
  /** Switch to the Browse tab with a search prefilled. */
  browseFor: (query: string) => void;
}

export function ProjectIcon({ url, className }: { url?: string | null; className?: string }) {
  const [failed, setFailed] = useState(false);
  if (url && !failed) {
    return (
      <img
        src={url}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
        className={cn("size-9 shrink-0 rounded-md bg-muted object-cover", className)}
      />
    );
  }
  return (
    <div className={cn("flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground", className)} aria-hidden>
      <Package className="size-4" />
    </div>
  );
}

const NOTICE_TONES = {
  info: { box: "border-sky-500/30 bg-sky-500/10", icon: "text-sky-600 dark:text-sky-400", Icon: Info },
  warning: { box: "border-amber-500/30 bg-amber-500/10", icon: "text-amber-600 dark:text-amber-400", Icon: AlertTriangle },
  error: { box: "border-destructive/30 bg-destructive/5", icon: "text-destructive", Icon: XCircle },
} as const;

export function Notice({
  tone = "info",
  title,
  children,
  action,
  className,
}: {
  tone?: keyof typeof NOTICE_TONES;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const t = NOTICE_TONES[tone];
  return (
    <div className={cn("flex flex-col gap-2 rounded-lg border px-3 py-2.5 text-sm sm:flex-row sm:items-center", t.box, className)}>
      <div className="flex min-w-0 flex-1 gap-2.5">
        <t.Icon className={cn("mt-0.5 size-4 shrink-0", t.icon)} aria-hidden />
        <div className="min-w-0">
          {title && <p className="font-medium">{title}</p>}
          {children && <div className={cn("text-muted-foreground", title && "mt-0.5")}>{children}</div>}
        </div>
      </div>
      {action && <div className="flex shrink-0 flex-wrap gap-2 sm:ml-2">{action}</div>}
    </div>
  );
}

// --- provider search results (Hangar, SpigotMC) ------------------------------------------
/** One search hit from a plugin source; same layout as the Modrinth browser rows. */
export function SourceResultRow({
  title,
  url,
  site,
  author,
  description,
  iconUrl,
  meta,
  action,
}: {
  title: string;
  /** Project page (already checked to be http(s)). */
  url: string | null;
  /** Site name for link labels, e.g. "Hangar". */
  site: string;
  author?: string | null;
  description?: string | null;
  iconUrl?: string | null;
  /** Small stats and badges under the description. */
  meta?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <li className="flex flex-col gap-3 rounded-lg border bg-card p-3 sm:flex-row sm:items-start">
      <div className="flex min-w-0 flex-1 gap-3">
        <ProjectIcon url={iconUrl} className="size-12 rounded-lg" />
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
            {url ? (
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                className="truncate font-medium hover:underline focus-visible:underline"
                aria-label={`${title} on ${site} (opens in a new tab)`}
              >
                {title}
              </a>
            ) : (
              <span className="truncate font-medium">{title}</span>
            )}
            {author && <span className="text-xs text-muted-foreground">by {author}</span>}
          </div>
          {description && <p className="mt-0.5 line-clamp-2 text-sm break-words text-muted-foreground">{description}</p>}
          {meta && <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">{meta}</div>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2 sm:flex-col sm:items-end">
        {action}
        {url && (
          <Button asChild variant="ghost" size="icon-sm" className="sm:hidden">
            <a href={url} target="_blank" rel="noopener noreferrer" aria-label={`Open ${title} on ${site}`}>
              <ExternalLink />
            </a>
          </Button>
        )}
      </div>
    </li>
  );
}

/** "Installed" pill, a (possibly locked) install button, or nothing for read-only users. */
export function InstallAction({
  env,
  installed,
  title,
  onInstall,
  label = "Install…",
}: {
  env: ContentEnv;
  installed: boolean;
  title: string;
  onInstall: () => void;
  label?: string;
}) {
  if (installed)
    return (
      <span className="inline-flex h-7 items-center gap-1 rounded-lg bg-emerald-500/10 px-2.5 text-xs font-medium text-emerald-700 dark:text-emerald-300">
        <CheckCircle2 className="size-3.5" aria-hidden /> Installed
      </span>
    );
  if (!env.canEdit) return null;
  if (env.locked)
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0}>
            <Button size="sm" disabled>
              <Download /> Install
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent>{env.locked}</TooltipContent>
      </Tooltip>
    );
  return (
    <Button size="sm" onClick={onInstall} aria-label={`Install ${title}`}>
      <Download /> {label}
    </Button>
  );
}

export function Pager({
  label,
  summary,
  canPrev,
  canNext,
  busy,
  onPrev,
  onNext,
}: {
  label: string;
  summary: ReactNode;
  canPrev: boolean;
  canNext: boolean;
  busy: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <nav className="flex flex-wrap items-center justify-between gap-2" aria-label={label}>
      <span className="text-xs text-muted-foreground tabular-nums">{summary}</span>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" disabled={!canPrev || busy} onClick={onPrev}>
          <ChevronLeft /> Previous
        </Button>
        <Button size="sm" variant="outline" disabled={!canNext || busy} onClick={onNext}>
          Next <ChevronRight />
        </Button>
      </div>
    </nav>
  );
}

// --- operation reports ------------------------------------------------------------------
const LINE_ICON = {
  ok: <CheckCircle2 className="size-3.5 text-emerald-600 dark:text-emerald-400" aria-hidden />,
  warn: <AlertTriangle className="size-3.5 text-amber-600 dark:text-amber-400" aria-hidden />,
  error: <XCircle className="size-3.5 text-destructive" aria-hidden />,
  muted: <span className="inline-block size-3.5 text-center text-muted-foreground" aria-hidden>·</span>,
};

const REPORT_TONES = {
  success: "border-emerald-500/30 bg-emerald-500/5",
  warning: "border-amber-500/30 bg-amber-500/5",
  error: "border-destructive/30 bg-destructive/5",
};

function MissingDependencyRow({ dep, env }: { dep: MissingDependency; env: ContentEnv }) {
  const invalidate = useInvalidateContent(env.instance.id);
  const install = useMutation({
    mutationFn: (projectId: string) =>
      api.post<InstallResponse>(contentPath(env.instance.id, "install"), { project_id: projectId, install_dependencies: true }),
    onSuccess: (res) => {
      invalidate();
      const r = reportFromInstall(res, env.noun);
      env.notify(r.title, res.restart_required);
      env.showReport(r.missing.length || r.warnings.length ? r : null);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  const name = dep.title || dep.slug || dep.project_id || dep.file_name || "Unknown dependency";
  const canInstall = env.canEdit && !env.locked && dep.reason === "not_installed" && !!dep.project_id;
  return (
    <li className="flex flex-col gap-1.5 rounded-md border bg-background/60 px-2.5 py-2 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{name}</p>
        <p className="text-xs text-muted-foreground">
          Required by {dep.required_by}
          {dep.message ? ` · ${dep.message}` : dep.reason === "not_installed" ? " · not installed" : ""}
        </p>
      </div>
      {canInstall && (
        <Button size="xs" variant="outline" disabled={install.isPending} onClick={() => install.mutate(dep.project_id!)}>
          <Download /> {install.isPending ? "Installing…" : "Install"}
        </Button>
      )}
      {!canInstall && dep.reason !== "disabled" && dep.reason !== "not_on_modrinth" && (dep.title || dep.slug) && (
        <Button size="xs" variant="ghost" onClick={() => env.browseFor(dep.title || dep.slug || "")}>
          Search
        </Button>
      )}
    </li>
  );
}

export function ReportCard({ report, env, onDismiss }: { report: ContentReport; env: ContentEnv; onDismiss: () => void }) {
  return (
    <section className={cn("rounded-xl border p-4", REPORT_TONES[report.tone])} aria-live="polite" aria-label="Result of the last change">
      <div className="flex items-start gap-2">
        <h3 className="min-w-0 flex-1 text-sm font-semibold">{report.title}</h3>
        <Button variant="ghost" size="icon-xs" aria-label="Dismiss result" onClick={onDismiss}>
          <X />
        </Button>
      </div>
      {report.lines.length > 0 && (
        <ul className="mt-2 grid max-h-56 gap-1 overflow-auto text-xs">
          {report.lines.map((l, i) => (
            <li key={i} className="flex items-start gap-2">
              <span className="mt-0.5 shrink-0">{LINE_ICON[l.tone]}</span>
              <span className="min-w-0">
                <span className="font-mono break-all">{l.label}</span>
                {l.detail && <span className="text-muted-foreground"> — {l.detail}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
      {report.warnings.length > 0 && (
        <ul className="mt-3 grid gap-1 text-xs text-amber-700 dark:text-amber-300">
          {report.warnings.map((w) => (
            <li key={w} className="flex gap-2">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              {w}
            </li>
          ))}
        </ul>
      )}
      {report.missing.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-medium">Missing required dependencies</p>
          <p className="text-xs text-muted-foreground">The server may fail to start until these are installed or enabled.</p>
          <ul className="mt-2 grid gap-1.5">
            {report.missing.map((d, i) => (
              <MissingDependencyRow key={`${d.project_id ?? d.file_name ?? i}`} dep={d} env={env} />
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
