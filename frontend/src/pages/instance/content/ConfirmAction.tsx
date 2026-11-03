import { useRef, useState, type ComponentProps } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";

/**
 * ConfirmDialog that closes itself once the action succeeds and stays open (without an
 * unhandled rejection) when it fails, so the user can read the error toast and retry.
 */
export function ConfirmAction({
  onConfirm,
  ...props
}: Omit<ComponentProps<typeof ConfirmDialog>, "open" | "onOpenChange" | "onConfirm"> & {
  onConfirm: () => Promise<unknown> | unknown;
}) {
  const [open, setOpen] = useState(false);
  const failed = useRef(false);
  return (
    <ConfirmDialog
      {...props}
      open={open}
      onOpenChange={(o) => {
        if (!o && failed.current) {
          failed.current = false;
          return;
        }
        setOpen(o);
      }}
      onConfirm={async () => {
        try {
          await onConfirm();
        } catch {
          failed.current = true;
        }
      }}
    />
  );
}
