"""URLs for the accounting app's custom (non-admin) views."""

from django.urls import path

from . import views


app_name = "accounting"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("reports/trial-balance/", views.trial_balance, name="trial_balance"),
    path("reports/general-ledger/", views.general_ledger, name="general_ledger"),
    path("reports/customer-aging/", views.customer_aging, name="customer_aging"),
    path("reports/supplier-aging/", views.supplier_aging, name="supplier_aging"),
    path("reports/fixed-assets-register/", views.fixed_assets_register, name="fixed_assets_register"),
]
