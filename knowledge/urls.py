"""URLs for the knowledge app's non-admin views."""

from django.urls import path

from . import views


app_name = "knowledge"

urlpatterns = [
    # JSON retrieval endpoint (Step 14). The rule-explorer UI is Step 23.
    path("retrieve/", views.retrieve_view, name="retrieve"),
]
