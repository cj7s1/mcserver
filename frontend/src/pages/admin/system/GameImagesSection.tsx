/**
 * Runtime image repository per game other than Minecraft (system setting `runtime_images`,
 * {game: {repository}}; backend contract B12). Minecraft keeps its own two settings in the panel
 * settings form. The tag follows each game's rule (Factorio: the server's exact version, rootless).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Save } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Section } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorMessage } from "@/lib/api";
import { MINECRAFT } from "@/lib/games";
import { qk } from "@/lib/queries";
import type { GameInfo, SystemSettings } from "@/lib/types";
import { runtimeImagesValue } from "./game-images";

const REPO_RE = /^[a-z0-9][a-z0-9./_-]{0,199}$/;

export function GameImagesSection({ settings, games }: { settings: SystemSettings; games: GameInfo[] }) {
  const qc = useQueryClient();
  const others = games.filter((g) => g.id !== MINECRAFT);
  const stored = settings.values.runtime_images ?? {};
  const initial = Object.fromEntries(others.map((g) => [g.id, stored[g.id]?.repository ?? ""]));
  const [texts, setTexts] = useState<Record<string, string>>(initial);
  const errors = Object.fromEntries(
    Object.entries(texts)
      .filter(([, v]) => v.trim() && !REPO_RE.test(v.trim()))
      .map(([k]) => [k, "Lower-case repository name, e.g. factoriotools/factorio or registry.example/factorio"]),
  );
  const dirty = JSON.stringify(texts) !== JSON.stringify(initial);
  const save = useMutation({
    mutationFn: () => api.patch<SystemSettings>("/system/settings", { values: { runtime_images: runtimeImagesValue(texts) } }),
    onSuccess: (data) => {
      qc.setQueryData(qk.settings, data);
      toast.success("Runtime images saved. Servers get them at their next start.");
    },
    onError: (err) => toast.error(errorMessage(err)),
  });
  if (!others.length) return null;
  return (
    <Section
      title="Runtime images of other games"
      description="Image repository per game; empty = the game's default. The tag is chosen per server (its exact game version), or pinned by the panel release for games without versions to choose. The node agent must allow the repository too (AGENT_GAME_<ID>_IMAGES)."
    >
      <div className="grid gap-4">
        {others.map((g) => (
          <div key={g.id} className="grid gap-1.5">
            <Label htmlFor={`runtime-image-${g.id}`}>{g.label}</Label>
            <Input
              id={`runtime-image-${g.id}`}
              value={texts[g.id] ?? ""}
              placeholder="Default"
              onChange={(e) => setTexts((t) => ({ ...t, [g.id]: e.target.value }))}
              aria-invalid={!!errors[g.id] || undefined}
              spellCheck={false}
            />
            {errors[g.id] && (
              <p className="text-xs text-destructive" role="alert">
                {errors[g.id]}
              </p>
            )}
          </div>
        ))}
        {dirty && (
          <div className="flex justify-end">
            <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending || Object.keys(errors).length > 0}>
              {save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save
            </Button>
          </div>
        )}
      </div>
    </Section>
  );
}
