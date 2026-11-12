/**
 * The answer to a player action by a typed name that the panel will not guess (409 `ambiguous_player`,
 * C-5): Rust and Palworld act by the player's id, and display names are free and not unique. The dialog
 * lists the online players the name may mean, with their ids; the action is sent again with the chosen
 * id. Nothing is sent when the user closes it.
 */
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import type { AmbiguousPlayer, PlayerAction, PlayerCandidate } from "./player-utils";

const VERBS: Record<PlayerAction, string> = {
  whitelist_add: "Add",
  whitelist_remove: "Remove",
  op: "Promote",
  deop: "Demote",
  ban: "Ban",
  pardon: "Unban",
  kick: "Kick",
};

export function PlayerChoiceDialog({
  choice,
  onPick,
  onClose,
}: {
  choice: AmbiguousPlayer | null;
  onPick: (candidate: PlayerCandidate) => void;
  onClose: () => void;
}) {
  if (!choice) return null;
  const verb = VERBS[choice.vars.action] ?? "Choose";
  const several = choice.candidates.length > 1;
  return (
    <AlertDialog open onOpenChange={(o) => !o && onClose()}>
      <AlertDialogContent>
        <AlertDialogHeader className="place-items-start text-left">
          <AlertDialogTitle>{several ? `Which “${choice.vars.name.trim()}” do you mean?` : "Confirm the player by its id"}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="grid gap-2">
              <p className="[overflow-wrap:anywhere]">{choice.message}</p>
              <p>
                Any player can take any display name. Check the id (for example on the player's Steam profile) before you
                choose.
              </p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <ul className="divide-y rounded-lg border" aria-label="Matching players">
          {choice.candidates.map((c) => (
            <li key={c.id} className="flex min-w-0 items-center gap-2 px-3 py-2 text-sm">
              <span className="grid min-w-0 gap-0.5">
                <span className="truncate font-medium">{c.name || "(no name)"}</span>
                <span className="truncate font-mono text-[11px] text-muted-foreground">{c.id}</span>
              </span>
              <Button
                size="xs"
                variant={choice.vars.action === "ban" ? "destructive" : "outline"}
                className="ml-auto shrink-0"
                aria-label={`${verb} ${c.name} (${c.id})`}
                onClick={() => {
                  onClose();
                  onPick(c);
                }}
              >
                {verb}
              </Button>
            </li>
          ))}
        </ul>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
