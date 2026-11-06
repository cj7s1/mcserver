/**
 * The import run after POST /instances/ (start: false) succeeded:
 *   1. wait for the server's `install` job (it creates the folder and container and holds the
 *      instance lock, so an import before it ends would be refused),
 *   2. wait until the instance is no longer installing and no job holds it,
 *   3. upload the archive to POST /instances/<id>/import (multipart "file", ?overwrite=true),
 *      which enqueues the `extract_archive` job, and open the server page.
 *
 * The run is not tied to the wizard staying mounted: leaving the page lets it finish in the
 * background (a toast reports the result); closing the browser tab cancels the upload.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { api, errorMessage, uploadWithProgress } from "@/lib/api";
import { qk } from "@/lib/queries";
import type { Instance, Job } from "@/lib/types";
import type { CreateResponse } from "../wizardModel";
import { type ImportPhase, type ImportStage, READY_TIMEOUT_MS, readyForImport } from "./importModel";

const POLL_MS = 1500;
const MAX_POLL_ERRORS = 5;
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

export interface ImportRun {
  phase: ImportPhase;
  instance: Instance;
  file: File;
  overwrite: boolean;
  /** The install job, as last polled. */
  job: Job | null;
  /** Upload progress 0..1. */
  progress: number;
  error: string | null;
  failedAt: ImportStage | null;
  /** false when retrying cannot help (e.g. no permission to write files). */
  retryable: boolean;
}

class StageFailure extends Error {
  stage: ImportStage;
  retryable: boolean;
  constructor(stage: ImportStage, message: string, retryable = true) {
    super(message);
    this.stage = stage;
    this.retryable = retryable;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function useImportDeploy() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [run, setRun] = useState<ImportRun | null>(null);
  const mounted = useRef(true);
  const busy = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const active = !!run && (run.phase === "preparing" || run.phase === "waiting" || run.phase === "uploading");
  useEffect(() => {
    if (!active) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [active]);

  const update = useCallback((p: Partial<ImportRun>) => setRun((r) => (r ? { ...r, ...p } : r)), []);

  const invalidate = useCallback(
    (id: string) => {
      qc.invalidateQueries({ queryKey: qk.instances });
      qc.invalidateQueries({ queryKey: qk.instance(id) });
      qc.invalidateQueries({ queryKey: qk.jobs });
    },
    [qc],
  );

  const waitForInstall = useCallback(
    async (job: Job): Promise<void> => {
      let current = job;
      let errors = 0;
      while (!TERMINAL.has(current.status)) {
        await sleep(POLL_MS);
        try {
          current = await api.get<Job>(`/jobs/${job.id}`);
          errors = 0;
          update({ job: current });
        } catch (err) {
          if (++errors >= MAX_POLL_ERRORS) throw new StageFailure("prepare", `Could not follow the install: ${errorMessage(err)}`);
        }
      }
      if (current.status !== "succeeded") {
        throw new StageFailure(
          "prepare",
          current.error || (current.status === "cancelled" ? "The install was cancelled." : "Preparing the server failed."),
        );
      }
    },
    [update],
  );

  /** Wait until the post-job reconcile has moved the server out of "installing". */
  const waitUntilReady = useCallback(async (id: string): Promise<void> => {
    const deadline = Date.now() + READY_TIMEOUT_MS;
    let errors = 0;
    for (;;) {
      try {
        const inst = await api.get<Instance>(`/instances/${id}`);
        errors = 0;
        if (readyForImport(inst)) return;
        if (inst.status === "running" || inst.status === "starting") {
          throw new StageFailure("upload", "The server is running; stop it before importing files into it.");
        }
      } catch (err) {
        if (err instanceof StageFailure) throw err;
        if (++errors >= MAX_POLL_ERRORS) throw new StageFailure("upload", errorMessage(err));
      }
      // after the deadline the upload is tried anyway; the server answers with a clear reason
      if (Date.now() > deadline) return;
      await sleep(POLL_MS);
    }
  }, []);

  const upload = useCallback(
    async (instance: Instance, file: File, overwrite: boolean): Promise<void> => {
      update({ phase: "uploading", progress: 0, error: null, failedAt: null, file, overwrite });
      const form = new FormData();
      form.append("file", file, file.name);
      try {
        await uploadWithProgress<Job>(
          `/instances/${instance.id}/import`,
          form,
          overwrite ? { overwrite: "true" } : undefined,
          (fraction) => update({ progress: fraction }),
        );
      } catch (err) {
        throw new StageFailure("upload", errorMessage(err));
      }
      update({ phase: "done", progress: 1 });
      invalidate(instance.id);
      toast.success(`Uploaded ${file.name}`, {
        description: `Extracting it into ${instance.name}. Follow the progress on the server page.`,
      });
      if (mounted.current) navigate(`/servers/${instance.id}`);
    },
    [invalidate, navigate, update],
  );

  const fail = useCallback(
    (instance: Instance, err: unknown) => {
      const stage = err instanceof StageFailure ? err.stage : "upload";
      const message = errorMessage(err);
      update({ phase: "failed", error: message, failedAt: stage, retryable: err instanceof StageFailure ? err.retryable : true });
      invalidate(instance.id);
      if (!mounted.current) {
        toast.error(`Import into ${instance.name} did not finish`, {
          description: `${message} The server was created and is stopped; upload the archive on its Files tab and extract it there.`,
        });
      }
    },
    [invalidate, update],
  );

  /** Start the run for a server that POST /instances/ just created (with start: false). */
  const begin = useCallback(
    async (res: CreateResponse, file: File, overwrite: boolean) => {
      if (busy.current) return;
      busy.current = true;
      const instance = res.instance;
      setRun({
        phase: "preparing",
        instance,
        file,
        overwrite,
        job: res.job,
        progress: 0,
        error: null,
        failedAt: null,
        retryable: true,
      });
      try {
        if (!instance.permissions.includes("instance.files.write")) {
          throw new StageFailure(
            "upload",
            "The server was created, but your role may not write its files, so the archive cannot be imported. Ask for the “Write files” permission on this server.",
            false,
          );
        }
        await waitForInstall(res.job);
        update({ phase: "waiting" });
        await waitUntilReady(instance.id);
        await upload(instance, file, overwrite);
      } catch (err) {
        fail(instance, err);
      } finally {
        busy.current = false;
      }
    },
    [fail, update, upload, waitForInstall, waitUntilReady],
  );

  /** Retry after a failure: upload again (optionally another archive), skipping the install wait. */
  const retry = useCallback(
    async (file?: File) => {
      if (!run || busy.current) return;
      busy.current = true;
      const { instance } = run;
      const archive = file ?? run.file;
      update({ phase: "waiting", error: null, failedAt: null, file: archive, progress: 0 });
      try {
        await waitUntilReady(instance.id);
        await upload(instance, archive, run.overwrite);
      } catch (err) {
        fail(instance, err);
      } finally {
        busy.current = false;
      }
    },
    [fail, run, update, upload, waitUntilReady],
  );

  return { run, active, begin, retry };
}
