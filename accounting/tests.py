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
