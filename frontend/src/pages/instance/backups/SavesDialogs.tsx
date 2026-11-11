/**
 * Saves of a non-Minecraft game (backend contract B8): "New map" (POST /instances/{id}/saves/reset ->
 * job saves_reset) and "Import a save" (POST /instances/{id}/saves/import, multipart -> job
 * saves_import). Both stop a running server, take a safety backup unless the user turns it off, move
 * the current save aside (it stays in the server folder) and need the server name typed to confirm.
 */
import { Loader2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";
import { ConfigFieldsForm } from "@/components/ConfigFieldsForm";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, errorMessage, uploadWithProgress } from "@/lib/api";
import { initialTexts } from "@/lib/game-config";
import type { ConfigField, Instance, Job } from "@/lib/types";
import { DEFAULT_RESET_FIELDS, resetBody } from "./saves-model";

const LIVE = ["running", "starting", "unhealthy", "restarting"];

function ConfirmName({ instance, value, onChange, id }: { instance: Instance; value: string; onChange: (v: string) => void; id: string }) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>
        Type <span className="font-mono font-semibold">{instance.name}</span> to confirm
      </Label>
      <Input id={id} value={value} onChange={(e) => onChange(e.target.value)} autoComplete="off" spellCheck={false} />
    </div>
  );
}

function BackupSwitch({ backup, setBackup, id }: { backup: boolean; setBackup: (v: boolean) => void; id: string }) {
  return (
    <div className="grid gap-1">
      <div className="flex items-center justify-between gap-4">
        <Label htmlFor={id}>Safety backup first</Label>
        <Switch id={id} checked={backup} onCheckedChange={setBackup} />
      </div>
      {!backup && (
        <p className="text-xs font-medium text-destructive" role="alert">
          Without a safety backup only the moved-aside save file is left to go back to.
        </p>
      )}
    </div>
  );
}

function Shell({
  title,
  description,
  children,
  onClose,
  footer,
}: {
  title: string;
  description: ReactNode;
  children: ReactNode;
  onClose: () => void;
  footer: ReactNode;
}) {
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">{children}</div>
        <DialogFooter>{footer}</DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function SavesResetDialog({
  instance,
  fields,
  onClose,
  onStarted,
}: {
  instance: Instance;
  fields?: ConfigField[];
  onClose: () => void;
  onStarted: (job: Job) => void;
}) {
  const resetFields = fields?.length ? fields : DEFAULT_RESET_FIELDS;
  const [texts, setTexts] = useState(() => initialTexts(resetFields));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState("");
  const [backup, setBackup] = useState(true);
  const [pending, setPending] = useState(false);
  const live = LIVE.includes(instance.status);
  const submit = async () => {
    const { body, errors: errs } = resetBody(resetFields, texts, confirm, backup);
    setErrors(errs);
    if (!body) return;
    setPending(true);
    try {
      const job = await api.post<Job>(`/instances/${instance.id}/saves/reset`, body);
      toast.success("Creating a new map");
      onStarted(job);
      onClose();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setPending(false);
    }
  };
  return (
    <Shell
      title="Start a new map?"
      description={
        <>
          {live ? "The server is stopped, " : ""}the current save files are moved aside (they stay in the server folder) and a new map
          is generated with the settings below{live ? "; then the server starts again" : " on the next start"}. Settings, admins and
          the whitelist are kept.
        </>
      }
      onClose={onClose}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={submit} disabled={pending || confirm !== instance.name}>
            {pending && <Loader2 className="animate-spin" />} Start a new map
          </Button>
        </>
      }
    >
      <ConfigFieldsForm
        fields={resetFields}
        texts={texts}
        onChange={(k, t) => setTexts((p) => ({ ...p, [k]: t }))}
        errors={errors}
        idPrefix="saves-reset"
      />
      <BackupSwitch backup={backup} setBackup={setBackup} id="saves-reset-backup" />
      <ConfirmName instance={instance} value={confirm} onChange={setConfirm} id="saves-reset-confirm" />
    </Shell>
  );
}

export function SavesImportDialog({
  instance,
  extension = ".zip",
  onClose,
  onStarted,
}: {
  instance: Instance;
  extension?: string;
  onClose: () => void;
  onStarted: (job: Job) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [confirm, setConfirm] = useState("");
  const [backup, setBackup] = useState(true);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const live = LIVE.includes(instance.status);
  const wrongType = !!file && !file.name.toLowerCase().endsWith(extension.toLowerCase());
  const submit = async () => {
    if (!file || wrongType) return;
    const form = new FormData();
    form.append("file", file);
    form.append("confirm_name", confirm);
    form.append("backup", backup ? "true" : "false");
    setProgress(0);
    setError(null);
    try {
      const job = await uploadWithProgress<Job>(`/instances/${instance.id}/saves/import`, form, undefined, setProgress);
      toast.success("Importing the save");
      onStarted(job);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      toast.error(errorMessage(err));
    } finally {
      setProgress(null);
    }
  };
  const pending = progress !== null;
  return (
    <Shell
      title="Import a save"
      description={
        <>
          The uploaded save becomes the server&apos;s map. {live ? "The server is stopped first and started again afterwards. " : ""}
          The current save is moved aside and stays in the server folder. The save must have been made with the same game version (or
          an older one) and, for Factorio, the same Space Age setting.
        </>
      }
      onClose={onClose}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={pending || !file || wrongType || confirm !== instance.name}>
            {pending && <Loader2 className="animate-spin" />}
            {pending ? `Uploading ${Math.round((progress ?? 0) * 100)}%` : "Import"}
          </Button>
        </>
      }
    >
      <div className="grid gap-1.5">
        <Label htmlFor="saves-import-file">Save file ({extension})</Label>
        <Input
          id="saves-import-file"
          type="file"
          accept={extension}
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          disabled={pending}
        />
        {wrongType && (
          <p className="text-xs text-destructive" role="alert">
            Choose a {extension} file
          </p>
        )}
      </div>
      <BackupSwitch backup={backup} setBackup={setBackup} id="saves-import-backup" />
      <ConfirmName instance={instance} value={confirm} onChange={setConfirm} id="saves-import-confirm" />
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </Shell>
  );
}
