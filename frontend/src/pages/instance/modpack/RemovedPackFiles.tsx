/**
 * "Removed pack files": the jars of a CurseForge/Modrinth pack that the panel keeps the runtime
 * from re-installing (backend apps/content/packfiles.py). A pack jar disabled, deleted or replaced
 * on the Mods tab, or one jar deleted or renamed directly in mods/ in the file manager, is recorded;
 * "Let the pack reinstall them" drops the record, and the next restart recreates the container so
 * the pack installs them again.
 *
 *   GET    /instances/{id}/modpack/removed-files -> RemovedPackFilesInfo (instance.view)
 *   DELETE /instances/{id}/modpack/removed-files -> RemovedPackFilesInfo & {cleared}  (instance.content)
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PackageX, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { api, errorMessage } from "@/lib/api";
import { useInvalidateInstance } from "@/lib/queries";
import type { Instance } from "@/lib/types";
import { removedPackFilesKey, useRemovedPackFiles, type RemovedPackFilesInfo } from "./modpack-api";

export function RemovedPackFiles({ instance, canManage, locked }: { instance: Instance; canManage: boolean; locked: boolean }) {
  const qc = useQueryClient();
  const invalidate = useInvalidateInstance();
  const query = useRemovedPackFiles(instance.id, instance.is_modpack);
  const clear = useMutation({
    mutationFn: () => api.delete<RemovedPackFilesInfo>(`/instances/${instance.id}/modpack/removed-files`),
    onSuccess: (data) => {
      qc.setQueryData(removedPackFilesKey(instance.id), data);
      invalidate(instance.id);
      const count = data.cleared?.length ?? 0;
      toast.success(`The pack reinstalls ${count} file${count === 1 ? "" : "s"}`, {
        description: data.restart_required ? "Restart the server to apply it." : "It happens the next time the server starts.",
      });
    },
    onError: (err) => toast.error(`Could not clear the list: ${errorMessage(err)}`),
  });
  const info = query.data;
  if (!info?.supported || info.paths.length === 0) return null;
  const count = info.paths.length;
  return (
    <Section
      title={
        <span className="inline-flex items-center gap-2">
          <PackageX className="size-4 text-muted-foreground" aria-hidden /> Removed pack files
        </span>
      }
      description="Files of the pack that were disabled, deleted or replaced. The pack does not put them back when the server starts."
      actions={
        canManage ? (
          <ConfirmDialog
            trigger={
              <Button size="sm" variant="outline" disabled={locked || clear.isPending}>
                <RotateCcw aria-hidden /> Let the pack reinstall them
              </Button>
            }
            title={`Let the pack reinstall ${count} file${count === 1 ? "" : "s"}?`}
            description="The list is cleared. At the next restart the server's container is recreated and the pack installs these files again as it ships them (a disabled copy you kept stays next to it: delete it then)."
            confirmLabel="Reinstall them"
            onConfirm={() => clear.mutateAsync()}
          />
        ) : undefined
      }
      className="min-w-0"
    >
      {!info.applies && (
        <p className="mb-2 text-xs text-muted-foreground">
          This list was made for another release of the pack ({info.release}); it is not used for the release installed now.
        </p>
      )}
      <ul className="grid gap-1 text-sm" aria-label="Removed pack files">
        {info.paths.map((p) => (
          <li key={p} className="truncate font-mono text-xs" title={p}>
            {p}
          </li>
        ))}
      </ul>
    </Section>
  );
}
