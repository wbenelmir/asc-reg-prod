from __future__ import annotations

from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("ops/events/registration-channels/", views.channel_list, name="channel-list"),
    path(
        "ops/events/<uuid:pk>/registration-channels/",
        views.channel_detail,
        name="channel-detail",
    ),
]
