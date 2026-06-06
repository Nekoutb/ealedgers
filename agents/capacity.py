"""Per-department capacity caps + plan-tier enforcement — Step 46.

Answers the question: "May the agent auto-post this amount in this
department, or must it go to a human for approval?"

Decision hierarchy
------------------
For a given (dept_code, amount) pair:

  1. Is the tenant subscribed to the department and is the subscription
     active?  If **no** → ``QUEUE`` (no subscription, no auto-actions).

  2. Does the subscription have an explicit ``auto_action_cap > 0``?
     If **yes** → use that cap (per-dept override takes priority).

  3. Otherwise fall back to the plan-tier default cap
     (``PLAN_DEFAULT_CAPS[tenant.plan]``).

  4. Evaluate:
       - ``amount <= 0``      → ``REFUSE``  (invalid financial amount)
       - ``cap == 0``         → ``QUEUE``   (zero cap = always queue)
       - ``amount <= cap``    → ``AUTO_APPROVE``
       - ``amount > cap``     → ``QUEUE``

``QUEUE`` is used (not ``REFUSE``) whenever the work is valid but the
amount exceeds the threshold.  The work is not lost — it lands in the
human-approval queue.  ``REFUSE`` is reserved for genuinely invalid
inputs (non-positive amounts).

Plan-tier defaults (XAF, OHADA market)
---------------------------------------
+-----------+-----------------+-----------------------------------------+
| Plan      | Default cap     | Rationale                               |
+===========+=================+=========================================+
| free      | 0               | No auto-actions; all work queued        |
| starter   | 500 000 XAF     | Small transactions only (~€762)         |
| pro       | 5 000 000 XAF   | Mid-size transactions (~€7 600)         |
| enterprise| 50 000 000 XAF  | Large transactions (~€76 000); override |
+-----------+-----------------+-----------------------------------------+

Usage
-----
::

    from agents.capacity import CapacityEngine, CapacityDecision

    engine = CapacityEngine(tenant)
    result = engine.check('D01', Decimal('250000'))

    if result.auto_approve:
        item.status = 'auto_approved'
    elif result.should_queue:
        item.status = 'pending'    # goes to human review
    else:  # refused
        raise ValueError(result.reason)

Integration with BaseDepartment (Step 60+)
-------------------------------------------
``BaseDepartment.can_auto_approve(proposal)`` will call::

    engine = CapacityEngine(self.tenant)
    result = engine.check(self.dept_code, proposal.inputs.get('amount', 0))
    return result.auto_approve

Until Step 60 wires this, ``can_auto_approve`` defaults to ``False`` and
all proposals go to the human queue.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Plan-tier default caps (XAF, integer precision — SYSCOHADA uses 0 decimals)
# ---------------------------------------------------------------------------

PLAN_DEFAULT_CAPS: dict[str, Decimal] = {
    'free':       Decimal('0'),
    'starter':    Decimal('500000'),
    'pro':        Decimal('5000000'),
    'enterprise': Decimal('50000000'),
}


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------

class CapacityDecision(str, Enum):
    """Outcome of a capacity check.

    Inherits from ``str`` so values can be stored/compared as plain
    strings without calling ``.value`` everywhere.
    """
    AUTO_APPROVE = 'auto_approve'  # within cap — agent may act immediately
    QUEUE        = 'queue'          # over cap or cap=0 — needs human approval
    REFUSE       = 'refuse'         # invalid input (non-positive amount)


# ---------------------------------------------------------------------------
# CapacityResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapacityResult:
    """The outcome of ``CapacityEngine.check()``.

    ``cap`` is the effective cap that was applied (0 = always queue).
    ``amount`` is the value that was tested.
    """

    decision:  CapacityDecision
    reason:    str
    cap:       Decimal
    amount:    Decimal
    dept_code: str

    # --- convenience properties ---------------------------------------------

    @property
    def auto_approve(self) -> bool:
        return self.decision == CapacityDecision.AUTO_APPROVE

    @property
    def should_queue(self) -> bool:
        return self.decision == CapacityDecision.QUEUE

    @property
    def refused(self) -> bool:
        return self.decision == CapacityDecision.REFUSE

    def __str__(self) -> str:
        return (
            f'CapacityResult({self.dept_code}, '
            f'amount={self.amount}, cap={self.cap}, '
            f'decision={self.decision.value}): {self.reason}'
        )


# ---------------------------------------------------------------------------
# CapacityEngine
# ---------------------------------------------------------------------------

class CapacityEngine:
    """Evaluates whether the agent may auto-act on a proposal.

    Instantiate with a ``Tenant`` instance; the engine reads the tenant's
    plan and per-department subscriptions from the DB.
    """

    def __init__(self, tenant) -> None:
        self.tenant = tenant

    # --- internal helpers ---------------------------------------------------

    def effective_cap(self, dept_code: str) -> Decimal:
        """Return the effective auto-action cap for ``dept_code``.

        Priority:
        1. Active subscription's ``auto_action_cap`` if > 0 (explicit override)
        2. Plan-tier default from ``PLAN_DEFAULT_CAPS``
        3. ``Decimal('0')`` if no active subscription or unknown plan
        """
        from accounting.models import TenantDepartmentSubscription
        sub = (
            TenantDepartmentSubscription.objects
            .filter(tenant=self.tenant, department=dept_code, active=True)
            .first()
        )
        if sub is None:
            # Not subscribed to this department at all
            return Decimal('0')

        if sub.auto_action_cap > 0:
            # Explicit per-dept override → honour it
            return sub.auto_action_cap

        # Fall back to the plan tier's default
        plan = getattr(self.tenant, 'plan', 'free')
        return PLAN_DEFAULT_CAPS.get(plan, Decimal('0'))

    # --- public API ---------------------------------------------------------

    def check(self, dept_code: str, amount: Decimal) -> CapacityResult:
        """Decide whether ``amount`` can be auto-approved in ``dept_code``.

        See module docstring for the full decision hierarchy.
        """
        # Normalise: accept int/float/str as well as Decimal
        amount = Decimal(str(amount))
        cap = self.effective_cap(dept_code)

        if amount <= 0:
            return CapacityResult(
                decision=CapacityDecision.REFUSE,
                reason='Amount must be positive.',
                cap=cap,
                amount=amount,
                dept_code=dept_code,
            )

        if cap == 0:
            return CapacityResult(
                decision=CapacityDecision.QUEUE,
                reason='Auto-action cap is 0 — all proposals queued for human approval.',
                cap=cap,
                amount=amount,
                dept_code=dept_code,
            )

        if amount <= cap:
            return CapacityResult(
                decision=CapacityDecision.AUTO_APPROVE,
                reason=f'{amount} is within the auto-action cap of {cap}.',
                cap=cap,
                amount=amount,
                dept_code=dept_code,
            )

        # amount > cap
        return CapacityResult(
            decision=CapacityDecision.QUEUE,
            reason=f'{amount} exceeds the auto-action cap of {cap} — queued for human approval.',
            cap=cap,
            amount=amount,
            dept_code=dept_code,
        )

    def __repr__(self) -> str:
        return f'CapacityEngine(tenant={self.tenant!r})'
