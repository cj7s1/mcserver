import { ChevronsUpDown, Loader2 } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export interface VersionOption {
  version: string;
  /** Short annotations shown next to the version (e.g. "snapshot", "recommended"). */
  tags?: string[];
  muted?: boolean;
}

/**
 * Searchable version picker (Popover + cmdk) — version lists can contain hundreds of
 * entries (all vanilla releases + snapshots), which is unwieldy in a plain select.
 */
export function VersionCombobox({
  id,
  value,
  onChange,
  options,
  placeholder = "Select a version",
  clearLabel,
  loading,
  disabled,
  invalid,
  className,
  "aria-describedby": describedBy,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  options: VersionOption[];
  placeholder?: string;
  /** When set, adds an option that clears the value (e.g. "Any version"). */
  clearLabel?: string;
  loading?: boolean;
  disabled?: boolean;
  invalid?: boolean;
  className?: string;
  "aria-describedby"?: string;
}) {
  const [open, setOpen] = useState(false);
  const current = options.find((o) => o.version === value);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          disabled={disabled}
          className={cn("w-full justify-between font-normal", !value && "text-muted-foreground", className)}
        >
          <span className="flex min-w-0 items-center gap-1.5 truncate">
            {loading && <Loader2 className="size-3.5 animate-spin" />}
            {value ? (
              <>
                <span className="truncate">{value}</span>
                {current?.tags?.slice(0, 1).map((t) => (
                  <span key={t} className="rounded bg-muted px-1 text-[10px] text-muted-foreground">
                    {t}
                  </span>
                ))}
              </>
            ) : (
              (clearLabel ?? placeholder)
            )}
          </span>
          <ChevronsUpDown className="text-muted-foreground" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-(--radix-popover-trigger-width) min-w-56 p-0" align="start">
        <Command>
          <CommandInput placeholder="Search versions…" />
          <CommandList>
            <CommandEmpty>{loading ? "Loading versions…" : "No matching version."}</CommandEmpty>
            <CommandGroup>
              {clearLabel && (
                <CommandItem
                  value={`__clear__ ${clearLabel}`}
                  data-checked={!value}
                  onSelect={() => {
                    onChange("");
                    setOpen(false);
                  }}
                >
                  {clearLabel}
                </CommandItem>
              )}
              {options.map((o) => (
                <CommandItem
                  key={o.version}
                  value={o.version}
                  data-checked={o.version === value}
                  onSelect={() => {
                    onChange(o.version);
                    setOpen(false);
                  }}
                  className={cn(o.muted && "text-muted-foreground")}
                >
                  <span className="truncate">{o.version}</span>
                  {o.tags?.map((t) => (
                    <span key={t} className="rounded bg-muted px-1 text-[10px] text-muted-foreground">
                      {t}
                    </span>
                  ))}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
