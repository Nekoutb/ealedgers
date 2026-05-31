"""Tests for the knowledge layer (Steps 13–14)."""

from datetime import date

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounting.models import Currency, Membership, Tenant
from knowledge.models import Citation, Rule, TenantProcedure
from knowledge.retrieval import retrieve, _tokenize


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


# ---------------------------------------------------------------------------
# Step 14 — rule retrieval
# ---------------------------------------------------------------------------


class TokenizeTests(SimpleTestCase):
    def test_drops_short_and_stopwords(self):
        toks = _tokenize("The depreciation of vehicles and machinery")
        self.assertNotIn("the", toks)
        self.assertNotIn("and", toks)
        self.assertNotIn("of", toks)  # len 2
        self.assertIn("depreciation", toks)
        self.assertIn("vehicles", toks)
        self.assertIn("machinery", toks)

    def test_keeps_accents_and_digits(self):
        toks = _tokenize("Amortissement compte 2441 récupérable")
        self.assertIn("amortissement", toks)
        self.assertIn("2441", toks)
        self.assertIn("récupérable", toks)


class RetrieveTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        # Isolate from migration-loaded knowledge slices (K01 ships 12 rules
        # via migration 0002). These tests assert exact result sets, so start
        # from a clean Rule table and seed our own small corpus.
        Rule.objects.all().delete()

        User = get_user_model()
        cls.user = User.objects.create_superuser("ret_admin", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.acme = Tenant.objects.create(slug="acme-ret", name="Acme Ret",
                                         currency=cls.xaf, owner=cls.user)
        Membership.objects.create(user=cls.user, tenant=cls.acme, role="owner")

        # A small corpus of rules
        cls.r_depr = Rule.objects.create(
            slug="syscohada-depr-component", scope="framework",
            framework="SYSCOHADA-2017", knowledge_slice="K02",
            title="Fixed-asset depreciation by component",
            source_ref="SYSCOHADA Titre VIII Ch.4",
            source_text="Significant components of an asset are depreciated "
                        "separately over their own useful lives.",
            trigger_conditions={"transaction_type": "fixed_asset_acquisition"},
            effects={"method": "component"},
        )
        cls.r_vat = Rule.objects.create(
            slug="cgi-2025-tva-deductible", scope="tax_code",
            framework="CGI-2025", knowledge_slice="K11",
            title="VAT deductibility on purchases",
            source_ref="CGI 2025 art. 144",
            source_text="Input VAT on purchases is deductible when the supplier "
                        "invoice is compliant.",
            trigger_conditions={"transaction_type": "vendor_bill"},
            effects={"vat": "recoverable"},
        )
        cls.r_wht = Rule.objects.create(
            slug="cgi-2025-wht-services", scope="tax_code",
            framework="CGI-2025", knowledge_slice="K12",
            title="Withholding tax on services to non-residents",
            source_ref="CGI 2025 art. 92",
            source_text="Payments for services to non-residents bear "
                        "withholding tax at source.",
        )

    def test_query_ranks_relevant_rule_first(self):
        results = retrieve("depreciation of a vehicle component")
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["slug"], "syscohada-depr-component")

    def test_query_returns_only_matching(self):
        results = retrieve("withholding tax non-resident services")
        slugs = [r["slug"] for r in results]
        self.assertIn("cgi-2025-wht-services", slugs)
        self.assertNotIn("syscohada-depr-component", slugs)

    def test_framework_filter(self):
        results = retrieve("tax", framework="CGI-2025")
        self.assertTrue(all(r["framework"] == "CGI-2025" for r in results))

    def test_scope_filter(self):
        results = retrieve("", scope="framework")
        self.assertTrue(all(r["kind"] == "rule" for r in results))
        # blank query → filtered set returned (component rule is the only
        # framework-scope rule)
        slugs = [r["slug"] for r in results]
        self.assertEqual(slugs, ["syscohada-depr-component"])

    def test_knowledge_slice_filter(self):
        results = retrieve("", knowledge_slice="K11")
        self.assertEqual([r["slug"] for r in results], ["cgi-2025-tva-deductible"])

    def test_k_limit(self):
        results = retrieve("", k=2)
        self.assertLessEqual(len(results), 2)

    def test_inactive_rule_excluded(self):
        self.r_wht.active = False
        self.r_wht.save()
        results = retrieve("withholding tax services")
        self.assertNotIn("cgi-2025-wht-services", [r["slug"] for r in results])

    def test_tenant_procedure_ranked_with_rules(self):
        TenantProcedure.objects.create(
            tenant=self.acme, slug="depr-vehicles-4y",
            title="Vehicle depreciation over 4 years",
            description="Vehicles depreciate straight-line over 48 months.",
            trigger_conditions={"asset_class": "vehicles"},
            effects={"useful_life_months": 48},
        )
        results = retrieve("vehicle depreciation", tenant=self.acme)
        kinds = {r["kind"] for r in results}
        self.assertIn("procedure", kinds)
        # the procedure (title hit on both 'vehicle' + 'depreciation') should
        # outrank or tie the framework component rule
        self.assertEqual(results[0]["kind"], "procedure")

    def test_procedures_excluded_without_tenant(self):
        TenantProcedure.objects.create(
            tenant=self.acme, slug="p1", title="vehicle depreciation thing",
        )
        results = retrieve("vehicle depreciation")  # no tenant
        self.assertTrue(all(r["kind"] == "rule" for r in results))


class RetrieveEndpointTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        # Isolate from migration-loaded K01 rules (this test asserts the
        # top result is its own seeded rule).
        Rule.objects.all().delete()

        User = get_user_model()
        cls.user = User.objects.create_superuser("ep_admin", "a@a.test", "x")
        cls.xaf = Currency.objects.create(code="XAF", name="CFA Franc", decimal_places=0)
        cls.acme = Tenant.objects.create(slug="acme-ep", name="Acme EP",
                                         currency=cls.xaf, owner=cls.user)
        Membership.objects.create(user=cls.user, tenant=cls.acme, role="owner")
        Rule.objects.create(
            slug="cgi-2025-tva-deductible", scope="tax_code",
            framework="CGI-2025", knowledge_slice="K11",
            title="VAT deductibility on purchases",
            source_ref="CGI 2025 art. 144",
            source_text="Input VAT on purchases is deductible.",
        )

    def test_endpoint_requires_login(self):
        r = self.client.get("/knowledge/retrieve/?q=vat")
        self.assertEqual(r.status_code, 302)  # bounced to login

    def test_endpoint_returns_json_results(self):
        self.client.force_login(self.user)
        r = self.client.get("/knowledge/retrieve/?q=VAT deductible purchases")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["query"], "VAT deductible purchases")
        self.assertGreaterEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["slug"], "cgi-2025-tva-deductible")

    def test_endpoint_k_clamped(self):
        self.client.force_login(self.user)
        r = self.client.get("/knowledge/retrieve/?q=&k=999")
        self.assertEqual(r.status_code, 200)
        # no crash; k clamped to <= 50
        self.assertLessEqual(len(r.json()["results"]), 50)


# ---------------------------------------------------------------------------
# Step 15 — K01 SYSCOHADA chart-of-accounts knowledge slice
# ---------------------------------------------------------------------------


class K01LoaderTests(TestCase):
    """The K01 slice loads (the data migration runs in the test DB, so the
    rules are already present) and the loader is idempotent + retrievable."""

    def test_k01_rules_present(self):
        # Migration 0002 loads them during test-DB setup.
        n = Rule.objects.filter(knowledge_slice="K01",
                                framework="SYSCOHADA-2017").count()
        self.assertEqual(n, 12)

    def test_all_nine_classes_encoded(self):
        for cls in range(1, 10):
            self.assertTrue(
                Rule.objects.filter(slug=f"syscohada-class-{cls}").exists(),
                f"class {cls} rule missing",
            )

    def test_class_rules_carry_statement_mapping(self):
        # Classes 1–5 → balance sheet; 6–8 → income statement.
        for cls in (1, 2, 3, 4, 5):
            r = Rule.objects.get(slug=f"syscohada-class-{cls}")
            self.assertEqual(r.effects["statement"], "balance_sheet")
        for cls in (6, 7, 8):
            r = Rule.objects.get(slug=f"syscohada-class-{cls}")
            self.assertEqual(r.effects["statement"], "income_statement")

    def test_rules_need_expert_review(self):
        # All K01 rules start needs_review until the Step-27 expert gate.
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K01")
            .exclude(review_status="needs_review").exists()
        )

    def test_loader_is_idempotent(self):
        from knowledge.loaders import load_slice
        before = Rule.objects.filter(knowledge_slice="K01").count()
        created, updated = load_slice(Rule, "K01")
        after = Rule.objects.filter(knowledge_slice="K01").count()
        self.assertEqual(created, 0)       # nothing new
        self.assertEqual(updated, before)  # all re-synced
        self.assertEqual(after, before)    # count unchanged

    def test_loader_rejects_unknown_slice(self):
        from knowledge.loaders import load_slice
        with self.assertRaises(ValueError):
            load_slice(Rule, "K99")

    def test_french_query_retrieves_class_4(self):
        res = retrieve("comptes fournisseurs et clients tiers",
                       framework="SYSCOHADA-2017", k=3)
        self.assertEqual(res[0]["slug"], "syscohada-class-4")

    def test_management_command_runs(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command("load_knowledge", slice_id="K01", stdout=out)
        self.assertIn("complete", out.getvalue())


# ---------------------------------------------------------------------------
# Step 16 — K15 SYSCOHADA evaluation & determination-of-result slice
# ---------------------------------------------------------------------------


class K15LoaderTests(TestCase):
    """K15 loads via migration 0003 and is retrievable."""

    def test_k15_rules_present(self):
        n = Rule.objects.filter(knowledge_slice="K15",
                                framework="SYSCOHADA-2017").count()
        self.assertEqual(n, 15)

    def test_key_eval_rules_encoded(self):
        for slug in ("syscohada-eval-base-conventions",
                     "syscohada-eval-acquisition-cost-immobilisation",
                     "syscohada-eval-depreciation",
                     "syscohada-eval-impairment",
                     "syscohada-eval-provisions",
                     "syscohada-eval-inventory-fifo-or-wac"):
            self.assertTrue(Rule.objects.filter(slug=slug).exists(),
                            f"{slug} missing")

    def test_base_conventions_effects(self):
        r = Rule.objects.get(slug="syscohada-eval-base-conventions")
        self.assertEqual(
            r.effects["conventions"],
            ["historical_cost", "prudence", "going_concern"],
        )

    def test_depreciation_forbids_revenue_based(self):
        r = Rule.objects.get(slug="syscohada-eval-depreciation")
        self.assertTrue(r.effects["revenue_based_forbidden"])
        self.assertTrue(r.effects["mandatory_even_without_profit"])

    def test_all_k15_need_expert_review(self):
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K15")
            .exclude(review_status="needs_review").exists()
        )

    def test_retrieval_acquisition_cost(self):
        res = retrieve("cout acquisition immobilisation droits enregistrement",
                       framework="SYSCOHADA-2017", k=1)
        self.assertEqual(res[0]["slug"],
                         "syscohada-eval-acquisition-cost-immobilisation")

    def test_retrieval_provisions(self):
        res = retrieve("provision risques et charges retraite",
                       framework="SYSCOHADA-2017", k=1)
        self.assertEqual(res[0]["slug"], "syscohada-eval-provisions")


# ---------------------------------------------------------------------------
# Step 17 — K20 SYSCOHADA first-application (transition) slice
# ---------------------------------------------------------------------------


class K20LoaderTests(TestCase):
    """K20 loads via migration 0004 and is retrievable."""

    def test_k20_rules_present(self):
        n = Rule.objects.filter(knowledge_slice="K20",
                                framework="SYSCOHADA-2017").count()
        self.assertEqual(n, 12)

    def test_compte_475_rule_encoded(self):
        r = Rule.objects.get(slug="syscohada-fta-account-475-transitional")
        self.assertEqual(r.effects["account"], "475")
        self.assertEqual(r.effects["sub_accounts"], {"4751": "asset", "4752": "liability"})

    def test_change_of_method_retrospective(self):
        r = Rule.objects.get(slug="syscohada-fta-change-of-method-retrospective")
        self.assertEqual(r.effects["basis"], "retrospective")
        self.assertEqual(r.effects["opening_impact_to"], "report_a_nouveau")

    def test_leases_prospective(self):
        r = Rule.objects.get(slug="syscohada-fta-leases-prospective")
        self.assertEqual(r.effects["existing_contracts"], "no_retreatment")

    def test_all_k20_need_expert_review(self):
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K20")
            .exclude(review_status="needs_review").exists()
        )

    def test_retrieval_compte_475(self):
        res = retrieve("compte transitoire 475 retraitement revision",
                       framework="SYSCOHADA-2017", k=1)
        self.assertEqual(res[0]["slug"], "syscohada-fta-account-475-transitional")


class KnowledgeBaseTotalsTests(TestCase):
    """Cross-slice sanity: the encoded SYSCOHADA base now spans K01+K15+K20."""

    def test_total_syscohada_rules(self):
        # 12 (K01) + 15 (K15) + 12 (K20) = 39
        self.assertEqual(
            Rule.objects.filter(framework="SYSCOHADA-2017").count(), 39)

    def test_three_slices_present(self):
        slices = set(
            Rule.objects.filter(framework="SYSCOHADA-2017")
            .values_list("knowledge_slice", flat=True)
        )
        self.assertEqual(slices, {"K01", "K15", "K20"})


# ---------------------------------------------------------------------------
# Step 18 — K10 CGI 2025 corporate-income-tax (IS) slice
# (encoded ahead of K11/TVA: the supplied CGI PDF ends at art. 124 and lacks
#  the TVA section, so IS — articles 2–23, fully present — is encoded first.)
# ---------------------------------------------------------------------------


class K10LoaderTests(TestCase):
    """K10 loads via migration 0005 and is retrievable. This is the first
    tax_code slice (CGI-2025), distinct from the SYSCOHADA framework slices."""

    def test_k10_rules_present(self):
        n = Rule.objects.filter(knowledge_slice="K10",
                                framework="CGI-2025").count()
        self.assertEqual(n, 30)

    def test_all_k10_are_tax_code_scope(self):
        # Tax-code slices use scope='tax_code', not 'framework'.
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K10")
            .exclude(scope="tax_code").exists()
        )

    def test_key_is_rules_encoded(self):
        for slug in ("cgi-2025-is-rate",
                     "cgi-2025-is-minimum-tax",
                     "cgi-2025-is-loss-carryforward",
                     "cgi-2025-is-participation-exemption",
                     "cgi-2025-is-depreciation-rates",
                     "cgi-2025-is-financial-charges-thin-cap",
                     "cgi-2025-is-installments-acompte"):
            self.assertTrue(Rule.objects.filter(slug=slug).exists(),
                            f"{slug} missing")

    def test_is_rate_effects(self):
        r = Rule.objects.get(slug="cgi-2025-is-rate")
        self.assertEqual(r.effects["taux_standard_pct"], 30)
        self.assertEqual(r.effects["taux_reduit_pct"], 25)
        self.assertEqual(r.effects["seuil_ca_taux_reduit_fcfa"], 3000000000)

    def test_minimum_tax_effects(self):
        r = Rule.objects.get(slug="cgi-2025-is-minimum-tax")
        self.assertEqual(r.effects["taux_regime_reel_avec_cac_pct"], 2.2)
        self.assertEqual(r.effects["taux_regime_simplifie_avec_cac_pct"], 5.5)
        self.assertEqual(
            r.effects["base"],
            "chiffre_affaires_global_HT_exercice_precedent",
        )

    def test_loss_carryforward_window(self):
        r = Rule.objects.get(slug="cgi-2025-is-loss-carryforward")
        self.assertEqual(r.effects["annees_general"], 4)
        self.assertEqual(
            r.effects["annees_etablissements_credit_et_portefeuille"], 6)

    def test_participation_exemption_quote_part(self):
        r = Rule.objects.get(slug="cgi-2025-is-participation-exemption")
        self.assertEqual(r.effects["quote_part_frais_et_charges_pct"], 10)
        self.assertEqual(r.effects["conditions"]["participation_min_pct"], 25)

    def test_depreciation_rate_lookup(self):
        r = Rule.objects.get(slug="cgi-2025-is-depreciation-rates")
        self.assertEqual(r.effects["taux_pct"]["materiel_informatique"], 25)
        self.assertEqual(r.effects["taux_pct"]["mobilier_de_bureau"], 10)

    def test_all_k10_need_expert_review(self):
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K10")
            .exclude(review_status="needs_review").exists()
        )

    def test_retrieval_minimum_tax(self):
        res = retrieve("minimum de perception chiffre d'affaires IS",
                       framework="CGI-2025", k=1)
        self.assertEqual(res[0]["slug"], "cgi-2025-is-minimum-tax")

    def test_retrieval_depreciation_rates(self):
        res = retrieve("taux amortissement materiel informatique mobilier",
                       framework="CGI-2025", k=1)
        self.assertEqual(res[0]["slug"], "cgi-2025-is-depreciation-rates")

    def test_retrieval_scoped_to_cgi_not_syscohada(self):
        # An IS query under the CGI framework must not surface SYSCOHADA rules.
        res = retrieve("taux de l'impot sur les societes",
                       framework="CGI-2025", k=3)
        self.assertTrue(res)
        for hit in res:
            self.assertEqual(hit["framework"], "CGI-2025")


# ---------------------------------------------------------------------------
# Step 20 — K12 CGI 2025 withholding-tax (retenues à la source) slice
# (TSR — the non-resident-services withholding — is deferred with K11/TVA:
#  its provisions sit at art. 225+, past the supplied PDF's art.-124 cutoff.)
# ---------------------------------------------------------------------------


class K12LoaderTests(TestCase):
    """K12 loads via migration 0006 and is retrievable. Second tax_code
    slice (CGI-2025), focused on withholding at source."""

    def test_k12_rules_present(self):
        n = Rule.objects.filter(knowledge_slice="K12",
                                framework="CGI-2025").count()
        self.assertEqual(n, 17)

    def test_all_k12_are_tax_code_scope(self):
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K12")
            .exclude(scope="tax_code").exists()
        )

    def test_key_wht_rules_encoded(self):
        for slug in ("cgi-2025-wht-ircm-rate",
                     "cgi-2025-wht-ircm-withholding",
                     "cgi-2025-wht-bvmac-securities-reduced",
                     "cgi-2025-wht-property-income",
                     "cgi-2025-wht-digital-platform",
                     "cgi-2025-wht-non-salaried-agents",
                     "cgi-2025-wht-irpp-salary-scale"):
            self.assertTrue(Rule.objects.filter(slug=slug).exists(),
                            f"{slug} missing")

    def test_ircm_rate_effects(self):
        r = Rule.objects.get(slug="cgi-2025-wht-ircm-rate")
        self.assertEqual(r.effects["taux_liberatoire_standard_pct"], 15)
        self.assertEqual(r.effects["taux_dividendes_pme_pct"], 10)
        self.assertEqual(r.effects["taux_paradis_fiscal_pct"], 30)
        self.assertEqual(r.effects["seuil_ca_dividendes_pme_fcfa"], 3000000000)

    def test_property_income_withholding_rate(self):
        r = Rule.objects.get(slug="cgi-2025-wht-property-income")
        self.assertEqual(r.effects["taux_pct"], 15)
        self.assertEqual(r.effects["reversement"], "15_du_mois_suivant")

    def test_bvmac_reduced_rates(self):
        r = Rule.objects.get(slug="cgi-2025-wht-bvmac-securities-reduced")
        self.assertEqual(
            r.effects["taux_dividendes_et_obligations_court_terme_pct"], 10)
        self.assertEqual(r.effects["taux_obligations_5ans_et_plus_pct"], 5)

    def test_salary_scale_brackets(self):
        r = Rule.objects.get(slug="cgi-2025-wht-irpp-salary-scale")
        scale = r.effects["bareme_annuel_fcfa"]
        self.assertEqual(scale[0]["taux_pct"], 10)
        self.assertEqual(scale[-1]["taux_pct"], 35)

    def test_all_k12_need_expert_review(self):
        self.assertFalse(
            Rule.objects.filter(knowledge_slice="K12")
            .exclude(review_status="needs_review").exists()
        )

    def test_retrieval_ircm_withholding(self):
        res = retrieve("retenue a la source revenus capitaux mobiliers dividendes",
                       framework="CGI-2025", k=1)
        self.assertEqual(res[0]["slug"], "cgi-2025-wht-ircm-withholding")

    def test_retrieval_property_withholding(self):
        res = retrieve("retenue source 15 pourcent loyers revenus fonciers",
                       framework="CGI-2025", k=1)
        self.assertEqual(res[0]["slug"], "cgi-2025-wht-property-income")


class CGITotalsTests(TestCase):
    """Cross-slice sanity for the CGI tax catalogue (K10 + K12; K11/TVA next)."""

    def test_total_cgi_rules(self):
        # 30 (K10 / IS) + 17 (K12 / WHT) = 47
        self.assertEqual(
            Rule.objects.filter(framework="CGI-2025").count(), 47)

    def test_cgi_slices_present(self):
        slices = set(
            Rule.objects.filter(framework="CGI-2025")
            .values_list("knowledge_slice", flat=True)
        )
        self.assertEqual(slices, {"K10", "K12"})
