import { useId, useState, type ReactNode } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Confirmation for destructive actions. When `confirmText` is given the user must
 * type it exactly (used for permanent deletes).
 */
export function ConfirmDialog({
  trigger,
  title,
  description,
  confirmLabel = "Confirm",
  destructive = false,
  confirmText,
  onConfirm,
  open,
  onOpenChange,
  onCloseAutoFocus,
  children,
}: {
  trigger?: ReactNode;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  destructive?: boolean;
  confirmText?: string;
  onConfirm: () => unknown | Promise<unknown>;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Where focus goes on close when there is no trigger (call `event.preventDefault()` to move it yourself). */
  onCloseAutoFocus?: (event: Event) => void;
  children?: ReactNode;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [innerOpen, setInnerOpen] = useState(false);
  const inputId = useId();
  const controlled = open !== undefined;
  const isOpen = controlled ? open : innerOpen;
  const setOpen = (o: boolean) => {
    if (!o) setTyped("");
    if (!controlled) setInnerOpen(o);
    onOpenChange?.(o);
  };
  const blocked = confirmText !== undefined && typed !== confirmText;
  return (
    <AlertDialog open={isOpen} onOpenChange={(o) => (busy && !o ? undefined : setOpen(o))}>
      {trigger && <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>}
      <AlertDialogContent onCloseAutoFocus={onCloseAutoFocus}>
        {/* Left-aligned at every width: descriptions here are lists and paragraphs, which read badly centred. */}
        <AlertDialogHeader className="place-items-start text-left">
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description && <AlertDialogDescription asChild><div>{description}</div></AlertDialogDescription>}
        </AlertDialogHeader>
        {children}
        {confirmText !== undefined && (
          <div className="grid gap-2">
            <Label htmlFor={inputId}>
              Type <span className="font-mono font-semibold break-all">{confirmText}</span> to confirm
            </Label>
            <Input id={inputId} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} />
          </div>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={blocked || busy}
            className={
              destructive
                ? "bg-destructive text-white hover:bg-destructive/90 focus-visible:border-destructive/40 focus-visible:ring-destructive/30"
                : undefined
            }
            onClick={async (e) => {
              e.preventDefault();
              if (blocked || busy) return;
              setBusy(true);
              try {
                await onConfirm();
                setBusy(false);
                setOpen(false);
              } catch {
                // The caller reports the error (mutation onError / toast); keep the dialog open.
                setBusy(false);
              }
            }}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
