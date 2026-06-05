"""Tests for the agents app — scaffold + Step 41 department base classes
+ Step 43 event bus."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from accounting.models import BusEvent, Currency, Tenant, TenantDepartmentSubscription

from agents.department import (
    BaseDepartment,
    DepartmentDisabledError,
    DepartmentManager,
    DepartmentSpecialist,
    Proposal,
    SpecialistResult,
)
from agents.events import (
    Event,
    clear_subscriptions,
    emit,
    subscribe,
    subscriptions_for,
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


# ---------------------------------------------------------------------------
# Step 43 — Event bus tests
# ---------------------------------------------------------------------------

# Force Q_CLUSTER sync=True so handlers run inline (no worker needed).
# Must be a flat dict matching the shape in settings.py (not nested).
_SYNC_Q = {'name': 'ealedgers', 'orm': 'default', 'sync': True, 'workers': 1}


@override_settings(Q_CLUSTER=_SYNC_Q)
class EventBusTests(TestCase):
    """Tests for agents.events: subscribe/emit/dispatch lifecycle."""

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code='XAF_eb', name='CFA eb', decimal_places=0)
        cls.tenant = Tenant.objects.create(slug='eb-co', name='EB Co', currency=cls.xaf)

    def setUp(self):
        """Clear all subscriptions before every test to prevent cross-test pollution."""
        clear_subscriptions()
        self._calls: list[dict] = []

    def _make_handler(self, tag='h1'):
        """Return a handler that appends its args to self._calls."""
        calls = self._calls

        def handler(event_type, payload, tenant, chain_id):
            calls.append({
                'tag': tag,
                'event_type': event_type,
                'payload': payload,
                'tenant': tenant,
                'chain_id': chain_id,
            })

        handler.__name__ = f'handler_{tag}'
        return handler

    # --- subscribe / subscriptions_for / clear_subscriptions ----------------

    def test_subscribe_registers_handler(self):
        h = self._make_handler()
        subscribe('bill.posted', h)
        self.assertIn(h, subscriptions_for('bill.posted'))

    def test_subscribe_is_idempotent(self):
        """Registering the same handler twice results in a single entry."""
        h = self._make_handler()
        subscribe('bill.posted', h)
        subscribe('bill.posted', h)
        self.assertEqual(subscriptions_for('bill.posted').count(h), 1)

    def test_clear_subscriptions_single_type(self):
        h = self._make_handler()
        subscribe('bill.posted', h)
        subscribe('invoice.sent', h)
        clear_subscriptions('bill.posted')
        self.assertEqual(subscriptions_for('bill.posted'), [])
        self.assertIn(h, subscriptions_for('invoice.sent'))

    def test_clear_subscriptions_all(self):
        h = self._make_handler()
        subscribe('bill.posted', h)
        subscribe('invoice.sent', h)
        clear_subscriptions()
        self.assertEqual(subscriptions_for('bill.posted'), [])
        self.assertEqual(subscriptions_for('invoice.sent'), [])

    def test_subscriptions_for_unknown_type_returns_empty(self):
        self.assertEqual(subscriptions_for('no.such.event'), [])

    # --- emit creates BusEvent row ------------------------------------------

    def test_emit_creates_bus_event_row(self):
        event = Event(event_type='bill.posted', tenant=self.tenant, payload={'amount': 100})
        bus_ev = emit(event)
        self.assertIsNotNone(bus_ev.pk)
        self.assertEqual(bus_ev.event_type, 'bill.posted')
        self.assertEqual(bus_ev.tenant, self.tenant)
        self.assertEqual(bus_ev.payload, {'amount': 100})

    def test_emit_sets_chain_id(self):
        event = Event(event_type='bill.posted', tenant=self.tenant, payload={})
        bus_ev = emit(event)
        self.assertTrue(bus_ev.chain_id)
        self.assertEqual(bus_ev.chain_id, event.chain_id)

    def test_emit_with_explicit_chain_id(self):
        event = Event(
            event_type='bill.posted', tenant=self.tenant, payload={},
            chain_id='test-chain-abc',
        )
        bus_ev = emit(event)
        self.assertEqual(bus_ev.chain_id, 'test-chain-abc')

    # --- dispatch calls handlers --------------------------------------------

    def test_handler_called_on_emit(self):
        h = self._make_handler()
        subscribe('bill.posted', h)
        emit(Event(event_type='bill.posted', tenant=self.tenant, payload={'x': 1}))
        self.assertEqual(len(self._calls), 1)
        self.assertEqual(self._calls[0]['event_type'], 'bill.posted')
        self.assertEqual(self._calls[0]['payload'], {'x': 1})
        self.assertEqual(self._calls[0]['tenant'], self.tenant)

    def test_multiple_handlers_all_called(self):
        subscribe('invoice.sent', self._make_handler('h1'))
        subscribe('invoice.sent', self._make_handler('h2'))
        subscribe('invoice.sent', self._make_handler('h3'))
        emit(Event(event_type='invoice.sent', tenant=self.tenant, payload={}))
        tags = [c['tag'] for c in self._calls]
        self.assertIn('h1', tags)
        self.assertIn('h2', tags)
        self.assertIn('h3', tags)

    def test_handler_not_called_for_different_event_type(self):
        h = self._make_handler()
        subscribe('bill.posted', h)
        emit(Event(event_type='invoice.sent', tenant=self.tenant, payload={}))
        self.assertEqual(self._calls, [])

    def test_no_handlers_dispatches_cleanly(self):
        """emit() with zero subscriptions must not raise and status is 'dispatched'."""
        bus_ev = emit(Event(event_type='payment.registered', tenant=self.tenant, payload={}))
        self.assertEqual(bus_ev.status, 'dispatched')
        self.assertEqual(bus_ev.handler_count, 0)

    # --- dispatch updates BusEvent row --------------------------------------

    def test_bus_event_status_dispatched_after_emit(self):
        subscribe('bill.posted', self._make_handler())
        bus_ev = emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        self.assertEqual(bus_ev.status, 'dispatched')

    def test_bus_event_handler_count_correct(self):
        subscribe('bill.posted', self._make_handler('h1'))
        subscribe('bill.posted', self._make_handler('h2'))
        bus_ev = emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        self.assertEqual(bus_ev.handler_count, 2)

    def test_bus_event_dispatched_at_set(self):
        bus_ev = emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        self.assertIsNotNone(bus_ev.dispatched_at)

    # --- fail-open behaviour ------------------------------------------------

    def test_failing_handler_does_not_stop_other_handlers(self):
        """A handler that raises must not prevent subsequent handlers from running."""
        def bad_handler(et, payload, tenant, chain_id):
            raise RuntimeError("boom")
        bad_handler.__name__ = 'bad_handler'

        good = self._make_handler('good')
        subscribe('bill.posted', bad_handler)
        subscribe('bill.posted', good)
        emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        # The good handler still ran
        self.assertEqual(len(self._calls), 1)
        self.assertEqual(self._calls[0]['tag'], 'good')

    def test_failing_handler_sets_status_failed(self):
        def bad_handler(et, payload, tenant, chain_id):
            raise RuntimeError("oops")
        bad_handler.__name__ = 'bad_handler'
        subscribe('bill.posted', bad_handler)
        bus_ev = emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        self.assertEqual(bus_ev.status, 'failed')

    def test_failing_handler_error_recorded(self):
        def bad_handler(et, payload, tenant, chain_id):
            raise ValueError("bad data")
        bad_handler.__name__ = 'bad_handler'
        subscribe('bill.posted', bad_handler)
        bus_ev = emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        self.assertIn('ValueError', bus_ev.error)
        self.assertIn('bad data', bus_ev.error)

    # --- synchronous flag ---------------------------------------------------

    def test_synchronous_flag_runs_inline(self):
        """emit(synchronous=True) runs handlers even without Q_CLUSTER sync."""
        h = self._make_handler()
        subscribe('period.closed', h)
        # Don't use override_settings here — pass synchronous=True explicitly
        with override_settings(Q_CLUSTER={}):
            bus_ev = emit(
                Event(event_type='period.closed', tenant=self.tenant, payload={}),
                synchronous=True,
            )
        self.assertEqual(len(self._calls), 1)
        self.assertEqual(bus_ev.status, 'dispatched')

    # --- BusEvent model str -------------------------------------------------

    def test_bus_event_str(self):
        bus_ev = emit(Event(event_type='bill.posted', tenant=self.tenant, payload={}))
        s = str(bus_ev)
        self.assertIn('bill.posted', s)
