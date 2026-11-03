import {
  Activity,
  Boxes,
  LayoutDashboard,
  LogOut,
  Menu,
  Monitor,
  Moon,
  Package,
  Plus,
  ScrollText,
  Server,
  Settings,
  Sun,
  Users,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useAuth } from "@/hooks/use-auth";
import { useTheme } from "@/hooks/use-theme";
import { useLiveEvents } from "@/lib/events";
import { useInstances } from "@/lib/queries";
import type { PermissionCode } from "@/lib/types";
import { cn } from "@/lib/utils";
import { StatusDot } from "../StatusBadge";

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  perm?: PermissionCode;
  end?: boolean;
}

const MAIN_NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: <LayoutDashboard />, end: true },
  { to: "/servers", label: "Servers", icon: <Server />, end: true },
  { to: "/servers/new", label: "Create server", icon: <Plus />, perm: "instance.create" },
  { to: "/modpacks", label: "Modpacks", icon: <Package /> },
  { to: "/activity", label: "Activity", icon: <Activity /> },
];

const ADMIN_NAV: NavItem[] = [
  { to: "/admin/users", label: "Users & roles", icon: <Users />, perm: "system.users" },
  { to: "/admin/audit", label: "Audit log", icon: <ScrollText />, perm: "system.audit" },
  { to: "/admin/system", label: "System", icon: <Settings />, perm: "system.settings" },
];

function Logo() {
  return (
    <div className="flex items-center gap-2 px-2">
      <div className="grid size-8 place-items-center rounded-lg bg-primary/15 text-primary">
        <Boxes className="size-4.5" />
      </div>
      <div className="leading-tight">
        <div className="text-sm font-semibold tracking-tight">MCPanel</div>
        <div className="text-[11px] text-muted-foreground">Minecraft server manager</div>
      </div>
    </div>
  );
}

function NavList({ items, onNavigate }: { items: NavItem[]; onNavigate?: () => void }) {
  const { can } = useAuth();
  return (
    <nav className="grid gap-0.5">
      {items
        .filter((i) => !i.perm || can(i.perm))
        .map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm text-sidebar-foreground/80 transition-colors hover:bg-sidebar-accent hover:text-sidebar-foreground [&_svg]:size-4",
                isActive && "bg-sidebar-accent font-medium text-sidebar-foreground",
              )
            }
          >
            {item.icon}
            {item.label}
          </NavLink>
        ))}
    </nav>
  );
}

function ServerShortcuts({ onNavigate }: { onNavigate?: () => void }) {
  const { data } = useInstances();
  if (!data?.length) return null;
  return (
    <div className="grid grid-cols-1 gap-0.5">
      <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">Servers</div>
      {data.slice(0, 12).map((inst) => (
        <NavLink
          key={inst.id}
          to={`/servers/${inst.id}`}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              "flex min-w-0 items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm text-sidebar-foreground/80 hover:bg-sidebar-accent",
              isActive && "bg-sidebar-accent text-sidebar-foreground",
            )
          }
        >
          <StatusDot status={inst.status} />
          <span className="min-w-0 truncate" title={inst.name}>{inst.name}</span>
        </NavLink>
      ))}
    </div>
  );
}

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const { can } = useAuth();
  const showAdmin = ADMIN_NAV.some((i) => !i.perm || can(i.perm));
  return (
    <div className="flex h-full flex-col gap-5 overflow-y-auto px-3 py-4">
      <Logo />
      <NavList items={MAIN_NAV} onNavigate={onNavigate} />
      <ServerShortcuts onNavigate={onNavigate} />
      {showAdmin && (
        <div className="grid gap-0.5">
          <div className="px-2.5 pb-1 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">Administration</div>
          <NavList items={ADMIN_NAV} onNavigate={onNavigate} />
        </div>
      )}
    </div>
  );
}

function UserMenu() {
  const { user, logout } = useAuth();
  const { theme, setTheme } = useTheme();
  if (!user) return null;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="gap-2" aria-label="Account menu">
          <span className="grid size-6 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
            {user.username.slice(0, 1).toUpperCase()}
          </span>
          <span className="hidden text-sm sm:inline">{user.username}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel>
          <div className="text-sm">{user.username}</div>
          <div className="text-xs font-normal text-muted-foreground">
            {user.is_owner ? "Owner" : user.role_name ?? "No global role"}
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(v) => setTheme(v as "dark" | "light" | "system")}>
          <DropdownMenuRadioItem value="dark">
            <Moon /> Dark
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">
            <Sun /> Light
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="system">
            <Monitor /> System
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <NavLink to="/account">Account &amp; password</NavLink>
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => logout()}>
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function AppLayout() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const live = useLiveEvents(true);
  const location = useLocation();
  const wide = /\/servers\/[^/]+\/(console|files)/.test(location.pathname);
  return (
    <div className="flex min-h-svh bg-background">
      <aside className="sticky top-0 hidden h-svh w-60 shrink-0 border-r bg-sidebar md:block">
        <SidebarContent />
      </aside>
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-72 bg-sidebar p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SidebarContent onNavigate={() => setMobileOpen(false)} />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-12 items-center gap-2 border-b bg-background/80 px-3 backdrop-blur md:px-5">
          <Button variant="ghost" size="icon-sm" className="md:hidden" onClick={() => setMobileOpen(true)} aria-label="Open navigation">
            <Menu />
          </Button>
          <div className="flex-1" />
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="flex items-center gap-1.5 text-xs text-muted-foreground" aria-label={live ? "Live updates connected" : "Live updates disconnected"}>
                <span className={cn("size-1.5 rounded-full", live ? "bg-emerald-400" : "bg-zinc-500")} />
                <span className="hidden sm:inline">{live ? "Live" : "Offline"}</span>
              </span>
            </TooltipTrigger>
            <TooltipContent>{live ? "Receiving live status updates" : "Live updates unavailable; data refreshes periodically"}</TooltipContent>
          </Tooltip>
          <UserMenu />
        </header>
        <main className={cn("mx-auto w-full flex-1 px-3 py-5 md:px-6", wide ? "max-w-[1600px]" : "max-w-7xl")}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
