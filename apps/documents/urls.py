from __future__ import annotations

from django.urls import path

from . import views

app_name = "documents"

urlpatterns = [
    path("documents/<uuid:document_id>/", views.document_stream, name="document-stream"),
]
