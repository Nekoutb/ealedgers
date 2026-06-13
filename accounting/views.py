"""Views for the EA Ledgers platform shell.

Post-pivot, this app holds the multi-tenant SHELL — login → workspace home,
self-serve sign-up, tenant switching — plus read-only platform pages that
present the virtual-finance-function vision: Departments, Agent Activity,
ERP Connections.

The accounting DATA layer (chart of accounts, journals, entries, periods,
invoices/bills/bank) still lives in ``models.py`` and is reached through the
Django admin (the "Ledger"); the agents will drive it from Phase P05. The v1
manual data-entry UI (dashboard, invoicing, bills, bank rec, report pages)
was retired in the post-pivot cleanup — those flows belong to the department
agents + the ERP, not hand-keyed screens.
"""

import uuid

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Min
from django.http import HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from connectors.capabilities import CAPABILITIES

from .forms import ERPConnectionForm, SignupForm
from .middleware import SESSION_TENANT_KEY, switch_tenant, tenant_required
from .models import (
    AgentRun,
    ApprovalQueueItem,
    BusEvent,
    ERPConnection,
    ERPOperation,
    Membership,
    SUBSCRIBABLE_DEPARTMENTS,
    TenantDepartmentSubscription,
)


# ---------------------------------------------------------------------------
# Tile icons for the workspace home
# ---------------------------------------------------------------------------

ICON_KNOWLEDGE = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#0d9488"/>
  <path d="M7.5 9 H15 a1.6 1.6 0 0 1 1.6 1.6 V24 a2 2 0 0 0-2-2 H7.5 Z" fill="white"/>
  <path d="M24.5 9 H17 a1.6 1.6 0 0 0-1.6 1.6 V24 a2 2 0 0 1 2-2 H24.5 Z" fill="white" opacity="0.82"/>
</svg>
""".strip()

ICON_DEPARTMENTS = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#C2526D"/>
  <g fill="white">
    <circle cx="12.5" cy="12.5" r="3.4"/>
    <path d="M6.2 25 Q6.2 17.6 12.5 17.6 Q18.8 17.6 18.8 25 Z"/>
    <circle cx="22" cy="14" r="2.8"/>
    <path d="M16.8 25 Q16.8 19.6 22 19.6 Q27.2 19.6 27.2 25 Z" opacity="0.78"/>
  </g>
</svg>
""".strip()

ICON_AGENTS = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#4f46e5"/>
  <path d="M16 6.5 l2.4 5.6 6.1 0.5 -4.6 4 1.4 6 -5.3-3.3 -5.3 3.3 1.4-6 -4.6-4 6.1-0.5 Z" fill="white"/>
</svg>
""".strip()

ICON_ERP = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#2E8B8E"/>
  <g stroke="white" stroke-width="1.7" stroke-linecap="round">
    <line x1="11" y1="10.5" x2="21" y2="16"/>
    <line x1="21" y1="16" x2="11" y2="21.5"/>
  </g>
  <g fill="white">
    <circle cx="10.5" cy="10.5" r="3"/>
    <circle cx="21.5" cy="16" r="3"/>
    <circle cx="10.5" cy="21.5" r="3"/>
  </g>
</svg>
""".strip()

ICON_LEDGER = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#0B6E4F"/>
  <g fill="white">
    <rect x="6" y="9.5" width="20" height="2.6" rx="0.4"/>
    <rect x="6" y="14.7" width="12" height="2.6" rx="0.4"/>
    <rect x="6" y="19.9" width="20" height="2.6" rx="0.4"/>
  </g>
</svg>
""".strip()


# The workspace home tiles. status: "live" (shipped) | "preview" (skeleton —
# the data layer exists; the agent that fills it ships in a later phase).
MODULES = [
    {"name": "Knowledge Base", "tagline": "Sourced rules the agents reason over",
     "url": "/knowledge/explorer/", "status": "live", "icon": ICON_KNOWLEDGE},
    {"name": "Departments", "tagline": "Your virtual finance team",
     "url": "/departments/", "status": "preview", "icon": ICON_DEPARTMENTS},
    {"name": "Agent Activity", "tagline": "What the agents proposed and did",
     "url": "/agents/", "status": "preview", "icon": ICON_AGENTS},
    {"name": "ERP Connections", "tagline": "Link your accounting system",
     "url": "/erp/", "status": "preview", "icon": ICON_ERP},
    {"name": "Ledger", "tagline": "Chart of accounts, journals, entries",
     "url": "/admin/accounting/", "status": "live", "icon": ICON_LEDGER},
]


# Department roster metadata for the org-chart skeleton.
# Tuple: (scope description, shipping phase, inbox URL or None)
# The inbox URL is set for departments that have a live user-facing inbox
# page; None for departments whose agent hasn't shipped yet.
_DEPT_META = {
    "D00": ("Cross-department orchestration, month-end close", "P11", None),
    "D01": ("Vendor bills, payments, withholding tax",         "P05", "/ap/inbox/"),
    "D02": ("Customer invoices, receipts, dunning",            "P07", None),
    "D03": ("Bank, cash, FX, payment execution",               "P08", None),
    "D04": ("Capex, componentisation, depreciation, disposals","P09", None),
    "D05": ("Manual journal entries, accruals, reconciliations","P10", None),
    "D06": ("TVA, WHT, IS, IRPP coordination, DGI filings",   "P06", None),
    "D07": ("Payroll journal, CNPS, IRPP, certificates",       "P16", None),
    "D08": ("Item master, receipts, issues, costing",          "P17", None),
    "D09": ("Statutory pack, DGI filings, management reports", "P12", None),
    "D10": ("Class 9, cost centres, project costing",          "P13", None),
    "D11": ("Budget, forecast, variance, scenarios",           "P14", None),
}


# ---------------------------------------------------------------------------
# Sign-up + tenant switching
# ---------------------------------------------------------------------------

def signup(request):
    """Self-serve sign-up. Creates User + Tenant + Membership atomically
    and logs the new user in. Authenticated users get bounced to /workspace/."""
    if request.user.is_authenticated:
        return redirect("workspace")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user,
                  backend="django.contrib.auth.backends.ModelBackend")
            new_tenant = getattr(user, "_signup_tenant", None)
            if new_tenant is not None:
                request.session[SESSION_TENANT_KEY] = new_tenant.slug
            return redirect("workspace")
    else:
        form = SignupForm()

    return render(request, "accounting/signup.html", {"form": form})


@login_required
@require_POST
def switch_tenant_view(request, slug):
    """POST-only endpoint that swaps the active tenant. Bounces back to the
    referrer (or /workspace/) on success; silently home on failure."""
    target = switch_tenant(request, slug)
    if target is None:
        return redirect("workspace")
    referer = request.META.get("HTTP_REFERER", "")
    if referer.startswith(request.build_absolute_uri("/")):
        return HttpResponseRedirect(referer)
    return redirect("workspace")


# ---------------------------------------------------------------------------
# Workspace home + platform pages
# ---------------------------------------------------------------------------

@login_required
def workspace(request):
    """The home launcher — visible to any authenticated user; works even
    without a tenant (a user with no memberships still sees it).

    Passes ``dept_tiles`` — a list of dicts with live state per department —
    to the template so the org-chart section shows real-time subscription,
    gate, and queue data without N+1 queries.
    """
    tenant = request.tenant
    tenant_name = tenant.name if tenant else "EA Ledgers"
    agent_enabled = bool(getattr(tenant, 'agent_enabled', False)) if tenant else False

    dept_tiles = []
    n_subscribed = 0
    n_live = 0
    n_pending = 0

    if tenant:
        # One query: every subscription row for this tenant (active or not).
        subs_by_dept = {
            s.department: s
            for s in TenantDepartmentSubscription.objects.filter(tenant=tenant)
        }

        # One query: pending ApprovalQueueItem counts grouped by dept.
        queue_counts = dict(
            ApprovalQueueItem.objects
            .for_tenant(tenant)
            .filter(status='pending')
            .values('dept_code')
            .annotate(n=Count('id'))
            .values_list('dept_code', 'n')
        )

        for code, label in SUBSCRIBABLE_DEPARTMENTS:
            sub = subs_by_dept.get(code)
            subscribed = sub is not None and bool(sub.active)
            agent_live = agent_enabled and subscribed
            scope, phase, inbox_url = _DEPT_META.get(code, ('', '', None))
            q_count = queue_counts.get(code, 0)
            dept_tiles.append({
                'code': code,
                'name': label,
                'scope': scope,
                'phase': phase,
                'subscribed': subscribed,
                'agent_live': agent_live,
                'queue_count': q_count,
                'url': inbox_url,  # None for departments without a live inbox yet
            })
            if subscribed:
                n_subscribed += 1
            if agent_live:
                n_live += 1
            n_pending += q_count

    return render(request, "accounting/workspace.html", {
        "modules": MODULES,
        "tenant_name": tenant_name,
        "agent_enabled": agent_enabled,
        "dept_tiles": dept_tiles,
        "n_subscribed": n_subscribed,
        "n_live": n_live,
        "n_pending": n_pending,
    })


@login_required
@tenant_required
def approver_assignment(request):
    """Per-department approver assignment — Step 49.

    Lists every active department subscription for the tenant and lets the
    user assign a specific member as the default reviewer for each queue.
    Leaving a row blank resets it to «use the tenant owner».

    Only users who are active members of the tenant may be assigned.
    This prevents cross-tenant privilege escalation.
    """
    User = get_user_model()
    tenant = request.tenant

    # Eligible approvers: active members of this tenant.
    member_users = list(
        User.objects.filter(
            memberships__tenant=tenant,
            memberships__active=True,
        ).distinct().order_by('username')
    )
    member_ids = {u.pk for u in member_users}

    saved = False
    save_errors = []

    if request.method == 'POST':
        subs_to_update = TenantDepartmentSubscription.objects.filter(
            tenant=tenant, active=True,
        )
        for sub in subs_to_update:
            raw = request.POST.get(f'approver_{sub.department}', '').strip()
            if not raw:
                sub.default_approver = None
                sub.save(update_fields=['default_approver'])
            else:
                try:
                    uid = int(raw)
                    if uid in member_ids:
                        sub.default_approver_id = uid
                        sub.save(update_fields=['default_approver'])
                    else:
                        save_errors.append(
                            f'{sub.department}: selected user is not a member of this tenant.'
                        )
                except (ValueError, OverflowError):
                    save_errors.append(f'{sub.department}: invalid user value.')
        if not save_errors:
            saved = True

    # Fetch (or re-fetch after save) subscriptions with approver joined.
    subscriptions = (
        TenantDepartmentSubscription.objects
        .filter(tenant=tenant, active=True)
        .select_related('default_approver')
        .order_by('department')
    )

    dept_labels = dict(SUBSCRIBABLE_DEPARTMENTS)
    owner = getattr(tenant, 'owner', None)

    rows = [
        {
            'sub': sub,
            'dept_code': sub.department,
            'dept_name': dept_labels.get(sub.department, sub.department),
            'dept_scope': _DEPT_META.get(sub.department, ('', ''))[0],
            'current_approver': sub.default_approver,
        }
        for sub in subscriptions
    ]

    n_assigned = sum(1 for r in rows if r['current_approver'] is not None)

    return render(request, 'accounting/approver_assignment.html', {
        'rows': rows,
        'member_users': member_users,
        'tenant_name': tenant.name,
        'owner': owner,
        'saved': saved,
        'save_errors': save_errors,
        'n_assigned': n_assigned,
        'n_subscribed': len(rows),
    })


@login_required
@tenant_required
def chain_list(request):
    """Audit-trail viewer — Step 50 list view.

    Shows the 50 most recent causal chains (identified by chain_id) that
    have at least one BusEvent.  Each row is a summary: how many bus events
    belong to this chain, when it started, when the last event fired.
    """
    chains = (
        BusEvent.objects
        .for_tenant(request.tenant)
        .filter(chain_id__gt='')      # exclude events with no chain
        .values('chain_id')
        .annotate(
            n_events=Count('id'),
            started_at=Min('created_at'),
            last_activity=Max('created_at'),
        )
        .order_by('-started_at')[:50]
    )
    return render(request, 'accounting/chain_list.html', {
        'chains': list(chains),
        'tenant_name': request.tenant.name,
    })


@login_required
@tenant_required
def chain_detail(request, chain_id):
    """Audit-trail viewer — Step 50 detail view.

    Shows every artefact that shares ``chain_id`` across all four sources
    (BusEvent, ApprovalQueueItem, AgentRun, Provenance) in a chronological
    vertical timeline.  Multi-tenant safe: ChainTracer scopes every query
    to the active tenant.
    """
    from agents.chain import trace_chain
    tracer = trace_chain(chain_id, request.tenant)
    timeline = tracer.timeline()

    # Admin deep-link bases per source type
    _ADMIN = {
        'event':      '/admin/accounting/busevent/',
        'proposal':   '/admin/accounting/approvalqueueitem/',
        'agent_run':  '/admin/accounting/agentrun/',
        'provenance': '/admin/accounting/provenance/',
    }

    entries = [
        {
            'entry': e,
            'admin_url': f'{_ADMIN.get(e.source, "")}{e.object_id}/change/'
                         if _ADMIN.get(e.source) else '',
        }
        for e in timeline
    ]

    return render(request, 'accounting/chain_detail.html', {
        'chain_id':       chain_id,
        'chain_id_short': chain_id[:12],
        'entries':        entries,
        'dept_codes':     sorted(tracer.dept_codes()),
        'n_entries':      len(entries),
        'not_found':      len(entries) == 0,
        'tenant_name':    request.tenant.name,
    })


@login_required
@tenant_required
def departments(request):
    """Org chart of the virtual finance function — the 12 departments, each
    tenant's subscription, and the phase its agent ships in (skeleton)."""
    subscribed = set(request.tenant.subscribed_departments())
    rows = []
    for code, label in SUBSCRIBABLE_DEPARTMENTS:
        scope, phase, _url = _DEPT_META.get(code, ("", "", None))
        rows.append({
            "code": code,
            "label": label,
            "scope": scope,
            "phase": phase,
            "subscribed": code in subscribed,
        })
    return render(request, "accounting/departments.html", {
        "page_name": "Departments",
        "departments": rows,
        "n_subscribed": len(subscribed),
        "n_total": len(rows),
    })


@login_required
@tenant_required
def agent_activity(request):
    """Recent agent runs for the tenant (skeleton — empty until the agents
    ship in Phase P05+)."""
    runs = list(
        AgentRun.objects.filter(tenant=request.tenant)
        .order_by("-id")[:50]
    )
    return render(request, "accounting/agent_activity.html", {
        "page_name": "Agent Activity",
        "runs": runs,
    })


@login_required
@tenant_required
def erp_connections(request):
    """The tenant's ERP connections + their health."""
    connections = list(
        ERPConnection.objects.filter(tenant=request.tenant)
        .order_by("-is_primary", "name")
    )
    return render(request, "accounting/erp_connections.html", {
        "page_name": "ERP Connections",
        "connections": connections,
    })


@login_required
@tenant_required
def capability_matrix(request):
    """Per-tenant capability matrix — every CAP.NN × each ERP connection, with
    health. Shows what the agents can actually do through each ERP today
    (driven by the capabilities each connection reported at its last
    health-check). Read-only."""
    connections = list(
        ERPConnection.objects.filter(tenant=request.tenant)
        .order_by("-is_primary", "name")
    )
    conn_caps = [set(c.capabilities or []) for c in connections]
    conn_rows = [
        {"conn": c, "n_caps": len(caps)}
        for c, caps in zip(connections, conn_caps)
    ]
    rows = [
        {
            "cap": cap,
            "supported": [cap.code in caps for caps in conn_caps],
            "any": any(cap.code in caps for caps in conn_caps),
        }
        for cap in CAPABILITIES.values()
    ]
    return render(request, "accounting/capability_matrix.html", {
        "page_name": "ERP Connections",
        "conn_rows": conn_rows,
        "rows": rows,
        "total_caps": len(CAPABILITIES),
    })


def _test_connection(connection):
    """Health-check the connection and persist the result — the
    "connection is tested on save" behaviour. Never raises."""
    from connectors.base import ConnectorError
    from connectors.registry import build_connector
    try:
        health = build_connector(connection).health_check()
        connection.health = health.state
        connection.capabilities = list(health.capabilities or [])
        connection.last_healthcheck_error = "" if health.ok else health.detail
    except ConnectorError as exc:
        connection.health = "down"
        connection.last_healthcheck_error = str(exc)
    except Exception as exc:  # noqa: BLE001 — report, never 500 the form
        connection.health = "down"
        connection.last_healthcheck_error = f"{type(exc).__name__}: {exc}"
    connection.last_healthcheck_at = timezone.now()
    connection.save(update_fields=[
        "health", "capabilities", "last_healthcheck_error",
        "last_healthcheck_at"])
    return connection


@login_required
@tenant_required
def erp_connection_create(request):
    """Create an ERP connection and test it on save."""
    if request.method == "POST":
        form = ERPConnectionForm(request.POST)
        if form.is_valid():
            conn = ERPConnection(tenant=request.tenant)
            form.apply_to(conn)
            key = form.cleaned_data.get("api_key")
            if key:
                conn.set_api_key(key)
            conn.save()
            _test_connection(conn)
            return redirect(reverse("erp_connections"))
    else:
        form = ERPConnectionForm()
    return render(request, "accounting/erp_connection_form.html", {
        "page_name": "ERP Connections", "form": form, "mode": "create"})


@login_required
@tenant_required
def erp_connection_edit(request, pk):
    """Edit a connection. A blank API key keeps the stored (encrypted) one.
    Re-tests on save."""
    conn = get_object_or_404(
        ERPConnection.objects.for_tenant(request.tenant), pk=pk)
    if request.method == "POST":
        form = ERPConnectionForm(request.POST)
        if form.is_valid():
            form.apply_to(conn)
            key = form.cleaned_data.get("api_key")
            if key:  # blank → keep the existing key
                conn.set_api_key(key)
            conn.save()
            _test_connection(conn)
            return redirect(reverse("erp_connections"))
    else:
        form = ERPConnectionForm(initial=ERPConnectionForm.initial_from(conn))
    return render(request, "accounting/erp_connection_form.html", {
        "page_name": "ERP Connections", "form": form, "mode": "edit",
        "connection": conn})


# ---------------------------------------------------------------------------
# Step 39 — ERP-operation audit log
# ---------------------------------------------------------------------------

@login_required
@tenant_required
def erp_operation_audit(request):
    """Full ERP-operation history for the current tenant — filterable,
    paginated. Read-only audit trail of every capability invocation."""
    from django.core.paginator import Paginator

    qs = (ERPOperation.objects.for_tenant(request.tenant)
          .select_related("connection")
          .order_by("-started_at"))

    # --- filters ---
    status_filter = request.GET.get("status", "")
    cap_filter = request.GET.get("cap", "")
    conn_filter = request.GET.get("conn", "")

    if status_filter:
        qs = qs.filter(status=status_filter)
    if cap_filter:
        qs = qs.filter(capability=cap_filter)
    if conn_filter:
        try:
            qs = qs.filter(connection_id=int(conn_filter))
        except (ValueError, TypeError):
            pass

    # --- filter option lists ---
    connections = ERPConnection.objects.for_tenant(request.tenant)
    cap_choices = sorted(
        ERPOperation.objects.for_tenant(request.tenant)
        .values_list("capability", flat=True)
        .distinct()
    )

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get("page", 1))

    # build query string for pagination links (preserve active filters)
    filter_qs = "&".join(
        f"{k}={v}"
        for k, v in [("status", status_filter), ("cap", cap_filter), ("conn", conn_filter)]
        if v
    )

    return render(request, "accounting/erp_operation_audit.html", {
        "page_name": "ERP Connections",
        "page": page,
        "total": paginator.count,
        "status_filter": status_filter,
        "cap_filter": cap_filter,
        "conn_filter": conn_filter,
        "connections": connections,
        "status_choices": ERPOperation.STATUSES,
        "cap_choices": cap_choices,
        "filter_qs": filter_qs,
    })


# ---------------------------------------------------------------------------
# Step 52 — AP document ingestion (upload UI + email webhook)
# ---------------------------------------------------------------------------

_AP_ALLOWED_TYPES = [
    'application/pdf', 'image/jpeg', 'image/png', 'image/tiff', 'image/webp',
]


def _ap_max_bytes():
    """Return the current AP upload size cap in bytes (reads settings at request time)."""
    return getattr(settings, 'AP_DOCUMENT_MAX_SIZE_MB', 20) * 1024 * 1024


def _dispatch_bill_received(tenant, doc):
    """Create a BusEvent(bill.received) and link it to ``doc``."""
    chain_id = str(uuid.uuid4())
    event = BusEvent.objects.create(
        tenant=tenant,
        event_type='bill.received',
        chain_id=chain_id,
        status='dispatched',
        payload={
            'dept_code': 'D01',
            'ap_document_id': doc.pk,
            'original_filename': doc.original_filename,
            'file_path': doc.file.name,
            'source': doc.source,
        },
    )
    doc.chain_id = chain_id
    doc.bus_event = event
    doc.save(update_fields=['chain_id', 'bus_event'])
    return event


@login_required
@tenant_required
def ap_inbox(request):
    """AP dept inbox — list inbound documents and handle bill uploads (Step 52).

    GET  — renders the inbox list plus an upload form.
    POST — validates and saves an uploaded vendor-bill document, then
           dispatches a ``bill.received`` BusEvent so the AP department
           can process it (Steps 53+).

    Validation:
        - File must be present and non-empty.
        - Content-type must be in AP_DOCUMENT_ALLOWED_TYPES.
        - File size must not exceed AP_DOCUMENT_MAX_SIZE_MB.
    """
    from agents.models import APDocument

    tenant = request.tenant
    save_errors = []
    saved_doc = None

    if request.method == 'POST':
        uploaded = request.FILES.get('bill_file')
        notes = request.POST.get('notes', '').strip()

        if not uploaded:
            save_errors.append("No file was selected. Please choose a PDF or image.")
        else:
            ct = uploaded.content_type or ''
            allowed = getattr(settings, 'AP_DOCUMENT_ALLOWED_TYPES', _AP_ALLOWED_TYPES)
            max_bytes = _ap_max_bytes()
            limit_mb = getattr(settings, 'AP_DOCUMENT_MAX_SIZE_MB', 20)
            if ct not in allowed:
                save_errors.append(
                    f"File type '{ct}' is not accepted. "
                    "Please upload a PDF, JPEG, PNG, TIFF, or WebP."
                )
            elif uploaded.size > max_bytes:
                save_errors.append(
                    f"File is too large ({uploaded.size // 1024:,} KB). "
                    f"Maximum allowed size is {limit_mb} MB."
                )
            else:
                doc = APDocument.objects.create(
                    tenant=tenant,
                    uploaded_by=request.user,
                    file=uploaded,
                    original_filename=uploaded.name,
                    content_type=ct,
                    file_size=uploaded.size,
                    source=APDocument.SOURCE_UPLOAD,
                    status=APDocument.STATUS_RECEIVED,
                    notes=notes,
                )
                _dispatch_bill_received(tenant, doc)
                saved_doc = doc

    # Fetch the 50 most recent documents for this tenant
    documents = list(
        APDocument.objects.filter(tenant=tenant)
        .select_related('uploaded_by', 'bus_event')
        .order_by('-received_at')[:50]
    )

    return render(request, 'accounting/ap_inbox.html', {
        'documents': documents,
        'save_errors': save_errors,
        'saved_doc': saved_doc,
        'max_mb': getattr(settings, 'AP_DOCUMENT_MAX_SIZE_MB', 20),
        'tenant_name': tenant.name,
    })


@csrf_exempt
@require_POST
def ap_email_webhook(request):
    """Email-to-bill webhook endpoint (Step 52).

    Accepts inbound-email POSTs from Mailgun / SendGrid / Postmark.
    Authentication: a shared secret token in the ``?token=`` query parameter
    (configured via ``AP_EMAIL_WEBHOOK_TOKEN`` environment variable).

    Expected multipart fields (Mailgun format; other providers are
    similar and can be normalised here):
        Subject     — email subject
        sender      — sender address
        attachment-1..N  — bill PDF / image attachments

    On success: creates an ``APDocument`` for each attachment and
    dispatches a ``bill.received`` BusEvent, returns HTTP 200 JSON.
    On authentication failure: HTTP 403.
    On bad request (no attachments, wrong type, oversized): HTTP 400.

    Tenant routing: resolved from the recipient address using the
    ``recipient`` field (format: ``<slug>@bills.ealedgers.com``).
    If no matching tenant is found the request is accepted (HTTP 200)
    but no document is created — avoids leaking tenant existence.
    """
    from agents.models import APDocument
    from accounting.models import Tenant  # noqa: local import avoids circular

    # --- token auth ---
    expected = getattr(settings, 'AP_EMAIL_WEBHOOK_TOKEN', '')
    provided = request.GET.get('token', '')
    if not expected or provided != expected:
        return HttpResponseForbidden('Invalid webhook token.')

    subject = request.POST.get('Subject', request.POST.get('subject', ''))
    sender = request.POST.get('sender', request.POST.get('from', ''))
    recipient = request.POST.get('recipient', request.POST.get('to', ''))

    # Resolve tenant from recipient slug (e.g. "elite-advisors@bills.ealedgers.com")
    tenant = None
    if recipient:
        local_part = recipient.split('@')[0].strip().lower()
        try:
            tenant = Tenant.objects.get(slug=local_part)
        except Tenant.DoesNotExist:
            pass

    if tenant is None:
        # Silently accept — don't leak which slugs exist
        return JsonResponse({'status': 'ok', 'docs_created': 0})

    notes = f"From: {sender}\nSubject: {subject}"
    docs_created = 0

    allowed_wh = getattr(settings, 'AP_DOCUMENT_ALLOWED_TYPES', _AP_ALLOWED_TYPES)
    max_bytes_wh = _ap_max_bytes()
    for key, f in request.FILES.items():
        if not key.startswith('attachment'):
            continue
        ct = f.content_type or 'application/octet-stream'
        if ct not in allowed_wh:
            continue  # skip non-bill attachments (signatures, calendars, etc.)
        if f.size > max_bytes_wh:
            continue  # too large — skip
        doc = APDocument.objects.create(
            tenant=tenant,
            uploaded_by=None,    # email ingestion — no user
            file=f,
            original_filename=f.name or 'attachment.pdf',
            content_type=ct,
            file_size=f.size,
            source=APDocument.SOURCE_EMAIL,
            status=APDocument.STATUS_RECEIVED,
            notes=notes,
        )
        _dispatch_bill_received(tenant, doc)
        docs_created += 1

    return JsonResponse({'status': 'ok', 'docs_created': docs_created})


# ---------------------------------------------------------------------------
# Step 57 — AP Approval-Queue UI (one-click approve / reject)
# ---------------------------------------------------------------------------

def _specialist_outputs(item):
    """Index an ApprovalQueueItem's specialist_results by specialist_type.

    Returns ``(by_type, errors)`` where ``by_type`` maps e.g. ``'proposer'``
    to its output dict (only for specialists that succeeded), and ``errors``
    is a list of ``{specialist_type, error}`` for those that failed.
    """
    by_type = {}
    errors = []
    for r in (item.specialist_results or []):
        st = r.get('specialist_type', '')
        if r.get('ok'):
            by_type[st] = r.get('output') or {}
        elif r.get('error'):
            errors.append({'specialist_type': st, 'error': r.get('error')})
    return by_type, errors


def summarize_queue_item(item):
    """Build a view-friendly summary of a proposal from its specialist outputs.

    Resilient to a partially-run pipeline (a halted/failed specialist simply
    leaves its section empty) so the UI always renders something useful.
    """
    by_type, errors = _specialist_outputs(item)
    extractor = by_type.get('extractor', {})
    proposer = by_type.get('proposer', {})
    reviewer = by_type.get('reviewer', {})
    je = proposer.get('proposed_je') or {}
    reviewer_ran = 'reviewer' in by_type

    return {
        'item': item,
        'vendor': extractor.get('vendor_name') or je.get('ref') or '',
        'currency': proposer.get('currency') or extractor.get('currency') or '',
        'date': je.get('date') or extractor.get('invoice_date') or '',
        'ref': je.get('ref') or extractor.get('invoice_number') or '',
        'je_lines': je.get('lines') or [],
        'journal_code': je.get('journal_code') or '',
        'debit_total': proposer.get('debit_total'),
        'credit_total': proposer.get('credit_total'),
        'balanced': proposer.get('balanced'),
        'total_matches': proposer.get('total_matches'),
        'proposer_issues': proposer.get('issues') or [],
        'needs_review': proposer.get('needs_review'),
        'has_proposal': bool(je),
        'reviewer_ran': reviewer_ran,
        'reviewer_approved': reviewer.get('approved') if reviewer_ran else None,
        'review_notes': reviewer.get('review_notes', '') if reviewer_ran else '',
        'citations': reviewer.get('citations') or [],
        'structural_issues': reviewer.get('structural_issues') or [],
        'classified_lines': by_type.get('classifier', {}).get('classified_lines') or [],
        'pipeline_errors': errors,
    }


@login_required
@tenant_required
def ap_queue(request):
    """AP approval queue — pending candidate journal entries for D01 (Step 57).

    Lists pending ``ApprovalQueueItem`` proposals with a one-click Approve
    action and a link through to the detail page (where Reject — which
    requires a reason — and the full proposed JE / citations live).
    """
    qs = ApprovalQueueItem.objects.for_tenant(request.tenant).filter(dept_code='D01')
    pending = list(qs.filter(status='pending').order_by('created_at')[:100])
    reviewed = list(
        qs.exclude(status='pending')
        .select_related('reviewed_by')
        .order_by('-reviewed_at', '-created_at')[:25]
    )

    return render(request, 'accounting/ap_queue.html', {
        'pending': [summarize_queue_item(i) for i in pending],
        'reviewed': reviewed,
        'pending_count': len(pending),
        'done': request.GET.get('done', ''),
        'ref': request.GET.get('ref', ''),
        'tenant_name': request.tenant.name,
    })


@login_required
@tenant_required
def ap_queue_detail(request, pk):
    """Full detail of one AP proposal; POST performs approve / reject (Step 57).

    Reject requires a reason (``note``); approve may carry an optional note.
    Acting on an already-reviewed item is a safe no-op (redirects with a
    notice) so a double-submit or stale tab cannot re-review.
    """
    item = get_object_or_404(
        ApprovalQueueItem.objects.for_tenant(request.tenant),
        pk=pk, dept_code='D01',
    )

    if request.method == 'POST':
        action = request.POST.get('action', '')
        note = request.POST.get('note', '').strip()
        queue_url = reverse('ap_queue')

        if not item.is_pending:
            return redirect(f"{queue_url}?done=already&ref={item.pk}")

        if action == 'approve':
            item.approve(user=request.user, note=note)
            return redirect(f"{queue_url}?done=approved&ref={item.pk}")

        if action == 'reject':
            if not note:
                return render(request, 'accounting/ap_queue_detail.html', {
                    'summary': summarize_queue_item(item),
                    'item': item,
                    'reject_error': 'A reason is required to reject a proposal.',
                    'tenant_name': request.tenant.name,
                })
            item.reject(user=request.user, note=note)
            return redirect(f"{queue_url}?done=rejected&ref={item.pk}")

        return HttpResponseBadRequest('Unknown action.')

    return render(request, 'accounting/ap_queue_detail.html', {
        'summary': summarize_queue_item(item),
        'item': item,
        'tenant_name': request.tenant.name,
    })
