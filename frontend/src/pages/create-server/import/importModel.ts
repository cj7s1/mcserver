/**
 * Import mode of the create-server wizard: archive validation, naming and the readiness rules
 * used before uploading to POST /instances/<id>/import (ImportArchiveView in
 * backend/apps/instances/views.py: multipart field "file", optional ?overwrite=true; the
 * server must not be running/starting/installing/unhealthy and the archive is extracted
 * by an `extract_archive` job with strip="auto").
 */
import type { Instance, Me } from "@/lib/types";

/** Archive suffixes the backend accepts (ARCHIVE_SUFFIXES in apps/instances/views.py). */
export const ARCHIVE_SUFFIXES = [".tar.gz", ".tar.xz", ".tgz", ".txz", ".tar", ".zip"] as const;

/** For the file input: browsers match the last extension only, so list the compressed ones too. */
export const ARCHIVE_ACCEPT = ".zip,.tar,.gz,.tgz,.xz,.txz,application/zip,application/x-tar,application/gzip,application/x-xz";

/** MCPANEL_MAX_UPLOAD_BYTES default (backend/mcpanel/settings.py); the admin can change it. */
export const DEFAULT_UPLOAD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024;

/** Statuses in which the backend refuses an import (`must_stop`). */
export const IMPORT_BLOCKING_STATUSES = ["running", "starting", "installing", "unhealthy"] as const;

/** How long to wait for the instance to settle after its install job before uploading anyway. */
export const READY_TIMEOUT_MS = 60_000;

export function archiveSuffix(name: string): string | null {
  const lower = name.trim().toLowerCase();
  return ARCHIVE_SUFFIXES.find((s) => lower.endsWith(s) && lower.length > s.length) ?? null;
}

/** null when the file can be uploaded; otherwise the message to show. */
export function validateImportArchive(file: { name: string; size: number } | null | undefined): string | null {
  if (!file) return "Choose an archive of the server folder to import";
  if (!archiveSuffix(file.name)) return `${file.name} is not a supported archive. Use .zip, .tar, .tar.gz, .tgz or .tar.xz`;
  if (file.size === 0) return `${file.name} is empty`;
  return null;
}

/** A server name suggestion from the archive's file name ("My_World-backup.tar.gz" -> "My World-backup"). */
export function nameFromArchive(fileName: string): string {
  const suffix = archiveSuffix(fileName);
  const base = suffix ? fileName.trim().slice(0, fileName.trim().length - suffix.length) : fileName.trim();
  return base.replace(/_+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80);
}

/**
 * Why the signed-in user could not import into the server they are about to create, or null.
 * Mirrors InstanceListView.post: non-owners without global "instance.view" get the server
 * manager role on servers they create; everyone else keeps only their global permissions.
 */
export function importPermissionProblem(user: Pick<Me, "is_owner" | "permissions"> | null): string | null {
  if (!user || user.is_owner) return null;
  const perms = user.permissions;
  if (perms.includes("instance.view") && !perms.includes("instance.files.write")) {
    return "Your role can see all servers but not write their files, so the panel would refuse the archive upload. Ask for the “Write files” permission, or create the server and upload the files later.";
  }
  return null;
}

/** The instance can take an import now (no job holds it and it is not running or installing). */
export function readyForImport(instance: Pick<Instance, "status" | "active_job">): boolean {
  return !instance.active_job && !(IMPORT_BLOCKING_STATUSES as readonly string[]).includes(instance.status);
}

export type ImportPhase = "preparing" | "waiting" | "uploading" | "done" | "failed";
export type ImportStage = "create" | "prepare" | "upload" | "extract";
export type StageState = "done" | "active" | "pending" | "failed";

/** Visual state of each stage of the import run for a phase (failedAt = stage that failed). */
export function stageStates(phase: ImportPhase, failedAt: ImportStage | null): Record<ImportStage, StageState> {
  const order: ImportStage[] = ["create", "prepare", "upload", "extract"];
  const current: ImportStage =
    phase === "failed" ? (failedAt ?? "upload") : phase === "preparing" || phase === "waiting" ? "prepare" : phase === "uploading" ? "upload" : "extract";
  const idx = order.indexOf(current);
  const out = {} as Record<ImportStage, StageState>;
  order.forEach((s, i) => {
    if (i < idx) out[s] = "done";
    // "done" = uploaded: the extract job now runs and is followed on the server page
    else if (i === idx) out[s] = phase === "failed" ? "failed" : "active";
    else out[s] = "pending";
  });
  return out;
}
