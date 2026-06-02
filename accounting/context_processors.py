"""Context processor — the platform top-nav, exposed to every template.

Post-pivot this is a lean, agent-oriented nav (Knowledge / Departments /
Agent Activity / ERP / Ledger), not the old Odoo-Accounting clone with its
"Soon" pills. The shell (`_app_base.html` / `_nav.html`) is shared by the
custom pages and the Django admin; it also surfaces the tenant switcher,
which needs ``user_memberships`` from here.
"""

import copy


def _menu(label, *, url=None, groups=None):
    """A top-level menu. Either a direct link (url) or a dropdown (groups)."""
    return {"label": label, "url": url, "groups": groups or []}


def _group(items, title=None):
    return {"title": title, "items": items}


def _item(label, url=None):
    return {"label": label, "url": url}


# Single source of truth for the platform top-nav. Flat links — the agent
# vision surfaces (Knowledge, Departments, Agents, ERP) + the Ledger
# (accounting substrate, via admin).
NAV = [
    _menu("Knowledge Base", url="/knowledge/explorer/"),
    _menu("Departments", url="/departments/"),
    _menu("Agent Activity", url="/agents/"),
    _menu("ERP Connections", url="/erp/"),
    _menu("Ledger", url="/admin/accounting/"),
]


def _annotate_active(nav, current_path):
    """Mark which top-level menu is currently active (longest-prefix match,
    so /knowledge/explorer/<slug>/ still highlights Knowledge Base)."""
    best = None
    for menu in nav:
        menu["active"] = False
        url = menu.get("url")
        if not url or url.startswith("/admin"):
            continue
        base = url.rstrip("/")
        if current_path.rstrip("/") == base or current_path.startswith(base + "/"):
            if best is None or len(base) > len(best[1]):
                best = (menu, base)
    if best:
        best[0]["active"] = True
    return nav


def accounting_nav(request):
    """Make NAV available to every template, with the active flag resolved,
    plus the user's active memberships for the tenant switcher."""
    nav = copy.deepcopy(NAV)
    _annotate_active(nav, request.path)

    memberships = []
    if getattr(request, "user", None) and request.user.is_authenticated:
        from accounting.models import Membership
        memberships = list(
            Membership.objects.filter(user=request.user, active=True)
            .select_related("tenant")
            .order_by("tenant__name")
        )
    return {"NAV": nav, "user_memberships": memberships}
