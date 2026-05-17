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

    # Customer invoicing (Phase 1.1)
    path("invoices/", views.invoice_list, name="invoice_list"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<int:pk>/post/", views.invoice_post, name="invoice_post"),
    path("invoices/<int:pk>/pay/", views.invoice_record_payment, name="invoice_record_payment"),
    path("invoices/<int:pk>/cancel/", views.invoice_cancel, name="invoice_cancel"),
]
