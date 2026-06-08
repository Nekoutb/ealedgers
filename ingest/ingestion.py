"""Ingestion service — Step 52.

The single entry point through which inbound source documents become routed
:class:`~ingest.models.Document` rows. Both the manual-upload view and the
email-to-bill webhook call into here, so routing + event emission live in one
place.

What ``receive_document`` does, in order:

1. Hash + persist the file (or raw text) as a ``Document`` (status=received).
2. Resolve its target department with the *same* routing table the dispatcher
   uses (:class:`agents.dispatcher.Dispatcher`), and stamp ``routed_dept`` /
   ``status`` directly on the row — so the document immediately "lands in the
   dept inbox" (``Document.objects.inbox_for(tenant, 'D01')``).
3. Emit a durable ``document.received`` bus event. The registered dispatcher
   (agents.dispatcher.register_dispatcher) re-resolves it and emits the
   ``{dept}.work.queued`` event, threading the same ``chain_id`` so the full
   causal chain is auditable (Steps 44 + 50).

Steps 2 and 3 share the one routing table, so the synchronous stamp on the
row and the asynchronous bus chain can never disagree.
"""

from __future__ import annotations

import uuid

from django.core.files.uploadedfile import UploadedFile
from django.utils import timezone

from ingest.models import Document


# File types we accept as a source document. Anything else is rejected at the
# edge (the upload form / webhook) before a Document row is created.
ALLOWED_CONTENT_TYPES = {
    'application/pdf',
    'image/jpeg',
    'image/png',
    'image/tiff',
    'image/heic',
    'image/webp',
}

ALLOWED_EXTENSIONS = {
    '.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.heic', '.webp',
}


class IngestError(Exception):
    """Raised when an inbound document fails validation (bad type, too big)."""


def _extension(filename: str) -> str:
    if '.' not in filename:
        return ''
    return '.' + filename.rsplit('.', 1)[-1].lower()


def receive_document(
    tenant,
    *,
    file: UploadedFile | None = None,
    doc_type: str = 'vendor_bill',
    source: str = 'upload',
    sender_email: str = '',
    subject: str = '',
    raw_text: str = '',
    original_filename: str = '',
    chain_id: str = '',
    metadata: dict | None = None,
    emit_event: bool = True,
) -> Document:
    """Create, route, and announce one inbound document.

    Either ``file`` or ``raw_text`` must be provided (an email body alone is a
    valid bill in the email-to-bill flow). Returns the saved, routed Document.
    """
    if file is None and not raw_text.strip():
        raise IngestError('A document needs either a file or raw text.')

    chain_id = chain_id or str(uuid.uuid4())

    doc = Document(
        tenant=tenant,
        source=source,
        doc_type=doc_type,
        sender_email=sender_email,
        subject=subject,
        raw_text=raw_text,
        chain_id=chain_id,
        metadata=metadata or {},
        status='received',
    )

    if file is not None:
        doc.original_filename = original_filename or getattr(file, 'name', '')
        doc.content_type = getattr(file, 'content_type', '') or ''
        doc.size_bytes = getattr(file, 'size', 0) or 0
        doc.sha256 = Document.compute_sha256(file)
        doc.file = file
    else:
        doc.original_filename = original_filename

    doc.save()

    _route(doc)

    if emit_event:
        _announce(doc)

    return doc


def receive_email(tenant, payload, files=None, *, chain_id: str = '') -> list[Document]:
    """Turn an inbound email-to-bill webhook into one or more Documents.

    ``payload`` is the POST data (Mailgun / SendGrid style); ``files`` is the
    uploaded-file mapping (``request.FILES``). Each attachment whose type is
    allowed becomes a ``vendor_bill`` Document. If the email carries no usable
    attachment, the plain-text body is ingested as a single text-only bill so
    nothing is silently dropped.

    Every document produced shares one ``chain_id`` (the email is one causal
    event) so a multi-attachment email traces as a single chain.
    """
    files = files or {}
    chain_id = chain_id or str(uuid.uuid4())

    sender = payload.get('sender') or payload.get('from') or ''
    subject = payload.get('subject') or ''
    body = payload.get('body-plain') or payload.get('text') or ''

    created: list[Document] = []

    for upload in _iter_email_attachments(files):
        if not _is_allowed(upload):
            continue
        created.append(receive_document(
            tenant,
            file=upload,
            doc_type='vendor_bill',
            source='email',
            sender_email=sender[:254],
            subject=subject[:512],
            raw_text=body,
            chain_id=chain_id,
        ))

    # No usable attachment → ingest the body so the bill still lands.
    if not created and body.strip():
        created.append(receive_document(
            tenant,
            doc_type='vendor_bill',
            source='email',
            sender_email=sender[:254],
            subject=subject[:512],
            raw_text=body,
            chain_id=chain_id,
        ))

    return created


# ---------------------------------------------------------------------------
# Validation helpers (used by the views before calling receive_*)
# ---------------------------------------------------------------------------

def validate_upload(file: UploadedFile, *, max_bytes: int | None = None) -> None:
    """Raise :class:`IngestError` if ``file`` is the wrong type or too large."""
    if not _is_allowed(file):
        raise IngestError(
            f'Unsupported file type: {getattr(file, "name", "?")!r}. '
            'Upload a PDF or image (JPG, PNG, TIFF).'
        )
    if max_bytes is None:
        from django.conf import settings
        max_bytes = getattr(settings, 'INGEST_MAX_UPLOAD_BYTES', 25 * 1024 * 1024)
    if (getattr(file, 'size', 0) or 0) > max_bytes:
        raise IngestError(
            f'File too large ({file.size} bytes). '
            f'Maximum is {max_bytes} bytes.'
        )


def _is_allowed(file) -> bool:
    name = getattr(file, 'name', '') or ''
    ctype = (getattr(file, 'content_type', '') or '').lower()
    if ctype and ctype in ALLOWED_CONTENT_TYPES:
        return True
    return _extension(name) in ALLOWED_EXTENSIONS


def _iter_email_attachments(files):
    """Yield uploaded files from a webhook FILES mapping.

    Handles both a plain dict and Django's ``MultiValueDict`` (which can hold
    several files under keys like ``attachment-1``, ``attachment-2``).
    """
    if hasattr(files, 'lists'):       # MultiValueDict
        for _key, uploads in files.lists():
            for upload in uploads:
                yield upload
    else:
        for upload in files.values():
            yield upload


# ---------------------------------------------------------------------------
# Routing + announcement
# ---------------------------------------------------------------------------

def _route(doc: Document) -> None:
    """Stamp ``routed_dept`` / ``status`` using the dispatcher's routing table."""
    from agents.dispatcher import Dispatcher

    target_dept, _routed_event = Dispatcher(doc.tenant).resolve(
        'document.received', doc.event_payload(),
    )

    if target_dept:
        doc.routed_dept = target_dept
        doc.status = 'routed'
    else:
        doc.routed_dept = ''
        doc.status = 'unrouted'
    doc.routed_at = timezone.now()
    doc.save(update_fields=['routed_dept', 'status', 'routed_at'])


def _announce(doc: Document) -> None:
    """Emit the durable ``document.received`` bus event for this document."""
    from agents.events import Event, emit

    emit(Event(
        event_type='document.received',
        tenant=doc.tenant,
        payload=doc.event_payload(),
        chain_id=doc.chain_id,
    ))
