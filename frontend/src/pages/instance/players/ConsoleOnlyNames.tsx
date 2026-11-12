/**
 * Online players the panel cannot act on (C-6): names in the server's list that are no account name the
 * game's commands take (EssentialsX nicknames "~Nick", Bedrock players through Floodgate ".Steve", a
 * Steam name with characters the panel refuses to send). They are shown, without buttons, with "use the
 * console": an admin can still deal with them there.
 */
import { Terminal } from "lucide-react";
import { Link } from "react-router";

export function ConsoleOnlyNames({ instanceId, names, className }: { instanceId: string; names: readonly string[]; className?: string }) {
  if (!names.length) return null;
  return (
    <div className={className}>
      <p className="text-xs text-muted-foreground">
        Also online, under names the panel cannot act on (nicknames, Bedrock players, names with characters commands
        cannot carry): use the{" "}
        <Link to={`/servers/${instanceId}/console`} className="font-medium text-primary hover:underline">
          console
        </Link>{" "}
        for these.
      </p>
      <ul className="mt-1.5 divide-y rounded-lg border" aria-label="Online players the panel cannot act on">
        {names.map((n) => (
          <li key={n} className="flex min-w-0 items-center gap-2 px-3 py-1.5 text-sm">
            <span className="min-w-0 truncate font-mono" title={n}>
              {n}
            </span>
            <span className="ml-auto flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
              <Terminal className="size-3.5" aria-hidden /> use the console
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
