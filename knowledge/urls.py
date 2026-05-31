"""URLs for the knowledge app's non-admin views."""

from django.urls import path

from . import views


app_name = "knowledge"

urlpatterns = [
    # JSON retrieval endpoint (Step 14) — the API the agents call.
    path("retrieve/", views.retrieve_view, name="retrieve"),
    # Rule-explorer UI (Step 23) — browse / search / cite-from.
    path("explorer/", views.explorer_view, name="explorer"),
    path("explorer/<slug:slug>/", views.rule_detail_view, name="rule_detail"),
]
