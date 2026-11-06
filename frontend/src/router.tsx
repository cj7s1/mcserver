import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet } from "react-router";
import { AppLayout } from "@/components/layout/AppLayout";
import { ErrorState, LoadingState } from "@/components/States";
import { useAuth, useAuthLifecycle } from "@/hooks/use-auth";
import { authGateView } from "@/lib/auth-gate";
import { LoginPage, ResetPasswordPage, SetupPage } from "@/pages/AuthPages";
import { InstanceLayout } from "@/pages/instance/InstanceLayout";

function page(loader: () => Promise<{ default: ComponentType }>): ReactNode {
  const C = lazy(loader);
  return (
    <Suspense fallback={<LoadingState />}>
      <C />
    </Suspense>
  );
}

/** Focused "Set a new password" screen (lazy, it lives with the Account page's password form). */
const forcedPasswordChange = page(() => import("@/pages/AccountPage").then((m) => ({ default: m.ForcedPasswordChange })));

function AuthGate() {
  const { state, isLoading, error, refetch } = useAuth();
  useAuthLifecycle(state);
  switch (authGateView({ state, isLoading, error })) {
    case "loading":
      return <LoadingState label="Connecting to MCPanel…" className="min-h-svh" />;
    case "error":
      return <div className="p-6"><ErrorState error={error} onRetry={() => refetch()} title="Cannot reach the panel API" /></div>;
    case "setup":
      return <SetupPage />;
    case "login":
      return <LoginPage />;
    case "change-password":
      // Rendered in place of every page (the URL is kept): once the password is changed the
      // auth state refreshes and the page the user asked for appears.
      return forcedPasswordChange;
    case "app":
      // Keyed by user: a different account (e.g. signed in from another tab) never inherits the
      // previous one's in-memory page state (console history, terminal, forms).
      return <Outlet key={state?.user?.id} />;
  }
}

export const router = createBrowserRouter([
  { path: "/reset-password", element: <ResetPasswordPage /> },
  {
    element: <AuthGate />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: page(() => import("@/pages/Dashboard")) },
          { path: "servers", element: page(() => import("@/pages/ServersPage")) },
          { path: "servers/new", element: page(() => import("@/pages/CreateServer")) },
          {
            path: "servers/:id",
            element: <InstanceLayout />,
            children: [
              { index: true, element: page(() => import("@/pages/instance/Overview")) },
              { path: "console", element: page(() => import("@/pages/instance/Console")) },
              { path: "files", element: page(() => import("@/pages/instance/Files")) },
              { path: "content", element: page(() => import("@/pages/instance/Content")) },
              { path: "modpack", element: page(() => import("@/pages/instance/Modpack")) },
              { path: "players", element: page(() => import("@/pages/instance/Players")) },
              { path: "backups", element: page(() => import("@/pages/instance/Backups")) },
              { path: "schedules", element: page(() => import("@/pages/instance/Schedules")) },
              { path: "network", element: page(() => import("@/pages/instance/Network")) },
              { path: "settings", element: page(() => import("@/pages/instance/Settings")) },
              { path: "access", element: page(() => import("@/pages/instance/Access")) },
              { path: "activity", element: page(() => import("@/pages/instance/InstanceActivity")) },
            ],
          },
          { path: "modpacks", element: page(() => import("@/pages/ModpacksPage")) },
          { path: "activity", element: page(() => import("@/pages/ActivityPage")) },
          { path: "account", element: page(() => import("@/pages/AccountPage")) },
          { path: "admin/users", element: page(() => import("@/pages/admin/UsersPage")) },
          { path: "admin/audit", element: page(() => import("@/pages/admin/AuditPage")) },
          { path: "admin/system", element: page(() => import("@/pages/admin/SystemPage")) },
          { path: "*", element: <Navigate to="/" replace /> },
        ],
      },
    ],
  },
]);
