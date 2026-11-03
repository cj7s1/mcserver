import { Archive, Boxes, Cpu, FileCog, Settings2, TriangleAlert, Variable } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ComponentType } from "react";
import { useSearchParams } from "react-router";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useInstanceContext } from "./InstanceLayout";
import { UnsavedChangesDialog, useUnsavedChangesGuard } from "./files/use-unsaved-guard";
import { DangerTab } from "./settings/danger-tab";
import { EnvTab } from "./settings/env-tab";
import { GeneralTab } from "./settings/general-tab";
import { PropertiesTab } from "./settings/properties-tab";
import { ResourcesTab } from "./settings/resources-tab";
import { Notice, type TabProps } from "./settings/settings-ui";
import { SoftwareTab } from "./settings/software-tab";

type TabId = "properties" | "resources" | "software" | "environment" | "general" | "danger";

const TABS: { id: TabId; label: string; icon: ComponentType<{ className?: string }>; component: ComponentType<TabProps> }[] = [
  { id: "properties", label: "Properties", icon: FileCog, component: PropertiesTab },
  { id: "resources", label: "Resources & JVM", icon: Cpu, component: ResourcesTab },
  { id: "software", label: "Software", icon: Boxes, component: SoftwareTab },
  { id: "environment", label: "Environment", icon: Variable, component: EnvTab },
  { id: "general", label: "General", icon: Settings2, component: GeneralTab },
  { id: "danger", label: "Danger zone", icon: TriangleAlert, component: DangerTab },
];

function isTab(v: string | null): v is TabId {
  return !!v && TABS.some((t) => t.id === v);
}

export default function Settings() {
  const { instance, can } = useInstanceContext();
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const tab: TabId = isTab(requested) ? requested : "properties";
  // Tabs are mounted on first visit and then kept (hidden) so unsaved edits survive switching.
  const [visited, setVisited] = useState<Set<TabId>>(() => new Set([tab]));
  const [dirty, setDirty] = useState<Partial<Record<TabId, boolean>>>({});
  if (!visited.has(tab)) setVisited(new Set([...visited, tab]));

  const reporters = useState(() => {
    const out = {} as Record<TabId, (d: boolean) => void>;
    for (const t of TABS) out[t.id] = (d: boolean) => setDirty((prev) => (!!prev[t.id] === d ? prev : { ...prev, [t.id]: d }));
    return out;
  })[0];
  const anyDirty = Object.values(dirty).some(Boolean);
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // keep the active tab visible in the horizontally scrolling tab bar (mobile)
    listRef.current?.querySelector('[data-state="active"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [tab]);
  const blocker = useUnsavedChangesGuard(anyDirty);

  const selectTab = useCallback(
    (v: string) =>
      setParams(
        (prev) => {
          const n = new URLSearchParams(prev);
          if (v === "properties") n.delete("tab");
          else n.set("tab", v);
          return n;
        },
        { replace: true },
      ),
    [setParams],
  );

  return (
    <div className="grid min-w-0 grid-cols-1 gap-4">
      {instance.archived_at && (
        <Notice
          title="This server is archived"
          action={
            tab !== "danger" && can("instance.delete") ? (
              <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => selectTab("danger")}>
                Unarchive…
              </button>
            ) : undefined
          }
        >
          <span className="inline-flex items-center gap-1">
            <Archive className="size-3" aria-hidden /> Its container was removed. Settings can still be edited and apply when it is
            unarchived and started.
          </span>
        </Notice>
      )}
      <Tabs value={tab} onValueChange={selectTab} className="min-w-0 gap-4">
        <div ref={listRef} className="-mx-3 overflow-x-auto px-3 pb-1.5 md:mx-0 md:px-0">
          <TabsList variant="line" aria-label="Settings sections" className="w-max">
            {TABS.map((t) => (
              <TabsTrigger
                key={t.id}
                value={t.id}
                className={t.id === "danger" ? "data-active:text-destructive" : undefined}
              >
                <t.icon className="size-4" />
                {t.label}
                {dirty[t.id] && (
                  <span className="size-1.5 rounded-full bg-primary" aria-label="(unsaved changes)" role="img" />
                )}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        {TABS.filter((t) => visited.has(t.id)).map((t) => (
          <TabsContent key={t.id} value={t.id} forceMount className="data-[state=inactive]:hidden">
            <t.component instance={instance} can={can} onDirtyChange={reporters[t.id]} />
          </TabsContent>
        ))}
      </Tabs>
      <UnsavedChangesDialog
        blocker={blocker}
        description="Some settings tabs have unsaved changes. They will be lost if you leave this page."
      />
    </div>
  );
}
