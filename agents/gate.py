"""Per-department kill-switch + per-tenant subscription enforcement — Step 47.

The ``AgentGate`` answers the question: "Is the agent *allowed* to act
at all in this department for this tenant right now?"  It is the first
check in the auto-action decision chain; the capacity engine (Step 46)
is the second.

Two independent checks must both pass before the gate opens:

1. **Master kill switch** — ``Tenant.agent_enabled`` must be ``True``.
   This is an R-safety flag that a platform operator or the tenant
   themselves can flip to halt *all* automated actions immediately,
   without touching any individual department subscription.  It defaults
   to ``False`` (off) — tenants must consciously opt in to automation.

2. **Dept subscription** — ``Tenant.is_subscribed(dept_code)`` must be
   ``True``.  This checks that the tenant has an *active*
   ``TenantDepartmentSubscription`` row for the given department.
   Setting that row's ``active`` flag to ``False`` is the per-department
   kill switch: work still queues (events are emitted, proposals are
   created), but no auto-action fires.

Design notes
------------
- **Gate ≠ capacity** — the gate is a binary allow/block; the capacity
  engine is a graduated threshold.  A gate-closed result means "stop
  here"; a capacity QUEUE means "work is valid, but a human must review".
- **Fail closed** — any missing or unexpected field on ``tenant``
  defaults to a closed gate.  Wrong direction to fail is open.
- **No DB writes** — ``AgentGate.check()`` is read-only: it queries one
  subscription row (via ``is_subscribed()`` which is already indexed).

Decision flow in a department handler
--------------------------------------
::

    from agents.gate import AgentGate
    from agents.capacity import CapacityEngine

    gate    = AgentGate(tenant)
    gate_r  = gate.check(dept_code)
    if gate_r.closed:
        # Work already queued; nothing further to do automatically.
        return

    cap_r = CapacityEngine(tenant).check(dept_code, amount)
    if cap_r.auto_approve:
        item.approve(automated=True)
    else:  # QUEUE or REFUSE
        item.status = 'pending'

Acceptance test for Step 47
-----------------------------
Set ``tenant.agent_enabled = False``  (or deactivate a subscription)
→ emit any event →
→ BusEvent row created with ``status='queued'``  *(work preserved)*
→ no ``ApprovalQueueItem`` auto-approved; human queue holds the item.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# GateDecision enum
# ---------------------------------------------------------------------------

class GateDecision(str, Enum):
    """Whether the agent may proceed with an automated action.

    Inherits from ``str`` so values compare equal to plain strings without
    calling ``.value``.
    """
    OPEN   = 'open'    # all checks pass — agent may act
    CLOSED = 'closed'  # at least one check failed — agent must not act


# ---------------------------------------------------------------------------
# GateResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """The outcome of ``AgentGate.check()``.

    ``reason`` is a human-readable explanation suitable for a log line or
    an admin tooltip.
    """

    decision:  GateDecision
    reason:    str
    dept_code: str

    # --- convenience properties ---------------------------------------------

    @property
    def open(self) -> bool:
        """True when all gate checks pass and the agent may proceed."""
        return self.decision == GateDecision.OPEN

    @property
    def closed(self) -> bool:
        """True when at least one gate check failed; agent must not act."""
        return self.decision == GateDecision.CLOSED

    def __str__(self) -> str:
        return (
            f'GateResult({self.dept_code}, '
            f'decision={self.decision.value}): {self.reason}'
        )


# ---------------------------------------------------------------------------
# AgentGate
# ---------------------------------------------------------------------------

class AgentGate:
    """Decides whether the agent is allowed to act in a given department.

    Instantiate with a ``Tenant`` instance; the gate reads the tenant's
    ``agent_enabled`` flag and queries ``TenantDepartmentSubscription``.

    Two checks, evaluated in order (first failure wins, fail-closed):
    1. ``tenant.agent_enabled`` — master kill switch.
    2. ``tenant.is_subscribed(dept_code)`` — per-dept subscription.
    """

    def __init__(self, tenant) -> None:
        self.tenant = tenant

    # --- public API ---------------------------------------------------------

    def check(self, dept_code: str) -> GateResult:
        """Return a ``GateResult`` for ``dept_code``.

        Fail-closed: if ``agent_enabled`` is absent on the tenant object
        (e.g. a mock without the field) the gate is treated as closed.
        """
        # 1. Master kill switch
        agent_enabled = getattr(self.tenant, 'agent_enabled', False)
        if not agent_enabled:
            return GateResult(
                decision=GateDecision.CLOSED,
                reason=(
                    'Tenant master kill switch is off '
                    '(agent_enabled=False). No automated actions will fire.'
                ),
                dept_code=dept_code,
            )

        # 2. Per-department subscription
        try:
            subscribed = self.tenant.is_subscribed(dept_code)
        except Exception:
            subscribed = False

        if not subscribed:
            return GateResult(
                decision=GateDecision.CLOSED,
                reason=(
                    f'Tenant has no active subscription to department '
                    f'{dept_code!r}. Work queues for human review.'
                ),
                dept_code=dept_code,
            )

        # All checks pass
        return GateResult(
            decision=GateDecision.OPEN,
            reason='All gate checks pass — agent may act.',
            dept_code=dept_code,
        )

    def __repr__(self) -> str:
        return f'AgentGate(tenant={self.tenant!r})'
