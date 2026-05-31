"""Tests for the knowledge layer (Step 13)."""

from datetime import date

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounting.models import Currency, Tenant
from knowledge.models import Citation, Rule, TenantProcedure


class KnowledgeAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('knowledge'))

    def test_app_config_loads(self):
        config = apps.get_app_config('knowledge')
        self.assertEqual(config.name, 'knowledge')


class RuleModelTests(TestCase):

    def test_create_framework_rule(self):
        r = Rule.objects.create(
            slug="syscohada-coa-class-2",
            scope="framework",
            framework="SYSCOHADA-2017",
            knowledge_slice="K01",
            jurisdiction="OHADA",
            title="Class 2 — fixed-asset accounts",
            source_ref="SYSCOHADA Titre VII Ch.2",
            source_text="Les comptes de la classe 2 …",
            trigger_conditions={"transaction_type": "fixed_asset_acquisition"},
            effects={"account_prefix": "2"},
        )
        self.assertEqual(r.review_status, "needs_review")  # default
        self.assertTrue(r.active)
        self.assertEqual(str(r), "[SYSCOHADA-2017] Class 2 — fixed-asset accounts")

    def test_confidence_floor_default(self):
        r = Rule.objects.create(
            slug="x", scope="tax_code", framework="CGI-2025", title="t",
        )
        self.assertEqual(float(r.confidence_floor), 0.9)

    def test_is_currently_effective_no_bounds(self):
        r = Rule.objects.create(slug="x", scope="framework",
                                framework="SYSCOHADA-2017", title="t")
        self.assertTrue(r.is_currently_effective)

    def test_is_currently_effective_future_rule(self):
        r = Rule.objects.create(
            slug="x", scope="tax_code", framework="CGI-2026", title="t",
            effective_from=date(2099, 1, 1),
        )
        self.assertFalse(r.is_currently_effective)

    def test_is_currently_effective_expired_rule(self):
        r = Rule.objects.create(
            slug="x", scope="tax_code", framework="CGI-2020", title="t",
            effective_to=date(2020, 12, 31),
        )
        self.assertFalse(r.is_currently_effective)

    def test_slug_unique(self):
        Rule.objects.create(slug="dup", scope="framework",
                            framework="SYSCOHADA-2017", title="a")
        with self.assertRaises(Exception):
            Rule.objects.create(slug="dup", scope="framework",
                                framework="SYSCOHADA-2017", title="b")


class CitationModelTests(TestCase):

    def test_multiple_citations_on_one_rule(self):
        r = Rule.objects.create(slug="r1", scope="tax_code",
                                framework="CGI-2025", title="WHT on services")
        Citation.objects.create(rule=r, sequence=0, reference="CGI 2025 art. 92")
        Citation.objects.create(rule=r, sequence=1,
                                reference="SYSCOHADA Titre VIII Ch.32")
        self.assertEqual(r.citations.count(), 2)
        self.assertEqual(
            list(r.citations.values_list("reference", flat=True)),
            ["CGI 2025 art. 92", "SYSCOHADA Titre VIII Ch.32"],
        )


class TenantProcedureModelTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_superuser("kp_admin", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.acme = Tenant.objects.create(slug="acme-kp", name="Acme KP",
                                         currency=cls.xaf, owner=cls.user)
        cls.beta = Tenant.objects.create(slug="beta-kp", name="Beta KP",
                                         currency=cls.xaf, owner=cls.user)

    def test_create_procedure(self):
        p = TenantProcedure.objects.create(
            tenant=self.acme, slug="depr-vehicles-4y",
            title="Vehicles depreciate over 4 years",
            description="Straight-line, 48 months.",
            trigger_conditions={"asset_class": "vehicles"},
            effects={"useful_life_months": 48, "method": "straight_line"},
        )
        self.assertEqual(p.validation_status, "pending")  # default
        self.assertEqual(p.effects["useful_life_months"], 48)

    def test_overrides_rule_link(self):
        rule = Rule.objects.create(
            slug="syscohada-depr-default", scope="framework",
            framework="SYSCOHADA-2017", title="Default depreciation",
        )
        p = TenantProcedure.objects.create(
            tenant=self.acme, slug="depr-vehicles-4y",
            title="Vehicles 4y", overrides_rule=rule,
        )
        self.assertEqual(p.overrides_rule, rule)
        self.assertIn(p, rule.overridden_by.all())

    def test_slug_unique_per_tenant_but_not_across(self):
        TenantProcedure.objects.create(tenant=self.acme, slug="p1", title="A")
        # Same slug, different tenant — allowed
        TenantProcedure.objects.create(tenant=self.beta, slug="p1", title="B")
        # Same slug, same tenant — rejected
        with self.assertRaises(Exception):
            TenantProcedure.objects.create(tenant=self.acme, slug="p1", title="C")

    def test_tenant_isolation_via_manager(self):
        TenantProcedure.objects.create(tenant=self.acme, slug="a", title="A")
        TenantProcedure.objects.create(tenant=self.beta, slug="b", title="B")
        self.assertEqual(TenantProcedure.objects.for_tenant(self.acme).count(), 1)
        self.assertEqual(TenantProcedure.objects.for_tenant(self.beta).count(), 1)
        self.assertEqual(
            TenantProcedure.objects.for_tenant(self.acme).first().slug, "a")
