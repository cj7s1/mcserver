/** Connection and Software cards for the instance Overview. */
import { ExternalLink, Globe, House, Info, Network as NetworkIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { Section } from "@/components/PageHeader";
import { useAuth } from "@/hooks/use-auth";
import { formatDateTime, formatMemoryMb, relativeTime } from "@/lib/format";
import type { ConnectionInfo, Instance } from "@/lib/types";
import { cn } from "@/lib/utils";
import { displayValue, optionValue } from "@/lib/game-config";
import { gameLabel, isMinecraft, knownVariantLabel } from "@/lib/games";
import { useGameInfo } from "../game-api";
import { BranchVersion } from "./BranchVersion";
import { ConnectionPorts } from "../network/GameNetwork";
import { CopyButton, capitalize } from "./overview-utils";

const KIND_STYLE: Record<ConnectionInfo["addresses"][number]["kind"], { label: string; className: string; icon: ReactNode }> = {
  lan: { label: "LAN", className: "bg-sky-500/10 text-sky-700 dark:text-sky-300", icon: <House aria-hidden /> },
  public: { label: "Public", className: "bg-violet-500/10 text-violet-700 dark:text-violet-300", icon: <Globe aria-hidden /> },
  bind: { label: "Bound", className: "bg-muted text-muted-foreground", icon: <NetworkIcon aria-hidden /> },
};

function SectionLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link to={to} className="text-xs font-medium text-primary hover:underline">
      {children}
    </Link>
  );
}

export function ConnectionCard({ instance }: { instance: Instance }) {
  // other games: one block per named port with its protocol (network/GameNetwork.tsx)
  if (!isMinecraft(instance)) {
    return (
      <Section
        title="Connection"
        description="Addresses players use to join"
        actions={<SectionLink to={`/servers/${instance.id}/network`}>Network settings</SectionLink>}
      >
        <ConnectionPorts instance={instance} />
      </Section>
    );
  }
  return <MinecraftConnectionCard instance={instance} />;
}

function MinecraftConnectionCard({ instance }: { instance: Instance }) {
  const { can } = useAuth();
  const c = instance.connection;
  const port = c.host_port;
  const bindAll = c.bind_address === "0.0.0.0" || c.bind_address === "::";
  return (
    <Section
      title="Connection"
      description="Addresses players use to join"
      actions={<SectionLink to={`/servers/${instance.id}/network`}>Network settings</SectionLink>}
    >
      <div className="grid gap-4">
        {c.addresses.length > 0 ? (
          <ul className="grid gap-2">
            {c.addresses.map((a) => {
              const k = KIND_STYLE[a.kind] ?? KIND_STYLE.bind;
              return (
                <li key={`${a.kind}-${a.address}`} className="flex items-center gap-3 rounded-lg border bg-background/40 px-3 py-2">
                  <span
                    className={cn(
                      "inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium [&_svg]:size-3",
                      k.className,
                    )}
                  >
                    {k.icon}
                    {k.label}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-sm">{a.address}</div>
                    <div className="truncate text-xs text-muted-foreground">{a.label}</div>
                  </div>
                  <CopyButton value={a.address} label={`Copy ${k.label} address`} />
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="rounded-lg border border-dashed px-3 py-3 text-sm">
            <p>
              No LAN or public address is configured, so the panel cannot show a complete join address. Players connect to
              this machine&apos;s IP address on port <span className="font-mono font-semibold">{port}</span>.
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {can("system.settings") ? (
                <>
                  Set the LAN and public address in <SectionLink to="/admin/system">System settings</SectionLink> to show
                  copyable addresses here.
                </>
              ) : (
                "An administrator can set the LAN and public address in System settings."
              )}
            </p>
          </div>
        )}

        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Game port</dt>
          <dd className="flex min-w-0 items-center gap-1">
            <span className="font-mono">{port}</span>
            <span className="text-xs text-muted-foreground">TCP</span>
            <CopyButton value={String(port)} label="Copy port" />
          </dd>
          <dt className="text-muted-foreground">Mapping</dt>
          <dd className="min-w-0 truncate font-mono text-xs">
            {bindAll ? "all interfaces" : c.bind_address}:{port} → container :{c.container_port}
          </dd>
          {instance.extra_ports.map((p) => (
            <div key={`${p.protocol}-${p.host_port}`} className="contents">
              <dt className="text-muted-foreground">{p.label || "Extra port"}</dt>
              <dd className="min-w-0 truncate font-mono text-xs">
                {p.host_port}/{p.protocol} → container :{p.container_port}
              </dd>
            </div>
          ))}
        </dl>

        <div className="grid gap-2 text-xs sm:grid-cols-2">
          <div className="rounded-lg bg-muted/50 p-3">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <House className="size-3.5 text-sky-600 dark:text-sky-400" aria-hidden /> LAN
            </div>
            <p className="text-muted-foreground">Players on the same local network as this machine. No router changes needed.</p>
          </div>
          <div className="rounded-lg bg-muted/50 p-3">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <Globe className="size-3.5 text-violet-600 dark:text-violet-400" aria-hidden /> Public
            </div>
            <p className="text-muted-foreground">
              Players on the internet. Forward TCP port <span className="font-mono">{port}</span> on your router to this
              machine yourself — the panel does not configure it.
            </p>
          </div>
        </div>

        {c.notes.length > 0 && (
          <ul className="grid gap-1.5">
            {c.notes.map((n) => (
              <li key={n} className="flex gap-2 text-xs text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span>{n}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

function modpackUrl(i: Instance): string | null {
  switch (i.modpack_provider) {
    case "modrinth":
      return i.modpack_slug || i.modpack_project_id ? `https://modrinth.com/modpack/${i.modpack_slug || i.modpack_project_id}` : null;
    case "curseforge":
      return i.modpack_slug ? `https://www.curseforge.com/minecraft/modpacks/${i.modpack_slug}` : null;
    case "ftb":
      return i.modpack_project_id
        ? `https://www.feed-the-beast.com/modpacks/${i.modpack_project_id}${i.modpack_slug ? `-${i.modpack_slug}` : ""}`
        : null;
    default:
      return null;
  }
}

const PROVIDER_LABELS: Record<string, string> = { modrinth: "Modrinth", curseforge: "CurseForge", ftb: "Feed The Beast" };
const JVM_PRESETS: Record<Instance["jvm_flags_preset"], string> = {
  none: "JVM defaults",
  aikar: "Aikar's flags",
  meowice: "MeowIce's flags",
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </>
  );
}

export function SoftwareCard({ instance, canConfigure }: { instance: Instance; canConfigure: boolean }) {
  return isMinecraft(instance) ? (
    <MinecraftSoftwareCard instance={instance} canConfigure={canConfigure} />
  ) : (
    <GameSoftwareCard instance={instance} canConfigure={canConfigure} />
  );
}

/** Software of a server of another game: game, version, pinned options, image, memory limit. */
function GameSoftwareCard({ instance: i, canConfigure }: { instance: Instance; canConfigure: boolean }) {
  const game = useGameInfo(i);
  const canControl = (i.permissions ?? []).includes("instance.control");
  const pinned = (game?.options ?? []).filter((o) => o.pinned || (i.game_options && o.key in i.game_options));
  return (
    <Section
      title="Software"
      description="What this server runs"
      actions={canConfigure ? <SectionLink to={`/servers/${i.id}/settings`}>Settings</SectionLink> : undefined}
    >
      {i.description && <p className="mb-3 text-sm text-muted-foreground">{i.description}</p>}
      <dl className="grid grid-cols-[minmax(6.5rem,auto)_1fr] gap-x-4 gap-y-2 text-sm">
        <Row label="Game">{gameLabel(i)}</Row>
        <Row label="Version">
          {game?.fixed_version ? <BranchVersion instance={i} info={game.fixed_version} canControl={canControl} /> : i.minecraft_version || "—"}
        </Row>
        {pinned.map((o) => (
          <Row key={o.key} label={o.label}>
            {displayValue(o, optionValue(i.game_options, o.key))}
          </Row>
        ))}
        <Row label="Image">
          <span className="flex min-w-0 items-center gap-1">
            <span className="truncate font-mono text-xs">{i.image}</span>
            <CopyButton value={i.image} label="Copy image name" />
          </span>
        </Row>
        <Row label="Memory">{formatMemoryMb(i.container_memory_mb)} container limit</Row>
        <Row label="Installed">
          {i.installed_at ? <span title={formatDateTime(i.installed_at)}>{relativeTime(i.installed_at)}</span> : <span className="text-muted-foreground">Not yet</span>}
        </Row>
        <Row label="Created">{formatDateTime(i.created_at)}</Row>
      </dl>
    </Section>
  );
}

function MinecraftSoftwareCard({ instance: i, canConfigure }: { instance: Instance; canConfigure: boolean }) {
  const url = i.is_modpack ? modpackUrl(i) : null;
  const hasLoader = i.loader && i.loader !== "vanilla" && i.loader !== "none";
  return (
    <Section
      title="Software"
      description="What this server runs"
      actions={canConfigure ? <SectionLink to={`/servers/${i.id}/settings`}>Settings</SectionLink> : undefined}
    >
      {i.description && <p className="mb-3 text-sm text-muted-foreground">{i.description}</p>}
      <dl className="grid grid-cols-[minmax(6.5rem,auto)_1fr] gap-x-4 gap-y-2 text-sm">
        <Row label="Server type">{knownVariantLabel(i.server_type) ?? i.server_type}</Row>
        {i.is_modpack && (
          <Row label="Modpack">
            <div className="flex min-w-0 flex-wrap items-center gap-x-2">
              <span className="truncate font-medium">{i.modpack_name || i.modpack_slug || i.modpack_project_id}</span>
              {i.modpack_version_name && <span className="text-muted-foreground">{i.modpack_version_name}</span>}
              {url ? (
                <a
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  {PROVIDER_LABELS[i.modpack_provider] ?? i.modpack_provider}
                  <ExternalLink className="size-3" aria-hidden />
                  <span className="sr-only">(opens in a new tab)</span>
                </a>
              ) : (
                i.modpack_provider && <span className="text-xs text-muted-foreground">{PROVIDER_LABELS[i.modpack_provider] ?? i.modpack_provider}</span>
              )}
            </div>
          </Row>
        )}
        <Row label={gameLabel(i)}>
          {i.minecraft_version || "—"} <span className="text-xs text-muted-foreground">{capitalize(i.edition)} edition</span>
        </Row>
        {hasLoader && (
          <Row label="Loader">
            {i.loader === "neoforge" ? "NeoForge" : capitalize(i.loader)}{" "}
            <span className={cn(!i.loader_version && "text-muted-foreground")}>{i.loader_version || "(resolved at install)"}</span>
          </Row>
        )}
        <Row label="Java">Java {i.java_version}</Row>
        <Row label="Image">
          <span className="flex min-w-0 items-center gap-1">
            <span className="truncate font-mono text-xs">{i.image}</span>
            <CopyButton value={i.image} label="Copy image name" />
          </span>
        </Row>
        <Row label="Memory">
          {formatMemoryMb(i.memory_mb)} heap <span className="text-xs text-muted-foreground">· {formatMemoryMb(i.container_memory_mb)} container limit</span>
        </Row>
        <Row label="JVM flags">{JVM_PRESETS[i.jvm_flags_preset] ?? i.jvm_flags_preset}</Row>
        <Row label="Installed">
          {i.installed_at ? <span title={formatDateTime(i.installed_at)}>{relativeTime(i.installed_at)}</span> : <span className="text-muted-foreground">Not yet</span>}
        </Row>
        <Row label="Created">{formatDateTime(i.created_at)}</Row>
      </dl>
    </Section>
  );
}
