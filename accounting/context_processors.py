"""Context processor — exposes the Odoo-mirroring Accounting nav to every template.

Structure mirrors Odoo's Accounting app top-nav (Dashboard / Customers / Vendors /
Accounting / Review / Reporting / Configuration) with grouped sub-menus. Items
that EA Ledgers already supports point to a real URL; the rest are rendered
with a 'Soon' pill so the information architecture is visible end-to-end.
"""


def _menu(label, *, url=None, groups=None):
    """A top-level menu. Either a direct link (url) or a dropdown (groups)."""
    return {"label": label, "url": url, "groups": groups or []}


def _group(items, title=None):
    return {"title": title, "items": items}


def _item(label, url=None):
    return {"label": label, "url": url}


# Single source of truth for the Accounting top-nav.
NAV = [
    _menu("Dashboard", url="/accounting/"),

    _menu("Customers", groups=[
        _group([
            _item("Invoices", "/accounting/invoices/"),
            _item("Credit Notes"),
            _item("Payments"),
            _item("Products"),
            _item("Customers", "/admin/accounting/partner/?partner_type__in=customer,both"),
        ]),
    ]),

    _menu("Vendors", groups=[
        _group([
            _item("Bills", "/accounting/bills/"),
            _item("Refunds"),
            _item("Payments"),
            _item("Products"),
            _item("Vendors", "/admin/accounting/partner/?partner_type__in=vendor,both"),
        ]),
    ]),

    _menu("Accounting", groups=[
        _group(title="Transactions", items=[
            _item("Journal Entries", "/admin/accounting/journalentry/"),
            _item("Analytic Items"),
            _item("Analytic Budget"),
        ]),
        _group(title="Assets & Liabilities", items=[
            _item("Assets", "/admin/accounting/fixedasset/"),
            _item("Loans"),
            _item("Fleet"),
        ]),
        _group(title="Closing", items=[
            _item("Reconcile", "/accounting/bank/"),
            _item("Tax Returns"),
            _item("Lock Dates"),
            _item("Secure Entries"),
        ]),
    ]),

    _menu("Review", groups=[
        _group(title="Control", items=[
            _item("Journal Items"),
            _item("Journal Audit"),
        ]),
        _group(title="Audit", items=[
            _item("Working Files"),
            _item("Annual Report"),
        ]),
        _group(title="Inventory", items=[
            _item("Inventory Valuation"),
            _item("Depreciation Schedule", "/accounting/reports/fixed-assets-register/"),
            _item("Loans Analysis"),
        ]),
        _group(title="Regularization Entries", items=[
            _item("Deferred Revenues"),
            _item("Deferred Expenses"),
        ]),
        _group(title="Purchases", items=[
            _item("Bill To Receive"),
            _item("Billed Not Received"),
        ]),
        _group(title="Sales", items=[
            _item("Invoices To Be Issued"),
            _item("Invoiced Not Delivered"),
        ]),
        _group(title="Logs", items=[
            _item("Audit Trail"),
        ]),
    ]),

    _menu("Reporting", groups=[
        _group(title="Statement Reports", items=[
            _item("Balance Sheet"),
            _item("Profit and Loss"),
            _item("Cash Flow Statement"),
        ]),
        _group(title="Ledgers", items=[
            _item("Trial Balance", "/accounting/reports/trial-balance/"),
            _item("General Ledger", "/accounting/reports/general-ledger/"),
        ]),
        _group(title="Partner Reports", items=[
            _item("Partner Ledger"),
            _item("Aged Receivable", "/accounting/reports/customer-aging/"),
            _item("Aged Payable", "/accounting/reports/supplier-aging/"),
        ]),
        _group(title="Taxes & Fiscal", items=[
            _item("Tax Report"),
            _item("Fiscal Report"),
        ]),
        _group(title="Management", items=[
            _item("Invoice Analysis"),
            _item("Analytic Report"),
            _item("Executive Summary"),
            _item("Budget Report"),
            _item("Product Margins"),
        ]),
    ]),

    _menu("Configuration", groups=[
        _group(title="Accounting", items=[
            _item("Settings"),
            _item("Chart of Accounts", "/admin/accounting/account/"),
            _item("Taxes"),
            _item("Journals", "/admin/accounting/journal/"),
            _item("Currencies", "/admin/accounting/currency/"),
            _item("Fiscal Positions"),
            _item("Multi-Ledger"),
            _item("Tax Groups"),
            _item("Tax Units"),
            _item("Account Tags"),
            _item("Account Groups"),
            _item("Accounting Reports"),
            _item("Horizontal Groups"),
            _item("Checks"),
            _item("Fiscal Categories"),
            _item("Asset Models"),
            _item("Return Types"),
            _item("Financial Budgets"),
            _item("Online Synchronization"),
        ]),
        _group(title="Invoicing", items=[
            _item("Payment Terms"),
            _item("Follow-up Levels"),
            _item("Incoterms"),
        ]),
        # Companies isn't in Odoo's Accounting nav but EA Ledgers needs it
        # accessible somewhere — tuck it under Configuration.
        _group(title="EA Ledgers", items=[
            _item("Companies", "/admin/accounting/company/"),
        ]),
    ]),
]


def _annotate_active(nav, current_path):
    """Mark which top-level menu and/or sub-item is currently active."""
    for menu in nav:
        menu_active = False
        if menu.get("url") and menu["url"].rstrip("/") == current_path.rstrip("/"):
            menu_active = True
        for group in menu.get("groups") or []:
            for item in group.get("items") or []:
                item_active = False
                url = item.get("url")
                if url:
                    # Strip query string for comparison
                    base = url.split("?", 1)[0]
                    if current_path.startswith(base) and base != "/admin/":
                        item_active = True
                    elif current_path == base:
                        item_active = True
                item["active"] = item_active
                if item_active:
                    menu_active = True
        menu["active"] = menu_active
    return nav


def accounting_nav(request):
    """Make NAV available to every template, with active flags resolved.

    Also exposes the user's active memberships so the tenant switcher in
    the top bar can render. Always returns a list (possibly empty) under
    ``user_memberships`` so templates can safely length-check it.
    """
    import copy
    nav = copy.deepcopy(NAV)
    _annotate_active(nav, request.path)

    memberships = []
    if getattr(request, "user", None) and request.user.is_authenticated:
        # Lazy import: context processors run inside template rendering, so
        # the app registry is guaranteed ready by now, but keeping the import
        # local keeps the module import-safe in management commands.
        from accounting.models import Membership
        memberships = list(
            Membership.objects.filter(user=request.user, active=True)
            .select_related("tenant")
            .order_by("tenant__name")
        )
    return {"NAV": nav, "user_memberships": memberships}
