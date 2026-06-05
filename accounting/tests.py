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
    AgentRun,
    AgentToolCall,
    BankStatement,
    BankStatementLine,
    Currency,
    CustomerInvoice,
    CustomerInvoiceLine,
    ERPConnection,
    ERPOperation,
    FxRate,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Partner,
    Period,
    PeriodLock,
    Provenance,
    SupplierBill,
    SupplierBillLine,
    parse_bank_csv,
    Tenant,
    TenantDepartmentSubscription,
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


# ---------------------------------------------------------------------------
# Step 7 — Provenance / AgentRun / AgentToolCall / ERPConnection / ERPOperation
# ---------------------------------------------------------------------------


class AgentRunModelTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_ar", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("agent-co", currency=cls.xaf, owner=cls.alice)
        Membership.objects.create(user=cls.alice, tenant=cls.tenant, role="owner")

    def test_create_run_default_status(self):
        r = AgentRun.objects.create(
            tenant=self.tenant, department="D01", task="classify_vendor_bill",
        )
        self.assertEqual(r.status, "pending")
        self.assertEqual(r.input_tokens, 0)
        self.assertEqual(r.output_tokens, 0)
        self.assertEqual(r.total_tokens, 0)
        self.assertIsNone(r.duration_seconds)

    def test_total_tokens(self):
        r = AgentRun.objects.create(
            tenant=self.tenant, department="D06", task="compute_vat",
            input_tokens=1500, output_tokens=320,
        )
        self.assertEqual(r.total_tokens, 1820)

    def test_chain_id_links_runs(self):
        chain = "doc-abc-123"
        AgentRun.objects.create(
            tenant=self.tenant, department="dispatcher", task="route",
            chain_id=chain,
        )
        AgentRun.objects.create(
            tenant=self.tenant, department="D01", task="propose_bill",
            chain_id=chain,
        )
        AgentRun.objects.create(
            tenant=self.tenant, department="D06", task="vat_impact",
            chain_id=chain,
        )
        related = AgentRun.objects.for_tenant(self.tenant).filter(chain_id=chain)
        self.assertEqual(related.count(), 3)
        self.assertEqual(
            sorted(related.values_list("department", flat=True)),
            ["D01", "D06", "dispatcher"],
        )

    def test_string_repr(self):
        r = AgentRun.objects.create(
            tenant=self.tenant, department="D04", task="depreciate",
            status="executed",
        )
        self.assertIn("D04", str(r))
        self.assertIn("depreciate", str(r))
        self.assertIn("executed", str(r))


class AgentToolCallTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_tc", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("tool-co", currency=cls.xaf, owner=cls.alice)
        cls.agent_run = AgentRun.objects.create(
            tenant=cls.tenant, department="D01", task="post_bill",
        )

    def test_create_tool_call(self):
        tc = AgentToolCall.objects.create(
            tenant=self.tenant, agent_run=self.agent_run, sequence=0,
            tool="erp.post_je",
            arguments={"date": "2026-05-31", "lines": [{"debit": 100}]},
            status="success",
            result={"je_id": 42},
        )
        self.assertEqual(tc.tool, "erp.post_je")
        self.assertEqual(tc.arguments["lines"][0]["debit"], 100)
        self.assertEqual(tc.result["je_id"], 42)

    def test_ordering_by_sequence_within_run(self):
        for i, tool in enumerate(["knowledge.retrieve", "chart.lookup",
                                   "erp.post_je"]):
            AgentToolCall.objects.create(
                tenant=self.tenant, agent_run=self.agent_run, sequence=i, tool=tool,
            )
        ordered = list(
            self.agent_run.tool_calls.order_by("sequence").values_list("tool", flat=True)
        )
        self.assertEqual(ordered, ["knowledge.retrieve", "chart.lookup",
                                    "erp.post_je"])


class ERPConnectionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_erp", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("erp-co", currency=cls.xaf, owner=cls.alice)

    def test_create_connection_default_health(self):
        c = ERPConnection.objects.create(
            tenant=self.tenant, name="Prod Odoo", vendor="odoo", version="17.0",
            config={"url": "https://odoo.example.com",
                    "auth_env_var": "ODOO_API_KEY"},
        )
        self.assertEqual(c.health, "unconfigured")
        self.assertTrue(c.is_active)
        self.assertFalse(c.is_primary)
        self.assertEqual(c.capabilities, [])

    def test_capabilities_list(self):
        c = ERPConnection.objects.create(
            tenant=self.tenant, name="Sandbox", vendor="odoo",
            capabilities=["CAP.01", "CAP.02", "CAP.03"],
        )
        self.assertIn("CAP.03", c.capabilities)


class ERPOperationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_eo", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("op-co", currency=cls.xaf, owner=cls.alice)
        cls.conn = ERPConnection.objects.create(
            tenant=cls.tenant, name="Odoo", vendor="odoo", version="17.0",
        )

    def test_create_operation(self):
        op = ERPOperation.objects.create(
            tenant=self.tenant, connection=self.conn,
            capability="CAP.03", method="account.move.create",
            request={"date": "2026-05-31"},
            response={"id": 1234},
            external_ids={"move_id": 1234},
            status="success",
        )
        self.assertEqual(op.external_ids["move_id"], 1234)
        self.assertEqual(op.status, "success")
        self.assertEqual(op.retry_count, 0)

    def test_capability_filtering(self):
        ERPOperation.objects.create(
            tenant=self.tenant, connection=self.conn,
            capability="CAP.01", status="success",
        )
        ERPOperation.objects.create(
            tenant=self.tenant, connection=self.conn,
            capability="CAP.03", status="success",
        )
        ERPOperation.objects.create(
            tenant=self.tenant, connection=self.conn,
            capability="CAP.03", status="failed",
        )
        cap03 = ERPOperation.objects.filter(tenant=self.tenant, capability="CAP.03")
        self.assertEqual(cap03.count(), 2)
        self.assertEqual(
            cap03.filter(status="success").count(), 1
        )


class ProvenanceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_pv", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("prov-co", currency=cls.xaf, owner=cls.alice)
        cls.agent_run = AgentRun.objects.create(
            tenant=cls.tenant, department="D01", task="post_bill", status="executed",
        )

    def test_create_provenance_with_agent_run(self):
        pv = Provenance.objects.create(
            tenant=self.tenant, source="agent", agent_run=self.agent_run,
            chain_id="doc-xyz",
            summary="Posted vendor bill for KISSYWEARS, 50 000 XAF",
            citations=["K01:412300", "K11:art-149"],
            extra={"vendor": "KISSYWEARS"},
        )
        self.assertEqual(pv.source, "agent")
        self.assertEqual(pv.citations, ["K01:412300", "K11:art-149"])
        self.assertEqual(pv.extra["vendor"], "KISSYWEARS")
        self.assertEqual(pv.agent_run, self.agent_run)

    def test_chain_id_groups_provenance(self):
        chain = "doc-abc-123"
        Provenance.objects.create(
            tenant=self.tenant, source="agent", chain_id=chain,
            summary="Bill classified",
        )
        Provenance.objects.create(
            tenant=self.tenant, source="agent", chain_id=chain,
            summary="Bill posted",
        )
        Provenance.objects.create(
            tenant=self.tenant, source="system", chain_id=chain,
            summary="VAT impact updated",
        )
        related = Provenance.objects.for_tenant(self.tenant).filter(chain_id=chain)
        self.assertEqual(related.count(), 3)
        # Confirm all 3 source types come back
        sources = sorted(set(related.values_list("source", flat=True)))
        self.assertEqual(sources, ["agent", "system"])

    def test_tenant_isolation(self):
        other_user = get_user_model().objects.create_user("ozzy_pv", "o@o.test", "x")
        other = _make_tenant("other-pv", currency=self.xaf, owner=other_user)
        Provenance.objects.create(
            tenant=self.tenant, source="manual", summary="ACME-OWN",
        )
        Provenance.objects.create(
            tenant=other, source="manual", summary="OTHER-OWN",
        )
        self.assertEqual(
            list(Provenance.objects.for_tenant(self.tenant)
                 .values_list("summary", flat=True)),
            ["ACME-OWN"],
        )
        self.assertEqual(
            list(Provenance.objects.for_tenant(other)
                 .values_list("summary", flat=True)),
            ["OTHER-OWN"],
        )


# ---------------------------------------------------------------------------
# Step 8 — Tenant.accounting_framework / agent_enabled / dept subscriptions
# ---------------------------------------------------------------------------


class TenantV2FieldsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_t8", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("t8-co", currency=cls.xaf, owner=cls.alice)

    def test_default_framework_is_syscohada(self):
        self.assertEqual(self.tenant.accounting_framework, "SYSCOHADA-2017")

    def test_agent_disabled_by_default(self):
        # Safety: agents must be opt-in, never auto-acting until the tenant
        # explicitly enables.
        self.assertFalse(self.tenant.agent_enabled)

    def test_framework_can_be_switched(self):
        self.tenant.accounting_framework = "IFRS"
        self.tenant.save()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.accounting_framework, "IFRS")


class TenantDepartmentSubscriptionTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_sub", "a@a.test", "x")
        cls.clerk = User.objects.create_user("ap_clerk", "ap@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("sub-co", currency=cls.xaf, owner=cls.alice)
        cls.other = _make_tenant("other-sub", currency=cls.xaf, owner=cls.alice)

    def test_subscribe_single_department(self):
        # "I only want an AP accountant"
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01",
        )
        self.assertEqual(self.tenant.subscribed_departments(), ["D01"])
        self.assertTrue(self.tenant.is_subscribed("D01"))
        self.assertFalse(self.tenant.is_subscribed("D02"))

    def test_subscribe_multiple_departments(self):
        for code in ("D01", "D02", "D06"):
            TenantDepartmentSubscription.objects.create(
                tenant=self.tenant, department=code,
            )
        self.assertEqual(
            sorted(self.tenant.subscribed_departments()),
            ["D01", "D02", "D06"],
        )

    def test_inactive_subscription_excluded(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01", active=True,
        )
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D02", active=False,
        )
        self.assertEqual(self.tenant.subscribed_departments(), ["D01"])
        self.assertFalse(self.tenant.is_subscribed("D02"))

    def test_unique_subscription_per_tenant_department(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01",
        )
        with self.assertRaises(Exception):  # IntegrityError
            TenantDepartmentSubscription.objects.create(
                tenant=self.tenant, department="D01",
            )

    def test_same_department_different_tenants_allowed(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01",
        )
        # Different tenant, same dept — must be allowed
        TenantDepartmentSubscription.objects.create(
            tenant=self.other, department="D01",
        )
        self.assertTrue(self.tenant.is_subscribed("D01"))
        self.assertTrue(self.other.is_subscribed("D01"))

    def test_default_approver_per_department(self):
        sub = TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01", default_approver=self.clerk,
        )
        self.assertEqual(sub.default_approver, self.clerk)

    def test_per_tenant_flexibility_two_tenants_different_depts(self):
        # tenant subscribes to AP only; other subscribes to AR only
        TenantDepartmentSubscription.objects.create(tenant=self.tenant, department="D01")
        TenantDepartmentSubscription.objects.create(tenant=self.other, department="D02")
        self.assertEqual(self.tenant.subscribed_departments(), ["D01"])
        self.assertEqual(self.other.subscribed_departments(), ["D02"])
        self.assertFalse(self.tenant.is_subscribed("D02"))
        self.assertFalse(self.other.is_subscribed("D01"))

    def test_tenant_isolation_via_manager(self):
        TenantDepartmentSubscription.objects.create(tenant=self.tenant, department="D01")
        TenantDepartmentSubscription.objects.create(tenant=self.other, department="D02")
        self.assertEqual(
            TenantDepartmentSubscription.objects.for_tenant(self.tenant).count(), 1)
        self.assertEqual(
            TenantDepartmentSubscription.objects.for_tenant(self.other).count(), 1)


# ---------------------------------------------------------------------------
# Step 9 — backfill Provenance for existing journal entries (data migration)
# ---------------------------------------------------------------------------


class BackfillProvenanceTests(TestCase):
    """Exercises the data-migration functions directly. The test DB runs the
    migration on empty tables (so it creates nothing); here we build a JE and
    call the migration's backfill/remove functions against the live apps
    registry to prove correctness, idempotency, and reversibility."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alice = User.objects.create_superuser("alice_bf", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.tenant = _make_tenant("bf-co", currency=cls.xaf, owner=cls.alice)
        cls.acct1 = Account.objects.create(
            tenant=cls.tenant, code="571000", name="Cash", type="asset_cash",
        )
        cls.acct2 = Account.objects.create(
            tenant=cls.tenant, code="618000", name="Misc expense", type="expense",
        )
        cls.journal = Journal.objects.create(
            tenant=cls.tenant, code="OD", name="Misc", type="general",
        )

    @property
    def mig(self):
        # Imported per-call (not cached on the class) — a module object can't
        # be deep-copied by Django's setUpTestData fixture machinery.
        import importlib
        return importlib.import_module(
            'accounting.migrations.0012_backfill_provenance_for_existing_jes'
        )

    @property
    def apps(self):
        from django.apps import apps as django_apps
        return django_apps

    def setUp(self):
        Provenance.objects.all().delete()

    def _make_je(self, name="OD/00001"):
        je = JournalEntry.objects.create(
            tenant=self.tenant, journal=self.journal, name=name,
            date=date(2026, 5, 31), state="posted",
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=je, account=self.acct1, debit=Decimal("100"),
        )
        JournalEntryLine.objects.create(
            tenant=self.tenant, entry=je, account=self.acct2, credit=Decimal("100"),
        )
        return je

    def test_backfill_creates_one_provenance_per_je(self):
        je1 = self._make_je("OD/00001")
        je2 = self._make_je("OD/00002")
        self.assertEqual(Provenance.objects.count(), 0)
        self.mig.backfill_provenance(self.apps, None)
        self.assertEqual(Provenance.objects.count(), 2)
        for je in (je1, je2):
            pv = Provenance.objects.get(journal_entry=je)
            self.assertEqual(pv.source, "manual")
            self.assertEqual(pv.tenant_id, self.tenant.id)
            self.assertTrue(pv.extra.get("backfilled"))
            self.assertIn(je.name, pv.summary)

    def test_backfill_is_idempotent(self):
        self._make_je("OD/00001")
        self.mig.backfill_provenance(self.apps, None)
        self.mig.backfill_provenance(self.apps, None)
        self.mig.backfill_provenance(self.apps, None)
        self.assertEqual(Provenance.objects.count(), 1)

    def test_backfill_skips_jes_that_already_have_provenance(self):
        je = self._make_je("OD/00001")
        Provenance.objects.create(
            tenant=self.tenant, journal_entry=je, source="agent",
            summary="real agent provenance",
        )
        self.mig.backfill_provenance(self.apps, None)
        self.assertEqual(Provenance.objects.filter(journal_entry=je).count(), 1)
        self.assertEqual(
            Provenance.objects.get(journal_entry=je).source, "agent")

    def test_reverse_removes_only_backfilled_rows(self):
        self._make_je("OD/00001")
        keeper = Provenance.objects.create(
            tenant=self.tenant, source="manual", summary="hand-made, keep me",
            extra={},
        )
        self.mig.backfill_provenance(self.apps, None)
        self.assertEqual(Provenance.objects.count(), 2)
        self.mig.remove_backfilled_provenance(self.apps, None)
        self.assertEqual(Provenance.objects.count(), 1)
        self.assertTrue(Provenance.objects.filter(id=keeper.id).exists())


# ---------------------------------------------------------------------------
# Step 12 — Django-Q2 background task queue (code/config portion)
# ---------------------------------------------------------------------------


class DjangoQConfigTests(TestCase):
    """The Django-Q2 queue is installed and configured with the ORM broker
    (no Redis). The qcluster worker process itself runs as a systemd unit
    on the server (SSH bundle) — these tests cover the code/config only."""

    def test_django_q_installed(self):
        from django.apps import apps
        self.assertTrue(apps.is_installed('django_q'))

    def test_q_cluster_uses_orm_broker(self):
        from django.conf import settings
        self.assertEqual(settings.Q_CLUSTER['orm'], 'default')
        self.assertEqual(settings.Q_CLUSTER['name'], 'ealedgers')

    def test_broker_constructs_with_orm_backend(self):
        from django_q.brokers import get_broker
        broker = get_broker()
        self.assertEqual(type(broker).__name__, 'ORM')
        self.assertEqual(broker.queue_size(), 0)

    def test_queue_tables_exist(self):
        from django_q.models import OrmQ, Task, Schedule
        self.assertEqual(OrmQ.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(Schedule.objects.count(), 0)


# ---------------------------------------------------------------------------
# Post-pivot platform shell (workspace home + department/agent/ERP skeletons)
# ---------------------------------------------------------------------------


class PlatformShellViewTests(TestCase):
    """The reframed shell: home launcher + the read-only platform pages that
    present the virtual-finance-function vision. (The v1 manual UI was retired.)"""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            "shell_u", "s@a.test", "pw123456")
        cls.xaf = Currency.objects.create(
            code="XAF", name="CFA Franc", decimal_places=0)
        cls.acme = Tenant.objects.create(
            slug="shell-acme", name="Shell Acme", currency=cls.xaf, owner=cls.user)
        Membership.objects.create(user=cls.user, tenant=cls.acme, role="owner")

    def setUp(self):
        self.client.force_login(self.user)

    def test_workspace_presents_the_vision(self):
        r = self.client.get("/workspace/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "virtual finance function")
        self.assertContains(r, "Knowledge Base")
        self.assertContains(r, "Departments")
        self.assertContains(r, "ERP Connections")

    def test_departments_lists_all_twelve(self):
        r = self.client.get("/departments/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["n_total"], 12)
        self.assertContains(r, "Accounts Payable")

    def test_departments_marks_subscription(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.acme, department="D01", active=True)
        r = self.client.get("/departments/")
        self.assertEqual(r.context["n_subscribed"], 1)

    def test_agent_activity_empty_state(self):
        r = self.client.get("/agents/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No agent runs yet")

    def test_erp_connections_empty_state(self):
        r = self.client.get("/erp/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No ERP connected yet")

    def test_platform_pages_require_login(self):
        self.client.logout()
        for path in ("/departments/", "/agents/", "/erp/"):
            self.assertEqual(self.client.get(path).status_code, 302)

    def test_platform_pages_require_tenant(self):
        orphan = get_user_model().objects.create_user(
            "shell_orphan", "o2@a.test", "pw123456")
        self.client.force_login(orphan)
        resp = self.client.get("/departments/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/workspace/", resp["Location"])


# ---------------------------------------------------------------------------
# ERP connection config UI + encrypted credentials
# ---------------------------------------------------------------------------

from django.test import SimpleTestCase as _SimpleTestCase  # noqa: E402
from accounting.crypto import encrypt_secret, decrypt_secret  # noqa: E402


class CryptoSecretTests(_SimpleTestCase):
    def test_round_trip(self):
        token = encrypt_secret("super-secret-api-key")
        self.assertNotIn("super-secret", token)          # not plaintext
        self.assertEqual(decrypt_secret(token), "super-secret-api-key")

    def test_empty_is_empty(self):
        self.assertEqual(encrypt_secret(""), "")
        self.assertEqual(decrypt_secret(""), "")

    def test_bad_token_returns_empty(self):
        self.assertEqual(decrypt_secret("not-a-valid-token"), "")


class ERPConnectionCredentialTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        cls.t = Tenant.objects.create(slug="erp-cred", name="ERP Cred", currency=cls.xaf)

    def test_set_get_api_key_encrypted(self):
        c = ERPConnection(tenant=self.t, name="Odoo", vendor="odoo")
        c.set_api_key("abc123")
        self.assertTrue(c.secret_ciphertext)
        self.assertNotIn("abc123", c.secret_ciphertext)   # stored encrypted
        self.assertEqual(c.get_api_key(), "abc123")
        self.assertTrue(c.has_api_key)

    def test_clear_api_key(self):
        c = ERPConnection(tenant=self.t, name="Odoo", vendor="odoo")
        c.set_api_key("x")
        c.set_api_key("")
        self.assertEqual(c.secret_ciphertext, "")
        self.assertFalse(c.has_api_key)

    def test_env_var_fallback(self):
        from unittest import mock
        c = ERPConnection(tenant=self.t, name="Odoo", vendor="odoo",
                          config={"api_key_env": "ODOO_FALLBACK_KEY"})
        with mock.patch.dict("os.environ", {"ODOO_FALLBACK_KEY": "from-env"}):
            self.assertEqual(c.get_api_key(), "from-env")
            self.assertTrue(c.has_api_key)


class ERPConnectionConfigViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            "erp_u", "e@a.test", "pw123456")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        cls.t = Tenant.objects.create(slug="erp-view", name="ERP View", currency=cls.xaf)
        Membership.objects.create(user=cls.user, tenant=cls.t, role="owner")

    def setUp(self):
        self.client.force_login(self.user)

    def test_create_tests_on_save_and_encrypts_key(self):
        # vendor=manual -> NullConnector health-check (no network).
        resp = self.client.post("/erp/new/", {
            "vendor": "manual", "name": "Standalone",
            "base_url": "", "database": "", "api_user": "",
            "api_key": "topsecret",
        })
        self.assertEqual(resp.status_code, 302)
        c = ERPConnection.objects.for_tenant(self.t).get(name="Standalone")
        self.assertIsNotNone(c.last_healthcheck_at)        # tested on save
        self.assertTrue(c.secret_ciphertext)
        self.assertNotIn("topsecret", c.secret_ciphertext)  # encrypted
        self.assertEqual(c.get_api_key(), "topsecret")

    def test_edit_blank_key_keeps_existing(self):
        c = ERPConnection.objects.create(tenant=self.t, name="Odoo", vendor="manual")
        c.set_api_key("keepme")
        c.save()
        resp = self.client.post(f"/erp/{c.pk}/edit/", {
            "vendor": "manual", "name": "Odoo renamed",
            "base_url": "", "database": "", "api_user": "", "api_key": "",
        })
        self.assertEqual(resp.status_code, 302)
        c.refresh_from_db()
        self.assertEqual(c.name, "Odoo renamed")
        self.assertEqual(c.get_api_key(), "keepme")        # kept

    def test_edit_new_key_replaces(self):
        c = ERPConnection.objects.create(tenant=self.t, name="Odoo", vendor="manual")
        c.set_api_key("old")
        c.save()
        self.client.post(f"/erp/{c.pk}/edit/", {
            "vendor": "manual", "name": "Odoo", "base_url": "", "database": "",
            "api_user": "", "api_key": "new",
        })
        c.refresh_from_db()
        self.assertEqual(c.get_api_key(), "new")

    def test_edit_other_tenant_404(self):
        other = Tenant.objects.create(slug="erp-other", name="Other", currency=self.xaf)
        c = ERPConnection.objects.create(tenant=other, name="Theirs", vendor="manual")
        self.assertEqual(self.client.get(f"/erp/{c.pk}/edit/").status_code, 404)

    def test_create_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/erp/new/").status_code, 302)


# ---------------------------------------------------------------------------
# Step 35 — capability matrix + nightly health-check
# ---------------------------------------------------------------------------

from django.core.management import call_command  # noqa: E402


class CapabilityMatrixViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            "cm_u", "c@a.test", "pw123456")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        cls.t = Tenant.objects.create(slug="cm-t", name="CM T", currency=cls.xaf)
        Membership.objects.create(user=cls.user, tenant=cls.t, role="owner")
        cls.conn = ERPConnection.objects.create(
            tenant=cls.t, name="Acme Odoo", vendor="odoo",
            capabilities=["CAP.01", "CAP.17"], health="ok")

    def setUp(self):
        self.client.force_login(self.user)

    def test_matrix_renders_with_caps(self):
        r = self.client.get("/erp/capabilities/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Capability Matrix")
        self.assertContains(r, "CAP.01")
        self.assertContains(r, "CAP.22")          # the whole registry is listed
        self.assertContains(r, "Acme Odoo")
        self.assertContains(r, "2/23")            # supported / total
        self.assertEqual(r.context["total_caps"], 23)

    def test_matrix_supported_flags(self):
        r = self.client.get("/erp/capabilities/")
        rows = {row["cap"].code: row for row in r.context["rows"]}
        self.assertTrue(rows["CAP.01"]["any"])
        self.assertTrue(rows["CAP.17"]["any"])
        self.assertFalse(rows["CAP.03"]["any"])   # not reported by this conn

    def test_cross_tenant_isolation(self):
        other = Tenant.objects.create(
            slug="cm-other", name="Other", currency=self.xaf)
        ERPConnection.objects.create(
            tenant=other, name="Secret ERP", vendor="odoo",
            capabilities=["CAP.01"])
        r = self.client.get("/erp/capabilities/")
        self.assertNotContains(r, "Secret ERP")
        self.assertEqual(len(r.context["conn_rows"]), 1)

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/erp/capabilities/").status_code, 302)


class HealthcheckConnectionsCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        cls.t1 = Tenant.objects.create(slug="hc-one", name="HC One", currency=cls.xaf)
        cls.t2 = Tenant.objects.create(slug="hc-two", name="HC Two", currency=cls.xaf)

    def test_refreshes_health_and_caps(self):
        # vendor=manual -> NullConnector (offline). Stale values get overwritten.
        c = ERPConnection.objects.create(
            tenant=self.t1, name="Standalone", vendor="manual",
            capabilities=["CAP.99"], health="ok")
        call_command("healthcheck_connections")
        c.refresh_from_db()
        self.assertIsNotNone(c.last_healthcheck_at)     # it ran + persisted
        self.assertEqual(c.capabilities, [])            # refreshed (NullConnector)
        self.assertEqual(c.health, "unconfigured")      # refreshed

    def test_tenant_filter_scopes(self):
        c1 = ERPConnection.objects.create(tenant=self.t1, name="A", vendor="manual")
        c2 = ERPConnection.objects.create(tenant=self.t2, name="B", vendor="manual")
        call_command("healthcheck_connections", tenant="hc-one")
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertIsNotNone(c1.last_healthcheck_at)
        self.assertIsNone(c2.last_healthcheck_at)       # out of scope, untouched


# ---------------------------------------------------------------------------
# Step 36 — ERP operation saga (track + retry/backoff + escalate)
# ---------------------------------------------------------------------------

from accounting.saga import (  # noqa: E402
    NonRetryableError, RetryableError, SagaOutcome, compute_backoff,
    run_erp_operation,
)


class SagaBackoffTests(_SimpleTestCase):
    def test_exponential_capped(self):
        self.assertEqual(compute_backoff(1, base=0.5), 0.5)
        self.assertEqual(compute_backoff(2, base=0.5), 1.0)
        self.assertEqual(compute_backoff(3, base=0.5), 2.0)
        self.assertEqual(compute_backoff(10, base=0.5, cap=5.0), 5.0)  # capped


class SagaRunnerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        cls.t = Tenant.objects.create(slug="saga-t", name="Saga T", currency=cls.xaf)
        cls.conn = ERPConnection.objects.create(
            tenant=cls.t, name="Odoo", vendor="manual")

    def _run(self, func, **kw):
        self.slept = []
        return run_erp_operation(
            connection=self.conn, capability="CAP.03", func=func,
            method="account.move.create", request={"x": 1},
            sleep=self.slept.append, **kw)

    def test_success_first_try(self):
        out = self._run(lambda: {"external_id": 99, "state": "posted"})
        self.assertTrue(out.ok)
        self.assertFalse(out.escalated)
        self.assertEqual(out.attempts, 1)
        op = out.operation
        self.assertEqual(op.status, "success")
        self.assertEqual(op.retry_count, 0)
        self.assertEqual(op.external_ids, {"external_id": 99})
        self.assertEqual(op.response["state"], "posted")
        self.assertIsNotNone(op.completed_at)
        self.assertEqual(self.slept, [])               # no backoff on success

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RetryableError("temporary glitch")
            return {"external_id": 7}

        out = self._run(flaky, backoff_base=0.5)
        self.assertTrue(out.ok)
        self.assertEqual(out.attempts, 3)
        self.assertEqual(out.operation.status, "success")
        self.assertEqual(out.operation.retry_count, 2)
        self.assertEqual(self.slept, [0.5, 1.0])       # backoff between retries

    def test_exhausts_then_escalates(self):
        def always_fail():
            raise RetryableError("still down")

        out = self._run(always_fail, max_attempts=3)
        self.assertFalse(out.ok)
        self.assertTrue(out.escalated)
        self.assertEqual(out.attempts, 3)
        op = out.operation
        self.assertEqual(op.status, "escalated")
        self.assertEqual(op.retry_count, 2)
        self.assertIn("still down", op.error)
        self.assertIsNotNone(op.completed_at)
        self.assertEqual(len(self.slept), 2)           # slept between 3 attempts

    def test_non_retryable_fails_fast(self):
        def bad_request():
            raise NonRetryableError("invalid account")

        out = self._run(bad_request)
        self.assertFalse(out.ok)
        self.assertFalse(out.escalated)                # failed, not escalated
        self.assertEqual(out.attempts, 1)
        self.assertEqual(out.operation.status, "failed")
        self.assertEqual(self.slept, [])               # never retried

    def test_creates_audited_operation_row(self):
        out = self._run(lambda: {"ok": True})
        op = ERPOperation.objects.for_tenant(self.t).get(pk=out.operation.pk)
        self.assertEqual(op.capability, "CAP.03")
        self.assertEqual(op.method, "account.move.create")
        self.assertEqual(op.request, {"x": 1})
        self.assertEqual(op.connection, self.conn)


# ---------------------------------------------------------------------------
# Step 37 — drift detector + reconciliation report
# ---------------------------------------------------------------------------

from accounting.reconciliation import (  # noqa: E402
    ReconciliationReport, reconcile_connection,
)


class ReconciliationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        cls.t = Tenant.objects.create(slug="rec-t", name="Rec T", currency=cls.xaf)
        cls.conn = ERPConnection.objects.create(
            tenant=cls.t, name="Odoo", vendor="odoo", health="ok")

    def _op(self, external_id, status="success"):
        return ERPOperation.objects.create(
            tenant=self.t, connection=self.conn, capability="CAP.03",
            status=status,
            external_ids={"external_id": external_id} if external_id else {})

    def test_no_recorded_ops_is_clean(self):
        # The "test tenant" case: nothing posted by us -> zero drift, no ERP read.
        called = {"n": 0}

        def read_states(ids):
            called["n"] += 1
            return {}

        report = reconcile_connection(self.conn, read_states=read_states)
        self.assertEqual(report.checked, 0)
        self.assertTrue(report.clean)
        self.assertEqual(report.drift_count, 0)
        self.assertEqual(called["n"], 0)        # ERP not read when nothing recorded

    def test_all_matched(self):
        self._op(101)
        self._op(102)
        report = reconcile_connection(
            self.conn, read_states=lambda ids: {101: "posted", 102: "posted"})
        self.assertEqual(report.checked, 2)
        self.assertEqual(report.matched, 2)
        self.assertTrue(report.clean)

    def test_missing_in_erp_is_drift(self):
        self._op(201)
        self._op(202)
        report = reconcile_connection(
            self.conn, read_states=lambda ids: {201: "posted"})  # 202 gone
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.missing_in_erp, [202])
        self.assertFalse(report.clean)
        self.assertEqual(report.drift_count, 1)

    def test_mismatched_state_is_drift(self):
        self._op(301)
        report = reconcile_connection(
            self.conn, read_states=lambda ids: {301: "draft"})  # un-posted!
        self.assertEqual(report.mismatched, [{"external_id": 301, "state": "draft"}])
        self.assertFalse(report.clean)

    def test_untracked_in_erp_is_informational_not_drift(self):
        self._op(401)
        report = reconcile_connection(
            self.conn,
            read_states=lambda ids: {401: "posted"},
            list_posted_ids=lambda: [401, 402, 403])  # 402/403 human-posted
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.untracked_in_erp, [402, 403])
        self.assertTrue(report.clean)            # untracked is NOT drift
        self.assertEqual(report.drift_count, 0)

    def test_only_successful_ops_counted(self):
        self._op(501, status="failed")           # a failed op carries no truth
        self._op(502, status="escalated")
        report = reconcile_connection(
            self.conn, read_states=lambda ids: {})
        self.assertEqual(report.checked, 0)       # neither is a recorded write
        self.assertTrue(report.clean)
