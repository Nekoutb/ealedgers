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

    # Supplier bills (Phase 1.2)
    path("bills/", views.bill_list, name="bill_list"),
    path("bills/<int:pk>/", views.bill_detail, name="bill_detail"),
    path("bills/<int:pk>/post/", views.bill_post, name="bill_post"),
    path("bills/<int:pk>/pay/", views.bill_record_payment, name="bill_record_payment"),
    path("bills/<int:pk>/cancel/", views.bill_cancel, name="bill_cancel"),

    # Bank reconciliation (Phase 1.3)
    path("bank/", views.bank_statement_list, name="bank_statement_list"),
    path("bank/import/", views.bank_statement_import, name="bank_statement_import"),
    path("bank/<int:pk>/", views.bank_statement_detail, name="bank_statement_detail"),
    path("bank/line/<int:pk>/match/", views.bank_line_match, name="bank_line_match"),
    path("bank/line/<int:pk>/unmatch/", views.bank_line_unmatch, name="bank_line_unmatch"),
]
