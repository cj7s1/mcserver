/** Turns content API results into the report card shown after a change. */
import type { InstallResponse, MissingDependency, UpdateResponse, UploadResponse, nouns } from "./content-api";

export interface ReportLine {
  tone: "ok" | "warn" | "error" | "muted";
  label: string;
  detail?: string;
}

export interface ContentReport {
  title: string;
  tone: "success" | "warning" | "error";
  lines: ReportLine[];
  missing: MissingDependency[];
  warnings: string[];
}

export function reportFromInstall(res: InstallResponse, noun: ReturnType<typeof nouns>): ContentReport {
  const main = res.installed.find((i) => !i.dependency) ?? res.installed[0];
  const deps = res.installed.filter((i) => i.dependency).length;
  const title = main
    ? `Installed ${main.title ?? main.filename}${main.version_number ? ` ${main.version_number}` : ""}${
        deps ? ` and ${deps} ${deps === 1 ? "dependency" : "dependencies"}` : ""
      }`
    : `Nothing was installed`;
  return {
    title,
    tone: res.missing_dependencies.length || res.warnings.length ? "warning" : "success",
    lines: res.installed.map((i) => ({
      tone: "ok",
      label: i.filename,
      detail: i.dependency ? `dependency of ${i.required_by ?? `the ${noun.one}`}` : (i.version_type ?? undefined),
    })),
    missing: res.missing_dependencies,
    warnings: res.warnings,
  };
}

export function reportFromUpdate(res: UpdateResponse): ContentReport {
  const parts = [`${res.updated} updated`];
  const upToDate = res.results.filter((r) => r.status === "up_to_date").length;
  const skipped = res.results.filter((r) => r.status === "skipped").length;
  if (upToDate) parts.push(`${upToDate} already up to date`);
  if (skipped) parts.push(`${skipped} skipped`);
  if (res.failed) parts.push(`${res.failed} failed`);
  const warnings = res.results.filter((r) => r.warning).map((r) => `${r.filename}: ${r.warning}`);
  return {
    title: `Update finished: ${parts.join(", ")}`,
    tone: res.failed ? (res.updated ? "warning" : "error") : res.missing_dependencies.length || warnings.length ? "warning" : "success",
    lines: res.results.map((r) => {
      if (r.status === "updated")
        return {
          tone: "ok",
          label: r.new_filename ?? r.filename,
          detail: `${r.from_version?.version_number ?? "?"} → ${r.to_version?.version_number ?? "?"}`,
        };
      if (r.status === "failed") return { tone: "error", label: r.filename, detail: r.message ?? r.reason ?? "failed" };
      if (r.status === "skipped") return { tone: "warn", label: r.filename, detail: r.message ?? "skipped" };
      return { tone: "muted", label: r.filename, detail: "already the newest compatible version" };
    }),
    missing: res.missing_dependencies,
    warnings,
  };
}

export function reportFromUpload(res: UploadResponse): ContentReport {
  const id = res.identified;
  const lines: ReportLine[] = [
    {
      tone: "ok",
      label: res.uploaded.filename,
      detail: id
        ? `identified on Modrinth as ${id.project.title ?? id.project.id}${id.version.version_number ? ` ${id.version.version_number}` : ""}`
        : "not found on Modrinth (treated as a manual file)",
    },
  ];
  if (res.detected_platforms.length)
    lines.push({ tone: "muted", label: "Detected platform", detail: res.detected_platforms.join(", ") });
  return {
    title: `Uploaded ${res.uploaded.filename}`,
    tone: res.warnings.length ? "warning" : "success",
    lines,
    missing: [],
    warnings: res.warnings,
  };
}
