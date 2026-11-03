import { ArchiveRestore, Blocks, Box, FolderUp, Info, Package, Puzzle, Wrench } from "lucide-react";
import { type ReactNode, useId } from "react";
import { Link } from "react-router";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { ChoiceCard } from "./WizardBits";
import type { Category } from "./wizardModel";

const CATEGORIES: { id: Category; title: string; description: string; meta: string; icon: ReactNode }[] = [
  {
    id: "vanilla",
    title: "Vanilla",
    description: "The official Mojang server, exactly as released.",
    meta: "No plugins or mods",
    icon: <Box />,
  },
  {
    id: "plugins",
    title: "Plugin server",
    description: "Bukkit/Spigot-compatible servers with plugin support and better performance.",
    meta: "Paper · Purpur · Folia",
    icon: <Puzzle />,
  },
  {
    id: "mods",
    title: "Modded",
    description: "A mod loader to build your own mod list (players need the same mods).",
    meta: "Fabric · Quilt · Forge · NeoForge",
    icon: <Wrench />,
  },
  {
    id: "modpack",
    title: "Modpack",
    description: "Deploy a published modpack release; the panel installs the pack, loader and mods.",
    meta: "Modrinth · CurseForge · Feed The Beast",
    icon: <Package />,
  },
  {
    id: "import",
    title: "Import or restore",
    description: "Bring an existing server folder or restore from a backup.",
    meta: "Guided — no upload here",
    icon: <ArchiveRestore />,
  },
];

function ImportGuide() {
  return (
    <div className="grid gap-3 rounded-lg border bg-muted/20 p-4 text-sm">
      <div className="flex items-start gap-2">
        <Info className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
        <p>
          MCPanel does not take a server folder in this wizard. Importing and restoring work through an existing server, so
          the panel always knows which software and Minecraft version the files belong to.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="grid content-start gap-1.5 rounded-lg border bg-card p-3">
          <div className="flex items-center gap-2 font-medium">
            <FolderUp className="size-4 text-muted-foreground" aria-hidden /> Import an existing server folder
          </div>
          <ol className="ml-4 list-decimal space-y-1 text-xs text-muted-foreground">
            <li>Go back and create a server with the same software and Minecraft version as your files.</li>
            <li>Turn off <span className="font-medium text-foreground">Start after creation</span> on the Resources step.</li>
            <li>When the install finishes, open the server&apos;s <span className="font-medium text-foreground">Files</span> tab and upload your world, configs and mods/plugins.</li>
            <li>Start the server.</li>
          </ol>
        </div>
        <div className="grid content-start gap-1.5 rounded-lg border bg-card p-3">
          <div className="flex items-center gap-2 font-medium">
            <ArchiveRestore className="size-4 text-muted-foreground" aria-hidden /> Restore from a backup
          </div>
          <p className="text-xs text-muted-foreground">
            Backups belong to a server. Open that server&apos;s <span className="font-medium text-foreground">Backups</span> tab
            to restore in place or restore a snapshot as a new server.
          </p>
          <Button asChild variant="outline" size="sm" className="mt-1 justify-self-start">
            <Link to="/servers">
              <Blocks /> Go to servers
            </Link>
          </Button>
        </div>
      </div>
    </div>
  );
}

export function StepType({
  category,
  onChange,
  error,
}: {
  category: Category | null;
  onChange: (c: Category) => void;
  error?: string;
}) {
  const name = useId();
  return (
    <Section title="What kind of server?" description="You can add plugins, mods and change most settings after creation.">
      <div className="grid gap-4">
        <div role="radiogroup" aria-label="Server type" aria-describedby={error && category !== "import" ? `${name}-err` : undefined} className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {CATEGORIES.map((c) => (
            <ChoiceCard
              key={c.id}
              name={name}
              value={c.id}
              checked={category === c.id}
              onChange={(v) => onChange(v as Category)}
              title={c.title}
              description={c.description}
              meta={c.meta}
              icon={c.icon}
            />
          ))}
        </div>
        {error && category !== "import" && (
          <p id={`${name}-err`} className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
        {category === "import" && <ImportGuide />}
      </div>
    </Section>
  );
}
