import { useId, type ReactNode } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { KeyCaps } from "./Kbd";
import { paletteHotkeyCaps, type ShortcutDef } from "./shortcuts";

function Row({ keys, sequence, children, muted }: { keys: readonly string[]; sequence?: boolean; children: string; muted?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <dt className={muted ? "text-sm text-muted-foreground" : "text-sm"}>{children}</dt>
      <dd className="shrink-0">
        <KeyCaps keys={keys} sequence={sequence} />
      </dd>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="grid gap-1">
      <h3 id={id} className="text-xs font-medium tracking-wider text-muted-foreground uppercase">
        {title}
      </h3>
      <dl className="divide-y divide-border">{children}</dl>
    </section>
  );
}

/** "?" dialog: every shortcut the user can use, plus the switch for single-key shortcuts. */
export function ShortcutsHelp({
  open,
  onOpenChange,
  mac,
  shortcuts,
  singleKeyEnabled,
  onSingleKeyChange,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mac: boolean;
  /** Shortcuts the user may use (filtered by permission). */
  shortcuts: readonly ShortcutDef[];
  singleKeyEnabled: boolean;
  onSingleKeyChange: (enabled: boolean) => void;
  /** It has no trigger button, so the caller decides where focus returns. */
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const switchId = useId();
  const hintId = useId();
  const help = shortcuts.find((s) => s.id === "help");
  const nav = shortcuts.filter((s) => s.id !== "help");
  const off = !singleKeyEnabled;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[calc(100svh-2rem)] overflow-y-auto sm:max-w-md motion-reduce:animate-none!"
        onCloseAutoFocus={onCloseAutoFocus}
      >
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>
            Single-key shortcuts work anywhere in the panel except while you type in a field or the console.
          </DialogDescription>
        </DialogHeader>
        <Section title="General">
          <Row keys={paletteHotkeyCaps(mac)}>Open the command palette</Row>
          {help && (
            <Row keys={help.keys} muted={off}>
              {help.description}
            </Row>
          )}
        </Section>
        {nav.length > 0 && (
          <Section title="Go to">
            {nav.map((s) => (
              <Row key={s.id} keys={s.keys} sequence={s.keys.length > 1} muted={off}>
                {s.description}
              </Row>
            ))}
          </Section>
        )}
        <Section title="In the command palette">
          <Row keys={["↑", "↓"]}>Move between results</Row>
          <Row keys={["↵"]}>Open or run the selected result</Row>
          <Row keys={["Esc"]}>Close</Row>
        </Section>
        <div className="grid gap-1.5 rounded-lg border bg-muted/40 p-3">
          <div className="flex items-center justify-between gap-3">
            <Label htmlFor={switchId}>Single-key shortcuts</Label>
            <Switch id={switchId} checked={singleKeyEnabled} onCheckedChange={onSingleKeyChange} aria-describedby={hintId} />
          </div>
          <p id={hintId} className="text-xs text-muted-foreground">
            Turn these off if you use speech input or they clash with assistive technology. The command palette
            shortcut always works. Saved in this browser.
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
}
