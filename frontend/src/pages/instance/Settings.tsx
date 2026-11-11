import { Archive } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ComponentType } from "react";
import { useSearchParams } from "react-router";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { defaultSettingsTab, SETTINGS_TABS, settingsTabs, type SettingsTabId } from "@/lib/sections";
import { isMinecraft } from "@/lib/games";
import { useInstanceContext } from "./InstanceLayout";
import { UnsavedChangesDialog, useUnsavedChangesGuard } from "./files/use-unsaved-guard";
import { DangerTab } from "./settings/danger-tab";
import { EnvTab } from "./settings/env-tab";
import { GameSettingsTab } from "./settings/game-settings-tab";
import { GameSoftwareTab } from "./settings/game-software-tab";
import { GeneralTab } from "./settings/general-tab";
import { PropertiesTab } from "./settings/properties-tab";
import { ResourcesTab } from "./settings/resources-tab";
import { Notice, type TabProps } from "./settings/settings-ui";
import { SoftwareTab } from "./settings/software-tab";

type TabId = SettingsTabId;

/** The page of each Settings tab; which tabs a server shows comes from the section registry. */
/** Minecraft's Software tab, or the version catalog of another game. */
function AnySoftwareTab(props: TabProps) {
  return isMinecraft(props.instance) ? <SoftwareTab {...props} /> : <GameSoftwareTab {...props} />;
}

const COMPONENTS: Record<TabId, ComponentType<TabProps>> = {
  "game-settings": GameSettingsTab,
  properties: PropertiesTab,
  resources: ResourcesTab,
  software: AnySoftwareTab,
  environment: EnvTab,
  general: GeneralTab,
  danger: DangerTab,
};

export default function Settings() {
  const { instance, can } = useInstanceContext();
  const [params, setParams] = useSearchParams();
  const TABS = settingsTabs(instance);
  const defaultTab = defaultSettingsTab(instance);
  const requested = params.get("tab");
  const tab: TabId = TABS.some((t) => t.id === requested) ? (requested as TabId) : defaultTab;
  // Tabs are mounted on first visit and then kept (hidden) so unsaved edits survive switching.
  const [visited, setVisited] = useState<Set<TabId>>(() => new Set([tab]));
  const [dirty, setDirty] = useState<Partial<Record<TabId, boolean>>>({});
  if (!visited.has(tab)) setVisited(new Set([...visited, tab]));

  const reporters = useState(() => {
    const out = {} as Record<TabId, (d: boolean) => void>;
    for (const t of SETTINGS_TABS) out[t.id] = (d: boolean) => setDirty((prev) => (!!prev[t.id] === d ? prev : { ...prev, [t.id]: d }));
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
          if (v === defaultTab) n.delete("tab");
          else n.set("tab", v);
          return n;
        },
        { replace: true },
      ),
    [setParams, defaultTab],
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
                {t.label(instance)}
                {dirty[t.id] && (
                  <span className="size-1.5 rounded-full bg-primary" aria-label="(unsaved changes)" role="img" />
                )}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        {TABS.filter((t) => visited.has(t.id)).map((t) => (
          <TabsContent key={t.id} value={t.id} forceMount className="data-[state=inactive]:hidden">
            <TabPage id={t.id} instance={instance} can={can} onDirtyChange={reporters[t.id]} />
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

function TabPage({ id, ...props }: TabProps & { id: TabId }) {
  const Page = COMPONENTS[id];
  return <Page {...props} />;
}
