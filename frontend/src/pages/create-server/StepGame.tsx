/** First wizard step when the panel runs more than one game: which game the server is for. */
import { useId } from "react";
import { Section } from "@/components/PageHeader";
import { gameIcon } from "@/lib/games";
import type { GameInfo } from "@/lib/types";
import { ChoiceCard } from "./WizardBits";
import { creatableGame } from "./wizardModel";

export function StepGame({
  games,
  game,
  onChange,
  error,
}: {
  games: readonly GameInfo[];
  game: string | null;
  onChange: (id: string) => void;
  error?: string;
}) {
  const name = useId();
  return (
    <Section title="Which game?" description="The next steps depend on the game.">
      <div className="grid gap-4">
        <div
          role="radiogroup"
          aria-label="Game"
          aria-describedby={error ? `${name}-err` : undefined}
          className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3"
        >
          {games.map((g) => {
            const available = creatableGame(g.id);
            const Icon = gameIcon(g.id);
            return (
              <ChoiceCard
                key={g.id}
                name={name}
                value={g.id}
                checked={game === g.id}
                onChange={onChange}
                disabled={!available}
                title={g.label}
                description={(Array.isArray(g.notes) ? g.notes.join(" ") : g.notes) || undefined}
                meta={
                  available
                    ? g.variants.map((v) => v.label).join(" · ") || undefined
                    : `${g.label} servers cannot be created with this panel version yet.`
                }
                icon={<Icon />}
              />
            );
          })}
        </div>
        {error && (
          <p id={`${name}-err`} className="text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>
    </Section>
  );
}
