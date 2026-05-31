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
    BankStatement,
    BankStatementLine,
    Currency,
    CustomerInvoice,
    CustomerInvoiceLine,
    FxRate,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Partner,
    Period,
    PeriodLock,
    SupplierBill,
    SupplierBillLine,
    parse_bank_csv,
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


class JournalEntryAdminSaveTests(TestCase):
    """Regression: saving a JournalEntry with inline lines through the
    admin must propagate tenant_id to the lines. Without that propagation,
    the NOT NULL constraint on JournalEntryLine.tenant raises a
    ValidationError and the page 500s."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_je", "a@a.test", "x")
        cls.tenant = _make_tenant("je-co", currency=cls.xaf, owner=cls.alice)
        Membership.objects.create(user=cls.alice, tenant=cls.tenant, role="owner")
        cls.acct_cash = Account.objects.create(
            tenant=cls.tenant, code="571000", name="Cash on hand", type="asset_cash",
        )
        cls.acct_exp = Account.objects.create(
            tenant=cls.tenant, code="618000", name="Misc expense", type="expense",
        )
        cls.j_gen = Journal.objects.create(
            tenant=cls.tenant, code="OD", name="Misc", type="general",
            sequence_prefix="OD/", next_sequence=1,
        )
        cls.j_sale = Journal.objects.create(
            tenant=cls.tenant, code="VEN", name="Sales", type="sale",
            sequence_prefix="VEN/", next_sequence=1,
        )
        cls.j_purchase = Journal.objects.create(
            tenant=cls.tenant, code="ACH", name="Purchases", type="purchase",
            sequence_prefix="ACH/", next_sequence=1,
        )

    def _post_form(self, journal_id, *, lines=None):
        """POST the admin add-form with two balanced inline lines.
        Returns the raw HttpResponse so tests can inspect status + content."""
        lines = lines or [
            ("acct_cash", "100", "0"),
            ("acct_exp", "0", "100"),
        ]
        data = {
            "journal": str(journal_id),
            "date": "2026-05-17",
            "ref": "TEST",
            "notes": "",
            "state": "draft",
            "name": "",
            "posted_at_0": "",
            "posted_at_1": "",
            "lines-TOTAL_FORMS": "2",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        for i, (acct_attr, debit, credit) in enumerate(lines):
            acct = getattr(self, acct_attr)
            data[f"lines-{i}-account"] = str(acct.id)
            data[f"lines-{i}-partner"] = ""
            data[f"lines-{i}-name"] = f"line-{i}"
            data[f"lines-{i}-debit"] = debit
            data[f"lines-{i}-credit"] = credit
        return self.client.post("/admin/accounting/journalentry/add/", data)

    def test_save_does_not_500_with_balanced_lines(self):
        """The crucial regression: was 500ing with ValidationError on tenant."""
        self.client.force_login(self.alice)
        r = self._post_form(self.j_gen.id)
        # On success Django admin redirects (302) to the changelist;
        # on validation error it re-renders the form (200). Anything 5xx
        # means we regressed.
        self.assertLess(r.status_code, 500,
                        msg=f"JE admin save 500'd (got {r.status_code})")

    def test_saved_lines_inherit_tenant_from_parent(self):
        self.client.force_login(self.alice)
        self._post_form(self.j_gen.id)
        je = JournalEntry.objects.filter(tenant=self.tenant).first()
        self.assertIsNotNone(je)
        # Both inline lines should carry the parent's tenant_id
        line_tenants = set(je.lines.values_list("tenant_id", flat=True))
        self.assertEqual(line_tenants, {self.tenant.id})

    def test_journal_selector_excludes_sale_and_purchase(self):
        """Manual JEs should not be allowed on the sale/purchase journals
        (which are auto-fed by CustomerInvoice / SupplierBill respectively)."""
        self.client.force_login(self.alice)
        r = self.client.get("/admin/accounting/journalentry/add/")
        body = r.content.decode("utf-8", errors="replace")
        # The OD (general) journal should be present in the journal dropdown
        self.assertIn("OD", body)
        # The sale/purchase journals must NOT be in the journal dropdown
        # (we look for them inside the <select id="id_journal">...</select>)
        import re
        match = re.search(r'<select[^>]*name="journal"[^>]*>(.*?)</select>',
                          body, re.DOTALL)
        self.assertIsNotNone(match, "Journal selector not found on add form")
        journal_options = match.group(1)
        self.assertNotIn("VEN", journal_options,
                         "Sales journal must not appear in manual-JE dropdown")
        self.assertNotIn("ACH", journal_options,
                         "Purchase journal must not appear in manual-JE dropdown")


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


# ---------------------------------------------------------------------------
# Phase 1.3 — Bank reconciliation
# ---------------------------------------------------------------------------


class BankCSVParseTests(TestCase):
    """The CSV parser handles the formats real banks emit."""

    def test_parses_simple_csv(self):
        csv = (
            "date,description,amount,reference\n"
            "2026-05-10,Wire from ACME,1073250,WIRE-001\n"
            "2026-05-11,Bank fee,-2500,\n"
        )
        rows = parse_bank_csv(csv)
        self.assertEqual(len(rows), 2)
        ok = [r for r, e in rows if e is None]
        self.assertEqual(len(ok), 2)
        self.assertEqual(ok[0]["transaction_date"], date(2026, 5, 10))
        self.assertEqual(ok[0]["amount"], Decimal("1073250"))
        self.assertEqual(ok[0]["reference"], "WIRE-001")
        self.assertEqual(ok[1]["amount"], Decimal("-2500"))

    def test_parses_semicolon_delimiter(self):
        csv = (
            "Date;Description;Amount\n"
            "10/05/2026;Wire in;1 234 567,89\n"
        )
        rows = parse_bank_csv(csv)
        ok = [r for r, e in rows if e is None]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["transaction_date"], date(2026, 5, 10))
        # 1 234 567,89 → spaces stripped, comma → dot → 1234567.89
        self.assertEqual(ok[0]["amount"], Decimal("1234567.89"))

    def test_parses_french_columns(self):
        csv = (
            "Date;Libellé;Montant;Référence\n"
            "15-05-2026;Virement;500.00;VIR-42\n"
        )
        rows = parse_bank_csv(csv)
        ok = [r for r, e in rows if e is None]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["transaction_date"], date(2026, 5, 15))
        self.assertEqual(ok[0]["amount"], Decimal("500.00"))
        self.assertEqual(ok[0]["reference"], "VIR-42")

    def test_rejects_missing_required_columns(self):
        csv = "foo,bar\n1,2\n"
        with self.assertRaises(ValueError):
            parse_bank_csv(csv)

    def test_collects_per_row_errors(self):
        csv = (
            "date,description,amount\n"
            "2026-05-10,OK,100\n"
            "not-a-date,BAD,200\n"
        )
        rows = parse_bank_csv(csv)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0][1])
        self.assertIsNotNone(rows[1][1])
        self.assertIn("date", rows[1][1].lower())


class BankReconciliationTests(TestCase):
    """End-to-end: import, candidate-match, manual match, unmatch."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_bank", "a@a.test", "x")
        cls.tenant = _make_tenant("bank-co", currency=cls.xaf, owner=cls.alice)
        Membership.objects.create(user=cls.alice, tenant=cls.tenant, role="owner")

        cls.acct_bank = Account.objects.create(
            tenant=cls.tenant, code="521000", name="Bank A/C",
            type="asset_cash", reconcile=True,
        )
        cls.acct_ar = Account.objects.create(
            tenant=cls.tenant, code="411100", name="Customers",
            type="receivable", reconcile=True,
        )
        cls.acct_exp = Account.objects.create(
            tenant=cls.tenant, code="627000", name="Bank fees", type="expense",
        )
        cls.partner = Partner.objects.create(
            tenant=cls.tenant, name="Sample Customer", partner_type="customer",
        )
        cls.bank = Journal.objects.create(
            tenant=cls.tenant, code="BNK", name="Bank", type="bank",
            default_account=cls.acct_bank,
            sequence_prefix="BNK/", next_sequence=1,
        )
        cls.gen = Journal.objects.create(
            tenant=cls.tenant, code="OD", name="Misc", type="general",
            sequence_prefix="OD/", next_sequence=1,
        )

    def _make_statement(self):
        return BankStatement.objects.create(
            tenant=self.tenant, journal=self.bank,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
            opening_balance=Decimal("0"), closing_balance=Decimal("100000"),
            imported_by=self.alice,
        )

    def _post_je(self, amount_to_bank, *, when=date(2026, 5, 10)):
        """Post a JE that has a bank-side line (debit if amount_to_bank > 0).
        The non-bank side uses receivable (which requires a partner) or
        expense (which doesn't), depending on direction."""
        je = JournalEntry.objects.create(
            tenant=self.tenant, journal=self.gen, date=when,
            ref=f"test JE for {amount_to_bank}",
        )
        if amount_to_bank > 0:
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=je, account=self.acct_bank,
                debit=amount_to_bank, credit=Decimal("0"),
            )
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=je, account=self.acct_ar,
                partner=self.partner,
                debit=Decimal("0"), credit=amount_to_bank,
            )
        else:
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=je, account=self.acct_bank,
                debit=Decimal("0"), credit=abs(amount_to_bank),
            )
            JournalEntryLine.objects.create(
                tenant=self.tenant, entry=je, account=self.acct_exp,
                debit=abs(amount_to_bank), credit=Decimal("0"),
            )
        je.post()
        return je

    # --- candidates -------------------------------------------------------

    def test_candidates_inflow_finds_matching_debit(self):
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire in", amount=Decimal("50000"),
        )
        cands = list(line.candidate_entry_lines())
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].debit, Decimal("50000"))

    def test_candidates_outflow_finds_matching_credit(self):
        je = self._post_je(Decimal("-2500"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 10),
            description="Bank fee", amount=Decimal("-2500"),
        )
        cands = list(line.candidate_entry_lines())
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].credit, Decimal("2500"))

    def test_candidates_excludes_lines_outside_date_window(self):
        self._post_je(Decimal("50000"), when=date(2026, 4, 1))  # too old
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Late wire", amount=Decimal("50000"),
        )
        self.assertEqual(line.candidate_entry_lines().count(), 0)

    def test_candidates_excludes_unposted_entries(self):
        je = JournalEntry.objects.create(
            tenant=self.tenant, journal=self.gen, date=date(2026, 5, 10),
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=je, account=self.acct_bank,
            debit=Decimal("50000"), credit=Decimal("0"),
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=je, account=self.acct_ar,
            partner=self.partner,
            debit=Decimal("0"), credit=Decimal("50000"),
        )
        # NOT posted — still draft
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        self.assertEqual(line.candidate_entry_lines().count(), 0)

    # --- match_to() --------------------------------------------------------

    def test_match_to_succeeds_and_updates_statement_state(self):
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        target = je.lines.get(account=self.acct_bank)
        line.match_to(target, user=self.alice)
        line.refresh_from_db()
        stmt.refresh_from_db()
        self.assertEqual(line.state, "matched")
        self.assertEqual(line.matched_entry_line_id, target.id)
        self.assertEqual(stmt.state, "reconciled")  # only 1 line, fully done

    def test_match_to_rejects_wrong_account(self):
        from django.core.exceptions import ValidationError
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        # AR side of the JE is on a different account
        ar_line = je.lines.get(account=self.acct_ar)
        with self.assertRaises(ValidationError):
            line.match_to(ar_line, user=self.alice)

    def test_match_to_rejects_amount_mismatch(self):
        from django.core.exceptions import ValidationError
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("60000"),  # different
        )
        target = je.lines.get(account=self.acct_bank)
        with self.assertRaises(ValidationError):
            line.match_to(target, user=self.alice)

    def test_match_to_prevents_double_match(self):
        from django.core.exceptions import ValidationError
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        l1 = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire 1", amount=Decimal("50000"),
        )
        l2 = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=20,
            transaction_date=date(2026, 5, 12),
            description="Wire 2", amount=Decimal("50000"),
        )
        target = je.lines.get(account=self.acct_bank)
        l1.match_to(target, user=self.alice)
        with self.assertRaises(ValidationError):
            l2.match_to(target, user=self.alice)

    def test_unmatch_reverses_match(self):
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        target = je.lines.get(account=self.acct_bank)
        line.match_to(target, user=self.alice)
        line.unmatch()
        line.refresh_from_db()
        self.assertEqual(line.state, "unmatched")
        self.assertIsNone(line.matched_entry_line)

    # --- statement state tracking -----------------------------------------

    def test_partial_match_sets_in_progress(self):
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=20,
            transaction_date=date(2026, 5, 12),
            description="Other", amount=Decimal("-1000"),
        )
        # Match the first line only
        target = je.lines.get(account=self.acct_bank)
        stmt.lines.first().match_to(target, user=self.alice)
        stmt.refresh_from_db()
        self.assertEqual(stmt.state, "in_progress")

    # --- views ------------------------------------------------------------

    def test_statement_list_view_renders(self):
        self._make_statement()
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/bank/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["statements"]), 1)

    def test_statement_detail_view_renders(self):
        stmt = self._make_statement()
        BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Some wire", amount=Decimal("50000"),
        )
        self.client.force_login(self.alice)
        r = self.client.get(f"/accounting/bank/{stmt.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Some wire")

    def test_import_view_creates_statement_and_lines(self):
        self.client.force_login(self.alice)
        csv = (
            "date,description,amount\n"
            "2026-05-10,Wire in,50000\n"
            "2026-05-11,Bank fee,-2500\n"
        )
        r = self.client.post("/accounting/bank/import/", {
            "journal_id": self.bank.id,
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "opening_balance": "0",
            "closing_balance": "47500",
            "csv_text": csv,
        })
        self.assertEqual(r.status_code, 302)
        stmt = BankStatement.objects.for_tenant(self.tenant).first()
        self.assertEqual(stmt.lines.count(), 2)
        self.assertEqual(stmt.computed_closing, Decimal("47500"))

    def test_match_view_get_lists_candidates(self):
        self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        self.client.force_login(self.alice)
        r = self.client.get(f"/accounting/bank/line/{line.id}/match/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["candidates"]), 1)

    def test_match_view_post_performs_match(self):
        je = self._post_je(Decimal("50000"))
        stmt = self._make_statement()
        line = BankStatementLine.objects.create(
            tenant=self.tenant, statement=stmt, sequence=10,
            transaction_date=date(2026, 5, 11),
            description="Wire", amount=Decimal("50000"),
        )
        target = je.lines.get(account=self.acct_bank)
        self.client.force_login(self.alice)
        r = self.client.post(
            f"/accounting/bank/line/{line.id}/match/",
            {"entry_line_id": target.id},
        )
        self.assertEqual(r.status_code, 302)
        line.refresh_from_db()
        self.assertEqual(line.state, "matched")


class BankStatementTenantIsolationTests(TestCase):
    """Bank statements are tenant-scoped."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.alice = User.objects.create_superuser("alice_bx", "a@a.test", "x")
        cls.bob = User.objects.create_superuser("bob_bx", "b@b.test", "x")
        cls.acme = _make_tenant("acme-bx", currency=cls.xaf, owner=cls.alice)
        cls.beta = _make_tenant("beta-bx", currency=cls.xaf, owner=cls.bob)
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        cls.j_a = Journal.objects.create(tenant=cls.acme, code="BNK", name="Bank", type="bank")
        cls.j_b = Journal.objects.create(tenant=cls.beta, code="BNK", name="Bank", type="bank")
        BankStatement.objects.create(
            tenant=cls.acme, journal=cls.j_a,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        )
        BankStatement.objects.create(
            tenant=cls.beta, journal=cls.j_b,
            period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        )

    def test_for_tenant_filters(self):
        self.assertEqual(BankStatement.objects.for_tenant(self.acme).count(), 1)
        self.assertEqual(BankStatement.objects.for_tenant(self.beta).count(), 1)

    def test_list_view_filters_by_tenant(self):
        self.client.force_login(self.alice)
        r = self.client.get("/accounting/bank/")
        self.assertEqual(len(r.context["statements"]), 1)
        self.assertEqual(r.context["statements"][0].tenant, self.acme)


# ---------------------------------------------------------------------------
# Step 4 — ensure_admin management command (R4)
# ---------------------------------------------------------------------------


class EnsureAdminCommandTests(TestCase):
    """The `ensure_admin` management command must keep R4 honest:
    `admin / admin` always exists, idempotently, on every environment.

    Note: the ``post_migrate`` signal in ``accounting.apps`` already runs
    this command during test-DB setup, so an ``admin`` user is present
    before any test method runs. Each test starts by clearing it so we
    can validate the command in isolation."""

    def setUp(self):
        # Clean slate per test — kill the admin the post_migrate signal
        # planted, so each test method exercises the command from a known
        # starting state.
        get_user_model().objects.filter(username__in=('admin', 'root')).delete()

    def _run(self, **env_overrides):
        """Invoke the command with optional env-var overrides; return user."""
        from django.core.management import call_command
        import os
        # Capture and restore env so tests don't bleed
        keys = ('EA_ADMIN_USERNAME', 'EA_ADMIN_PASSWORD', 'EA_ADMIN_EMAIL',
                'EA_ADMIN_SKIP')
        original = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ.pop(k, None)
            for k, v in env_overrides.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            call_command('ensure_admin', quiet=True)
        finally:
            for k, v in original.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_creates_admin_user_if_missing(self):
        User = get_user_model()
        self.assertFalse(User.objects.filter(username='admin').exists())
        self._run()
        u = User.objects.get(username='admin')
        self.assertTrue(u.is_superuser)
        self.assertTrue(u.is_staff)
        self.assertTrue(u.is_active)
        self.assertTrue(u.check_password('admin'))

    def test_idempotent_does_not_duplicate(self):
        self._run()
        self._run()
        self._run()
        User = get_user_model()
        self.assertEqual(User.objects.filter(username='admin').count(), 1)

    def test_refreshes_password_on_existing_user(self):
        User = get_user_model()
        u = User.objects.create_user('admin', 'old@x.test', 'old-password')
        u.is_superuser = False  # deliberately demoted
        u.save()
        self._run()
        u.refresh_from_db()
        self.assertTrue(u.is_superuser)  # promoted back
        self.assertTrue(u.is_staff)
        self.assertTrue(u.check_password('admin'))
        self.assertFalse(u.check_password('old-password'))

    def test_env_var_password_override(self):
        self._run(EA_ADMIN_PASSWORD='s3cret-prod-pw')
        u = get_user_model().objects.get(username='admin')
        self.assertTrue(u.check_password('s3cret-prod-pw'))
        self.assertFalse(u.check_password('admin'))

    def test_env_var_username_override(self):
        self._run(EA_ADMIN_USERNAME='root', EA_ADMIN_PASSWORD='root')
        User = get_user_model()
        self.assertTrue(User.objects.filter(username='root').exists())
        u = User.objects.get(username='root')
        self.assertTrue(u.check_password('root'))

    def test_skip_flag_is_noop(self):
        self._run(EA_ADMIN_SKIP='1')
        self.assertFalse(get_user_model().objects.filter(username='admin').exists())

    def test_post_migrate_signal_calls_command(self):
        """post_migrate fires during test-DB setup (via setUpTestData / fixtures
        not even needed) — but to validate the wiring without that subtlety
        we just confirm signal handler is connected and callable."""
        from django.db.models.signals import post_migrate
        from accounting.apps import _run_ensure_admin
        receivers = [
            r[1]() for r in post_migrate.receivers if r[1]() is not None
        ]
        # If our handler is registered, it's referenced somewhere in receivers.
        self.assertIn(_run_ensure_admin, receivers)


# ---------------------------------------------------------------------------
# Step 6 — Period, PeriodLock, FxRate
# ---------------------------------------------------------------------------


class PeriodTests(TestCase):
    """Period model + state machine + lock/unlock events."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_p", "a@a.test", "x")
        cls.bob = User.objects.create_superuser("bob_p", "b@b.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("period-co", currency=cls.xaf, owner=cls.alice)
        Membership.objects.create(user=cls.alice, tenant=cls.tenant, role="owner")

    def _make_period(self, code="2026-05",
                     start=date(2026, 5, 1), end=date(2026, 5, 31)):
        return Period.objects.create(
            tenant=self.tenant, code=code, start_date=start, end_date=end,
        )

    def test_create_period_default_state_is_open(self):
        p = self._make_period()
        self.assertEqual(p.state, "open")
        self.assertFalse(p.is_closed)
        self.assertIsNone(p.closed_at)

    def test_end_before_start_raises(self):
        from django.core.exceptions import ValidationError
        p = Period(
            tenant=self.tenant, code="bad",
            start_date=date(2026, 5, 31), end_date=date(2026, 5, 1),
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_lock_transitions_state_and_records_event(self):
        p = self._make_period()
        p.lock(user=self.alice, reason="Month closed")
        p.refresh_from_db()
        self.assertEqual(p.state, "closed")
        self.assertTrue(p.is_closed)
        self.assertIsNotNone(p.closed_at)
        ev = p.latest_lock_event
        self.assertEqual(ev.action, "lock")
        self.assertEqual(ev.acted_by, self.alice)
        self.assertEqual(ev.reason, "Month closed")

    def test_unlock_transitions_and_records_event(self):
        p = self._make_period()
        p.lock(user=self.alice, reason="oops")
        p.unlock(user=self.bob, reason="reopen for late JE")
        p.refresh_from_db()
        self.assertEqual(p.state, "open")
        self.assertIsNone(p.closed_at)
        # Two events recorded: lock then unlock
        events = list(p.lock_events.order_by("acted_at").values_list("action", flat=True))
        self.assertEqual(events, ["lock", "unlock"])

    def test_lock_is_idempotent(self):
        p = self._make_period()
        p.lock(user=self.alice)
        p.lock(user=self.alice)  # second call should be a no-op
        self.assertEqual(p.lock_events.count(), 1)

    def test_unlock_idempotent_on_open(self):
        p = self._make_period()
        p.unlock(user=self.alice)
        self.assertEqual(p.lock_events.count(), 0)

    def test_start_close_blocks_on_closed_period(self):
        from django.core.exceptions import ValidationError
        p = self._make_period()
        p.lock(user=self.alice)
        with self.assertRaises(ValidationError):
            p.start_close(user=self.alice)

    def test_period_code_unique_within_tenant(self):
        self._make_period()
        with self.assertRaises(Exception):  # IntegrityError under SQLite
            self._make_period()

    def test_tenant_isolation_via_manager(self):
        other_user = get_user_model().objects.create_user("ozzy", "o@o.test", "x")
        other = _make_tenant("other-co", currency=self.xaf, owner=other_user)
        self._make_period()  # belongs to self.tenant
        Period.objects.create(
            tenant=other, code="2026-05",
            start_date=date(2026, 5, 1), end_date=date(2026, 5, 31),
        )
        self.assertEqual(Period.objects.for_tenant(self.tenant).count(), 1)
        self.assertEqual(Period.objects.for_tenant(other).count(), 1)


class FxRateTests(TestCase):
    """Global FX time-series."""

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.eur = Currency.objects.create(code="EUR", name="Euro", decimal_places=2)
        cls.usd = Currency.objects.create(code="USD", name="US Dollar", decimal_places=2)

    def test_create_fixing(self):
        rate = FxRate.objects.create(
            base_currency=self.eur, quote_currency=self.xaf,
            fixing_date=date(2026, 5, 30), rate=Decimal("655.957"),
            source="BCEAO",
        )
        self.assertEqual(str(rate),
                         f"EUR/XAF @ 2026-05-30 = 655.957")

    def test_same_currency_pair_raises(self):
        from django.core.exceptions import ValidationError
        rate = FxRate(
            base_currency=self.eur, quote_currency=self.eur,
            fixing_date=date(2026, 5, 30), rate=Decimal("1"),
        )
        with self.assertRaises(ValidationError):
            rate.full_clean()

    def test_zero_or_negative_rate_raises(self):
        from django.core.exceptions import ValidationError
        rate = FxRate(
            base_currency=self.eur, quote_currency=self.xaf,
            fixing_date=date(2026, 5, 30), rate=Decimal("0"),
        )
        with self.assertRaises(ValidationError):
            rate.full_clean()

    def test_unique_constraint_pair_per_date(self):
        FxRate.objects.create(
            base_currency=self.eur, quote_currency=self.xaf,
            fixing_date=date(2026, 5, 30), rate=Decimal("655.957"),
        )
        with self.assertRaises(Exception):  # IntegrityError
            FxRate.objects.create(
                base_currency=self.eur, quote_currency=self.xaf,
                fixing_date=date(2026, 5, 30), rate=Decimal("656"),
            )

    def test_get_rate_returns_latest_on_or_before(self):
        FxRate.objects.create(
            base_currency=self.eur, quote_currency=self.xaf,
            fixing_date=date(2026, 5, 28), rate=Decimal("655.5"),
        )
        FxRate.objects.create(
            base_currency=self.eur, quote_currency=self.xaf,
            fixing_date=date(2026, 5, 30), rate=Decimal("656.0"),
        )
        # On the later date → latest
        self.assertEqual(
            FxRate.get_rate(self.eur, self.xaf, date(2026, 5, 31)),
            Decimal("656.0"),
        )
        # On an earlier date → the earlier fixing
        self.assertEqual(
            FxRate.get_rate(self.eur, self.xaf, date(2026, 5, 29)),
            Decimal("655.5"),
        )
        # Before any fixing → None
        self.assertIsNone(
            FxRate.get_rate(self.eur, self.xaf, date(2026, 5, 1))
        )

    def test_get_rate_identity_for_same_currency(self):
        self.assertEqual(
            FxRate.get_rate(self.eur, self.eur, date(2026, 5, 30)),
            Decimal("1"),
        )
