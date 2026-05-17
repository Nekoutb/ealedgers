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
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
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
