"""Tests for the accounting app.

Phase 0.1 added the tenant FK as a data column. Phase 0.2 wires the
``TenantContextMiddleware`` + ``TenantManager.for_tenant()`` + tightens the
FK to NOT NULL with per-tenant uniqueness on code fields. These tests
verify all of that.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, models as dj_models, transaction
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings

from accounting.middleware import TenantContextMiddleware
from accounting.models import (
    Account,
    Currency,
    CustomerInvoice,
    CustomerInvoiceLine,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Partner,
    SupplierBill,
    SupplierBillLine,
    Tenant,
)


def _make_tenant(slug, *, currency=None, owner=None, business_type="services"):
    return Tenant.objects.create(
        slug=slug, name=f"{slug.title()} SARL", country="Cameroon",
        currency=currency, business_type=business_type, owner=owner,
    )


class TenantDataIsolationTests(TestCase):
    """Filter-by-tenant must never leak data across tenants."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_user("alice", "alice@a.test", "x")
        cls.bob = User.objects.create_user("bob", "bob@b.test", "x")
        cls.acme = _make_tenant("acme", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta", currency=cls.xaf, owner=cls.bob, business_type="goods")
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        cls.acme_cash = Account.objects.create(
            tenant=cls.acme, code="521100", name="Acme Cash", type="asset_cash",
        )
        cls.acme_revenue = Account.objects.create(
            tenant=cls.acme, code="707100", name="Acme Service Revenue", type="income",
        )
        cls.beta_cash = Account.objects.create(
            tenant=cls.beta, code="521100", name="Beta Cash", type="asset_cash",
        )
        cls.beta_revenue = Account.objects.create(
            tenant=cls.beta, code="707100", name="Beta Sales Revenue", type="income",
        )

        cls.acme_journal = Journal.objects.create(
            tenant=cls.acme, code="VEN", name="Acme Sales", type="sale",
            sequence_prefix="A/", next_sequence=1,
        )
        cls.beta_journal = Journal.objects.create(
            tenant=cls.beta, code="VEN", name="Beta Sales", type="sale",
            sequence_prefix="B/", next_sequence=1,
        )

    def _post_entry(self, *, tenant, journal, debit_account, credit_account, amount):
        entry = JournalEntry.objects.create(
            tenant=tenant, journal=journal, date=date.today(),
            ref=f"{tenant.slug} test entry",
        )
        JournalEntryLine.objects.create(
            tenant=tenant, entry=entry, account=debit_account,
            name="Cash in", debit=amount,
        )
        JournalEntryLine.objects.create(
            tenant=tenant, entry=entry, account=credit_account,
            name="Revenue", credit=amount,
        )
        entry.post()
        return entry

    def test_for_tenant_returns_only_that_tenants_rows(self):
        self.assertEqual(Account.objects.for_tenant(self.acme).count(), 2)
        self.assertEqual(Account.objects.for_tenant(self.beta).count(), 2)
        self.assertEqual(Account.objects.all().count(), 4)  # super-admin view

    def test_for_tenant_none_returns_empty_queryset(self):
        """Belt-and-suspenders: a missing tenant must NOT leak data."""
        self.assertEqual(Account.objects.for_tenant(None).count(), 0)
        self.assertFalse(Account.objects.for_tenant(None).exists())

    def test_journal_entries_isolated(self):
        self._post_entry(
            tenant=self.acme, journal=self.acme_journal,
            debit_account=self.acme_cash, credit_account=self.acme_revenue,
            amount=Decimal("100000"),
        )
        self._post_entry(
            tenant=self.beta, journal=self.beta_journal,
            debit_account=self.beta_cash, credit_account=self.beta_revenue,
            amount=Decimal("250000"),
        )
        self.assertEqual(JournalEntry.objects.for_tenant(self.acme).count(), 1)
        self.assertEqual(JournalEntry.objects.for_tenant(self.beta).count(), 1)
        acme_total = JournalEntryLine.objects.for_tenant(self.acme).aggregate(
            s=dj_models.Sum("debit"),
        )["s"]
        beta_total = JournalEntryLine.objects.for_tenant(self.beta).aggregate(
            s=dj_models.Sum("debit"),
        )["s"]
        self.assertEqual(acme_total, Decimal("100000"))
        self.assertEqual(beta_total, Decimal("250000"))

    def test_same_code_is_allowed_in_different_tenants(self):
        """Per-tenant uniqueness: '521100' in acme + '521100' in beta is fine."""
        # Already covered by setUpTestData — both tenants have a 521100 cash
        # account. If unique-per-tenant weren't enforced correctly, setup
        # itself would have failed.
        self.assertEqual(
            Account.objects.filter(code="521100").count(), 2,
        )

    def test_same_code_twice_in_same_tenant_fails(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Account.objects.create(
                    tenant=self.acme, code="521100", name="Duplicate", type="asset_cash",
                )

    def test_membership_unique_per_user_tenant(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Membership.objects.create(user=self.alice, tenant=self.acme, role="admin")

    def test_user_can_belong_to_multiple_tenants(self):
        Membership.objects.create(user=self.alice, tenant=self.beta, role="accountant")
        self.assertEqual(self.alice.memberships.count(), 2)
        self.assertEqual(
            set(self.alice.memberships.values_list("tenant__slug", flat=True)),
            {"acme", "beta"},
        )


class TenantContextMiddlewareTests(TestCase):
    """The middleware resolves request.tenant from session/membership."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_user("alice", "alice@a.test", "x")
        cls.bob = User.objects.create_user("bob", "bob@b.test", "x")
        cls.lonely = User.objects.create_user("lonely", "lonely@x.test", "x")  # no memberships
        cls.acme = _make_tenant("acme", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta", currency=cls.xaf, owner=cls.bob)
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.alice, tenant=cls.beta, role="accountant")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

    def _mw_request(self, user, session=None):
        """Build a request that's passed through the middleware. Returns the
        request with .tenant attached."""
        rf = RequestFactory()
        req = rf.get("/")
        req.user = user
        req.session = session if session is not None else {}
        mw = TenantContextMiddleware(lambda r: r)  # identity response
        mw(req)
        return req

    def test_anonymous_user_has_no_tenant(self):
        from django.contrib.auth.models import AnonymousUser
        req = self._mw_request(AnonymousUser())
        self.assertIsNone(req.tenant)

    def test_user_with_no_memberships_has_no_tenant(self):
        req = self._mw_request(self.lonely)
        self.assertIsNone(req.tenant)

    def test_user_with_one_membership_lands_there(self):
        req = self._mw_request(self.bob)
        self.assertEqual(req.tenant, self.beta)
        self.assertEqual(req.session.get("tenant_slug"), "beta")

    def test_session_selection_is_honoured(self):
        # Alice has memberships in BOTH acme and beta. Default is acme (first
        # created); session pin to beta should win.
        req = self._mw_request(self.alice, session={"tenant_slug": "beta"})
        self.assertEqual(req.tenant, self.beta)

    def test_stale_session_pointer_falls_back(self):
        """Session points to a slug the user no longer has membership in."""
        req = self._mw_request(self.bob, session={"tenant_slug": "acme"})  # bob has no acme
        # Falls back to bob's actual first membership (beta), and the stale
        # session pointer should have been cleared/overwritten.
        self.assertEqual(req.tenant, self.beta)
        self.assertEqual(req.session.get("tenant_slug"), "beta")

    def test_default_membership_is_earliest_created(self):
        """When the user has multiple memberships, default is the earliest."""
        # Alice was added to acme first, then beta. With no session pin, the
        # default should be acme.
        req = self._mw_request(self.alice)
        self.assertEqual(req.tenant, self.acme)


class TenantAwareViewTests(TestCase):
    """Phase 0.2b: views use Model.objects.for_tenant(request.tenant) and
    show only the current tenant's data, never the other tenant's."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_user("alice", "alice@a.test", "x")
        cls.bob = User.objects.create_user("bob", "bob@b.test", "x")
        cls.acme = _make_tenant("acme", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta", currency=cls.xaf, owner=cls.bob, business_type="goods")
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        # Each tenant gets one identifiable account that the OTHER tenant
        # must never see.
        Account.objects.create(
            tenant=cls.acme, code="ACME-CASH", name="Acme Cash Account",
            type="asset_cash",
        )
        Account.objects.create(
            tenant=cls.beta, code="BETA-CASH", name="Beta Cash Account",
            type="asset_cash",
        )

    def test_alice_dashboard_shows_only_acme_data(self):
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/")
        self.assertEqual(r.status_code, 200)
        # Alice's tenant context is acme → dashboard counts must reflect acme only
        # (1 account, not 2 across both tenants).
        # We assert against the rendered counters via the response context.
        counters = r.context["counters"]
        self.assertEqual(counters["accounts_count"], 1)
        self.assertEqual(r.context["tenant_name"], "Acme SARL")

    def test_bob_dashboard_shows_only_beta_data(self):
        self.client.force_login(self.bob)
        r = self.client.get("/accounting/")
        self.assertEqual(r.status_code, 200)
        counters = r.context["counters"]
        self.assertEqual(counters["accounts_count"], 1)
        self.assertEqual(r.context["tenant_name"], "Beta SARL")

    def test_trial_balance_isolated_by_tenant(self):
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/reports/trial-balance/")
        self.assertEqual(r.status_code, 200)
        # Neither tenant has posted entries yet, so 0 rows is expected.
        # Important assertion: tenant_name is Acme (not Beta).
        self.assertEqual(r.context["tenant_name"], "Acme SARL")

    def test_fixed_assets_register_isolated_by_tenant(self):
        from accounting.models import Journal as J, FixedAsset
        # Give Acme one fixed asset; Beta gets none.
        j = J.objects.create(tenant=self.acme, code="OD", name="Misc", type="general")
        a = Account.objects.create(
            tenant=self.acme, code="ACME-244", name="Office equipment", type="asset_fixed",
        )
        b = Account.objects.create(
            tenant=self.acme, code="ACME-2844", name="Accum depr office equipment",
            type="asset_fixed",
        )
        c = Account.objects.create(
            tenant=self.acme, code="ACME-6813", name="Depr expense", type="expense_depreciation",
        )
        FixedAsset.objects.create(
            tenant=self.acme, code="ACME-A1", name="Laptop",
            purchase_date=date.today(), in_service_date=date.today(),
            purchase_cost=Decimal("1000000"), useful_life_months=36,
            asset_account=a, accumulated_depreciation_account=b,
            depreciation_expense_account=c, depreciation_journal=j,
        )

        # Alice (acme) sees 1 asset; Bob (beta) sees 0.
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/reports/fixed-assets-register/")
        self.assertEqual(len(r.context["assets"]), 1)

        self.client.force_login(self.bob)
        r = self.client.get("/accounting/reports/fixed-assets-register/")
        self.assertEqual(len(r.context["assets"]), 0)


class TenantAwareAdminTests(TestCase):
    """Phase 0.2b: Django admin list views are filtered to request.tenant."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        # Superusers so we can hit admin pages without role gymnastics.
        cls.alice = User.objects.create_superuser("alice", "alice@a.test", "x")
        cls.bob = User.objects.create_superuser("bob", "bob@b.test", "x")
        cls.acme = _make_tenant("acme", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta", currency=cls.xaf, owner=cls.bob)
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        Account.objects.create(
            tenant=cls.acme, code="500", name="ACME-ONLY-ACCOUNT", type="asset_cash",
        )
        Account.objects.create(
            tenant=cls.beta, code="500", name="BETA-ONLY-ACCOUNT", type="asset_cash",
        )

    def test_admin_account_list_filters_to_alices_tenant(self):
        self.client.force_login(self.alice)
        r = self.client.get("/admin/accounting/account/")
        body = r.content.decode("utf-8", errors="replace")
        self.assertIn("ACME-ONLY-ACCOUNT", body)
        self.assertNotIn("BETA-ONLY-ACCOUNT", body)

    def test_admin_account_list_filters_to_bobs_tenant(self):
        self.client.force_login(self.bob)
        r = self.client.get("/admin/accounting/account/")
        body = r.content.decode("utf-8", errors="replace")
        self.assertIn("BETA-ONLY-ACCOUNT", body)
        self.assertNotIn("ACME-ONLY-ACCOUNT", body)


class SignupTests(TestCase):
    """Phase 0.6: self-serve sign-up creates User + Tenant + Membership."""

    def test_signup_page_renders(self):
        r = self.client.get("/signup/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Create your books")

    def test_signup_creates_user_tenant_membership_atomically(self):
        User = get_user_model()
        before_users = User.objects.count()
        before_tenants = Tenant.objects.count()
        r = self.client.post("/signup/", {
            "username": "newco",
            "email": "founder@newco.test",
            "password1": "longenoughpw1",
            "password2": "longenoughpw1",
            "company_name": "New Co SARL",
            "country": "Cameroon",
        })
        # Redirects to workspace
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, "/workspace/")

        # User created
        self.assertEqual(User.objects.count(), before_users + 1)
        user = User.objects.get(username="newco")
        self.assertEqual(user.email, "founder@newco.test")

        # Tenant created with the right slug + owner pointer
        self.assertEqual(Tenant.objects.count(), before_tenants + 1)
        tenant = Tenant.objects.get(slug="new-co-sarl")
        self.assertEqual(tenant.name, "New Co SARL")
        self.assertEqual(tenant.country, "Cameroon")
        self.assertEqual(tenant.owner_id, user.id)

        # Membership created with role=owner
        m = Membership.objects.get(user=user, tenant=tenant)
        self.assertEqual(m.role, "owner")
        self.assertTrue(m.active)

    def test_signup_logs_user_in(self):
        self.client.post("/signup/", {
            "username": "loggedin",
            "email": "x@x.test",
            "password1": "longenoughpw1",
            "password2": "longenoughpw1",
            "company_name": "Login Co",
        })
        # The session has an authenticated user now
        r = self.client.get("/workspace/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "loggedin")  # username in top bar

    def test_signup_rejects_mismatched_passwords(self):
        r = self.client.post("/signup/", {
            "username": "x",
            "email": "x@x.test",
            "password1": "longenoughpw1",
            "password2": "differentpw1",
            "company_name": "X",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Passwords don&#x27;t match.")
        User = get_user_model()
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_signup_rejects_duplicate_username(self):
        User = get_user_model()
        User.objects.create_user("taken", "taken@x.test", "x")
        r = self.client.post("/signup/", {
            "username": "taken",
            "email": "new@x.test",
            "password1": "longenoughpw1",
            "password2": "longenoughpw1",
            "company_name": "Y",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already taken")
        # No tenant was created either
        self.assertFalse(Tenant.objects.filter(slug="y").exists())

    def test_signup_generates_unique_slug_when_company_name_clashes(self):
        User = get_user_model()
        first = User.objects.create_user("u1", "u1@x.test", "x")
        Tenant.objects.create(slug="clash-co", name="Clash Co", owner=first)
        r = self.client.post("/signup/", {
            "username": "u2",
            "email": "u2@x.test",
            "password1": "longenoughpw1",
            "password2": "longenoughpw1",
            "company_name": "Clash Co",
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Tenant.objects.filter(slug="clash-co-2").exists())

    def test_signup_rejects_reserved_company_slug(self):
        """A company name that slugifies to a reserved word should be suffixed."""
        r = self.client.post("/signup/", {
            "username": "adminish",
            "email": "a@a.test",
            "password1": "longenoughpw1",
            "password2": "longenoughpw1",
            "company_name": "Admin",
        })
        self.assertEqual(r.status_code, 302)
        # Reserved 'admin' slug should be turned into 'admin-co' (or similar suffix).
        self.assertFalse(Tenant.objects.filter(slug="admin").exists())
        self.assertTrue(Tenant.objects.filter(slug="admin-co").exists())

    def test_signup_authenticated_user_bounced_to_workspace(self):
        User = get_user_model()
        u = User.objects.create_user("already", "a@a.test", "x")
        self.client.force_login(u)
        r = self.client.get("/signup/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.url, "/workspace/")


class TenantSwitcherTests(TestCase):
    """Phase 0.6: the tenant switcher swaps request.tenant via the session."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_user("alice2", "a@a.test", "x")
        cls.bob = User.objects.create_user("bob2", "b@b.test", "x")
        cls.acme = _make_tenant("acme-sw")
        cls.beta = _make_tenant("beta-sw")
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.alice, tenant=cls.beta, role="admin")
        # Bob only belongs to acme — must NOT be allowed to switch to beta.
        Membership.objects.create(user=cls.bob, tenant=cls.acme, role="viewer")

    def test_switch_tenant_changes_active_tenant(self):
        self.client.force_login(self.alice)
        # First request: tenant defaults to whichever membership was earliest
        # — order_by("created_at", "id") — which is acme.
        r = self.client.get("/workspace/")
        self.assertEqual(r.context["request"].tenant, self.acme)

        # Switch to beta
        r = self.client.post("/tenant/switch/beta-sw/")
        self.assertEqual(r.status_code, 302)

        # Subsequent request reads from session → beta
        r = self.client.get("/workspace/")
        self.assertEqual(r.context["request"].tenant, self.beta)

    def test_switch_tenant_rejects_unauthorized_target(self):
        self.client.force_login(self.bob)
        r = self.client.post("/tenant/switch/beta-sw/")  # bob has no membership in beta
        self.assertEqual(r.status_code, 302)
        # Bob's active tenant stays acme
        r = self.client.get("/workspace/")
        self.assertEqual(r.context["request"].tenant, self.acme)

    def test_switch_tenant_requires_authentication(self):
        r = self.client.post("/tenant/switch/acme-sw/")
        # Bounced to login
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.url.startswith("/?next=") or r.url.startswith("/accounts/login/"))

    def test_switch_tenant_get_not_allowed(self):
        self.client.force_login(self.alice)
        r = self.client.get("/tenant/switch/acme-sw/")
        self.assertEqual(r.status_code, 405)

    def test_switcher_shown_in_workspace_when_user_has_multiple_memberships(self):
        self.client.force_login(self.alice)  # 2 memberships
        r = self.client.get("/workspace/")
        self.assertContains(r, "ea-tenant-switcher")
        # _make_tenant title-cases the slug, so "acme-sw" → "Acme-Sw SARL"
        self.assertContains(r, self.acme.name)
        self.assertContains(r, self.beta.name)

    def test_switcher_hidden_when_user_has_one_membership(self):
        self.client.force_login(self.bob)  # 1 membership only
        r = self.client.get("/workspace/")
        self.assertNotContains(r, "ea-tenant-switcher")


# ---------------------------------------------------------------------------
# Phase 1.1 — Customer invoicing
# ---------------------------------------------------------------------------


class CustomerInvoiceTests(TestCase):
    """The invoice lifecycle: draft → posted → paid, including WHT math."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_inv", "a@a.test", "x")
        cls.tenant = _make_tenant("inv-co", currency=cls.xaf, owner=cls.alice)
        Membership.objects.create(user=cls.alice, tenant=cls.tenant, role="owner")

        # SYSCOHADA accounts the invoicing engine relies on
        cls.acct_ar = Account.objects.create(
            tenant=cls.tenant, code="411100", name="Customers", type="receivable", reconcile=True,
        )
        cls.acct_rev = Account.objects.create(
            tenant=cls.tenant, code="706000", name="Services revenue", type="income",
        )
        cls.acct_vat = Account.objects.create(
            tenant=cls.tenant, code="4434", name="VAT collected", type="liability_current",
        )
        cls.acct_wht = Account.objects.create(
            tenant=cls.tenant, code="4423", name="WHT credit", type="asset_current",
        )
        cls.acct_bank = Account.objects.create(
            tenant=cls.tenant, code="521000", name="Bank A/C", type="asset_cash", reconcile=True,
        )

        # Journals
        cls.sales = Journal.objects.create(
            tenant=cls.tenant, code="VEN", name="Sales", type="sale",
            sequence_prefix="INV/", next_sequence=1,
        )
        cls.bank = Journal.objects.create(
            tenant=cls.tenant, code="BNK", name="Bank", type="bank",
            default_account=cls.acct_bank, sequence_prefix="BNK/", next_sequence=1,
        )

        cls.customer = Partner.objects.create(
            tenant=cls.tenant, name="Acme Customer", partner_type="customer",
            account_receivable=cls.acct_ar,
        )

    # ----- helpers ---------------------------------------------------------

    def _make_invoice(self, *, wht=Decimal("0"), tax=Decimal("0"), qty=1, price=Decimal("100000")):
        inv = CustomerInvoice.objects.create(
            tenant=self.tenant,
            partner=self.customer,
            journal=self.sales,
            currency=self.xaf,
            date=date(2026, 5, 17),
            due_date=date(2026, 6, 16),
            withholding_tax_rate=wht,
        )
        CustomerInvoiceLine.objects.create(
            tenant=self.tenant, invoice=inv, sequence=10,
            description="Consulting services",
            account=self.acct_rev,
            quantity=Decimal(qty), unit_price=price, tax_rate=tax,
        )
        inv.recompute_amounts()
        return inv

    # ----- amount calculations --------------------------------------------

    def test_amounts_no_tax_no_wht(self):
        inv = self._make_invoice()
        self.assertEqual(inv.amount_subtotal, Decimal("100000.00"))
        self.assertEqual(inv.amount_tax, Decimal("0"))
        self.assertEqual(inv.amount_total, Decimal("100000.00"))
        self.assertEqual(inv.amount_withholding, Decimal("0.00"))

    def test_amounts_with_vat(self):
        inv = self._make_invoice(tax=Decimal("19.25"))
        self.assertEqual(inv.amount_subtotal, Decimal("100000.00"))
        self.assertEqual(inv.amount_tax, Decimal("19250.00"))
        self.assertEqual(inv.amount_total, Decimal("119250.00"))

    def test_amounts_with_wht_only(self):
        inv = self._make_invoice(wht=Decimal("10"))
        self.assertEqual(inv.amount_total, Decimal("100000.00"))
        self.assertEqual(inv.amount_withholding, Decimal("10000.00"))
        self.assertEqual(inv.amount_net_receivable, Decimal("90000.00"))

    def test_amounts_with_vat_and_wht(self):
        inv = self._make_invoice(tax=Decimal("19.25"), wht=Decimal("10"))
        # WHT applied to total (incl. VAT) by convention here
        self.assertEqual(inv.amount_total, Decimal("119250.00"))
        self.assertEqual(inv.amount_withholding, Decimal("11925.00"))

    # ----- post() ---------------------------------------------------------

    def test_post_creates_balanced_journal_entry_no_tax(self):
        inv = self._make_invoice()
        inv.post()
        self.assertEqual(inv.state, "posted")
        self.assertIsNotNone(inv.journal_entry)
        je = inv.journal_entry
        self.assertEqual(je.state, "posted")
        self.assertTrue(je.is_balanced)
        # Dr Receivable 100000 / Cr Revenue 100000
        debits = list(je.lines.filter(debit__gt=0).values_list("account__code", "debit"))
        credits = list(je.lines.filter(credit__gt=0).values_list("account__code", "credit"))
        self.assertEqual(debits, [("411100", Decimal("100000.00"))])
        self.assertEqual(credits, [("706000", Decimal("100000.00"))])

    def test_post_creates_vat_line(self):
        inv = self._make_invoice(tax=Decimal("19.25"))
        inv.post()
        je = inv.journal_entry
        self.assertTrue(je.is_balanced)
        # Dr 411 119250
        # Cr 706 100000 + Cr 4434 19250
        debits = je.lines.filter(debit__gt=0)
        credits = je.lines.filter(credit__gt=0)
        self.assertEqual(debits.count(), 1)
        self.assertEqual(debits.first().debit, Decimal("119250.00"))
        self.assertEqual(credits.count(), 2)
        revenue_credit = credits.get(account=self.acct_rev).credit
        vat_credit = credits.get(account=self.acct_vat).credit
        self.assertEqual(revenue_credit, Decimal("100000.00"))
        self.assertEqual(vat_credit, Decimal("19250.00"))

    def test_post_creates_wht_debit_line(self):
        inv = self._make_invoice(wht=Decimal("10"))
        inv.post()
        je = inv.journal_entry
        self.assertTrue(je.is_balanced)
        # Dr 411 90000 + Dr 4423 10000 = 100000 = Cr 706
        ar = je.lines.get(account=self.acct_ar).debit
        wht = je.lines.get(account=self.acct_wht).debit
        rev = je.lines.get(account=self.acct_rev).credit
        self.assertEqual(ar, Decimal("90000.00"))
        self.assertEqual(wht, Decimal("10000.00"))
        self.assertEqual(rev, Decimal("100000.00"))

    def test_post_assigns_number_from_journal_sequence(self):
        inv = self._make_invoice()
        self.assertEqual(inv.number, "")
        inv.post()
        self.assertTrue(inv.number.startswith("INV/"))
        # Posting another bumps the sequence
        inv2 = self._make_invoice()
        inv2.post()
        self.assertNotEqual(inv2.number, inv.number)

    def test_post_rejects_empty_invoice(self):
        from django.core.exceptions import ValidationError
        inv = CustomerInvoice.objects.create(
            tenant=self.tenant, partner=self.customer, journal=self.sales,
            currency=self.xaf, date=date.today(), due_date=date.today(),
        )
        with self.assertRaises(ValidationError):
            inv.post()
        inv.refresh_from_db()
        self.assertEqual(inv.state, "draft")

    def test_post_is_idempotent(self):
        inv = self._make_invoice()
        inv.post()
        number = inv.number
        inv.post()  # Should be a no-op
        inv.refresh_from_db()
        self.assertEqual(inv.number, number)

    def test_post_rejects_cancelled(self):
        from django.core.exceptions import ValidationError
        inv = self._make_invoice()
        inv.cancel()
        with self.assertRaises(ValidationError):
            inv.post()

    # ----- record_payment() ----------------------------------------------

    def test_record_payment_creates_balanced_entry(self):
        inv = self._make_invoice(wht=Decimal("10"))
        inv.post()
        inv.record_payment(self.bank)
        self.assertEqual(inv.state, "paid")
        self.assertIsNotNone(inv.payment_entry)
        je = inv.payment_entry
        self.assertTrue(je.is_balanced)
        # Dr Bank net / Cr AR net (the WHT portion stays as a 4423 asset)
        bank_debit = je.lines.get(account=self.acct_bank).debit
        ar_credit = je.lines.get(account=self.acct_ar).credit
        self.assertEqual(bank_debit, Decimal("90000.00"))
        self.assertEqual(ar_credit, Decimal("90000.00"))

    def test_record_payment_rejects_draft(self):
        from django.core.exceptions import ValidationError
        inv = self._make_invoice()
        with self.assertRaises(ValidationError):
            inv.record_payment(self.bank)

    def test_cancel_only_draft(self):
        from django.core.exceptions import ValidationError
        inv = self._make_invoice()
        inv.post()
        with self.assertRaises(ValidationError):
            inv.cancel()

    # ----- views ----------------------------------------------------------

    def test_invoice_list_view_renders(self):
        self._make_invoice()
        self._make_invoice()
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/invoices/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["invoices"]), 2)

    def test_invoice_detail_view_renders(self):
        inv = self._make_invoice()
        self.client.force_login(self.alice)
        r = self.client.get(f"/accounting/invoices/{inv.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Acme Customer")
        self.assertContains(r, "Consulting services")

    def test_invoice_post_view_transitions_to_posted(self):
        inv = self._make_invoice()
        self.client.force_login(self.alice)
        r = self.client.post(f"/accounting/invoices/{inv.id}/post/")
        self.assertEqual(r.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual(inv.state, "posted")

    def test_invoice_payment_view_transitions_to_paid(self):
        inv = self._make_invoice()
        inv.post()
        self.client.force_login(self.alice)
        r = self.client.post(
            f"/accounting/invoices/{inv.id}/pay/",
            {"journal_id": self.bank.id},
        )
        self.assertEqual(r.status_code, 302)
        inv.refresh_from_db()
        self.assertEqual(inv.state, "paid")


class CustomerInvoiceTenantIsolationTests(TestCase):
    """Invoices belong to their tenant; data must not leak across."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_iso", "a@a.test", "x")
        cls.bob = User.objects.create_superuser("bob_iso", "b@b.test", "x")
        cls.acme = _make_tenant("acme-iso", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta-iso", currency=cls.xaf, owner=cls.bob)
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        cls.j_a = Journal.objects.create(tenant=cls.acme, code="VEN", name="Sales", type="sale")
        cls.j_b = Journal.objects.create(tenant=cls.beta, code="VEN", name="Sales", type="sale")
        cls.p_a = Partner.objects.create(tenant=cls.acme, name="ACME-CUST", partner_type="customer")
        cls.p_b = Partner.objects.create(tenant=cls.beta, name="BETA-CUST", partner_type="customer")

        CustomerInvoice.objects.create(
            tenant=cls.acme, partner=cls.p_a, journal=cls.j_a, currency=cls.xaf,
            date=date.today(), due_date=date.today(),
        )
        CustomerInvoice.objects.create(
            tenant=cls.beta, partner=cls.p_b, journal=cls.j_b, currency=cls.xaf,
            date=date.today(), due_date=date.today(),
        )

    def test_for_tenant_filters_correctly(self):
        self.assertEqual(CustomerInvoice.objects.for_tenant(self.acme).count(), 1)
        self.assertEqual(CustomerInvoice.objects.for_tenant(self.beta).count(), 1)
        self.assertEqual(
            CustomerInvoice.objects.for_tenant(self.acme).first().partner.name,
            "ACME-CUST",
        )

    def test_invoice_list_view_filters_by_tenant(self):
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/invoices/")
        body = r.content.decode("utf-8", errors="replace")
        self.assertIn("ACME-CUST", body)
        self.assertNotIn("BETA-CUST", body)

    def test_admin_invoice_list_filters_by_tenant(self):
        self.client.force_login(self.bob)
        r = self.client.get("/admin/accounting/customerinvoice/")
        body = r.content.decode("utf-8", errors="replace")
        self.assertIn("BETA-CUST", body)
        self.assertNotIn("ACME-CUST", body)

    def test_invoice_number_unique_within_tenant_but_not_across(self):
        """Two tenants can both have invoice INV/00001."""
        inv_a = CustomerInvoice.objects.filter(tenant=self.acme).first()
        inv_b = CustomerInvoice.objects.filter(tenant=self.beta).first()
        inv_a.number = "INV/00001"
        inv_a.save()
        inv_b.number = "INV/00001"
        inv_b.save()  # Should succeed — per-tenant uniqueness
        self.assertEqual(inv_a.number, inv_b.number)
        self.assertNotEqual(inv_a.tenant_id, inv_b.tenant_id)


# ---------------------------------------------------------------------------
# Phase 1.2 — Supplier bills
# ---------------------------------------------------------------------------


class SupplierBillTests(TestCase):
    """Bill lifecycle: draft → posted → paid, including buy-side WHT math."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_bill", "a@a.test", "x")
        cls.tenant = _make_tenant("bill-co", currency=cls.xaf, owner=cls.alice)
        Membership.objects.create(user=cls.alice, tenant=cls.tenant, role="owner")

        cls.acct_ap = Account.objects.create(
            tenant=cls.tenant, code="401100", name="Vendors", type="payable", reconcile=True,
        )
        cls.acct_exp = Account.objects.create(
            tenant=cls.tenant, code="604000", name="Services purchased", type="expense",
        )
        cls.acct_vat = Account.objects.create(
            tenant=cls.tenant, code="4451", name="VAT recoverable", type="asset_current",
        )
        cls.acct_wht_pay = Account.objects.create(
            tenant=cls.tenant, code="4424", name="WHT payable to gov", type="liability_current",
        )
        cls.acct_bank = Account.objects.create(
            tenant=cls.tenant, code="521000", name="Bank A/C", type="asset_cash", reconcile=True,
        )

        cls.purch = Journal.objects.create(
            tenant=cls.tenant, code="ACH", name="Purchases", type="purchase",
            sequence_prefix="BILL/", next_sequence=1,
        )
        cls.bank = Journal.objects.create(
            tenant=cls.tenant, code="BNK", name="Bank", type="bank",
            default_account=cls.acct_bank, sequence_prefix="BNK/", next_sequence=1,
        )

        cls.vendor = Partner.objects.create(
            tenant=cls.tenant, name="Acme Vendor", partner_type="vendor",
            account_payable=cls.acct_ap,
        )

    def _make_bill(self, *, wht=Decimal("0"), tax=Decimal("0"), qty=1, price=Decimal("50000")):
        bill = SupplierBill.objects.create(
            tenant=self.tenant,
            partner=self.vendor,
            journal=self.purch,
            currency=self.xaf,
            date=date(2026, 5, 17),
            due_date=date(2026, 6, 16),
            vendor_reference="VND-001",
            withholding_tax_rate=wht,
        )
        SupplierBillLine.objects.create(
            tenant=self.tenant, bill=bill, sequence=10,
            description="Office supplies", account=self.acct_exp,
            quantity=Decimal(qty), unit_price=price, tax_rate=tax,
        )
        bill.recompute_amounts()
        return bill

    # ----- amounts --------------------------------------------------------

    def test_amounts_no_tax_no_wht(self):
        bill = self._make_bill()
        self.assertEqual(bill.amount_subtotal, Decimal("50000.00"))
        self.assertEqual(bill.amount_total, Decimal("50000.00"))
        self.assertEqual(bill.amount_net_payable, Decimal("50000.00"))

    def test_amounts_with_vat(self):
        bill = self._make_bill(tax=Decimal("19.25"))
        self.assertEqual(bill.amount_tax, Decimal("9625.00"))
        self.assertEqual(bill.amount_total, Decimal("59625.00"))

    def test_amounts_with_wht(self):
        bill = self._make_bill(wht=Decimal("5.5"))
        self.assertEqual(bill.amount_total, Decimal("50000.00"))
        self.assertEqual(bill.amount_withholding, Decimal("2750.00"))
        self.assertEqual(bill.amount_net_payable, Decimal("47250.00"))

    # ----- post() ---------------------------------------------------------

    def test_post_creates_balanced_entry_no_tax(self):
        bill = self._make_bill()
        bill.post()
        self.assertEqual(bill.state, "posted")
        je = bill.journal_entry
        self.assertTrue(je.is_balanced)
        # Dr 604 50000 / Cr 401 50000
        debits = list(je.lines.filter(debit__gt=0).values_list("account__code", "debit"))
        credits = list(je.lines.filter(credit__gt=0).values_list("account__code", "credit"))
        self.assertEqual(debits, [("604000", Decimal("50000.00"))])
        self.assertEqual(credits, [("401100", Decimal("50000.00"))])

    def test_post_creates_vat_recoverable(self):
        bill = self._make_bill(tax=Decimal("19.25"))
        bill.post()
        je = bill.journal_entry
        self.assertTrue(je.is_balanced)
        # Dr 604 50000 + Dr 4451 9625 / Cr 401 59625
        exp_debit = je.lines.get(account=self.acct_exp).debit
        vat_debit = je.lines.get(account=self.acct_vat).debit
        ap_credit = je.lines.get(account=self.acct_ap).credit
        self.assertEqual(exp_debit, Decimal("50000.00"))
        self.assertEqual(vat_debit, Decimal("9625.00"))
        self.assertEqual(ap_credit, Decimal("59625.00"))

    def test_post_splits_wht_to_payable(self):
        bill = self._make_bill(wht=Decimal("5.5"))
        bill.post()
        je = bill.journal_entry
        self.assertTrue(je.is_balanced)
        # Dr 604 50000 / Cr 401 47250 + Cr 4424 2750
        exp_debit = je.lines.get(account=self.acct_exp).debit
        ap_credit = je.lines.get(account=self.acct_ap).credit
        wht_credit = je.lines.get(account=self.acct_wht_pay).credit
        self.assertEqual(exp_debit, Decimal("50000.00"))
        self.assertEqual(ap_credit, Decimal("47250.00"))
        self.assertEqual(wht_credit, Decimal("2750.00"))

    def test_post_assigns_number_from_journal_sequence(self):
        bill = self._make_bill()
        self.assertEqual(bill.number, "")
        bill.post()
        self.assertTrue(bill.number.startswith("BILL/"))

    def test_post_rejects_empty_bill(self):
        from django.core.exceptions import ValidationError
        bill = SupplierBill.objects.create(
            tenant=self.tenant, partner=self.vendor, journal=self.purch,
            currency=self.xaf, date=date.today(), due_date=date.today(),
        )
        with self.assertRaises(ValidationError):
            bill.post()

    def test_post_idempotent(self):
        bill = self._make_bill()
        bill.post()
        number = bill.number
        bill.post()
        bill.refresh_from_db()
        self.assertEqual(bill.number, number)

    # ----- record_payment() ----------------------------------------------

    def test_record_payment_balanced(self):
        bill = self._make_bill(wht=Decimal("5.5"))
        bill.post()
        bill.record_payment(self.bank)
        self.assertEqual(bill.state, "paid")
        je = bill.payment_entry
        self.assertTrue(je.is_balanced)
        # Dr 401 47250 / Cr Bank 47250
        ap_debit = je.lines.get(account=self.acct_ap).debit
        bank_credit = je.lines.get(account=self.acct_bank).credit
        self.assertEqual(ap_debit, Decimal("47250.00"))
        self.assertEqual(bank_credit, Decimal("47250.00"))

    def test_record_payment_rejects_draft(self):
        from django.core.exceptions import ValidationError
        bill = self._make_bill()
        with self.assertRaises(ValidationError):
            bill.record_payment(self.bank)

    def test_cancel_only_draft(self):
        from django.core.exceptions import ValidationError
        bill = self._make_bill()
        bill.post()
        with self.assertRaises(ValidationError):
            bill.cancel()

    # ----- views ----------------------------------------------------------

    def test_bill_list_renders(self):
        self._make_bill()
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/bills/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["bills"]), 1)

    def test_bill_detail_renders(self):
        bill = self._make_bill()
        self.client.force_login(self.alice)
        r = self.client.get(f"/accounting/bills/{bill.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Acme Vendor")
        self.assertContains(r, "Office supplies")

    def test_bill_post_view_transitions(self):
        bill = self._make_bill()
        self.client.force_login(self.alice)
        r = self.client.post(f"/accounting/bills/{bill.id}/post/")
        self.assertEqual(r.status_code, 302)
        bill.refresh_from_db()
        self.assertEqual(bill.state, "posted")

    def test_bill_payment_view_transitions(self):
        bill = self._make_bill()
        bill.post()
        self.client.force_login(self.alice)
        r = self.client.post(
            f"/accounting/bills/{bill.id}/pay/",
            {"journal_id": self.bank.id},
        )
        self.assertEqual(r.status_code, 302)
        bill.refresh_from_db()
        self.assertEqual(bill.state, "paid")


class SupplierBillTenantIsolationTests(TestCase):
    """Bills are tenant-scoped — no cross-tenant leakage."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_b", "a@a.test", "x")
        cls.bob = User.objects.create_superuser("bob_b", "b@b.test", "x")
        cls.acme = _make_tenant("acme-b", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta-b", currency=cls.xaf, owner=cls.bob)
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        cls.j_a = Journal.objects.create(tenant=cls.acme, code="ACH", name="Purch", type="purchase")
        cls.j_b = Journal.objects.create(tenant=cls.beta, code="ACH", name="Purch", type="purchase")
        cls.v_a = Partner.objects.create(tenant=cls.acme, name="ACME-VEND", partner_type="vendor")
        cls.v_b = Partner.objects.create(tenant=cls.beta, name="BETA-VEND", partner_type="vendor")

        SupplierBill.objects.create(
            tenant=cls.acme, partner=cls.v_a, journal=cls.j_a, currency=cls.xaf,
            date=date.today(), due_date=date.today(),
        )
        SupplierBill.objects.create(
            tenant=cls.beta, partner=cls.v_b, journal=cls.j_b, currency=cls.xaf,
            date=date.today(), due_date=date.today(),
        )

    def test_for_tenant_filters(self):
        self.assertEqual(SupplierBill.objects.for_tenant(self.acme).count(), 1)
        self.assertEqual(SupplierBill.objects.for_tenant(self.beta).count(), 1)

    def test_bill_list_view_filters_by_tenant(self):
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/bills/")
        body = r.content.decode("utf-8", errors="replace")
        self.assertIn("ACME-VEND", body)
        self.assertNotIn("BETA-VEND", body)

    def test_bill_number_unique_within_tenant_but_not_across(self):
        b_a = SupplierBill.objects.filter(tenant=self.acme).first()
        b_b = SupplierBill.objects.filter(tenant=self.beta).first()
        b_a.number = "BILL/00001"
        b_a.save()
        b_b.number = "BILL/00001"
        b_b.save()  # Should succeed — per-tenant uniqueness
        self.assertEqual(b_a.number, b_b.number)
