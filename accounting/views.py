from django.contrib.auth.decorators import login_required
from django.shortcuts import render


# ---------------------------------------------------------------------------
# Module icons — each is a complete inline SVG. Designed by hand to feel
# distinctive (not stock icon-set generic): a saturated coloured tile with
# a white symbol that reads at a glance.
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
# Module manifest
# Order produces a 3x3 grid; Accounting (only live) sits in the centre cell.
# ---------------------------------------------------------------------------

MODULES = [
    # Row 1
    {"slug": "missions",     "name": "Mission Orders",  "tagline": "Ordres de mission",
     "url": None,     "status": "soon", "icon": ICON_MISSIONS},
    {"slug": "timesheets",   "name": "Time Tracking",   "tagline": "Hours on engagements",
     "url": None,     "status": "soon", "icon": ICON_TIME},
    {"slug": "purchasing",   "name": "Purchase Orders", "tagline": "Procurement",
     "url": None,     "status": "soon", "icon": ICON_PURCHASE},

    # Row 2 — Accounting in the centre
    {"slug": "engagements",  "name": "Engagements",     "tagline": "Client projects",
     "url": None,     "status": "soon", "icon": ICON_ENGAGEMENTS},
    {"slug": "accounting",   "name": "Accounting",      "tagline": "General ledger",
     "url": "/admin/", "status": "live", "icon": ICON_ACCOUNTING},
    {"slug": "billing",      "name": "Billing",         "tagline": "Customer invoices",
     "url": None,     "status": "soon", "icon": ICON_BILLING},

    # Row 3
    {"slug": "hr",           "name": "People",          "tagline": "Employees & grades",
     "url": None,     "status": "soon", "icon": ICON_PEOPLE},
    {"slug": "reports",      "name": "Reports",         "tagline": "Analytics",
     "url": None,     "status": "soon", "icon": ICON_REPORTS},
    {"slug": "settings",     "name": "Settings",        "tagline": "Configuration",
     "url": None,     "status": "soon", "icon": ICON_SETTINGS},
]


@login_required
def workspace(request):
    """Module launcher shown immediately after login.

    Hardcoded tenant for now. When the Tenant model lands, this resolves
    the user's active tenant via request.user.memberships.
    """
    return render(
        request,
        "accounting/workspace.html",
        {
            "modules": MODULES,
            "tenant_name": "Elite Advisors SARL",
        },
    )
