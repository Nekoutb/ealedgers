"""Ingestion views — Step 52.

Two inbound paths for source documents:

  ``document_upload`` — a login-gated, tenant-scoped page where a user uploads
  a PDF/image. On success the document is routed and lands in a dept inbox.

  ``email_webhook`` — a CSRF-exempt POST endpoint for the email-to-bill flow.
  The tenant is identified from the inbound recipient address and (optionally)
  guarded by a shared secret.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounting.middleware import tenant_required
from accounting.models import Tenant
from ingest.forms import DocumentUploadForm
from ingest.ingestion import IngestError, receive_document, receive_email
from ingest.models import Document


@login_required
@tenant_required
def document_upload(request):
    """Upload a source document and show the current departmental inbox."""
    if request.method == 'POST':
        form = DocumentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                doc = receive_document(
                    request.tenant,
                    file=form.cleaned_data['file'],
                    doc_type=form.cleaned_data['doc_type'],
                    source='upload',
                )
            except IngestError as exc:
                form.add_error('file', str(exc))
            else:
                if doc.is_routed:
                    messages.success(
                        request,
                        f'“{doc.original_filename}” received and routed to '
                        f'{doc.get_routed_dept_display()}.',
                    )
                else:
                    messages.warning(
                        request,
                        f'“{doc.original_filename}” received but could not be '
                        'auto-routed — it needs manual triage.',
                    )
                return redirect(reverse('ingest:document_upload'))
    else:
        form = DocumentUploadForm()

    recent = list(Document.objects.for_tenant(request.tenant)[:20])

    return render(request, 'ingest/document_upload.html', {
        'form': form,
        'recent': recent,
        'page_name': 'Document ingestion',
    })


@csrf_exempt
@require_POST
def email_webhook(request):
    """Email-to-bill inbound webhook (Mailgun / SendGrid style POST).

    Resolves the tenant from the recipient address, validates the shared
    secret (when configured), and ingests each attachment as a vendor bill.
    Returns JSON describing how many documents were created.
    """
    if not _check_webhook_token(request):
        return JsonResponse({'error': 'invalid or missing token'}, status=403)

    tenant = _resolve_tenant(request)
    if tenant is None:
        return JsonResponse(
            {'error': 'could not resolve tenant from recipient'}, status=422,
        )

    documents = receive_email(tenant, request.POST, request.FILES)

    return JsonResponse({
        'status': 'ok',
        'tenant': tenant.slug,
        'created': len(documents),
        'documents': [
            {'id': d.pk, 'doc_type': d.doc_type, 'routed_dept': d.routed_dept}
            for d in documents
        ],
    })


# ---------------------------------------------------------------------------
# Webhook helpers
# ---------------------------------------------------------------------------

def _check_webhook_token(request) -> bool:
    """True when no token is configured, or the request presents the right one."""
    expected = getattr(settings, 'INGEST_WEBHOOK_TOKEN', '')
    if not expected:
        return True   # dev / unguarded
    presented = (
        request.headers.get('X-Ingest-Token')
        or request.POST.get('token')
        or ''
    )
    return presented == expected


def _resolve_tenant(request):
    """Identify the tenant from the inbound recipient address (or ?tenant=slug).

    The email-to-bill address convention is ``<slug>@<anything>`` — the local
    part of the recipient is the tenant slug. We also honour an explicit
    ``tenant`` field/query param for direct API testing.
    """
    slug = request.POST.get('tenant') or request.GET.get('tenant') or ''

    if not slug:
        recipient = (
            request.POST.get('recipient')
            or request.POST.get('to')
            or ''
        )
        slug = _slug_from_address(recipient)

    if not slug:
        return None
    return Tenant.objects.filter(slug=slug).first()


def _slug_from_address(address: str) -> str:
    """Extract the local part of the first email address in ``address``.

    ``bills+acme@inbound.ealedgers.com`` → ``acme`` (sub-addressing) or
    ``acme@inbound.ealedgers.com`` → ``acme``.
    """
    address = (address or '').strip()
    if not address:
        return ''
    # Take the first address if a comma-separated list was supplied.
    address = address.split(',')[0].strip()
    # Strip an optional display name: "Name <local@host>".
    if '<' in address and '>' in address:
        address = address[address.find('<') + 1:address.find('>')]
    if '@' not in address:
        return ''
    local = address.split('@', 1)[0]
    # Sub-addressing: bills+acme → acme; otherwise the local part itself.
    if '+' in local:
        return local.split('+', 1)[1]
    return local
