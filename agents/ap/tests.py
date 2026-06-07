"""Tests for the D01 AP department scaffold — Step 51.

Coverage:
  - Specialist hierarchy (subclasses, specialist_type, stubs raise NotImplementedError)
  - APManager pipeline wiring (order, stop_on_failure, run-to-first-failure)
  - APDepartment class attributes (dept_code, dept_name, capabilities_needed)
  - APDepartment.handle() subscription gate (DepartmentDisabledError when disabled)
  - APDepartment.handle() returns a Proposal with correct fields when enabled
  - APDepartment.handle() chain_id threading
  - APDepartment.execute() stub raises NotImplementedError
  - APDepartment.can_auto_approve() default False
"""

from django.test import TestCase

from agents.ap.department import (
    APClassifier,
    APDepartment,
    APExtractor,
    APManager,
    APProposer,
    APReviewer,
)
from agents.department import (
    BaseDepartment,
    DepartmentDisabledError,
    DepartmentManager,
    DepartmentSpecialist,
    Proposal,
)


# ---------------------------------------------------------------------------
# Minimal tenant stub (no DB access needed for pure-unit tests)
# ---------------------------------------------------------------------------

class _FakeTenant:
    """Lightweight stand-in for a real Tenant model instance."""

    id = 9901
    plan = "starter"
    agent_enabled = True

    def __repr__(self):
        return "<FakeTenant#9901>"


# ---------------------------------------------------------------------------
# Specialist hierarchy tests
# ---------------------------------------------------------------------------

class APSpecialistHierarchyTests(TestCase):
    """Each AP specialist must be a proper DepartmentSpecialist subclass."""

    # --- subclass checks ---

    def test_extractor_is_specialist(self):
        self.assertTrue(issubclass(APExtractor, DepartmentSpecialist))

    def test_classifier_is_specialist(self):
        self.assertTrue(issubclass(APClassifier, DepartmentSpecialist))

    def test_reviewer_is_specialist(self):
        self.assertTrue(issubclass(APReviewer, DepartmentSpecialist))

    def test_proposer_is_specialist(self):
        self.assertTrue(issubclass(APProposer, DepartmentSpecialist))

    # --- specialist_type checks ---

    def test_extractor_specialist_type(self):
        self.assertEqual(APExtractor(_FakeTenant()).specialist_type, "extractor")

    def test_classifier_specialist_type(self):
        self.assertEqual(APClassifier(_FakeTenant()).specialist_type, "classifier")

    def test_reviewer_specialist_type(self):
        self.assertEqual(APReviewer(_FakeTenant()).specialist_type, "reviewer")

    def test_proposer_specialist_type(self):
        self.assertEqual(APProposer(_FakeTenant()).specialist_type, "proposer")

    # --- stub raises NotImplementedError ---

    def test_extractor_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APExtractor(_FakeTenant()).run({})

    def test_classifier_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APClassifier(_FakeTenant()).run({})

    def test_reviewer_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APReviewer(_FakeTenant()).run({})

    def test_proposer_run_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            APProposer(_FakeTenant()).run({})


# ---------------------------------------------------------------------------
# APManager pipeline wiring tests (pure-unit, no DB)
# ---------------------------------------------------------------------------

class APManagerTests(TestCase):
    """APManager must assemble the correct pipeline in the correct order."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        tenant = _FakeTenant()
        # Build a department shell without hitting the DB (no is_enabled() call here)
        dept = APDepartment.__new__(APDepartment)
        dept.tenant = tenant
        dept._connector = None
        dept._manager = APManager(dept)
        cls.manager = APManager(dept)

    def test_manager_is_department_manager(self):
        self.assertIsInstance(self.manager, DepartmentManager)

    def test_pipeline_has_four_specialists(self):
        self.assertEqual(len(self.manager.specialists), 4)

    def test_pipeline_order_extractor_first(self):
        self.assertIsInstance(self.manager.specialists[0], APExtractor)

    def test_pipeline_order_classifier_second(self):
        self.assertIsInstance(self.manager.specialists[1], APClassifier)

    def test_pipeline_order_proposer_third(self):
        self.assertIsInstance(self.manager.specialists[2], APProposer)

    def test_pipeline_order_reviewer_last(self):
        self.assertIsInstance(self.manager.specialists[3], APReviewer)

    def test_stop_on_failure_is_true(self):
        self.assertTrue(self.manager.stop_on_failure)

    def test_run_halts_after_first_failure(self):
        """Pipeline halts at APExtractor (stub raises); only 1 result returned."""
        results = self.manager.run({"document_path": "/tmp/bill.pdf"})
        # stop_on_failure=True means we get exactly 1 result (the failed extractor)
        self.assertEqual(len(results), 1)

    def test_run_first_result_is_failed(self):
        results = self.manager.run({})
        self.assertFalse(results[0].ok)

    def test_run_first_result_mentions_not_implemented(self):
        results = self.manager.run({})
        self.assertIn("NotImplementedError", results[0].error)

    def test_run_first_result_specialist_type_is_extractor(self):
        results = self.manager.run({})
        self.assertEqual(results[0].specialist_type, "extractor")


# ---------------------------------------------------------------------------
# APDepartment class-attribute tests (pure-unit, no DB)
# ---------------------------------------------------------------------------

class APDepartmentHierarchyTests(TestCase):
    """Static checks: ABCs, class attributes, manager type."""

    def test_is_base_department(self):
        self.assertTrue(issubclass(APDepartment, BaseDepartment))

    def test_dept_code_is_d01(self):
        self.assertEqual(APDepartment.dept_code, "D01")

    def test_dept_name(self):
        self.assertEqual(APDepartment.dept_name, "AP — Accounts Payable")

    def test_capabilities_includes_cap03(self):
        self.assertIn("CAP.03", APDepartment.capabilities_needed)

    def test_capabilities_includes_cap05(self):
        self.assertIn("CAP.05", APDepartment.capabilities_needed)

    def test_manager_attribute_is_ap_manager(self):
        dept = APDepartment(_FakeTenant())
        self.assertIsInstance(dept.manager, APManager)

    def test_manager_department_back_reference(self):
        dept = APDepartment(_FakeTenant())
        self.assertIs(dept.manager.department, dept)


# ---------------------------------------------------------------------------
# APDepartment.handle() — uses DB for TenantDepartmentSubscription
# ---------------------------------------------------------------------------

class APDepartmentHandleTests(TestCase):
    """handle() subscription gate and Proposal construction."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from accounting.models import Currency, Tenant, TenantDepartmentSubscription

        User = get_user_model()
        cls.currency = Currency.objects.create(
            code="XAF_51a",
            name="CFA Franc (Step 51a)",
            symbol="XAF",
            decimal_places=0,
        )
        cls.tenant = Tenant.objects.create(
            name="AP Scaffold Co",
            slug="ap-scaffold-co",
            currency=cls.currency,
        )
        cls.user = User.objects.create_user("ap51_user", password="pw51")
        # Active D01 subscription for the primary test tenant
        TenantDepartmentSubscription.objects.create(
            tenant=cls.tenant,
            department="D01",
            active=True,
        )

    def _dept(self) -> APDepartment:
        return APDepartment(self.tenant)

    # --- gate: subscription absent ---

    def test_handle_raises_when_not_subscribed(self):
        from accounting.models import Currency, Tenant

        currency = Currency.objects.create(
            code="XAF_51b", name="CFA Franc (Step 51b)", symbol="XAF", decimal_places=0
        )
        other = Tenant.objects.create(
            name="No Sub Co", slug="no-sub-co-51", currency=currency
        )
        with self.assertRaises(DepartmentDisabledError):
            APDepartment(other).handle({"document_path": "/tmp/x.pdf"})

    # --- gate: inactive subscription ---

    def test_handle_raises_when_subscription_inactive(self):
        from accounting.models import Currency, Tenant, TenantDepartmentSubscription

        currency = Currency.objects.create(
            code="XAF_51c", name="CFA Franc (Step 51c)", symbol="XAF", decimal_places=0
        )
        inactive_tenant = Tenant.objects.create(
            name="Inactive Sub Co", slug="inactive-sub-51", currency=currency
        )
        TenantDepartmentSubscription.objects.create(
            tenant=inactive_tenant, department="D01", active=False
        )
        with self.assertRaises(DepartmentDisabledError):
            APDepartment(inactive_tenant).handle({})

    # --- successful handle path ---

    def test_handle_returns_proposal(self):
        proposal = self._dept().handle({})
        self.assertIsInstance(proposal, Proposal)

    def test_handle_proposal_dept_code_is_d01(self):
        proposal = self._dept().handle({})
        self.assertEqual(proposal.dept_code, "D01")

    def test_handle_proposal_tenant_id(self):
        proposal = self._dept().handle({})
        self.assertEqual(proposal.tenant_id, self.tenant.id)

    def test_handle_proposal_all_ok_false_while_stubs(self):
        """Proposal.all_ok is False because stubs raise NotImplementedError."""
        proposal = self._dept().handle({})
        self.assertFalse(proposal.all_ok)

    def test_handle_threads_chain_id(self):
        proposal = self._dept().handle({"chain_id": "test-chain-51"})
        self.assertEqual(proposal.chain_id, "test-chain-51")

    def test_handle_generates_chain_id_when_absent(self):
        proposal = self._dept().handle({})
        self.assertTrue(proposal.chain_id)  # non-empty

    # --- execute stub ---

    def test_execute_raises_not_implemented(self):
        dept = self._dept()
        proposal = dept.handle({})
        with self.assertRaises(NotImplementedError):
            dept.execute(proposal)

    # --- can_auto_approve default ---

    def test_can_auto_approve_is_false_by_default(self):
        dept = self._dept()
        proposal = dept.handle({})
        self.assertFalse(dept.can_auto_approve(proposal))
