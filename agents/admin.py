"""Agents-layer admin (Step 52+)."""

from django.contrib import admin
from django.utils.html import format_html

from .models import APDocument


@admin.register(APDocument)
class APDocumentAdmin(admin.ModelAdmin):
    """Admin view for inbound AP documents."""

    list_display = (
        'original_filename', 'tenant', 'source', 'status',
        'content_type', 'file_size_display', 'chain_id_short', 'received_at',
    )
    list_filter = ('status', 'source', 'tenant')
    search_fields = ('original_filename', 'notes', 'chain_id', 'tenant__name')
    readonly_fields = ('chain_id', 'bus_event', 'received_at', 'file_size', 'content_type')
    ordering = ('-received_at',)
    date_hierarchy = 'received_at'

    @admin.display(description='Size')
    def file_size_display(self, obj):
        return obj.file_size_kb

    @admin.display(description='Chain (short)')
    def chain_id_short(self, obj):
        if not obj.chain_id:
            return '—'
        return format_html(
            '<a href="/workspace/chains/{}/">{}</a>',
            obj.chain_id, obj.chain_id[:12] + '…',
        )
