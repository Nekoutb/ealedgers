"""Views for the accounting app — both the workspace launcher and the
in-module pages (dashboard, reports). All in-module views go through
``@tenant_required`` so they always see a real ``request.tenant`` and use
``Model.objects.for_tenant(request.tenant)`` to scope every query."""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Q, F
from django.shortcuts import render

from .middleware import tenant_required
from .models import (
    Account,
    DepreciationLine,
    FixedAsset,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Partner,
)


# ---------------------------------------------------------------------------
# Module icons (used by the workspace launcher only)
# ---------------------------------------------------------------------------

ICON_ACCOUNTING = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#0B6E4F"/>
  <g fill="white">
    <rect x="6" y="9.5" width="20" height="2.6" rx="0.4"/>
    <rect x="6" y="14.7" width="12" height="2.6" rx="0.4"/>
    <rect x="6" y="19.9" width="20" height="2.6" rx="0.4"/>
  </g>
</svg>
""".strip()

ICON_MISSIONS = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#C0563A"/>
  <path d="M6 16 L26 6 L20 26 L16.5 17.5 Z" fill="white"/>
  <path d="M16.5 17.5 L26 6" stroke="#C0563A" stroke-width="1.2" stroke-linecap="round"/>
</svg>
""".strip()

ICON_TIME = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#2E4A6B"/>
  <circle cx="16" cy="16" r="8" fill="none" stroke="white" stroke-width="2.2"/>
  <path d="M16 10.5 V16 L20 18" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
""".strip()

ICON_PURCHASE = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#D49B1A"/>
  <path d="M16 6.5 L25.5 11 L25.5 21 L16 25.5 L6.5 21 L6.5 11 Z" fill="white"/>
  <path d="M6.5 11 L16 15.6 L25.5 11 M16 15.6 L16 25.5 M11.2 8.7 L20.7 13.3" stroke="#D49B1A" stroke-width="1.2" fill="none" stroke-linejoin="round"/>
</svg>
""".strip()

ICON_BILLING = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#7B3F71"/>
  <path d="M9 6.5 L20 6.5 L24 10.5 L24 25.5 L9 25.5 Z" fill="white"/>
  <path d="M20 6.5 L20 10.5 L24 10.5" fill="#7B3F71"/>
  <g stroke="#7B3F71" stroke-width="1.6" stroke-linecap="round">
    <line x1="12.5" y1="15" x2="20.5" y2="15"/>
    <line x1="12.5" y1="18.5" x2="20.5" y2="18.5"/>
    <line x1="12.5" y1="22" x2="17" y2="22"/>
  </g>
</svg>
""".strip()

ICON_PEOPLE = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#C2526D"/>
  <g fill="white">
    <circle cx="12.5" cy="12.5" r="3.6"/>
    <path d="M6 25.5 Q6 17.5 12.5 17.5 Q19 17.5 19 25.5 Z"/>
    <circle cx="22" cy="14" r="3"/>
    <path d="M16.5 25.5 Q16.5 19.5 22 19.5 Q27.5 19.5 27.5 25.5 Z" opacity="0.78"/>
  </g>
</svg>
""".strip()

ICON_ENGAGEMENTS = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#2E8B8E"/>
  <g stroke="white" stroke-width="1.7" stroke-linecap="round">
    <line x1="11" y1="10" x2="22" y2="16"/>
    <line x1="22" y1="16" x2="11" y2="22"/>
  </g>
  <g fill="white">
    <circle cx="10.5" cy="10" r="3.2"/>
    <circle cx="22" cy="16" r="3.2"/>
    <circle cx="10.5" cy="22" r="3.2"/>
  </g>
</svg>
""".strip()

ICON_REPORTS = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#E07B30"/>
  <g fill="white">
    <rect x="6.5" y="19" width="4" height="7" rx="0.4"/>
    <rect x="14" y="13" width="4" height="13" rx="0.4"/>
    <rect x="21.5" y="7.5" width="4" height="18.5" rx="0.4"/>
  </g>
  <path d="M6.5 11 L14 14 L21.5 6.5" stroke="white" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round" opacity="0.65"/>
</svg>
""".strip()

ICON_SETTINGS = """
<svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
  <rect width="32" height="32" rx="7" fill="#3F3F3F"/>
  <g fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="16" cy="16" r="3.5"/>
    <path d="M16 5.5 V8.5 M16 23.5 V26.5 M5.5 16 H8.5 M23.5 16 H26.5 M8.6 8.6 L10.8 10.8 M21.2 21.2 L23.4 23.4 M23.4 8.6 L21.2 10.8 M8.6 23.4 L10.8 21.2"/>
  </g>
</svg>
""".strip()


# ---------------------------------------------------------------------------
# Module manifest for the workspace launcher
# ---------------------------------------------------------------------------

MODULES = [
    {"slug": "missions",    "name": "Mission Orders",  "tagline": "Ordres de mission",
     "url": None,           "status": "soon", "icon": ICON_MISSIONS},
    {"slug": "timesheets",  "name": "Time Tracking",   "tagline": "Hours on engagements",
     "url": None,           "status": "soon", "icon": ICON_TIME},
    {"slug": "purchasing",  "name": "Purchase Orders", "tagline": "Procurement",
     "url": None,           "status": "soon", "icon": ICON_PURCHASE},
    {"slug": "engagements", "name": "Engagements",     "tagline": "Client projects",
     "url": None,           "status": "soon", "icon": ICON_ENGAGEMENTS},
    {"slug": "accounting",  "name": "Accounting",      "tagline": "General ledger",
     "url": "/accounting/", "status": "live", "icon": ICON_ACCOUNTING},
    {"slug": "billing",     "name": "Billing",         "tagline": "Customer invoices",
     "url": None,           "status": "soon", "icon": ICON_BILLING},
    {"slug": "hr",          "name": "People",          "tagline": "Employees & grades",
     "url": None,           "status": "soon", "icon": ICON_PEOPLE},
    {"slug": "reports",     "name": "Reports",         "tagline": "Analytics",
     "url": None,           "status": "soon", "icon": ICON_REPORTS},
    {"slug": "settings",    "name": "Settings",        "tagline": "Configuration",
     "url": None,           "status": "soon", "icon": ICON_SETTINGS},
]


# ---------------------------------------------------------------------------
# Workspace launcher
# ---------------------------------------------------------------------------

@login_required
def workspace(request):
    """Module launcher — visible to any authenticated user; doesn't need
    a tenant (a user with no memberships still sees the page)."""
    tenant_name = request.tenant.name if request.tenant else "EA Ledgers"
    return render(
        request,
        "accounting/workspace.html",
        {"modules": MODULES, "tenant_name": tenant_name},
    )


# ---------------------------------------------------------------------------
# Accounting Dashboard
# ---------------------------------------------------------------------------

def _journal_balance(tenant, journal):
    """Net balance of all posted lines on this journal's default account.
    For bank/cash journals this is effectively the bank balance."""
    if not journal.default_account_id:
        return Decimal("0")
    agg = JournalEntryLine.objects.for_tenant(tenant).filter(
        account_id=journal.default_account_id,
        entry__state="posted",
    ).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or Decimal("0")) - (agg["c"] or Decimal("0"))


@login_required
@tenant_required
def dashboard(request):
    t = request.tenant
    bank_cash_journals = list(
        Journal.objects.for_tenant(t).filter(type__in=["bank", "cash"], active=True).order_by("code")
    )
    for j in bank_cash_journals:
        j.balance = _journal_balance(t, j)
        j.tx_count = JournalEntryLine.objects.for_tenant(t).filter(
            entry__journal=j, entry__state="posted",
        ).count()

    counters = {
        "draft_entries": JournalEntry.objects.for_tenant(t).filter(state="draft").count(),
        "posted_entries": JournalEntry.objects.for_tenant(t).filter(state="posted").count(),
        "customers": Partner.objects.for_tenant(t).filter(
            Q(partner_type="customer") | Q(partner_type="both"),
        ).count(),
        "suppliers": Partner.objects.for_tenant(t).filter(
            Q(partner_type="vendor") | Q(partner_type="both"),
        ).count(),
        "accounts_count": Account.objects.for_tenant(t).filter(deprecated=False).count(),
        "fixed_assets": FixedAsset.objects.for_tenant(t).exclude(state="disposed").count(),
    }

    return render(
        request,
        "accounting/dashboard.html",
        {
            "bank_cash_journals": bank_cash_journals,
            "counters": counters,
            "tenant_name": t.name,
        },
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@login_required
@tenant_required
def trial_balance(request):
    t = request.tenant
    accounts = (
        Account.objects.for_tenant(t).filter(deprecated=False)
        .annotate(
            debit_sum=Sum("lines__debit", filter=Q(lines__entry__state="posted") & Q(lines__tenant=t)),
            credit_sum=Sum("lines__credit", filter=Q(lines__entry__state="posted") & Q(lines__tenant=t)),
        )
        .order_by("code")
    )
    rows = []
    total_d = Decimal("0")
    total_c = Decimal("0")
    for a in accounts:
        d = a.debit_sum or Decimal("0")
        c = a.credit_sum or Decimal("0")
        if d == 0 and c == 0:
            continue
        rows.append(
            {
                "code": a.code,
                "name": a.name,
                "type": a.get_type_display(),
                "debit": d,
                "credit": c,
                "balance": d - c,
            }
        )
        total_d += d
        total_c += c
    return render(
        request,
        "accounting/trial_balance.html",
        {
            "rows": rows,
            "total_debit": total_d,
            "total_credit": total_c,
            "balanced": total_d == total_c,
            "tenant_name": t.name,
        },
    )


@login_required
@tenant_required
def general_ledger(request):
    t = request.tenant
    qs = (
        JournalEntryLine.objects.for_tenant(t).filter(entry__state="posted")
        .select_related("entry", "entry__journal", "account", "partner")
        .order_by("-entry__date", "-entry__id", "id")[:500]
    )
    return render(
        request,
        "accounting/general_ledger.html",
        {"lines": qs, "tenant_name": t.name},
    )


def _aging_buckets(qs, today=None):
    """Bucket open (unreconciled) lines into 0-30 / 31-60 / 61-90 / >90."""
    today = today or date.today()
    buckets = {"current": Decimal("0"), "b30": Decimal("0"), "b60": Decimal("0"),
               "b90": Decimal("0"), "over": Decimal("0")}
    per_partner = {}
    for line in qs.select_related("entry", "partner"):
        age = (today - line.entry.date).days
        amount = (line.debit or 0) - (line.credit or 0)
        if line.partner_id is None:
            continue
        pp = per_partner.setdefault(
            line.partner,
            {"current": Decimal("0"), "b30": Decimal("0"), "b60": Decimal("0"),
             "b90": Decimal("0"), "over": Decimal("0"), "total": Decimal("0")},
        )
        if age <= 30:
            pp["current"] += amount; buckets["current"] += amount
        elif age <= 60:
            pp["b30"] += amount; buckets["b30"] += amount
        elif age <= 90:
            pp["b60"] += amount; buckets["b60"] += amount
        elif age <= 180:
            pp["b90"] += amount; buckets["b90"] += amount
        else:
            pp["over"] += amount; buckets["over"] += amount
        pp["total"] += amount
    rows = [{"partner": p, **vals} for p, vals in per_partner.items() if vals["total"] != 0]
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows, buckets


@login_required
@tenant_required
def customer_aging(request):
    t = request.tenant
    qs = JournalEntryLine.objects.for_tenant(t).filter(
        entry__state="posted",
        account__type="receivable",
        reconciled=False,
    )
    rows, totals = _aging_buckets(qs)
    return render(
        request,
        "accounting/aging.html",
        {
            "title": "Customer Aging",
            "rows": rows,
            "totals": totals,
            "partner_kind": "Customer",
            "tenant_name": t.name,
        },
    )


@login_required
@tenant_required
def supplier_aging(request):
    t = request.tenant
    qs = JournalEntryLine.objects.for_tenant(t).filter(
        entry__state="posted",
        account__type="payable",
        reconciled=False,
    )
    rows, totals = _aging_buckets(qs)
    # Payables show as credits → flip signs so the report reads positive
    for r in rows:
        for k in ("current", "b30", "b60", "b90", "over", "total"):
            r[k] = -r[k]
    for k in ("current", "b30", "b60", "b90", "over"):
        totals[k] = -totals[k]
    return render(
        request,
        "accounting/aging.html",
        {
            "title": "Supplier Aging",
            "rows": rows,
            "totals": totals,
            "partner_kind": "Supplier",
            "tenant_name": t.name,
        },
    )


@login_required
@tenant_required
def fixed_assets_register(request):
    t = request.tenant
    assets = list(FixedAsset.objects.for_tenant(t).order_by("code"))
    for a in assets:
        a.computed_total_posted = a.total_posted
        a.computed_book_value = a.book_value
    totals = {
        "cost": sum((a.purchase_cost for a in assets), Decimal("0")),
        "depreciation": sum((a.computed_total_posted for a in assets), Decimal("0")),
        "book_value": sum((a.computed_book_value for a in assets), Decimal("0")),
    }
    return render(
        request,
        "accounting/fixed_assets_register.html",
        {"assets": assets, "totals": totals, "tenant_name": t.name},
    )
