"""Dispatcher agent — Step 45.

Routes generic inbound work events to the correct department by emitting
a department-specific ``{dept_code}.work.queued`` event on the bus.

Architecture
------------
``RoutingRule`` objects express *predicates* — pure functions that decide
whether a given (event_type, payload) pair should go to a particular
department.  Rules are evaluated in order; the first match wins
("routing table" semantics, same as a router's longest-prefix match).

``Dispatcher`` holds an ordered list of ``RoutingRule`` objects, runs
``resolve()`` (pure logic, no I/O) to pick a target department, then
calls ``route()`` which emits the result as a bus event.

``register_dispatcher()`` subscribes the module-level handler
``_dispatcher_bus_handler`` to one or more event types so the dispatcher
fires automatically when those events are emitted.

Routing result
--------------
``RoutingResult.routed_event_type`` follows the pattern::

    '{dept_code}.work.queued'    # e.g. 'D01.work.queued'

If no rule matches, the event type is ``'work.unrouted'`` — a sentinel
that an operations team can subscribe to for manual triage.

Default rules (evaluated top-to-bottom, first match wins)
----------------------------------------------------------
+-------------------+--------+
| payload doc_type  | dept   |
+===================+========+
| vendor_bill       | D01 AP |
| vendor_invoice    | D01 AP |
| purchase_order    | D01 AP |
| customer_invoice  | D02 AR |
| credit_note       | D02 AR |
| payment           | D03    |
| bank_statement    | D03    |
| asset_acquisition | D04    |
| journal_entry     | D05    |
| period_close      | D00    |
| payroll           | D07    |
+-------------------+--------+

Custom rules may be passed to ``Dispatcher(tenant, rules=[...])``, or
added later with ``dispatcher.add_rule(rule, prepend=True)``.

Usage
-----
::

    from agents.dispatcher import Dispatcher, RoutingRule, register_dispatcher

    # Manual one-shot route
    d = Dispatcher(tenant)
    result = d.route('document.received', {'doc_type': 'vendor_bill'})
    # result.target_dept  == 'D01'
    # result.routed_event_type == 'D01.work.queued'
    # result.was_routed   == True

    # Register on the event bus (call once at startup / AppConfig.ready)
    register_dispatcher()

    # Custom rule: all tax-notice documents → D06 Tax
    rule = RoutingRule.by_payload_value('doc_type', 'tax_notice', 'D06')
    d.add_rule(rule, prepend=True)   # checked before defaults

Handler signature expected by the bus (Step 43)::

    handler(event_type: str, payload: dict, tenant, chain_id: str) -> None
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable


# ---------------------------------------------------------------------------
# RoutingRule
# ---------------------------------------------------------------------------

class RoutingRule:
    """A single routing predicate: if ``matches(event_type, payload)`` is True,
    route to ``dept_code``.

    Build rules with the class-method factories rather than calling
    ``__init__`` directly::

        RoutingRule.by_event_type('tax.received', 'D06')
        RoutingRule.by_payload_value('doc_type', 'vendor_bill', 'D01')
        RoutingRule.by_predicate(lambda et, p: 'payroll' in p, 'D07')
    """

    def __init__(
        self,
        dept_code: str,
        predicate: Callable[[str, dict], bool],
        description: str = '',
    ) -> None:
        self.dept_code = dept_code
        self._predicate = predicate
        self.description = description or repr(predicate)

    def matches(self, event_type: str, payload: dict) -> bool:
        """Evaluate the predicate. Exceptions in the predicate return False
        (fail-safe — a broken rule never crashes the dispatcher)."""
        try:
            return bool(self._predicate(event_type, payload))
        except Exception:
            return False

    # --- factories ----------------------------------------------------------

    @classmethod
    def by_event_type(cls, event_type: str, dept_code: str) -> 'RoutingRule':
        """Match when ``event_type`` equals the given string exactly."""
        return cls(
            dept_code=dept_code,
            predicate=lambda et, p, _et=event_type: et == _et,
            description=f'event_type=={event_type!r}',
        )

    @classmethod
    def by_payload_value(cls, key: str, value: object, dept_code: str) -> 'RoutingRule':
        """Match when ``payload[key] == value``."""
        return cls(
            dept_code=dept_code,
            predicate=lambda et, p, _k=key, _v=value: p.get(_k) == _v,
            description=f'payload[{key!r}]=={value!r}',
        )

    @classmethod
    def by_predicate(
        cls,
        fn: Callable[[str, dict], bool],
        dept_code: str,
        description: str = '',
    ) -> 'RoutingRule':
        """Match when ``fn(event_type, payload)`` returns True."""
        return cls(dept_code=dept_code, predicate=fn, description=description)

    def __repr__(self) -> str:
        return f'RoutingRule({self.dept_code!r}, {self.description})'


# ---------------------------------------------------------------------------
# RoutingResult
# ---------------------------------------------------------------------------

@dataclass
class RoutingResult:
    """The outcome of a single routing decision.

    ``target_dept`` is ``None`` when no rule matched; ``routed_event_type``
    is ``'work.unrouted'`` in that case.
    ``chain_id`` is the UUID threaded through to the emitted BusEvent.
    """

    original_event_type: str
    target_dept: str | None       # e.g. 'D01', or None for unrouted
    routed_event_type: str        # e.g. 'D01.work.queued' or 'work.unrouted'
    chain_id: str

    @property
    def was_routed(self) -> bool:
        """True when a rule matched and the event was routed to a department."""
        return self.target_dept is not None


# ---------------------------------------------------------------------------
# Default routing table
# ---------------------------------------------------------------------------

_DEFAULT_RULES: list[RoutingRule] = [
    # AP — Accounts Payable
    RoutingRule.by_payload_value('doc_type', 'vendor_bill',      'D01'),
    RoutingRule.by_payload_value('doc_type', 'vendor_invoice',   'D01'),
    RoutingRule.by_payload_value('doc_type', 'purchase_order',   'D01'),
    # AR — Accounts Receivable
    RoutingRule.by_payload_value('doc_type', 'customer_invoice', 'D02'),
    RoutingRule.by_payload_value('doc_type', 'credit_note',      'D02'),
    # Treasury
    RoutingRule.by_payload_value('doc_type', 'payment',          'D03'),
    RoutingRule.by_payload_value('doc_type', 'bank_statement',   'D03'),
    # Fixed Assets
    RoutingRule.by_payload_value('doc_type', 'asset_acquisition','D04'),
    # GL — General Ledger
    RoutingRule.by_payload_value('doc_type', 'journal_entry',    'D05'),
    # Controller
    RoutingRule.by_payload_value('doc_type', 'period_close',     'D00'),
    # Payroll
    RoutingRule.by_payload_value('doc_type', 'payroll',          'D07'),
]

# Event types the dispatcher subscribes to by default.
_DISPATCHER_EVENT_TYPES: tuple[str, ...] = ('document.received', 'work.submitted')


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    """Routes an inbound event to the correct department.

    Rules are evaluated top-to-bottom; the first match wins.

    ``resolve()`` is the pure logic layer (no I/O).
    ``route()`` calls ``resolve()`` then emits the result on the event bus.
    """

    dept_code: str = 'dispatcher'

    def __init__(self, tenant, rules: list[RoutingRule] | None = None) -> None:
        self.tenant = tenant
        self._rules: list[RoutingRule] = (
            list(rules) if rules is not None else list(_DEFAULT_RULES)
        )

    # --- pure logic ---------------------------------------------------------

    def resolve(
        self,
        event_type: str,
        payload: dict,
    ) -> tuple[str | None, str]:
        """Return ``(target_dept, routed_event_type)`` without any I/O.

        ``target_dept`` is ``None`` if no rule matched.
        ``routed_event_type`` is ``'{target_dept}.work.queued'`` or
        ``'work.unrouted'``.
        """
        for rule in self._rules:
            if rule.matches(event_type, payload):
                dept = rule.dept_code
                return dept, f'{dept}.work.queued'
        return None, 'work.unrouted'

    # --- full execution (resolve + emit) ------------------------------------

    def route(
        self,
        event_type: str,
        payload: dict,
        chain_id: str = '',
    ) -> RoutingResult:
        """Resolve the target department and emit a routed bus event.

        The emitted event's ``event_type`` follows the pattern::

            '{dept_code}.work.queued'    # e.g. 'D01.work.queued'
            'work.unrouted'              # if no rule matched

        The emitted event carries the original ``payload`` augmented with
        two metadata keys::

            '_routed_from'   : the original event_type
            '_target_dept'   : the matched dept_code ('' if unrouted)

        Returns a ``RoutingResult`` describing the decision.
        """
        if not chain_id:
            chain_id = str(uuid.uuid4())

        target_dept, routed_event_type = self.resolve(event_type, payload)

        routed_payload = {
            **payload,
            '_routed_from': event_type,
            '_target_dept': target_dept or '',
        }

        from agents.events import Event, emit as bus_emit
        bus_emit(Event(
            event_type=routed_event_type,
            tenant=self.tenant,
            payload=routed_payload,
            chain_id=chain_id,
        ))

        return RoutingResult(
            original_event_type=event_type,
            target_dept=target_dept,
            routed_event_type=routed_event_type,
            chain_id=chain_id,
        )

    # --- rule management ----------------------------------------------------

    def add_rule(self, rule: RoutingRule, *, prepend: bool = False) -> None:
        """Add a routing rule.

        ``prepend=True`` inserts the rule *before* the existing rules so it
        takes priority over the defaults.  ``prepend=False`` (default)
        appends it after — useful for catch-all fallback rules.
        """
        if prepend:
            self._rules.insert(0, rule)
        else:
            self._rules.append(rule)

    @property
    def rules(self) -> list[RoutingRule]:
        """Shallow copy of the current rule list (safe to inspect; mutate
        via ``add_rule()``, not by modifying the list directly)."""
        return list(self._rules)

    def __repr__(self) -> str:
        return (
            f'Dispatcher(tenant={self.tenant!r}, '
            f'rules={len(self._rules)})'
        )


# ---------------------------------------------------------------------------
# Bus integration
# ---------------------------------------------------------------------------

def _dispatcher_bus_handler(
    event_type: str,
    payload: dict,
    tenant,
    chain_id: str,
) -> None:
    """Bus-event handler: run the dispatcher for the tenant that owns the event.

    Registered as a handler by ``register_dispatcher()``. It is a stable
    module-level function so ``subscribe()``'s idempotency check works
    correctly — repeated calls to ``register_dispatcher()`` do not stack
    duplicate handlers.
    """
    Dispatcher(tenant).route(event_type, payload, chain_id=chain_id)


def register_dispatcher(
    event_types: tuple[str, ...] | None = None,
) -> None:
    """Subscribe ``_dispatcher_bus_handler`` to the given event types.

    ``event_types`` defaults to ``_DISPATCHER_EVENT_TYPES``
    (``'document.received'`` and ``'work.submitted'``).

    Call once at application startup, e.g. in an ``AppConfig.ready()``::

        from agents.dispatcher import register_dispatcher
        register_dispatcher()

    Idempotent: calling it multiple times (e.g. on hot-reload) does not
    stack duplicate handlers because ``subscribe()`` de-duplicates.
    """
    from agents.events import subscribe
    for et in (event_types or _DISPATCHER_EVENT_TYPES):
        subscribe(et, _dispatcher_bus_handler)
