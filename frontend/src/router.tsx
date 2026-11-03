import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import { createBrowserRouter, Navigate, Outlet } from "react-router";
import { AppLayout } from "@/components/layout/AppLayout";
import { ErrorState, LoadingState } from "@/components/States";
import { useAuth } from "@/hooks/use-auth";
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

function AuthGate() {
  const { state, isLoading, error, refetch } = useAuth();
  if (isLoading) return <LoadingState label="Connecting to MCPanel…" className="min-h-svh" />;
  if (error || !state) return <div className="p-6"><ErrorState error={error} onRetry={() => refetch()} title="Cannot reach the panel API" /></div>;
  if (state.setup_required) return <SetupPage />;
  if (!state.authenticated) return <LoginPage />;
  return <Outlet />;
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
