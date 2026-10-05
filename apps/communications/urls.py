from django.urls import path

from . import views

app_name = "communications"

urlpatterns = [
    path("ops/communications/", views.operations_messages, name="operations-messages"),
]
