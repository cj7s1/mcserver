import { useCallback, useRef } from "react";

const DIALOG = "[role=dialog],[role=alertdialog]";

// Last element focused outside any dialog: the opener of a dialog whose open event was skipped.
let lastOutside: HTMLElement | null = null;
if (typeof document !== "undefined") {
  document.addEventListener(
    "focusin",
    (e) => {
      if (e.target instanceof HTMLElement && !e.target.closest(DIALOG)) lastOutside = focusOrigin(e.target);
    },
    true,
  );
}

/**
 * Radix dialogs return focus only to their own `<DialogTrigger>`. Most dialogs in the panel are
 * opened from state (row buttons, menu items, toolbar buttons), so after Escape or Cancel focus fell
 * back to `<body>` and keyboard users lost their place. This remembers the element that had focus
 * when the dialog opened and restores it on close when it is still on the page. A dialog opened
 * from a dropdown menu item returns focus to that menu's trigger (the item itself is gone by then).
 */
export function useReturnFocus(
  onOpenAutoFocus?: (event: Event) => void,
  onCloseAutoFocus?: (event: Event) => void,
): { onOpenAutoFocus: (event: Event) => void; onCloseAutoFocus: (event: Event) => void } {
  // undefined: the open event never fired (Radix skips it when an autoFocus field inside the
  // dialog already has focus); null: nothing to return to.
  const returnTo = useRef<HTMLElement | null | undefined>(undefined);

  const handleOpen = useCallback(
    (event: Event) => {
      // FocusScope dispatches this on the content before moving focus, so activeElement is
      // still the opener.
      returnTo.current = focusOrigin(document.activeElement);
      onOpenAutoFocus?.(event);
    },
    [onOpenAutoFocus],
  );

  const handleClose = useCallback(
    (event: Event) => {
      onCloseAutoFocus?.(event);
      const el = returnTo.current === undefined ? lastOutside : returnTo.current;
      returnTo.current = undefined;
      if (event.defaultPrevented || !el?.isConnected) return;
      event.preventDefault();
      el.focus({ preventScroll: true });
    },
    [onCloseAutoFocus],
  );

  return { onOpenAutoFocus: handleOpen, onCloseAutoFocus: handleClose };
}

function focusOrigin(active: Element | null): HTMLElement | null {
  if (!(active instanceof HTMLElement) || active === document.body) return null;
  const menu = active.closest<HTMLElement>("[role=menu]");
  if (menu?.id) {
    const trigger = document.querySelector<HTMLElement>(`[aria-controls="${CSS.escape(menu.id)}"]`);
    if (trigger) return trigger;
  }
  return active;
}
