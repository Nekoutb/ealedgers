"""Ingestion-layer models — Step 52.

The :class:`Document` is the inbound source artefact: a vendor bill, invoice,
or other accounting document that arrives as an uploaded PDF/image or via the
email-to-bill webhook. Once received it is *routed* to a department (D01 AP
for bills, etc.) using the same routing table the dispatcher uses, so it
"lands in the dept inbox" — i.e. ``Document.objects.inbox_for(tenant, 'D01')``.

Lifecycle::

    received  → routed     (a routing rule matched → routed_dept set)
              → unrouted    (no rule matched → needs manual triage)

Later steps enrich the same row:
  - Step 53 — APExtractor writes ``extracted_data`` (vendor, date, lines …)
  - Step 55 — the proposed JE is built from ``extracted_data``

See docs/EXECUTION_PLAN.md §2 (L2 Ingestion) and Phase P05.
"""

from __future__ import annotations

import hashlib

from django.db import models

from accounting.managers import TenantManager, TenantQuerySet
from accounting.models import DEPARTMENT_CHOICES, Tenant


# Document types we recognise on the way in. These mirror the ``doc_type``
# values the dispatcher routes on (agents/dispatcher.py) so a routed document
# always resolves to a department.
DOC_TYPE_CHOICES = [
    ('vendor_bill', 'Vendor bill'),
    ('vendor_invoice', 'Vendor invoice'),
    ('purchase_order', 'Purchase order'),
    ('customer_invoice', 'Customer invoice'),
    ('credit_note', 'Credit note'),
    ('payment', 'Payment advice'),
    ('bank_statement', 'Bank statement'),
    ('asset_acquisition', 'Asset acquisition'),
    ('journal_entry', 'Journal entry'),
    ('payroll', 'Payroll pack'),
    ('other', 'Other / unclassified'),
]

SOURCE_CHOICES = [
    ('upload', 'Manual upload'),
    ('email', 'Email-to-bill'),
    ('api', 'API / connector'),
]

STATUS_CHOICES = [
    ('received', 'Received — awaiting routing'),
    ('routed', 'Routed to department inbox'),
    ('unrouted', 'Unrouted — needs manual triage'),
]


class DocumentQuerySet(TenantQuerySet):
    """Adds an ``inbox_for`` filter on top of the tenant-scoped queryset."""

    def inbox_for(self, tenant, dept_code: str):
        """Documents routed to ``dept_code``'s inbox for ``tenant``.

        Returns the *open* inbox: routed documents that have landed in the
        department but not yet been pulled into a downstream proposal. Steps
        53+ will advance the status further; until then ``routed`` is the
        terminal inbox state.
        """
        return self.for_tenant(tenant).filter(
            routed_dept=dept_code, status='routed',
        )


DocumentManager = TenantManager.from_queryset(DocumentQuerySet)


class Document(models.Model):
    """An inbound source document (PDF / image) plus its routing state."""

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='documents',
    )

    source = models.CharField(
        max_length=12, choices=SOURCE_CHOICES, default='upload', db_index=True,
        help_text='How the document entered the system.',
    )
    doc_type = models.CharField(
        max_length=24, choices=DOC_TYPE_CHOICES, default='vendor_bill',
        help_text='Drives departmental routing (vendor_bill → D01 AP, etc.).',
    )

    file = models.FileField(
        upload_to='documents/%Y/%m/', blank=True, null=True,
        help_text='The uploaded / emailed PDF or image.',
    )
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text='Content hash — used to detect duplicate uploads.',
    )

    # Email-to-bill provenance (blank for uploads / API).
    sender_email = models.EmailField(blank=True)
    subject = models.CharField(max_length=512, blank=True)

    # Optional pre-extracted text (e.g. an email body, or a text-layer PDF).
    # When present, APExtractor (Step 53) can skip OCR.
    raw_text = models.TextField(blank=True)

    # Routing + lifecycle.
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default='received', db_index=True,
    )
    routed_dept = models.CharField(
        max_length=12, choices=DEPARTMENT_CHOICES, blank=True, db_index=True,
        help_text='Department this document was routed to (blank if unrouted).',
    )
    chain_id = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text='UUID linking this document to its causal event chain (Step 44).',
    )

    # Structured fields written by APExtractor in Step 53.
    extracted_data = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    routed_at = models.DateTimeField(null=True, blank=True)

    objects = DocumentManager()

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tenant', 'routed_dept', 'status']),
            models.Index(fields=['tenant', 'status', '-created_at']),
            models.Index(fields=['chain_id']),
        ]

    def __str__(self):
        name = self.original_filename or self.subject or f'document #{self.pk}'
        return f'[{self.get_doc_type_display()}] {name}'

    # ----- helpers -----------------------------------------------------------

    @staticmethod
    def compute_sha256(file_obj) -> str:
        """Return the SHA-256 hex digest of an open file-like object.

        Reads in chunks and restores the file pointer so the caller can still
        save the file afterwards.
        """
        pos = file_obj.tell() if hasattr(file_obj, 'tell') else None
        hasher = hashlib.sha256()
        for chunk in iter(lambda: file_obj.read(65536), b''):
            hasher.update(chunk if isinstance(chunk, bytes) else chunk.encode())
        if pos is not None:
            file_obj.seek(pos)
        return hasher.hexdigest()

    @property
    def is_routed(self) -> bool:
        return self.status == 'routed' and bool(self.routed_dept)

    def event_payload(self) -> dict:
        """The payload emitted on the ``document.received`` bus event.

        Carries everything a department needs to begin processing without
        re-reading the DB: the document id, its type (for routing), and its
        source provenance.
        """
        return {
            'document_id': self.pk,
            'doc_type': self.doc_type,
            'source': self.source,
            'original_filename': self.original_filename,
            'sender_email': self.sender_email,
            'subject': self.subject,
        }
