/** Small single-field dialog used for "New folder" and "Rename / move". */
import { Loader2 } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/api";

const INVALID = /[\0]/;

export function NameDialog({
  title,
  description,
  label,
  initial,
  confirmLabel,
  help,
  onSubmit,
  onClose,
}: {
  title: string;
  description?: ReactNode;
  label: string;
  initial: string;
  confirmLabel: string;
  help?: ReactNode;
  /** Resolve to close the dialog; throw to show the error inline. */
  onSubmit: (value: string) => Promise<void>;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const preselected = useRef(false);
  const trimmed = value.trim();
  const invalid = !trimmed || trimmed === "." || trimmed === ".." || INVALID.test(trimmed);

  const submit = async () => {
    if (invalid || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(trimmed);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="sm:max-w-md">
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description && <DialogDescription>{description}</DialogDescription>}
          </DialogHeader>
          <div className="grid gap-2">
            <Label htmlFor="file-name-input">{label}</Label>
            <Input
              id="file-name-input"
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                setError(null);
              }}
              onFocus={(e) => {
                // Pre-select the name without its extension (once), like desktop file managers.
                if (preselected.current) return;
                preselected.current = true;
                const dot = e.target.value.lastIndexOf(".");
                e.target.setSelectionRange(0, dot > 0 ? dot : e.target.value.length);
              }}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              aria-invalid={!!error || undefined}
              aria-describedby={error ? "file-name-error" : help ? "file-name-help" : undefined}
            />
            {help && !error && (
              <p id="file-name-help" className="text-xs text-muted-foreground">
                {help}
              </p>
            )}
            {error && (
              <p id="file-name-error" className="text-xs text-destructive" role="alert">
                {error}
              </p>
            )}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" disabled={invalid || busy || trimmed === initial}>
              {busy && <Loader2 className="animate-spin" />} {confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
