from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def workspace(request):
    """Module launcher shown immediately after login.

    For now, the tenant is hardcoded. When the Tenant model lands, this view
    will resolve the user's active tenant via request.user.memberships and
    pass the selected tenant in.
    """
    modules = [
        {
            "slug": "accounting",
            "name": "Accounting",
            "tagline": "Books in good order.",
            "description": (
                "Chart of accounts, journals, fixed assets, AR &amp; AP sub-ledgers. "
                "SYSCOHADA Révisé compliant."
            ),
            "accent": "#0B6E4F",
            "accent_soft": "#E1EFE8",
            "url": "/admin/",
            "status": "live",
            "icon": "accounting",
        },
        {
            "slug": "missions",
            "name": "Mission Orders",
            "tagline": "Ordres de mission.",
            "description": "Internal travel and assignment requests, with two-step approvals and on-return expense reports.",
            "accent": "#C0563A",
            "accent_soft": "#F6E6DF",
            "url": None,
            "status": "soon",
            "icon": "missions",
        },
        {
            "slug": "timesheets",
            "name": "Time Tracking",
            "tagline": "Hours on engagements.",
            "description": "Billable and non-billable hours by employee grade, with utilisation reporting per engagement.",
            "accent": "#2E4A6B",
            "accent_soft": "#E1E7EE",
            "url": None,
            "status": "soon",
            "icon": "time",
        },
        {
            "slug": "purchasing",
            "name": "Purchase Orders",
            "tagline": "Procurement workflow.",
            "description": "Request, approve, receive — vendor bills and three-way matching against the GL.",
            "accent": "#D49B1A",
            "accent_soft": "#F8EFD4",
            "url": None,
            "status": "soon",
            "icon": "purchase",
        },
        {
            "slug": "billing",
            "name": "Billing",
            "tagline": "Invoices and dunning.",
            "description": "Customer invoices from time and fixed-fee engagements, with withholding-tax support.",
            "accent": "#7B3F71",
            "accent_soft": "#EDE2EB",
            "url": None,
            "status": "soon",
            "icon": "billing",
        },
        {
            "slug": "hr",
            "name": "People",
            "tagline": "Employees and grades.",
            "description": "Staff register, grades and standard rates, departments and reporting lines.",
            "accent": "#C2526D",
            "accent_soft": "#F2DEE4",
            "url": None,
            "status": "soon",
            "icon": "people",
        },
    ]
    return render(
        request,
        "accounting/workspace.html",
        {
            "modules": modules,
            "tenant_name": "Elite Advisors SARL",
        },
    )
