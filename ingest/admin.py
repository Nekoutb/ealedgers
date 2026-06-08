"""Ingestion-layer admin — Step 52."""

from django.contrib import admin

from ingest.models import Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'tenant', 'doc_type', 'source', 'status',
        'routed_dept', 'original_filename', 'created_at',
    )
    list_filter = ('source', 'doc_type', 'status', 'routed_dept', 'tenant')
    search_fields = (
        'original_filename', 'subject', 'sender_email', 'sha256', 'chain_id',
    )
    readonly_fields = (
        'sha256', 'size_bytes', 'content_type', 'chain_id',
        'routed_dept', 'routed_at', 'created_at', 'extracted_data',
    )
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
