/** Non-component helpers shared by the admin pages. */
import { useEffect, useState } from "react";

/** Copy text to the clipboard, falling back to execCommand on insecure (plain HTTP LAN) origins. */
export { copyText } from "@/lib/clipboard";

export function useDebouncedValue<T>(value: T, delay = 400): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

/** Query key for GET /permissions — same key the Account page uses, so the cache is shared. */
export const permissionDefsKey = ["permissions", "defs"] as const;
