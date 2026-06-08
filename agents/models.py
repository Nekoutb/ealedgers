"""Agents-layer models (Step 52+).

Orchestration-specific models for the virtual finance departments.

Note: the agent-RUNTIME audit models (AgentRun, AgentToolCall, BusEvent,
ApprovalQueueItem) already live in the ``accounting`` app (Step 7 / 42 / 43)
beside the ledger they describe. This app holds the domain objects that
belong to the agent orchestration layer — starting with AP inbound documents.
"""

import uuid

from django.conf import settings
from django.db import models


def _ap_document_upload_path(instance, filename):
    """Store AP documents under media/ap/<tenant_id>/<year>/<month>/."""
    return f"ap/{instance.tenant_id}/{instance.received_at.strftime('%Y/%m') if instance.received_at else 'pending'}/{filename}"


class APDocument(models.Model):
    """A raw inbound vendor-bill document awaiting AP specialist processing.

    Created by the document-ingestion view on manual upload or by the
    email-to-bill webhook when an incoming email carries an attachment.

    The ``chain_id`` is propagated to the ``BusEvent`` that is dispatched
    when the document is saved, and then threads through every SpecialistResult,
    the resulting ApprovalQueueItem, and the ERP operation — making the full
    causal trail traceable from upload to bill-posting via the audit-trail
    viewer (Step 50).

    Lifecycle::

        received  — document arrived; bus event dispatched
        processing — AP specialist pipeline is running (Step 53+)
        processed — pipeline complete; proposal created
        failed    — pipeline or extraction failed permanently
    """

    SOURCE_UPLOAD = 'upload'
    SOURCE_EMAIL  = 'email'
    SOURCE_CHOICES = [
        ('upload', 'Manual upload'),
        ('email',  'Email attachment'),
    ]

    STATUS_RECEIVED   = 'received'
    STATUS_PROCESSING = 'processing'
    STATUS_PROCESSED  = 'processed'
    STATUS_FAILED     = 'failed'
    STATUS_CHOICES = [
        ('received',   'Received'),
        ('processing', 'Processing'),
        ('processed',  'Processed'),
        ('failed',     'Failed'),
    ]

    # --- tenant / user --------------------------------------------------------

    tenant = models.ForeignKey(
        'accounting.Tenant',
        on_delete=models.CASCADE,
        related_name='ap_documents',
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
        help_text="The user who uploaded the document (null for webhook ingestion).",
    )

    # --- file -----------------------------------------------------------------

    file = models.FileField(
        upload_to='ap/%Y/%m/',
        help_text="Uploaded vendor-bill document (PDF, JPG, PNG, TIFF, WebP).",
    )
    original_filename = models.CharField(
        max_length=255,
        help_text="Original filename as supplied by the uploader / email client.",
    )
    content_type = models.CharField(
        max_length=100,
        help_text="MIME type reported by the client (e.g. application/pdf).",
    )
    file_size = models.PositiveIntegerField(
        help_text="File size in bytes.",
    )

    # --- routing / status -----------------------------------------------------

    source = models.CharField(
        max_length=16, choices=SOURCE_CHOICES, default=SOURCE_UPLOAD,
        help_text="How the document arrived.",
    )
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_RECEIVED,
        help_text="Processing lifecycle stage.",
    )

    # --- provenance threading (Step 44 / 50) ----------------------------------

    chain_id = models.CharField(
        max_length=64, blank=True,
        help_text="Cross-department chain ID — set when the bill.received BusEvent is dispatched.",
    )
    bus_event = models.OneToOneField(
        'accounting.BusEvent',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ap_document',
        help_text="The bill.received BusEvent dispatched for this document.",
    )

    # --- extra context --------------------------------------------------------

    notes = models.TextField(
        blank=True,
        help_text="Free-text notes — e.g. email subject + sender, or manual annotations.",
    )

    # --- timestamps -----------------------------------------------------------

    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-received_at']
        verbose_name = 'AP Inbound Document'
        verbose_name_plural = 'AP Inbound Documents'

    def __str__(self):
        return f"{self.original_filename} ({self.get_status_display()}) — {self.tenant}"

    @property
    def chain_id_short(self) -> str:
        """First 12 characters of chain_id for compact display."""
        return self.chain_id[:12] if self.chain_id else ''

    @property
    def file_size_kb(self) -> str:
        """Human-readable file size (KB, rounded)."""
        return f"{self.file_size // 1024:,} KB"
