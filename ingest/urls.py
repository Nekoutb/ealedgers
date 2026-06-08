"""URLs for the ingestion app (Step 52)."""

from django.urls import path

from . import views


app_name = 'ingest'

urlpatterns = [
    # Manual PDF/image upload + departmental inbox (login-gated).
    path('upload/', views.document_upload, name='document_upload'),
    # Email-to-bill inbound webhook (CSRF-exempt, token-guarded).
    path('email/', views.email_webhook, name='email_webhook'),
]
