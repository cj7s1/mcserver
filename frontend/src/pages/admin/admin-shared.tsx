/** Small helpers shared by the admin pages (users, audit, system). */
import { Check, Copy, ShieldAlert } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { EmptyState } from "@/components/States";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { copyText } from "./admin-utils";

export function AdminCopyButton({
  value,
  label = "Copy",
  className,
  size = "sm",
}: {
  value: string;
  label?: string;
  className?: string;
  size?: "sm" | "default";
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <Button
      type="button"
      variant="outline"
      size={size}
      className={className}
      onClick={async () => {
        const ok = await copyText(value);
        if (!ok) {
          toast.error("Copy failed. Select the text and copy it manually.");
          return;
        }
        setCopied(true);
        toast.success("Copied to clipboard");
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <Check /> : <Copy />}
      {copied ? "Copied" : label}
    </Button>
  );
}

export function AdminNoPermission({ permission, what }: { permission: string; what: string }) {
  return (
    <EmptyState
      icon={<ShieldAlert className="size-5" />}
      title="You don't have access to this page"
      description={
        <>
          {what} requires the <span className="font-mono text-xs">{permission}</span> permission. Ask the panel owner or an
          administrator to grant it.
        </>
      }
    />
  );
}

export function AdminFieldError({ id, message }: { id: string; message?: string }) {
  if (!message) return null;
  return (
    <p id={id} className="text-xs text-destructive">
      {message}
    </p>
  );
}

export function AdminHint({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("text-xs text-muted-foreground", className)}>{children}</p>;
}

/** Callout box using theme-aware status hues. */
export function AdminCallout({
  tone = "info",
  icon,
  title,
  children,
  className,
}: {
  tone?: "info" | "warning" | "danger" | "success";
  icon?: ReactNode;
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}) {
  const tones = {
    info: "border-sky-500/30 bg-sky-500/5 text-sky-900 dark:text-sky-100",
    warning: "border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-100",
    danger: "border-destructive/40 bg-destructive/5 text-destructive",
    success: "border-emerald-500/30 bg-emerald-500/5 text-emerald-900 dark:text-emerald-100",
  } as const;
  const bodyTones = {
    info: "text-sky-900/80 dark:text-sky-100/75",
    warning: "text-amber-900/85 dark:text-amber-100/80",
    danger: "text-foreground/80",
    success: "text-emerald-900/80 dark:text-emerald-100/75",
  } as const;
  return (
    <div
      role={tone === "danger" || tone === "warning" ? "alert" : "note"}
      className={cn("flex items-start gap-3 rounded-lg border px-3 py-2.5 text-sm", tones[tone], className)}
    >
      {icon && <span className="mt-0.5 shrink-0 [&_svg]:size-4" aria-hidden>{icon}</span>}
      <div className="min-w-0 space-y-1">
        {title && <p className="font-medium">{title}</p>}
        {children && (
          <div className={cn("text-[13px] leading-relaxed", bodyTones[tone])}>
            {children}
          </div>
        )}
      </div>
    </div>
  );
}
