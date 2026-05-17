"""Tests for the accounting app.

Phase 0.1 only verifies the tenant FK behaves as a data column: rows for two
distinct tenants are independently queryable and don't bleed into each other.
Phase 0.2 will add implicit-isolation tests once the tenant-aware manager
middleware lands.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, models as dj_models, transaction
from django.test import TestCase

from accounting.models import (
    Account,
    Currency,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Membership,
    Tenant,
)


class TenantDataIsolationTests(TestCase):
    """Two tenants, each posting their own journal entry. Filtering by tenant
    must never return the other tenant's rows."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)

        cls.alice = User.objects.create_user("alice", "alice@a.test", "x")
        cls.bob = User.objects.create_user("bob", "bob@b.test", "x")

        cls.acme = Tenant.objects.create(
            slug="acme", name="Acme SARL", country="Cameroon",
            currency=cls.xaf, business_type="services", owner=cls.alice,
        )
        cls.beta = Tenant.objects.create(
            slug="beta", name="Beta SARL", country="Cameroon",
            currency=cls.xaf, business_type="goods", owner=cls.bob,
        )
        Membership.objects.create(user=cls.alice, tenant=cls.acme, role="owner")
        Membership.objects.create(user=cls.bob, tenant=cls.beta, role="owner")

        cls.acme_cash = Account.objects.create(
            tenant=cls.acme, code="ACME-521", name="Acme Cash", type="asset_cash",
        )
        cls.acme_revenue = Account.objects.create(
            tenant=cls.acme, code="ACME-707", name="Acme Service Revenue", type="income",
        )
        cls.beta_cash = Account.objects.create(
            tenant=cls.beta, code="BETA-521", name="Beta Cash", type="asset_cash",
        )
        cls.beta_revenue = Account.objects.create(
            tenant=cls.beta, code="BETA-707", name="Beta Sales Revenue", type="income",
        )

        cls.acme_journal = Journal.objects.create(
            tenant=cls.acme, code="A-VEN", name="Acme Sales", type="sale",
            sequence_prefix="A/", next_sequence=1,
        )
        cls.beta_journal = Journal.objects.create(
            tenant=cls.beta, code="B-VEN", name="Beta Sales", type="sale",
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

    def test_each_tenant_sees_only_its_own_data(self):
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

        self.assertEqual(Account.objects.filter(tenant=self.acme).count(), 2)
        self.assertEqual(Account.objects.filter(tenant=self.beta).count(), 2)

        acme_entries = JournalEntry.objects.filter(tenant=self.acme)
        beta_entries = JournalEntry.objects.filter(tenant=self.beta)
        self.assertEqual(acme_entries.count(), 1)
        self.assertEqual(beta_entries.count(), 1)
        self.assertNotIn(beta_entries.first(), list(acme_entries))

        acme_total = JournalEntryLine.objects.filter(tenant=self.acme).aggregate(
            s=dj_models.Sum("debit"),
        )["s"]
        beta_total = JournalEntryLine.objects.filter(tenant=self.beta).aggregate(
            s=dj_models.Sum("debit"),
        )["s"]
        self.assertEqual(acme_total, Decimal("100000"))
        self.assertEqual(beta_total, Decimal("250000"))

    def test_membership_unique_per_user_tenant(self):
        """A user can't have two memberships in the same tenant."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Membership.objects.create(user=self.alice, tenant=self.acme, role="admin")

    def test_a_user_can_belong_to_multiple_tenants(self):
        """An accountant serving multiple SMEs is a valid case."""
        Membership.objects.create(user=self.alice, tenant=self.beta, role="accountant")
        self.assertEqual(self.alice.memberships.count(), 2)
        self.assertEqual(
            set(self.alice.memberships.values_list("tenant__slug", flat=True)),
            {"acme", "beta"},
        )
