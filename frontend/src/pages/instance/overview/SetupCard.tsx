/**
 * "Setup needed" on the Overview (instance.setup, backend contract B4/B7): the next first-start step
 * of a game that has them. An automatic step can be run (POST /instances/{id}/setup); a step that needs
 * a secret links to Settings → Game settings, where the secrets are entered.
 */
import { CircleAlert, Loader2, Play } from "lucide-react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/api";
import type { Instance } from "@/lib/types";
import { useRunSetup } from "../game-api";

export function SetupCard({ instance, canConfigure }: { instance: Instance; canConfigure: boolean }) {
  const run = useRunSetup(instance.id);
  const setup = instance.setup;
  if (!setup?.needed) return null;
  const step = setup.steps.find((s) => s.id === setup.next) ?? setup.steps.find((s) => s.status !== "done");
  const failed = step?.status === "failed";
  const running = step?.status === "running" || instance.active_job?.kind === "setup";
  return (
    <section
      aria-label="Setup needed"
      className="flex flex-wrap items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm"
    >
      <CircleAlert className="mt-0.5 size-5 shrink-0 text-amber-600 dark:text-amber-400" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-medium">Setup needed{step ? `: ${step.label}` : ""}</p>
        {step?.help && <p className="mt-1 text-muted-foreground">{step.help}</p>}
        {step?.message && <p className={failed ? "mt-1 text-destructive" : "mt-1 text-muted-foreground"}>{step.message}</p>}
        {setup.ports_open === false && (
          <p className="mt-1 text-xs text-muted-foreground">
            No port is published until these steps are done, so nobody outside the panel can reach the server yet.
          </p>
        )}
        <ol className="mt-2 grid gap-0.5 text-xs text-muted-foreground">
          {setup.steps.map((s, i) => (
            <li key={s.id}>
              {i + 1}. {s.label} — {s.status}
            </li>
          ))}
        </ol>
      </div>
      {canConfigure && step && (
        <div className="shrink-0">
          {step.kind === "secret" ? (
            <Button asChild size="sm" variant="outline">
              <Link to={`/servers/${instance.id}/settings?tab=game-settings`}>Enter it in Game settings</Link>
            </Button>
          ) : (
            <Button
              size="sm"
              onClick={() =>
                run.mutate(step.id, {
                  onSuccess: () => toast.success(`${step.label}: started`),
                  onError: (err) => toast.error(errorMessage(err)),
                })
              }
              disabled={run.isPending || running || !!instance.active_job}
            >
              {run.isPending || running ? <Loader2 className="animate-spin" /> : <Play />} {failed ? "Try again" : "Run setup"}
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
