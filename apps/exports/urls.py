from __future__ import annotations

from django.urls import path

from . import views

app_name = "exports"

urlpatterns = [
    path("ops/exports/", views.export_workspace, name="workspace"),
    path("ops/exports/<uuid:pk>/download/", views.export_download, name="download"),
]
