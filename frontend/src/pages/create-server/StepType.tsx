import { ArchiveRestore, Box, Package, Puzzle, Wrench } from "lucide-react";
import { type ReactNode, useId } from "react";
import { Section } from "@/components/PageHeader";
import { ImportIntro } from "./import/ImportIntro";
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
    description: "Create a server from an archive of an existing server folder, or restore a backup.",
    meta: ".zip · .tar · .tar.gz · .tar.xz",
    icon: <ArchiveRestore />,
  },
];

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
        <div role="radiogroup" aria-label="Server type" aria-describedby={error ? `${name}-err` : undefined} className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
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
        {error && (
          <p id={`${name}-err`} className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
        {category === "import" && <ImportIntro />}
      </div>
    </Section>
  );
}
