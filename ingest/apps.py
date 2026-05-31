from django.apps import AppConfig


class IngestConfig(AppConfig):
    """L2 — Ingestion layer (inbound).

    Where transactions enter the system: document upload + OCR, email-to-bill
    webhook, bank-statement feeds, and ERP inbound sync (pulling master data
    and balances for context). The `Document` model + OCR pipeline land in
    Step 52; bank-feed parsers in Step 82.

    Scaffolded empty in Step 11.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ingest'
    verbose_name = 'Ingestion (documents, OCR, feeds)'
