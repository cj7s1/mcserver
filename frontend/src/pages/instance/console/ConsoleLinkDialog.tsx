/**
 * Asks before a link from console output is opened (console/console-links.ts): the full URL, where it
 * came from, and a strong warning when it points to the panel's own host name on another port.
 */
import { AlertTriangle } from "lucide-react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { ConsoleLink } from "./console-links";

export function ConsoleLinkDialog({ link, onClose }: { link: ConsoleLink | null; onClose: () => void }) {
  if (!link) return null;
  const panelPort = link.kind === "panel-other-port";
  return (
    <ConfirmDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={panelPort ? "This link goes to the panel's address on another port" : "Open a link from the console?"}
      description={
        <div className="grid gap-3">
          <p className="rounded-md border bg-muted/50 px-2.5 py-2 font-mono text-xs break-all text-foreground" aria-label="Link address">
            {link.url}
          </p>
          {panelPort ? (
            <p className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-2 text-destructive">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                It points to <span className="font-mono">{link.host}</span>, the address of this panel, but on port{" "}
                <span className="font-mono">{link.port}</span>. Your browser sends the panel's sign-in cookies to every port of
                this address, so whatever answers there (the game server that printed the link, or another service on this
                host) could take over your panel session. Do not open it unless you know what runs on port {link.port}.
              </span>
            </p>
          ) : (
            <p>
              {link.kind === "same-origin" ? "It points to this panel. " : ""}
              The server's output printed it: plugins, mods and players' chat can print any link. Open it only if you trust
              where it goes.
            </p>
          )}
        </div>
      }
      confirmLabel={panelPort ? "Open anyway" : "Open link"}
      destructive={panelPort}
      onConfirm={() => {
        window.open(link.url, "_blank", "noopener,noreferrer");
      }}
    />
  );
}
