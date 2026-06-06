"""Tests for the agents app — scaffold + Step 41 department base classes
+ Step 43 event bus + Step 44 chain provenance threading
+ Step 45 dispatcher agent + Step 46 capacity engine
+ Step 47 kill-switch gate."""

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from accounting.models import (
    AgentRun,
    ApprovalQueueItem,
    BusEvent,
    Currency,
    Provenance,
    Tenant,
    TenantDepartmentSubscription,
)

from agents.chain import ChainEntry, ChainTracer, trace_chain
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


# ---------------------------------------------------------------------------
# Step 44 — ChainTracer tests
# ---------------------------------------------------------------------------

_CHAIN_ID = 'test-chain-44-abcdef'


class ChainTracerTests(TestCase):
    """Verify that chain_id threads correctly across BusEvent, ApprovalQueueItem,
    AgentRun, and Provenance, and that ChainTracer queries / timeline work."""

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code='XAF_ct', name='CFA ct', decimal_places=0)
        cls.tenant = Tenant.objects.create(slug='ct-co', name='CT Co', currency=cls.xaf)
        # Second tenant for cross-tenant isolation tests
        cls.other = Tenant.objects.create(slug='ct-other', name='CT Other', currency=cls.xaf)

    # --- construction / repr ------------------------------------------------

    def test_trace_chain_convenience_returns_tracer(self):
        tracer = trace_chain(_CHAIN_ID, self.tenant)
        self.assertIsInstance(tracer, ChainTracer)
        self.assertEqual(tracer.chain_id, _CHAIN_ID)

    def test_repr_contains_chain_id(self):
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertIn(_CHAIN_ID, repr(tracer))

    # --- is_empty -----------------------------------------------------------

    def test_is_empty_true_for_unknown_chain(self):
        tracer = ChainTracer('no-such-chain-xyz', self.tenant)
        self.assertTrue(tracer.is_empty())

    def test_is_empty_false_once_bus_event_exists(self):
        BusEvent.objects.create(
            tenant=self.tenant, event_type='bill.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertFalse(tracer.is_empty())

    # --- bus_events() -------------------------------------------------------

    def test_bus_events_returns_matching_row(self):
        ev = BusEvent.objects.create(
            tenant=self.tenant, event_type='invoice.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        pks = list(tracer.bus_events().values_list('pk', flat=True))
        self.assertIn(ev.pk, pks)

    def test_bus_events_excludes_other_chain(self):
        BusEvent.objects.create(
            tenant=self.tenant, event_type='bill.posted',
            chain_id='different-chain', status='dispatched',
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertEqual(tracer.bus_events().count(), 0)

    def test_bus_events_tenant_isolation(self):
        """Another tenant's BusEvent with the same chain_id must not appear."""
        BusEvent.objects.create(
            tenant=self.other, event_type='bill.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertEqual(tracer.bus_events().count(), 0)

    # --- proposals() --------------------------------------------------------

    def test_proposals_returns_matching_row(self):
        item = ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D01',
            action='Post vendor bill', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        pks = list(tracer.proposals().values_list('pk', flat=True))
        self.assertIn(item.pk, pks)

    def test_proposals_tenant_isolation(self):
        ApprovalQueueItem.objects.create(
            tenant=self.other, dept_code='D01',
            action='Other tenant bill', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertEqual(tracer.proposals().count(), 0)

    # --- agent_runs() -------------------------------------------------------

    def test_agent_runs_returns_matching_row(self):
        run = AgentRun.objects.create(
            tenant=self.tenant, department='D05',
            task='post_je', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        pks = list(tracer.agent_runs().values_list('pk', flat=True))
        self.assertIn(run.pk, pks)

    def test_agent_runs_tenant_isolation(self):
        AgentRun.objects.create(
            tenant=self.other, department='D05',
            task='post_je', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertEqual(tracer.agent_runs().count(), 0)

    # --- dept_codes() -------------------------------------------------------

    def test_dept_codes_from_proposals(self):
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D01',
            action='AP task', chain_id=_CHAIN_ID,
        )
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D05',
            action='GL task', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        codes = tracer.dept_codes()
        self.assertIn('D01', codes)
        self.assertIn('D05', codes)

    def test_dept_codes_from_agent_runs(self):
        AgentRun.objects.create(
            tenant=self.tenant, department='D02',
            task='ar_task', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        self.assertIn('D02', tracer.dept_codes())

    def test_dept_codes_empty_for_unknown_chain(self):
        tracer = ChainTracer('no-such-chain-xyz', self.tenant)
        self.assertEqual(tracer.dept_codes(), set())

    # --- timeline() ---------------------------------------------------------

    def test_timeline_empty_for_unknown_chain(self):
        tracer = ChainTracer('no-such-chain-xyz', self.tenant)
        self.assertEqual(tracer.timeline(), [])

    def test_timeline_returns_chain_entries(self):
        BusEvent.objects.create(
            tenant=self.tenant, event_type='bill.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D01',
            action='Post AP bill', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        tl = tracer.timeline()
        self.assertGreaterEqual(len(tl), 2)
        for entry in tl:
            self.assertIsInstance(entry, ChainEntry)

    def test_timeline_sources_present(self):
        """All four sources should appear when rows exist for each."""
        User = get_user_model()
        BusEvent.objects.create(
            tenant=self.tenant, event_type='bill.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D01',
            action='AP task', chain_id=_CHAIN_ID,
        )
        AgentRun.objects.create(
            tenant=self.tenant, department='D01',
            task='classify_bill', chain_id=_CHAIN_ID,
        )
        Provenance.objects.create(
            tenant=self.tenant, source='agent',
            chain_id=_CHAIN_ID, summary='Posted bill',
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        sources = {e.source for e in tracer.timeline()}
        self.assertIn('event', sources)
        self.assertIn('proposal', sources)
        self.assertIn('agent_run', sources)
        self.assertIn('provenance', sources)

    def test_timeline_sorted_chronologically(self):
        """timeline() must be sorted ascending by timestamp."""
        BusEvent.objects.create(
            tenant=self.tenant, event_type='bill.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D01',
            action='AP task', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        tl = tracer.timeline()
        timestamps = [e.timestamp for e in tl]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_timeline_cross_dept_chain(self):
        """A chain that spans D01 (AP) and D05 (GL) shows both dept_codes."""
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D01',
            action='AP: post bill', chain_id=_CHAIN_ID,
        )
        ApprovalQueueItem.objects.create(
            tenant=self.tenant, dept_code='D05',
            action='GL: reconcile', chain_id=_CHAIN_ID,
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        dept_codes_in_tl = {e.dept_code for e in tracer.timeline() if e.dept_code}
        self.assertIn('D01', dept_codes_in_tl)
        self.assertIn('D05', dept_codes_in_tl)

    def test_timeline_tenant_isolation(self):
        """The other tenant's rows must never appear in our timeline."""
        BusEvent.objects.create(
            tenant=self.other, event_type='bill.posted',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        BusEvent.objects.create(
            tenant=self.tenant, event_type='invoice.sent',
            chain_id=_CHAIN_ID, status='dispatched',
        )
        tracer = ChainTracer(_CHAIN_ID, self.tenant)
        tl = tracer.timeline()
        labels = [e.label for e in tl]
        self.assertIn('invoice.sent', labels)
        self.assertNotIn('bill.posted', labels)

    # --- ChainEntry immutability --------------------------------------------

    def test_chain_entry_is_frozen(self):
        """ChainEntry is a frozen dataclass — fields must not be writable."""
        from datetime import timezone
        entry = ChainEntry(
            chain_id='x', source='event', dept_code='',
            label='bill.posted', status='dispatched',
            timestamp=__import__('datetime').datetime.now(timezone.utc),
            object_id=1, object_repr='BusEvent',
        )
        with self.assertRaises(Exception):
            entry.source = 'mutated'  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Step 45 — Dispatcher agent tests
# ---------------------------------------------------------------------------

from agents.dispatcher import (   # noqa: E402
    Dispatcher,
    RoutingResult,
    RoutingRule,
    _DEFAULT_RULES,
    _DISPATCHER_EVENT_TYPES,
    _dispatcher_bus_handler,
    register_dispatcher,
)
from agents.capacity import (  # noqa: E402
    CapacityDecision,
    CapacityEngine,
    CapacityResult,
    PLAN_DEFAULT_CAPS,
)
from agents.gate import (  # noqa: E402
    AgentGate,
    GateDecision,
    GateResult,
)


# --- RoutingRule (pure, no DB) ----------------------------------------------

class RoutingRuleTests(SimpleTestCase):
    """Pure predicate tests — no database needed."""

    def test_by_event_type_matches(self):
        rule = RoutingRule.by_event_type('bill.posted', 'D01')
        self.assertTrue(rule.matches('bill.posted', {}))

    def test_by_event_type_no_match(self):
        rule = RoutingRule.by_event_type('bill.posted', 'D01')
        self.assertFalse(rule.matches('invoice.sent', {}))

    def test_by_payload_value_matches(self):
        rule = RoutingRule.by_payload_value('doc_type', 'vendor_bill', 'D01')
        self.assertTrue(rule.matches('document.received', {'doc_type': 'vendor_bill'}))

    def test_by_payload_value_no_match_wrong_value(self):
        rule = RoutingRule.by_payload_value('doc_type', 'vendor_bill', 'D01')
        self.assertFalse(rule.matches('document.received', {'doc_type': 'customer_invoice'}))

    def test_by_payload_value_no_match_missing_key(self):
        rule = RoutingRule.by_payload_value('doc_type', 'vendor_bill', 'D01')
        self.assertFalse(rule.matches('document.received', {}))

    def test_by_predicate_matches(self):
        rule = RoutingRule.by_predicate(
            lambda et, p: 'payroll' in p.get('doc_type', ''),
            'D07',
        )
        self.assertTrue(rule.matches('work.submitted', {'doc_type': 'payroll_run'}))

    def test_predicate_exception_returns_false(self):
        """A predicate that raises must not crash the dispatcher."""
        rule = RoutingRule.by_predicate(lambda et, p: 1 / 0, 'D01')
        self.assertFalse(rule.matches('any.event', {}))

    def test_repr_contains_dept_code(self):
        rule = RoutingRule.by_event_type('bill.posted', 'D01')
        self.assertIn('D01', repr(rule))


# --- Dispatcher.resolve() (pure, no DB) -------------------------------------

class DispatcherResolveTests(SimpleTestCase):
    """Pure routing-logic tests — uses resolve(), no DB, no bus emit."""

    def _d(self, rules=None):
        """Return a Dispatcher with a dummy tenant (not persisted)."""
        class _FakeTenant:
            pk = 0
            def __str__(self): return 'fake'
        return Dispatcher(_FakeTenant(), rules=rules)

    def test_vendor_bill_routes_to_d01(self):
        dept, et = self._d().resolve('document.received', {'doc_type': 'vendor_bill'})
        self.assertEqual(dept, 'D01')
        self.assertEqual(et, 'D01.work.queued')

    def test_vendor_invoice_routes_to_d01(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'vendor_invoice'})
        self.assertEqual(dept, 'D01')

    def test_purchase_order_routes_to_d01(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'purchase_order'})
        self.assertEqual(dept, 'D01')

    def test_customer_invoice_routes_to_d02(self):
        dept, et = self._d().resolve('document.received', {'doc_type': 'customer_invoice'})
        self.assertEqual(dept, 'D02')
        self.assertEqual(et, 'D02.work.queued')

    def test_credit_note_routes_to_d02(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'credit_note'})
        self.assertEqual(dept, 'D02')

    def test_payment_routes_to_d03(self):
        dept, et = self._d().resolve('document.received', {'doc_type': 'payment'})
        self.assertEqual(dept, 'D03')
        self.assertEqual(et, 'D03.work.queued')

    def test_bank_statement_routes_to_d03(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'bank_statement'})
        self.assertEqual(dept, 'D03')

    def test_asset_acquisition_routes_to_d04(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'asset_acquisition'})
        self.assertEqual(dept, 'D04')

    def test_journal_entry_routes_to_d05(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'journal_entry'})
        self.assertEqual(dept, 'D05')

    def test_period_close_routes_to_d00(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'period_close'})
        self.assertEqual(dept, 'D00')

    def test_payroll_routes_to_d07(self):
        dept, _ = self._d().resolve('document.received', {'doc_type': 'payroll'})
        self.assertEqual(dept, 'D07')

    def test_unknown_doc_type_returns_none_and_work_unrouted(self):
        dept, et = self._d().resolve('document.received', {'doc_type': 'mystery_doc'})
        self.assertIsNone(dept)
        self.assertEqual(et, 'work.unrouted')

    def test_empty_payload_returns_unrouted(self):
        dept, et = self._d().resolve('document.received', {})
        self.assertIsNone(dept)
        self.assertEqual(et, 'work.unrouted')

    def test_custom_rules_override_defaults(self):
        """Supplying rules= replaces all defaults."""
        rules = [RoutingRule.by_payload_value('doc_type', 'mystery_doc', 'D06')]
        dept, et = self._d(rules=rules).resolve('document.received', {'doc_type': 'mystery_doc'})
        self.assertEqual(dept, 'D06')
        self.assertEqual(et, 'D06.work.queued')

    def test_custom_rules_do_not_include_defaults(self):
        """When rules= is provided, defaults are gone."""
        rules = [RoutingRule.by_payload_value('doc_type', 'mystery_doc', 'D06')]
        dept, _ = self._d(rules=rules).resolve('document.received', {'doc_type': 'vendor_bill'})
        self.assertIsNone(dept)   # vendor_bill default is not in the custom list

    def test_add_rule_appended_checked_after_defaults(self):
        """add_rule(prepend=False) appends — defaults checked first."""
        d = self._d()
        d.add_rule(RoutingRule.by_payload_value('doc_type', 'vendor_bill', 'D99'))
        # D01 default fires before D99 rule
        dept, _ = d.resolve('document.received', {'doc_type': 'vendor_bill'})
        self.assertEqual(dept, 'D01')

    def test_add_rule_prepend_checked_before_defaults(self):
        """add_rule(prepend=True) inserts at front — fires before defaults."""
        d = self._d()
        d.add_rule(
            RoutingRule.by_payload_value('doc_type', 'vendor_bill', 'D99'),
            prepend=True,
        )
        dept, _ = d.resolve('document.received', {'doc_type': 'vendor_bill'})
        self.assertEqual(dept, 'D99')

    def test_rules_property_returns_copy(self):
        """Mutating the returned list must not affect the dispatcher."""
        d = self._d()
        original_len = len(d.rules)
        d.rules.clear()              # modifies the copy, not the internal list
        self.assertEqual(len(d.rules), original_len)

    def test_repr_contains_dept_code(self):
        d = self._d()
        self.assertIn('dispatcher', d.dept_code)


# --- Integration: route() emits on bus, register_dispatcher() ---------------

@override_settings(Q_CLUSTER=_SYNC_Q)
class DispatcherRouteTests(TestCase):
    """Integration tests for Dispatcher.route() and register_dispatcher().

    Requires DB (BusEvent rows are written). Sync Q_CLUSTER so emission
    completes inline with no worker needed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code='XAF_di', name='CFA di', decimal_places=0)
        cls.tenant = Tenant.objects.create(slug='di-co', name='DI Co', currency=cls.xaf)

    def setUp(self):
        clear_subscriptions()

    # --- route() creates a BusEvent -----------------------------------------

    def test_route_vendor_bill_emits_d01_work_queued(self):
        """Acceptance criterion: vendor bill → D01.work.queued event on bus."""
        d = Dispatcher(self.tenant)
        result = d.route('document.received', {'doc_type': 'vendor_bill'})
        self.assertEqual(result.target_dept, 'D01')
        self.assertEqual(result.routed_event_type, 'D01.work.queued')
        self.assertTrue(result.was_routed)
        # BusEvent row persisted
        ev = BusEvent.objects.get(
            tenant=self.tenant, event_type='D01.work.queued',
        )
        self.assertEqual(ev.payload.get('_target_dept'), 'D01')
        self.assertEqual(ev.payload.get('_routed_from'), 'document.received')
        self.assertEqual(ev.payload.get('doc_type'), 'vendor_bill')

    def test_route_unrouted_emits_work_unrouted(self):
        d = Dispatcher(self.tenant)
        result = d.route('document.received', {'doc_type': 'mystery'})
        self.assertFalse(result.was_routed)
        self.assertEqual(result.routed_event_type, 'work.unrouted')
        self.assertTrue(BusEvent.objects.filter(
            tenant=self.tenant, event_type='work.unrouted',
        ).exists())

    def test_route_preserves_chain_id(self):
        d = Dispatcher(self.tenant)
        result = d.route('document.received', {'doc_type': 'vendor_bill'}, chain_id='my-chain')
        self.assertEqual(result.chain_id, 'my-chain')
        ev = BusEvent.objects.get(tenant=self.tenant, event_type='D01.work.queued')
        self.assertEqual(ev.chain_id, 'my-chain')

    def test_route_generates_chain_id_when_missing(self):
        d = Dispatcher(self.tenant)
        result = d.route('document.received', {'doc_type': 'vendor_bill'})
        self.assertTrue(result.chain_id)

    def test_routing_result_dataclass_fields(self):
        d = Dispatcher(self.tenant)
        result = d.route('document.received', {'doc_type': 'customer_invoice'})
        self.assertEqual(result.original_event_type, 'document.received')
        self.assertEqual(result.target_dept, 'D02')
        self.assertEqual(result.routed_event_type, 'D02.work.queued')
        self.assertTrue(result.was_routed)

    # --- register_dispatcher() + full event-driven flow ---------------------

    def test_register_dispatcher_subscribes_handler(self):
        register_dispatcher()
        from agents.events import subscriptions_for
        handlers = subscriptions_for('document.received')
        self.assertIn(_dispatcher_bus_handler, handlers)

    def test_register_dispatcher_idempotent(self):
        register_dispatcher()
        register_dispatcher()
        from agents.events import subscriptions_for
        handlers = subscriptions_for('document.received')
        self.assertEqual(handlers.count(_dispatcher_bus_handler), 1)

    def test_full_flow_emit_document_received_creates_routed_event(self):
        """End-to-end: emit 'document.received' with vendor_bill → D01.work.queued exists."""
        register_dispatcher()
        emit(Event(
            event_type='document.received',
            tenant=self.tenant,
            payload={'doc_type': 'vendor_bill', 'filename': 'facture.pdf'},
        ))
        self.assertTrue(BusEvent.objects.filter(
            tenant=self.tenant, event_type='D01.work.queued',
        ).exists())

    def test_full_flow_default_event_types(self):
        """Both default event types ('document.received', 'work.submitted') are subscribed."""
        register_dispatcher()
        from agents.events import subscriptions_for
        self.assertIn(_dispatcher_bus_handler, subscriptions_for('document.received'))
        self.assertIn(_dispatcher_bus_handler, subscriptions_for('work.submitted'))


# ---------------------------------------------------------------------------
# Step 46 — CapacityEngine tests
# ---------------------------------------------------------------------------

class CapacityEngineTests(TestCase):
    """Tests for agents.capacity.CapacityEngine.

    Decision hierarchy (recapped for readability):
    1. No active subscription → QUEUE (cap = 0)
    2. Active sub + auto_action_cap > 0 → use that override cap
    3. Active sub + auto_action_cap == 0 → use PLAN_DEFAULT_CAPS[tenant.plan]
    4. amount <= 0 → REFUSE  (invalid)
    5. cap == 0 → QUEUE
    6. amount <= cap → AUTO_APPROVE
    7. amount > cap → QUEUE
    """

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(code='XAF_cap', name='CFA Franc (cap tests)', decimal_places=0)

    def _make_tenant(self, slug, plan='free'):
        return Tenant.objects.create(slug=slug, name=f'{slug} SARL', currency=self.xaf, plan=plan)

    def _subscribe(self, tenant, dept_code, active=True, cap=None):
        """Create a TenantDepartmentSubscription; return the instance."""
        from decimal import Decimal as D
        kwargs = dict(tenant=tenant, department=dept_code, active=active)
        if cap is not None:
            kwargs['auto_action_cap'] = D(str(cap))
        return TenantDepartmentSubscription.objects.create(**kwargs)

    # --- PLAN_DEFAULT_CAPS constant -----------------------------------------

    def test_plan_default_caps_keys_cover_all_plans(self):
        for plan in ('free', 'starter', 'pro', 'enterprise'):
            self.assertIn(plan, PLAN_DEFAULT_CAPS)

    def test_plan_default_caps_free_is_zero(self):
        from decimal import Decimal
        self.assertEqual(PLAN_DEFAULT_CAPS['free'], Decimal('0'))

    def test_plan_default_caps_starter(self):
        from decimal import Decimal
        self.assertEqual(PLAN_DEFAULT_CAPS['starter'], Decimal('500000'))

    def test_plan_default_caps_pro(self):
        from decimal import Decimal
        self.assertEqual(PLAN_DEFAULT_CAPS['pro'], Decimal('5000000'))

    def test_plan_default_caps_enterprise(self):
        from decimal import Decimal
        self.assertEqual(PLAN_DEFAULT_CAPS['enterprise'], Decimal('50000000'))

    # --- CapacityDecision enum ----------------------------------------------

    def test_capacity_decision_values(self):
        self.assertEqual(CapacityDecision.AUTO_APPROVE, 'auto_approve')
        self.assertEqual(CapacityDecision.QUEUE,        'queue')
        self.assertEqual(CapacityDecision.REFUSE,       'refuse')

    # --- CapacityResult properties ------------------------------------------

    def test_result_auto_approve_property(self):
        from decimal import Decimal
        r = CapacityResult(
            decision=CapacityDecision.AUTO_APPROVE,
            reason='ok',
            cap=Decimal('500000'),
            amount=Decimal('100000'),
            dept_code='D01',
        )
        self.assertTrue(r.auto_approve)
        self.assertFalse(r.should_queue)
        self.assertFalse(r.refused)

    def test_result_queue_property(self):
        from decimal import Decimal
        r = CapacityResult(
            decision=CapacityDecision.QUEUE,
            reason='over cap',
            cap=Decimal('500000'),
            amount=Decimal('600000'),
            dept_code='D01',
        )
        self.assertFalse(r.auto_approve)
        self.assertTrue(r.should_queue)
        self.assertFalse(r.refused)

    def test_result_refuse_property(self):
        from decimal import Decimal
        r = CapacityResult(
            decision=CapacityDecision.REFUSE,
            reason='negative',
            cap=Decimal('0'),
            amount=Decimal('-1'),
            dept_code='D01',
        )
        self.assertFalse(r.auto_approve)
        self.assertFalse(r.should_queue)
        self.assertTrue(r.refused)

    # --- no subscription → always QUEUE ------------------------------------

    def test_no_subscription_queues(self):
        t = self._make_tenant('nosub', plan='pro')
        result = CapacityEngine(t).check('D01', 100)
        self.assertTrue(result.should_queue)
        self.assertEqual(result.decision, CapacityDecision.QUEUE)

    def test_no_subscription_cap_is_zero(self):
        t = self._make_tenant('nosub2', plan='enterprise')
        result = CapacityEngine(t).check('D01', 100)
        from decimal import Decimal
        self.assertEqual(result.cap, Decimal('0'))

    def test_inactive_subscription_behaves_like_no_subscription(self):
        t = self._make_tenant('inactive', plan='pro')
        self._subscribe(t, 'D01', active=False, cap=None)
        result = CapacityEngine(t).check('D01', 100)
        self.assertTrue(result.should_queue)

    # --- invalid amount → REFUSE -------------------------------------------

    def test_zero_amount_is_refused(self):
        t = self._make_tenant('refuseamt', plan='pro')
        self._subscribe(t, 'D01', active=True, cap=None)
        result = CapacityEngine(t).check('D01', 0)
        self.assertTrue(result.refused)
        self.assertEqual(result.decision, CapacityDecision.REFUSE)

    def test_negative_amount_is_refused(self):
        t = self._make_tenant('neg', plan='pro')
        self._subscribe(t, 'D01', active=True, cap=None)
        result = CapacityEngine(t).check('D01', -1)
        self.assertTrue(result.refused)

    # --- free plan: default cap 0 → always QUEUE ---------------------------

    def test_free_plan_always_queues(self):
        t = self._make_tenant('free1', plan='free')
        self._subscribe(t, 'D01')
        result = CapacityEngine(t).check('D01', 1)
        self.assertTrue(result.should_queue)

    # --- starter plan -------------------------------------------------------

    def test_starter_plan_below_cap_auto_approves(self):
        t = self._make_tenant('starter1', plan='starter')
        self._subscribe(t, 'D01')
        result = CapacityEngine(t).check('D01', 400000)
        self.assertTrue(result.auto_approve)

    def test_starter_plan_at_cap_boundary_auto_approves(self):
        t = self._make_tenant('starter2', plan='starter')
        self._subscribe(t, 'D01')
        result = CapacityEngine(t).check('D01', 500000)  # exactly at cap
        self.assertTrue(result.auto_approve)

    def test_starter_plan_above_cap_queues(self):
        t = self._make_tenant('starter3', plan='starter')
        self._subscribe(t, 'D01')
        result = CapacityEngine(t).check('D01', 500001)
        self.assertTrue(result.should_queue)

    # --- pro plan -----------------------------------------------------------

    def test_pro_plan_below_cap_auto_approves(self):
        t = self._make_tenant('pro1', plan='pro')
        self._subscribe(t, 'D02')
        result = CapacityEngine(t).check('D02', 3000000)
        self.assertTrue(result.auto_approve)

    def test_pro_plan_above_cap_queues(self):
        t = self._make_tenant('pro2', plan='pro')
        self._subscribe(t, 'D02')
        result = CapacityEngine(t).check('D02', 5000001)
        self.assertTrue(result.should_queue)

    # --- enterprise plan ----------------------------------------------------

    def test_enterprise_plan_below_cap_auto_approves(self):
        t = self._make_tenant('ent1', plan='enterprise')
        self._subscribe(t, 'D03')
        result = CapacityEngine(t).check('D03', 40000000)
        self.assertTrue(result.auto_approve)

    def test_enterprise_plan_above_cap_queues(self):
        t = self._make_tenant('ent2', plan='enterprise')
        self._subscribe(t, 'D03')
        result = CapacityEngine(t).check('D03', 50000001)
        self.assertTrue(result.should_queue)

    # --- explicit auto_action_cap override ----------------------------------

    def test_explicit_cap_overrides_plan_default(self):
        """auto_action_cap > 0 on the subscription takes priority over the plan."""
        t = self._make_tenant('override1', plan='free')   # free plan = 0 default
        self._subscribe(t, 'D01', cap=200000)             # but explicit override = 200k
        result = CapacityEngine(t).check('D01', 150000)
        self.assertTrue(result.auto_approve)
        from decimal import Decimal
        self.assertEqual(result.cap, Decimal('200000'))

    def test_explicit_cap_below_amount_queues(self):
        t = self._make_tenant('override2', plan='enterprise')  # 50M default
        self._subscribe(t, 'D01', cap=100000)                  # but override = 100k
        result = CapacityEngine(t).check('D01', 200000)
        self.assertTrue(result.should_queue)
        from decimal import Decimal
        self.assertEqual(result.cap, Decimal('100000'))

    # --- unknown plan falls back to Decimal('0') ---------------------------

    def test_unknown_plan_falls_back_to_zero(self):
        t = self._make_tenant('unknown1', plan='free')
        t.plan = 'nonexistent'   # bypass model choices validation
        self._subscribe(t, 'D01')
        result = CapacityEngine(t).check('D01', 1)
        self.assertTrue(result.should_queue)

    # --- dept_code on result ------------------------------------------------

    def test_result_carries_dept_code(self):
        t = self._make_tenant('deptcode', plan='pro')
        self._subscribe(t, 'D05')
        result = CapacityEngine(t).check('D05', 1000000)
        self.assertEqual(result.dept_code, 'D05')

    # --- str representation -------------------------------------------------

    def test_engine_repr(self):
        t = self._make_tenant('repr1', plan='free')
        engine = CapacityEngine(t)
        self.assertIn('CapacityEngine', repr(engine))

    def test_result_str(self):
        from decimal import Decimal
        r = CapacityResult(
            decision=CapacityDecision.AUTO_APPROVE,
            reason='ok',
            cap=Decimal('500000'),
            amount=Decimal('100000'),
            dept_code='D01',
        )
        s = str(r)
        self.assertIn('D01', s)
        self.assertIn('auto_approve', s)


# ---------------------------------------------------------------------------
# Step 47 — AgentGate (kill-switch + subscription enforcement) tests
# ---------------------------------------------------------------------------

class AgentGateTests(TestCase):
    """Tests for agents.gate.AgentGate.

    Two checks must BOTH pass for the gate to open:
    1. tenant.agent_enabled == True   (master kill switch)
    2. tenant.is_subscribed(dept_code)  (active subscription exists)

    The gate is fail-closed: any missing field defaults to CLOSED.
    """

    @classmethod
    def setUpTestData(cls):
        cls.xaf = Currency.objects.create(
            code='XAF_gate', name='CFA Franc (gate tests)', decimal_places=0,
        )

    def _make_tenant(self, slug, agent_enabled=True):
        return Tenant.objects.create(
            slug=slug, name=f'{slug} SARL',
            currency=self.xaf,
            agent_enabled=agent_enabled,
        )

    def _subscribe(self, tenant, dept_code, active=True):
        return TenantDepartmentSubscription.objects.create(
            tenant=tenant, department=dept_code, active=active,
        )

    # --- GateDecision enum --------------------------------------------------

    def test_gate_decision_values(self):
        self.assertEqual(GateDecision.OPEN,   'open')
        self.assertEqual(GateDecision.CLOSED, 'closed')

    # --- GateResult properties ----------------------------------------------

    def test_gate_result_open_property(self):
        r = GateResult(decision=GateDecision.OPEN, reason='ok', dept_code='D01')
        self.assertTrue(r.open)
        self.assertFalse(r.closed)

    def test_gate_result_closed_property(self):
        r = GateResult(decision=GateDecision.CLOSED, reason='off', dept_code='D01')
        self.assertFalse(r.open)
        self.assertTrue(r.closed)

    def test_gate_result_str(self):
        r = GateResult(decision=GateDecision.OPEN, reason='ok', dept_code='D02')
        s = str(r)
        self.assertIn('D02', s)
        self.assertIn('open', s)

    # --- master kill switch (agent_enabled=False) ---------------------------

    def test_master_kill_switch_off_closes_gate(self):
        t = self._make_tenant('ks-off', agent_enabled=False)
        self._subscribe(t, 'D01', active=True)
        result = AgentGate(t).check('D01')
        self.assertTrue(result.closed)
        self.assertEqual(result.decision, GateDecision.CLOSED)

    def test_master_kill_switch_off_regardless_of_subscription(self):
        """Kill switch off → gate closed even if subscription is active."""
        t = self._make_tenant('ks-sub', agent_enabled=False)
        self._subscribe(t, 'D01', active=True)
        self._subscribe(t, 'D02', active=True)
        for dept in ('D01', 'D02'):
            with self.subTest(dept=dept):
                self.assertTrue(AgentGate(t).check(dept).closed)

    def test_master_kill_switch_off_reason_mentions_agent_enabled(self):
        t = self._make_tenant('ks-reason', agent_enabled=False)
        result = AgentGate(t).check('D01')
        self.assertIn('agent_enabled', result.reason)

    # --- subscription enforcement -------------------------------------------

    def test_no_subscription_closes_gate(self):
        t = self._make_tenant('nosub-gate', agent_enabled=True)
        result = AgentGate(t).check('D01')
        self.assertTrue(result.closed)

    def test_inactive_subscription_closes_gate(self):
        """active=False is the per-department kill switch."""
        t = self._make_tenant('inactive-gate', agent_enabled=True)
        self._subscribe(t, 'D01', active=False)
        result = AgentGate(t).check('D01')
        self.assertTrue(result.closed)

    def test_inactive_subscription_reason_mentions_dept(self):
        t = self._make_tenant('inactive-reason', agent_enabled=True)
        self._subscribe(t, 'D03', active=False)
        result = AgentGate(t).check('D03')
        self.assertIn('D03', result.reason)

    def test_subscription_for_different_dept_does_not_open_gate(self):
        """Subscribing to D02 does not open the gate for D01."""
        t = self._make_tenant('wrong-dept', agent_enabled=True)
        self._subscribe(t, 'D02', active=True)
        result = AgentGate(t).check('D01')
        self.assertTrue(result.closed)

    # --- gate opens when both checks pass -----------------------------------

    def test_gate_opens_with_enabled_tenant_and_active_subscription(self):
        t = self._make_tenant('open-gate', agent_enabled=True)
        self._subscribe(t, 'D01', active=True)
        result = AgentGate(t).check('D01')
        self.assertTrue(result.open)
        self.assertEqual(result.decision, GateDecision.OPEN)

    def test_gate_open_carries_dept_code(self):
        t = self._make_tenant('open-dept', agent_enabled=True)
        self._subscribe(t, 'D05', active=True)
        result = AgentGate(t).check('D05')
        self.assertEqual(result.dept_code, 'D05')
        self.assertTrue(result.open)

    def test_each_dept_checked_independently(self):
        """Open for D01 but closed for D02 (no sub) on same tenant."""
        t = self._make_tenant('multi-dept', agent_enabled=True)
        self._subscribe(t, 'D01', active=True)
        self.assertTrue(AgentGate(t).check('D01').open)
        self.assertTrue(AgentGate(t).check('D02').closed)

    # --- fail-closed: missing agent_enabled field ---------------------------

    def test_missing_agent_enabled_attribute_closes_gate(self):
        """A tenant-like object without agent_enabled defaults to closed."""
        class FakeTenant:
            def is_subscribed(self, dept): return True
        result = AgentGate(FakeTenant()).check('D01')
        self.assertTrue(result.closed)

    # --- repr ---------------------------------------------------------------

    def test_gate_repr(self):
        t = self._make_tenant('repr-gate', agent_enabled=True)
        self.assertIn('AgentGate', repr(AgentGate(t)))

    # --- integration: gate + capacity together ------------------------------

    def test_gate_closed_prevents_capacity_check_being_meaningful(self):
        """Canonical usage: gate-closed → stop; capacity check is for gate-open only."""
        from decimal import Decimal
        t = self._make_tenant('int-test', agent_enabled=False)
        self._subscribe(t, 'D01', active=True)
        gate_r = AgentGate(t).check('D01')
        self.assertTrue(gate_r.closed)
        # Caller should short-circuit here; capacity result is irrelevant.

    def test_gate_open_then_capacity_auto_approves(self):
        """Gate open + amount within cap → full auto-approve path."""
        from decimal import Decimal
        t = self._make_tenant('int-open', agent_enabled=True, )
        t.plan = 'starter'   # 500 000 XAF cap by plan default
        t.save()
        sub = self._subscribe(t, 'D01', active=True)
        # No explicit cap override → plan default applies
        gate_r = AgentGate(t).check('D01')
        cap_r  = CapacityEngine(t).check('D01', Decimal('100000'))
        self.assertTrue(gate_r.open)
        self.assertTrue(cap_r.auto_approve)
