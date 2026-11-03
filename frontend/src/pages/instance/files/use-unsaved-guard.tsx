/** Guards against losing unsaved edits: browser unload prompt + in-app navigation blocker. */
import { useEffect, useRef } from "react";
import { useBlocker, type Blocker } from "react-router";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

export function useUnsavedChangesGuard(dirty: boolean): Blocker {
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Legacy browsers need returnValue set to show the prompt.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  // Only leaving the page counts; search-param changes (tabs, folders) keep the component mounted.
  return useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
}

export function UnsavedChangesDialog({ blocker, description }: { blocker: Blocker; description?: string }) {
  const proceeding = useRef(false);
  const open = blocker.state === "blocked";
  useEffect(() => {
    if (blocker.state !== "blocked") proceeding.current = false;
  }, [blocker.state]);
  return (
    <AlertDialog
      open={open}
      onOpenChange={(o) => {
        if (!o && !proceeding.current && blocker.state === "blocked") blocker.reset();
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
          <AlertDialogDescription>
            {description ?? "You have unsaved changes on this page. They will be lost if you leave now."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep editing</AlertDialogCancel>
          <Button
            variant="destructive"
            onClick={() => {
              if (blocker.state !== "blocked") return;
              proceeding.current = true;
              blocker.proceed();
            }}
          >
            Discard and leave
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
