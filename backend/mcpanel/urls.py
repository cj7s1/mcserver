from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest
from django.urls import include, path, re_path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.accounts import views as accounts
from apps.audit import views as audit
from apps.backups import views as backups
from apps.catalog import views as catalog
from apps.content import views as content
from apps.core import views as core
from apps.instances import views as instances
from apps.jobs import views as jobs
from apps.metrics import views as metrics
from apps.modpacks import views as modpacks
from apps.notifications import views as notifications
from apps.players import views as players

auth_patterns = [
    path("state", accounts.AuthStateView.as_view()),
    path("setup", accounts.SetupView.as_view()),
    path("login", accounts.LoginView.as_view()),
    path("logout", accounts.LogoutView.as_view()),
    path("me", accounts.MeView.as_view()),
    path("password", accounts.PasswordChangeView.as_view()),
    path("reset", accounts.PasswordResetView.as_view()),
]

instance_patterns = [
    path("", instances.InstanceListView.as_view()),
    path("<uuid:pk>", instances.InstanceDetailView.as_view()),
    path("<uuid:pk>/power", instances.InstancePowerView.as_view()),
    path("<uuid:pk>/clone", instances.InstanceCloneView.as_view()),
    path("<uuid:pk>/stats", instances.InstanceStatsView.as_view()),
    path("<uuid:pk>/logs", instances.InstanceLogsView.as_view()),
    path("<uuid:pk>/command", instances.InstanceCommandView.as_view()),
    path("<uuid:pk>/properties", instances.PropertiesView.as_view()),
    path("<uuid:pk>/jobs", instances.InstanceJobsView.as_view()),
    path("<uuid:pk>/access", instances.AccessListView.as_view()),
    path("<uuid:pk>/players", players.PlayersView.as_view()),
    path("<uuid:pk>/players/history", metrics.PlayerHistoryView.as_view()),
    path("<uuid:pk>/metrics", metrics.MetricsView.as_view()),
    path("<uuid:pk>/files", instances.FilesListView.as_view()),
    path("<uuid:pk>/files/text", instances.FilesTextView.as_view()),
    path("<uuid:pk>/files/download", instances.FilesDownloadView.as_view()),
    path("<uuid:pk>/files/upload", instances.FilesUploadView.as_view()),
    path("<uuid:pk>/files/extract", instances.ExtractView.as_view()),
    path("<uuid:pk>/import", instances.ImportArchiveView.as_view()),
    path("<uuid:pk>/diagnostics", instances.DiagnosticsView.as_view()),
    path("<uuid:pk>/software", instances.SoftwareUpdateView.as_view()),
    path("<uuid:pk>/manual-downloads", instances.ManualDownloadsView.as_view()),
    path("<uuid:pk>/files/<str:op>", instances.FilesOpView.as_view()),
    path("<uuid:pk>/schedules", instances.ScheduleListView.as_view()),
    path("<uuid:pk>/schedules/<int:sid>", instances.ScheduleDetailView.as_view()),
    path("<uuid:pk>/snapshots", backups.SnapshotListView.as_view()),
    path("<uuid:pk>/snapshots/convert", backups.SnapshotConvertView.as_view()),
    path("<uuid:pk>/snapshots/<str:name>", backups.SnapshotDetailView.as_view()),
    path("<uuid:pk>/backups", backups.BackupListView.as_view()),
    path("<uuid:pk>/backups/policy", backups.BackupPolicyView.as_view()),
    path("<uuid:pk>/backups/verify", backups.BackupVerifyView.as_view()),
    path("<uuid:pk>/backups/<uuid:bid>", backups.BackupDetailView.as_view()),
    path("<uuid:pk>/backups/<uuid:bid>/restore", backups.BackupRestoreView.as_view()),
    path("<uuid:pk>/backups/<uuid:bid>/restore-as-new", backups.BackupRestoreNewView.as_view()),
    path("<uuid:pk>/backups/<uuid:bid>/files", backups.BackupBrowseView.as_view()),
    path("<uuid:pk>/backups/<uuid:bid>/download", backups.BackupFileDownloadView.as_view()),
    path("<uuid:pk>/backups/<uuid:bid>/restore-path", backups.BackupRestorePathView.as_view()),
    # without the slash GET .../content would fall through to the instance-action route below
    path("<uuid:pk>/content", content.ContentListView.as_view()),
    path("<uuid:pk>/content/", include("apps.content.urls")),
    path("<uuid:pk>/modpack", modpacks.InstanceModpackView.as_view()),
    path("<uuid:pk>/modpack/plan/<str:version_id>", modpacks.InstanceModpackPlanView.as_view()),
    path("<uuid:pk>/modpack/update", modpacks.InstanceModpackUpdateView.as_view()),
    path("<uuid:pk>/modpack/rollback", modpacks.InstanceModpackRollbackView.as_view()),
    path("<uuid:pk>/<str:action>", instances.InstanceActionView.as_view()),
]

api_patterns = [
    path("health", core.HealthView.as_view()),
    path("auth/", include(auth_patterns)),
    path("users", accounts.UserListCreateView.as_view()),
    path("users/<int:pk>", accounts.UserDetailView.as_view()),
    path("users/<int:pk>/reset-link", accounts.UserResetLinkView.as_view()),
    path("roles", accounts.RoleListCreateView.as_view()),
    path("roles/<int:pk>", accounts.RoleDetailView.as_view()),
    path("permissions", accounts.PermissionDefsView.as_view()),
    path("instances/", include(instance_patterns)),
    path("jobs", jobs.JobListView.as_view()),
    path("jobs/<uuid:pk>", jobs.JobDetailView.as_view()),
    path("jobs/<uuid:pk>/abandon", jobs.JobAbandonView.as_view()),
    path("audit", audit.AuditListView.as_view()),
    path("catalog/server-types", catalog.ServerTypesView.as_view()),
    path("catalog/versions", catalog.GameVersionsView.as_view()),
    path("catalog/loaders", catalog.LoaderVersionsView.as_view()),
    path("catalog/java", catalog.JavaView.as_view()),
    path("modpacks/providers", modpacks.ProvidersView.as_view()),
    path("modpacks/<str:provider>/search", modpacks.SearchView.as_view()),
    path("modpacks/<str:provider>/projects/<str:project_id>", modpacks.ProjectView.as_view()),
    path("modpacks/<str:provider>/projects/<str:project_id>/releases", modpacks.ReleasesView.as_view()),
    path("modpacks/<str:provider>/projects/<str:project_id>/releases/<str:version_id>", modpacks.ReleaseDetailView.as_view()),
    path("modpacks/<str:provider>/projects/<str:project_id>/releases/<str:version_id>/resolve", modpacks.ResolveView.as_view()),
    path("system/info", core.SystemInfoView.as_view()),
    path("system/settings", core.SystemSettingsView.as_view()),
    path("system/ports", core.PortsView.as_view()),
    path("notifications/events", notifications.EventsView.as_view()),
    path("notifications/channels", notifications.ChannelListView.as_view()),
    path("notifications/channels/<int:pk>", notifications.ChannelDetailView.as_view()),
    path("notifications/channels/<int:pk>/test", notifications.ChannelTestView.as_view()),
    path("schema", SpectacularAPIView.as_view(), name="schema"),
    path("docs", SpectacularSwaggerView.as_view(url_name="schema")),
]


def spa(request: HttpRequest, path: str = "") -> FileResponse:
    index = settings.FRONTEND_DIST / "index.html"
    if not index.exists():
        raise Http404("Frontend build not found")
    response = FileResponse(index.open("rb"), content_type="text/html")
    response["Cache-Control"] = "no-cache"
    return response


urlpatterns = [
    path("api/", include(api_patterns)),
    re_path(r"^(?!api/|ws/|static/).*$", spa),
]
