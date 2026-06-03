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
    # Reviewer CSV export (Step 27 enablement).
    path("export.csv", views.export_rules_view, name="export_rules"),
    # Tenant-procedure UI (Step 24) — a tenant's own rules.
    path("procedures/", views.procedure_list_view, name="procedure_list"),
    path("procedures/new/", views.procedure_create_view, name="procedure_create"),
    path("procedures/<slug:slug>/edit/", views.procedure_edit_view, name="procedure_edit"),
]
