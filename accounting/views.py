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

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ERPConnectionForm, SignupForm
from .middleware import SESSION_TENANT_KEY, switch_tenant, tenant_required
from .models import (
    AgentRun,
    ERPConnection,
    SUBSCRIBABLE_DEPARTMENTS,
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


# Department roster metadata for the org-chart skeleton (scope + the phase the
# agent ships in). Codes/labels come from SUBSCRIBABLE_DEPARTMENTS.
_DEPT_META = {
    "D00": ("Cross-department orchestration, month-end close", "P11"),
    "D01": ("Vendor bills, payments, withholding tax", "P05"),
    "D02": ("Customer invoices, receipts, dunning", "P07"),
    "D03": ("Bank, cash, FX, payment execution", "P08"),
    "D04": ("Capex, componentisation, depreciation, disposals", "P09"),
    "D05": ("Manual journal entries, accruals, reconciliations", "P10"),
    "D06": ("TVA, WHT, IS, IRPP coordination, DGI filings", "P06"),
    "D07": ("Payroll journal, CNPS, IRPP, certificates", "P16"),
    "D08": ("Item master, receipts, issues, costing", "P17"),
    "D09": ("Statutory pack, DGI filings, management reports", "P12"),
    "D10": ("Class 9, cost centres, project costing", "P13"),
    "D11": ("Budget, forecast, variance, scenarios", "P14"),
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
    without a tenant (a user with no memberships still sees it)."""
    tenant_name = request.tenant.name if request.tenant else "EA Ledgers"
    return render(request, "accounting/workspace.html",
                  {"modules": MODULES, "tenant_name": tenant_name})


@login_required
@tenant_required
def departments(request):
    """Org chart of the virtual finance function — the 12 departments, each
    tenant's subscription, and the phase its agent ships in (skeleton)."""
    subscribed = set(request.tenant.subscribed_departments())
    rows = []
    for code, label in SUBSCRIBABLE_DEPARTMENTS:
        scope, phase = _DEPT_META.get(code, ("", ""))
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
    """The tenant's ERP connections + their health (skeleton — connecting a
    live Odoo is Phase P03)."""
    connections = list(
        ERPConnection.objects.filter(tenant=request.tenant)
        .order_by("-is_primary", "name")
    )
    return render(request, "accounting/erp_connections.html", {
        "page_name": "ERP Connections",
        "connections": connections,
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
