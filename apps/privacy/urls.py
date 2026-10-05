from django.urls import path

from . import views

app_name = "privacy"

urlpatterns = [
    path("legal/", views.legal_information, name="legal-information"),
]
