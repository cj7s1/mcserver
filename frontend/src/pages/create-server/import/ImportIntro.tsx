import { AlertTriangle, ArchiveRestore, Blocks, FolderUp } from "lucide-react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/hooks/use-auth";
import { importPermissionProblem } from "./importModel";

/** Shown on the type step when "Import or restore" is selected. */
export function ImportIntro() {
  const { user } = useAuth();
  const problem = importPermissionProblem(user);
  return (
    <div className="grid gap-3 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div className="grid content-start gap-2 rounded-lg border bg-muted/20 p-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <FolderUp className="size-4 text-primary" aria-hidden /> Import an existing server folder
        </div>
        <ol className="ml-4 list-decimal space-y-1 text-xs text-muted-foreground">
          <li>Pick the software and Minecraft version your files were made with.</li>
          <li>
            Choose a <span className="font-medium text-foreground">.zip</span>,{" "}
            <span className="font-medium text-foreground">.tar</span>,{" "}
            <span className="font-medium text-foreground">.tar.gz</span> or{" "}
            <span className="font-medium text-foreground">.tar.xz</span> archive of the server folder.
          </li>
          <li>Size it and deploy: the panel creates the server stopped, uploads the archive and extracts it.</li>
          <li>Check the files on the server page, then start it.</li>
        </ol>
        <p className="text-xs text-muted-foreground">
          Worlds, configs, mods, plugins and player data are kept. A single top-level folder in the archive is
          stripped automatically.
        </p>
        {problem && (
          <p className="flex items-start gap-1.5 text-xs text-amber-700 dark:text-amber-300" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            {problem}
          </p>
        )}
      </div>
      <div className="grid content-start gap-1.5 rounded-lg border bg-card p-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <ArchiveRestore className="size-4 text-muted-foreground" aria-hidden /> Restore from a backup
        </div>
        <p className="text-xs text-muted-foreground">
          Backups belong to a server. Open that server&apos;s <span className="font-medium text-foreground">Backups</span>{" "}
          tab to restore in place or restore a backup as a new server.
        </p>
        <Button asChild variant="outline" size="sm" className="mt-1 justify-self-start">
          <Link to="/servers">
            <Blocks /> Go to servers
          </Link>
        </Button>
      </div>
    </div>
  );
}
