"""Tests for the agents app — scaffold + Step 41 department base classes."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from accounting.models import Currency, Tenant, TenantDepartmentSubscription

from agents.department import (
    BaseDepartment,
    DepartmentDisabledError,
    DepartmentManager,
    DepartmentSpecialist,
    Proposal,
    SpecialistResult,
)


# ---------------------------------------------------------------------------
# Scaffold tests (Step 11)
# ---------------------------------------------------------------------------

class AgentsAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('agents'))

    def test_app_config_loads(self):
        config = apps.get_app_config('agents')
        self.assertEqual(config.name, 'agents')


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_tenant(slug, xaf):
    return Tenant.objects.create(slug=slug, name=f"{slug} SARL", currency=xaf)


class _OkSpecialist(DepartmentSpecialist):
    """A specialist that always succeeds and echos its input."""
    specialist_type = "ok_spec"

    def run(self, input_data: dict) -> SpecialistResult:
        return SpecialistResult(
            specialist_type=self.specialist_type,
            output={"processed": True, **input_data},
            ok=True,
        )


class _FailSpecialist(DepartmentSpecialist):
    """A specialist that always raises a ValueError."""
    specialist_type = "fail_spec"

    def run(self, input_data: dict) -> SpecialistResult:
        raise ValueError("simulated failure")


class _TagSpecialist(DepartmentSpecialist):
    """Adds a 'tag' key to its output — used to verify context merging."""
    specialist_type = "tag_spec"

    def run(self, input_data: dict) -> SpecialistResult:
        return SpecialistResult(
            specialist_type=self.specialist_type,
            output={"tag": "added"},
            ok=True,
        )


class _CaptureSpecialist(DepartmentSpecialist):
    """Records the input_data it received for inspection in tests."""
    specialist_type = "capture_spec"
    captured: list = []

    def run(self, input_data: dict) -> SpecialistResult:
        self.captured.append(dict(input_data))
        return SpecialistResult(self.specialist_type, {}, ok=True)


class _ConcreteDept(BaseDepartment):
    """Minimal concrete department wired to D01 for testing."""
    dept_code = "D01"
    dept_name = "AP — Accounts Payable"
    capabilities_needed = frozenset({"CAP.03", "CAP.05"})

    def handle(self, event: dict) -> Proposal:
        if not self.is_enabled():
            raise DepartmentDisabledError(f"{self.dept_code} not subscribed")
        return Proposal(
            dept_code=self.dept_code,
            tenant_id=self.tenant.id,
            action=self.dept_name,
            inputs=event,
            specialist_results=[],
        )

    def execute(self, proposal: Proposal) -> dict:
        return {"status": "executed", "dept": self.dept_code}


# ---------------------------------------------------------------------------
# Step 41 — DepartmentSpecialist tests
# ---------------------------------------------------------------------------

class DepartmentSpecialistTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF_sp", name="CFA sp", decimal_places=0)
        cls.tenant = _make_tenant("sp-co", cls.xaf)

    def test_concrete_run_returns_result(self):
        s = _OkSpecialist(self.tenant)
        r = s.run({"invoice_id": 1})
        self.assertIsInstance(r, SpecialistResult)
        self.assertTrue(r.ok)
        self.assertEqual(r.specialist_type, "ok_spec")
        self.assertIn("invoice_id", r.output)

    def test_abstract_without_run_raises(self):
        """DepartmentSpecialist without run() cannot be instantiated."""
        with self.assertRaises(TypeError):
            class _BadSpec(DepartmentSpecialist):
                pass
            _BadSpec(self.tenant)

    def test_context_stored(self):
        s = _OkSpecialist(self.tenant, context={"currency": "XAF"})
        self.assertEqual(s.context["currency"], "XAF")

    def test_context_defaults_to_empty_dict(self):
        s = _OkSpecialist(self.tenant)
        self.assertEqual(s.context, {})


# ---------------------------------------------------------------------------
# Step 41 — DepartmentManager tests
# ---------------------------------------------------------------------------

class DepartmentManagerTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF_dm", name="CFA dm", decimal_places=0)
        cls.tenant = _make_tenant("dm-co", cls.xaf)

    def _dept(self):
        return _ConcreteDept(self.tenant)

    def test_run_through_single_specialist(self):
        mgr = DepartmentManager(self._dept(), [_OkSpecialist(self.tenant)])
        results = mgr.run({"doc": "bill.pdf"})
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)

    def test_pipeline_merges_output_into_context(self):
        """Second specialist receives merged context from first."""
        captured = _CaptureSpecialist(self.tenant)
        captured.captured = []
        mgr = DepartmentManager(
            self._dept(),
            [_TagSpecialist(self.tenant), captured],
        )
        mgr.run({"doc": "bill.pdf"})
        # The capture specialist should see 'tag' key from _TagSpecialist
        self.assertEqual(len(captured.captured), 1)
        self.assertIn("tag", captured.captured[0])

    def test_stop_on_failure_halts_pipeline(self):
        """With stop_on_failure=True (default), pipeline stops after first failure."""
        mgr = DepartmentManager(
            self._dept(),
            [_FailSpecialist(self.tenant), _OkSpecialist(self.tenant)],
        )
        results = mgr.run({"doc": "bill.pdf"})
        self.assertEqual(len(results), 1)          # second spec never ran
        self.assertFalse(results[0].ok)
        self.assertIn("simulated failure", results[0].error)

    def test_continue_on_failure(self):
        """With stop_on_failure=False all specialists run."""
        mgr = DepartmentManager(
            self._dept(),
            [_FailSpecialist(self.tenant), _OkSpecialist(self.tenant)],
            stop_on_failure=False,
        )
        results = mgr.run({"doc": "bill.pdf"})
        self.assertEqual(len(results), 2)

    def test_exception_wrapped_in_result(self):
        """An exception in run() becomes a SpecialistResult with ok=False."""
        mgr = DepartmentManager(self._dept(), [_FailSpecialist(self.tenant)])
        results = mgr.run({})
        self.assertFalse(results[0].ok)
        self.assertIn("ValueError", results[0].error)

    def test_propose_returns_proposal(self):
        mgr = DepartmentManager(self._dept(), [_OkSpecialist(self.tenant)])
        p = mgr.propose({"doc": "bill.pdf"})
        self.assertIsInstance(p, Proposal)
        self.assertEqual(p.dept_code, "D01")
        self.assertEqual(p.tenant_id, self.tenant.id)
        self.assertTrue(p.chain_id)  # auto-generated UUID

    def test_propose_with_explicit_chain_id(self):
        mgr = DepartmentManager(self._dept(), [_OkSpecialist(self.tenant)])
        p = mgr.propose({"doc": "bill.pdf"}, chain_id="my-chain-123")
        self.assertEqual(p.chain_id, "my-chain-123")

    def test_register_adds_specialist(self):
        mgr = DepartmentManager(self._dept())
        self.assertEqual(len(mgr.specialists), 0)
        mgr.register(_OkSpecialist(self.tenant))
        self.assertEqual(len(mgr.specialists), 1)

    def test_specialists_property_returns_copy(self):
        """Mutating the returned list must not affect the manager's internal list."""
        mgr = DepartmentManager(self._dept(), [_OkSpecialist(self.tenant)])
        specs = mgr.specialists
        specs.clear()
        self.assertEqual(len(mgr.specialists), 1)

    def test_empty_pipeline_returns_empty_results(self):
        mgr = DepartmentManager(self._dept())
        results = mgr.run({"doc": "bill.pdf"})
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Step 41 — BaseDepartment tests
# ---------------------------------------------------------------------------

class BaseDepartmentTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF_bd", name="CFA bd", decimal_places=0)
        cls.tenant = _make_tenant("bd-co", cls.xaf)

    def test_abstract_without_handle_raises(self):
        """BaseDepartment without handle() cannot be instantiated."""
        with self.assertRaises(TypeError):
            class _Bad(BaseDepartment):
                dept_code = "D01"
                def execute(self, proposal): pass
            _Bad(self.tenant)

    def test_abstract_without_execute_raises(self):
        """BaseDepartment without execute() cannot be instantiated."""
        with self.assertRaises(TypeError):
            class _Bad(BaseDepartment):
                dept_code = "D01"
                def handle(self, event): pass
            _Bad(self.tenant)

    def test_is_enabled_false_when_no_subscription(self):
        dept = _ConcreteDept(self.tenant)
        self.assertFalse(dept.is_enabled())

    def test_is_enabled_true_when_subscribed(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01", active=True)
        dept = _ConcreteDept(self.tenant)
        self.assertTrue(dept.is_enabled())

    def test_is_enabled_false_when_inactive_subscription(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01", active=False)
        dept = _ConcreteDept(self.tenant)
        self.assertFalse(dept.is_enabled())

    def test_is_enabled_false_wrong_dept_code(self):
        """Subscription for D02 must not enable D01."""
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D02", active=True)
        dept = _ConcreteDept(self.tenant)
        self.assertFalse(dept.is_enabled())

    def test_can_auto_approve_default_false(self):
        dept = _ConcreteDept(self.tenant)
        dummy = Proposal(
            dept_code="D01", tenant_id=self.tenant.id,
            action="test", inputs={}, specialist_results=[],
        )
        self.assertFalse(dept.can_auto_approve(dummy))

    def test_capabilities_needed_declared(self):
        self.assertIn("CAP.03", _ConcreteDept.capabilities_needed)
        self.assertIn("CAP.05", _ConcreteDept.capabilities_needed)

    def test_dept_disabled_error_when_not_subscribed(self):
        dept = _ConcreteDept(self.tenant)
        with self.assertRaises(DepartmentDisabledError):
            dept.handle({"event": "bill_received"})

    def test_handle_returns_proposal_when_enabled(self):
        TenantDepartmentSubscription.objects.create(
            tenant=self.tenant, department="D01", active=True)
        dept = _ConcreteDept(self.tenant)
        p = dept.handle({"event": "bill_received"})
        self.assertIsInstance(p, Proposal)
        self.assertEqual(p.dept_code, "D01")

    def test_connector_resolves_to_local_gl_when_no_erp(self):
        """With no connected ERP, connector property returns LocalGLConnector."""
        from accounting.local_gl import LocalGLConnector
        dept = _ConcreteDept(self.tenant)   # no connector kwarg
        self.assertIsInstance(dept.connector, LocalGLConnector)

    def test_injected_connector_used_directly(self):
        """Passing a connector bypasses connector_for_tenant()."""
        from unittest.mock import MagicMock
        mock_conn = MagicMock()
        dept = _ConcreteDept(self.tenant, connector=mock_conn)
        self.assertIs(dept.connector, mock_conn)

    def test_execute_returns_dict(self):
        dept = _ConcreteDept(self.tenant)
        dummy = Proposal(
            dept_code="D01", tenant_id=self.tenant.id,
            action="test", inputs={}, specialist_results=[],
        )
        result = dept.execute(dummy)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["dept"], "D01")


# ---------------------------------------------------------------------------
# Step 41 — Proposal dataclass tests
# ---------------------------------------------------------------------------

class ProposalTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code="XAF_pr", name="CFA pr", decimal_places=0)
        cls.tenant = _make_tenant("pr-co", cls.xaf)

    def _p(self, results=None):
        return Proposal(
            dept_code="D01",
            tenant_id=self.tenant.id,
            action="Post vendor bill",
            inputs={"amount": 100},
            specialist_results=results or [],
        )

    def test_chain_id_auto_generated(self):
        p = self._p()
        self.assertTrue(p.chain_id)
        self.assertNotEqual(p.chain_id, self._p().chain_id)  # unique per instance

    def test_all_ok_true_when_all_succeed(self):
        results = [
            SpecialistResult("s1", {}, ok=True),
            SpecialistResult("s2", {}, ok=True),
        ]
        self.assertTrue(self._p(results).all_ok)

    def test_all_ok_false_when_any_fail(self):
        results = [
            SpecialistResult("s1", {}, ok=True),
            SpecialistResult("s2", {}, ok=False, error="oops"),
        ]
        self.assertFalse(self._p(results).all_ok)

    def test_all_ok_true_for_empty_results(self):
        self.assertTrue(self._p([]).all_ok)
