/** Editor for additional published ports (query, voice chat, web maps…). */
import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { MAX_EXTRA_PORTS, PORT_PRESETS, type PortPreset, type PortRow } from "./port-presets";

export function ExtraPortsEditor({
  rows,
  errors,
  disabled,
  onChange,
  onAdd,
}: {
  rows: PortRow[];
  errors: Record<string, Partial<Record<"container_port" | "host_port" | "label" | "host_ip" | "row", string>>>;
  disabled: boolean;
  onChange: (rows: PortRow[]) => void;
  onAdd: (preset?: PortPreset) => void;
}) {
  const update = (key: string, patch: Partial<PortRow>) => onChange(rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const full = rows.length >= MAX_EXTRA_PORTS;
  const has = (p: PortPreset) => rows.some((r) => Number(r.container_port) === p.container_port && r.protocol === p.protocol);

  return (
    <div className="grid gap-3">
      {rows.length > 0 && (
        <div className="grid gap-2">
          <div
            className="hidden grid-cols-[minmax(0,1.2fr)_7rem_6rem_7rem_minmax(0,1fr)_2rem] gap-2 px-1 text-xs font-medium text-muted-foreground md:grid"
            aria-hidden
          >
            <span>Label</span>
            <span>Container port</span>
            <span>Protocol</span>
            <span>Host port</span>
            <span>Bind IP (optional)</span>
            <span />
          </div>
          <ul className="grid gap-2" aria-label="Extra port mappings">
            {rows.map((r, i) => {
              const e = errors[r.key] ?? {};
              const id = (f: string) => `extra-${r.key}-${f}`;
              const fieldCls = "grid content-start gap-1";
              const labelCls = "text-xs md:sr-only";
              return (
                <li key={r.key} className="rounded-lg border p-2.5 md:border-0 md:p-0">
                  <div className="grid grid-cols-2 gap-2 md:grid-cols-[minmax(0,1.2fr)_7rem_6rem_7rem_minmax(0,1fr)_2rem] md:items-start">
                    <div className={`${fieldCls} col-span-2 md:col-span-1`}>
                      <Label htmlFor={id("label")} className={labelCls}>
                        Label (mapping {i + 1})
                      </Label>
                      <Input
                        id={id("label")}
                        value={r.label}
                        maxLength={40}
                        placeholder="e.g. Voice chat"
                        disabled={disabled}
                        onChange={(ev) => update(r.key, { label: ev.target.value })}
                      />
                    </div>
                    <div className={fieldCls}>
                      <Label htmlFor={id("container")} className={labelCls}>
                        Container port
                      </Label>
                      <Input
                        id={id("container")}
                        inputMode="numeric"
                        value={r.container_port}
                        disabled={disabled}
                        onChange={(ev) => update(r.key, { container_port: ev.target.value.replace(/[^\d]/g, "") })}
                        aria-invalid={!!e.container_port || undefined}
                        className="tabular-nums"
                      />
                      {e.container_port && <p className="text-xs text-destructive">{e.container_port}</p>}
                    </div>
                    <div className={fieldCls}>
                      <Label htmlFor={id("protocol")} className={labelCls}>
                        Protocol
                      </Label>
                      <Select
                        value={r.protocol}
                        onValueChange={(v) => update(r.key, { protocol: v as "tcp" | "udp" })}
                        disabled={disabled}
                      >
                        <SelectTrigger id={id("protocol")} className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="tcp">TCP</SelectItem>
                          <SelectItem value="udp">UDP</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                    <div className={fieldCls}>
                      <Label htmlFor={id("host")} className={labelCls}>
                        Host port
                      </Label>
                      <Input
                        id={id("host")}
                        inputMode="numeric"
                        value={r.host_port}
                        disabled={disabled}
                        onChange={(ev) => update(r.key, { host_port: ev.target.value.replace(/[^\d]/g, "") })}
                        aria-invalid={!!e.host_port || undefined}
                        className="tabular-nums"
                      />
                      {e.host_port && <p className="text-xs text-destructive">{e.host_port}</p>}
                    </div>
                    <div className={fieldCls}>
                      <Label htmlFor={id("ip")} className={labelCls}>
                        Bind IP (optional)
                      </Label>
                      <Input
                        id={id("ip")}
                        value={r.host_ip}
                        placeholder="same as game"
                        disabled={disabled}
                        onChange={(ev) => update(r.key, { host_ip: ev.target.value.trim() })}
                        aria-invalid={!!e.host_ip || undefined}
                        className="font-mono"
                      />
                      {e.host_ip && <p className="text-xs text-destructive">{e.host_ip}</p>}
                    </div>
                    <div className="col-span-2 flex justify-end md:col-span-1">
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        aria-label={`Remove mapping ${r.label || i + 1}`}
                        disabled={disabled}
                        onClick={() => onChange(rows.filter((x) => x.key !== r.key))}
                      >
                        <Trash2 />
                      </Button>
                    </div>
                  </div>
                  {e.row && <p className="mt-1 text-xs text-destructive">{e.row}</p>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {!disabled && (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button type="button" size="sm" variant="outline" onClick={() => onAdd()} disabled={full}>
            <Plus /> Add port
          </Button>
          <span className="px-1 text-xs text-muted-foreground">Presets:</span>
          {PORT_PRESETS.map((p) => (
            <Button
              key={p.id}
              type="button"
              size="xs"
              variant="secondary"
              title={p.help}
              disabled={full || has(p)}
              onClick={() => onAdd(p)}
            >
              <Plus /> {p.label}{" "}
              <span className="font-mono text-[10px] text-muted-foreground">
                {p.protocol.toUpperCase()} {p.container_port}
              </span>
            </Button>
          ))}
        </div>
      )}
      {full && !disabled && <p className="text-xs text-muted-foreground">Up to {MAX_EXTRA_PORTS} extra ports.</p>}
    </div>
  );
}
