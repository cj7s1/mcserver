"""Mods/plugins content manager routes (mounted at /api/instances/<uuid:pk>/content/)."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.ContentListView.as_view()),
    path("updates", views.ContentUpdatesView.as_view()),
    path("search", views.ContentSearchView.as_view()),
    path("projects/<str:project_id>/versions", views.ContentProjectVersionsView.as_view()),
    path("install", views.ContentInstallView.as_view()),
    path("update", views.ContentUpdateView.as_view()),
    path("toggle", views.ContentToggleView.as_view()),
    path("files", views.ContentFilesView.as_view()),
    path("upload", views.ContentUploadView.as_view()),
]
