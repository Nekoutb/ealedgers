"""Department base classes — Step 41.

The three ABCs at the heart of the multi-agent runtime:

  BaseDepartment        — one per accounting department (D01–D11); owns the
                          lifecycle from incoming work to ERP execution.
  DepartmentSpecialist  — a focused sub-agent for one processing step
                          (extraction, classification, review, …).
  DepartmentManager     — wires specialists into an ordered pipeline and
                          synthesises their outputs into a Proposal.

These are pure Python (no new Django models). The persistence layer lands
in Step 42 (ApprovalQueueItem) and Step 43 (event bus).

Lifecycle of one work item
--------------------------
  event  →  BaseDepartment.handle()
         →  DepartmentManager.run()       [runs registered specialists]
         →  DepartmentManager.propose()   [wraps results in a Proposal]
         →  [human / auto approval — Step 42]
         →  BaseDepartment.execute()      [posts to ERP via connector]

``connector`` defaults to ``connector_for_tenant(tenant)`` (Step 38)
so standalone-mode tenants (no ERP) work identically to Odoo-connected ones.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Result / Proposal value objects
# ---------------------------------------------------------------------------

@dataclass
class SpecialistResult:
    """Structured output from one DepartmentSpecialist.run() call.

    ``output`` is a free-form dict whose keys are convention between the
    specialist and its consumer (the next specialist or the Proposer).
    ``ok=False`` means the specialist could not complete its task; the
    DepartmentManager decides whether to halt or continue.
    """
    specialist_type: str
    output: dict
    ok: bool
    error: str = ""


@dataclass
class Proposal:
    """A department's proposed action, awaiting approval (Step 42).

    Built by the DepartmentManager from specialist outputs. Carries
    enough context for a human approver (or the auto-approval engine,
    Step 60) to accept or reject.

    ``chain_id`` links this proposal — and the subsequent execution — to
    every other event in the same causal chain (Step 44 wires this to the
    Provenance / AgentRun audit trail).
    """
    dept_code: str                            # 'D01', 'D02', …
    tenant_id: int
    action: str                               # e.g. "Post vendor bill"
    inputs: dict                              # event payload / extracted doc
    specialist_results: list[SpecialistResult]
    metadata: dict = field(default_factory=dict)
    chain_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def all_ok(self) -> bool:
        """True when every specialist completed successfully."""
        return all(r.ok for r in self.specialist_results)


# ---------------------------------------------------------------------------
# DepartmentSpecialist ABC
# ---------------------------------------------------------------------------

class DepartmentSpecialist(ABC):
    """A focused sub-agent for one processing step within a department.

    Specialists are *stateless transformers*: they receive an input dict
    (typically the event payload or the merged output of all preceding
    specialists) and return a SpecialistResult. They never write to the
    ERP directly — only the department's execute() path does.

    Naming convention for ``specialist_type``::

        'extractor'   — OCR / LLM structured extraction
        'classifier'  — account-code / category suggestion
        'reviewer'    — adversarial citation check
        'proposer'    — final JE builder
        'validator'   — balance / business-rule check

    Raise any exception for hard failures; DepartmentManager wraps them in
    SpecialistResult(ok=False, error=...) so the pipeline never crashes.
    """

    specialist_type: str = "base"

    def __init__(self, tenant, context: dict | None = None):
        self.tenant = tenant
        self.context: dict = context or {}

    @abstractmethod
    def run(self, input_data: dict) -> SpecialistResult:
        """Execute the specialist's task and return a SpecialistResult."""


# ---------------------------------------------------------------------------
# DepartmentManager
# ---------------------------------------------------------------------------

class DepartmentManager:
    """Wires an ordered list of DepartmentSpecialists into a pipeline.

    Calling ``run()`` drives an event dict through each registered
    specialist in order, collecting SpecialistResults.  Each specialist
    receives a dict that is the union of the original event and every
    prior specialist's ``output`` — so later specialists always see the
    full enriched context.

    ``stop_on_failure=True`` (default) halts after the first failing
    specialist; ``stop_on_failure=False`` runs all specialists regardless.
    The caller (BaseDepartment.handle) decides what to do with partial
    results.

    ``propose()`` is a convenience that calls ``run()`` and wraps the
    results in a Proposal, ready for the approval queue (Step 42).
    """

    def __init__(
        self,
        department: BaseDepartment,
        specialists: list[DepartmentSpecialist] | None = None,
        stop_on_failure: bool = True,
    ):
        self.department = department
        self._specialists: list[DepartmentSpecialist] = list(specialists or [])
        self.stop_on_failure = stop_on_failure

    # ----- specialist roster --------------------------------------------------

    def register(self, specialist: DepartmentSpecialist) -> None:
        """Append a specialist to the end of the pipeline."""
        self._specialists.append(specialist)

    @property
    def specialists(self) -> list[DepartmentSpecialist]:
        return list(self._specialists)

    # ----- pipeline -----------------------------------------------------------

    def run(self, event: dict) -> list[SpecialistResult]:
        """Drive the event through all specialists; return results list."""
        results: list[SpecialistResult] = []
        current = dict(event)   # running context — each specialist enriches it
        for specialist in self._specialists:
            try:
                result = specialist.run(current)
            except Exception as exc:  # noqa: BLE001 — pipeline must not crash
                result = SpecialistResult(
                    specialist_type=specialist.specialist_type,
                    output={},
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if not result.ok and self.stop_on_failure:
                break           # halt — remaining specialists skipped
            if result.ok:
                current.update(result.output)   # merge into running context
        return results

    def propose(self, event: dict, chain_id: str = "") -> Proposal:
        """Run the pipeline and return results wrapped in a Proposal."""
        results = self.run(event)
        return Proposal(
            dept_code=self.department.dept_code,
            tenant_id=self.department.tenant.id,
            action=self.department.dept_name,
            inputs=event,
            specialist_results=results,
            chain_id=chain_id or str(uuid.uuid4()),
        )


# ---------------------------------------------------------------------------
# BaseDepartment ABC
# ---------------------------------------------------------------------------

class DepartmentDisabledError(Exception):
    """Raised when handle() or execute() is called for a disabled department.

    The tenant has not subscribed to this department
    (TenantDepartmentSubscription.active=False or row absent). Callers
    should surface this to the user rather than queuing work silently.
    """


class BaseDepartment(ABC):
    """Abstract base for every accounting department agent (D01–D11).

    Concrete subclasses declare three class attributes::

        dept_code          = 'D01'
        dept_name          = 'AP — Accounts Payable'
        capabilities_needed = frozenset({'CAP.03', 'CAP.05'})

    and implement two abstract methods::

        handle(event)    — process an event, return a Proposal
        execute(proposal) — execute an APPROVED proposal via the ERP connector

    ``is_enabled()`` checks the tenant's TenantDepartmentSubscription; call
    it first in handle() and raise DepartmentDisabledError if False.

    ``can_auto_approve()`` defaults to False; override in concrete classes to
    implement rule-based auto-approval (built out in Step 60).

    ``connector`` is resolved lazily via ``connector_for_tenant(tenant)``
    (Step 38) when not passed explicitly, so standalone-mode tenants and
    ERP-connected tenants share the same code path.
    """

    dept_code: str = ""
    dept_name: str = ""
    capabilities_needed: frozenset = frozenset()

    def __init__(self, tenant, connector=None):
        """
        Args:
            tenant:    The Tenant instance this department is working for.
            connector: Optional pre-built IConnector (useful in tests to
                       inject a mock). When None, resolved lazily.
        """
        self.tenant = tenant
        self._connector = connector

    # ----- connector resolution -----------------------------------------------

    @property
    def connector(self):
        """The ERP connector for this tenant (lazy, from connector_for_tenant)."""
        if self._connector is not None:
            return self._connector
        from accounting.local_gl import connector_for_tenant
        return connector_for_tenant(self.tenant)

    # ----- subscription gate --------------------------------------------------

    def is_enabled(self) -> bool:
        """True when the tenant has an active subscription for this department."""
        from accounting.models import TenantDepartmentSubscription
        return TenantDepartmentSubscription.objects.filter(
            tenant=self.tenant,
            department=self.dept_code,
            active=True,
        ).exists()

    # ----- lifecycle ----------------------------------------------------------

    @abstractmethod
    def handle(self, event: dict) -> Proposal:
        """Process an incoming work event; return a Proposal (or raise).

        Implementations MUST call ``is_enabled()`` first and raise
        ``DepartmentDisabledError`` if the department is disabled. Beyond
        that, typically delegates to a ``DepartmentManager`` to run the
        specialist pipeline and calls ``propose()`` to build the result.
        """

    @abstractmethod
    def execute(self, proposal: Proposal) -> dict:
        """Execute an APPROVED Proposal against the ERP connector.

        Returns a dict describing the connector's response (e.g.
        ``{"external_id": 1234, "state": "posted"}``). Called only after
        human (or auto) approval — never directly from handle().
        """

    def can_auto_approve(self, proposal: Proposal) -> bool:
        """Return True if this proposal qualifies for automatic approval.

        Default: False — every proposal goes to the human queue. Override
        in concrete departments; the auto-approval rule engine (Step 60)
        expands this with per-tenant caps and pattern matching.
        """
        return False
