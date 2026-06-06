"""Cross-department chain_id provenance threading — Step 44.

The ``chain_id`` UUID is threaded through every durable artefact that
participates in a causal chain:

  BusEvent          → ``chain_id`` (Step 43)
  ApprovalQueueItem → ``chain_id`` (Step 42)
  AgentRun          → ``chain_id`` (Step  7)
  Provenance        → ``chain_id`` (Step  7)

``ChainTracer`` unifies these four sources into a single chronological
timeline so any consumer — a human reviewer, the audit-log UI (Step 50),
or another department's handler — can answer:

    "What happened in this causal chain, across which departments?"

Usage
-----
::

    from agents.chain import ChainTracer

    tracer = ChainTracer(chain_id="<uuid>", tenant=request.tenant)

    # Individual source queries (lazy querysets):
    tracer.bus_events()          # BusEvent rows
    tracer.proposals()           # ApprovalQueueItem rows
    tracer.agent_runs()          # AgentRun rows
    tracer.provenance_records()  # Provenance rows

    # Unified timeline (list, sorted by timestamp):
    for entry in tracer.timeline():
        print(entry.source, entry.dept_code, entry.status, entry.timestamp)

    # Which departments participated?
    tracer.dept_codes()   # e.g. {'D01', 'D05'}

Handler propagation pattern
---------------------------
When a bus-event handler creates a new event or a proposal, it receives
``chain_id`` as its fourth argument and should pass it through::

    def ap_bill_received_handler(event_type, payload, tenant, chain_id):
        dept = APDepartment(tenant)
        mgr  = DepartmentManager(dept, specialists=[...])
        proposal = mgr.propose(payload, chain_id=chain_id)   # ← thread it
        item = ApprovalQueueItem.from_proposal(proposal, tenant)
        item.save()

    subscribe('bill.received', ap_bill_received_handler)

The ``chain_id`` is then carried from the ``BusEvent`` → ``Proposal``
dataclass → ``ApprovalQueueItem`` row → any downstream ``AgentRun`` →
``Provenance`` record, creating a complete end-to-end audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass   # keep runtime import-free for type checkers


# ---------------------------------------------------------------------------
# ChainEntry value object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChainEntry:
    """A single normalised artefact in a causal chain timeline.

    ``source`` identifies which table the row came from so callers can
    render different icons / links in a UI without isinstance-checking.

    ``dept_code`` is blank for BusEvent and Provenance rows (those are not
    department-specific), and filled for ApprovalQueueItem and AgentRun.
    """

    chain_id: str
    source: str          # 'event' | 'proposal' | 'agent_run' | 'provenance'
    dept_code: str       # D01, D02 … or '' for cross-dept artefacts
    label: str           # human-readable one-liner (event_type or action/task)
    status: str          # the row's own status field value
    timestamp: datetime
    object_id: int
    object_repr: str


# ---------------------------------------------------------------------------
# ChainTracer
# ---------------------------------------------------------------------------

class ChainTracer:
    """Query all artefacts that share ``chain_id`` within ``tenant``.

    All queries are **lazy** (Django QuerySets); only ``timeline()`` and
    ``dept_codes()`` evaluate them eagerly to build the combined result.

    Multi-tenancy: every query is scoped to ``tenant`` so one tenant can
    never accidentally see another tenant's chain.
    """

    def __init__(self, chain_id: str, tenant) -> None:
        self.chain_id = chain_id
        self.tenant = tenant

    # --- per-source queries -----------------------------------------------

    def bus_events(self):
        """BusEvent rows that belong to this chain."""
        from accounting.models import BusEvent
        return (BusEvent.objects
                .for_tenant(self.tenant)
                .filter(chain_id=self.chain_id)
                .order_by('created_at'))

    def proposals(self):
        """ApprovalQueueItem rows that belong to this chain."""
        from accounting.models import ApprovalQueueItem
        return (ApprovalQueueItem.objects
                .for_tenant(self.tenant)
                .filter(chain_id=self.chain_id)
                .order_by('created_at'))

    def agent_runs(self):
        """AgentRun rows that belong to this chain."""
        from accounting.models import AgentRun
        return (AgentRun.objects
                .for_tenant(self.tenant)
                .filter(chain_id=self.chain_id)
                .order_by('started_at'))

    def provenance_records(self):
        """Provenance rows that belong to this chain."""
        from accounting.models import Provenance
        return (Provenance.objects
                .for_tenant(self.tenant)
                .filter(chain_id=self.chain_id)
                .order_by('created_at'))

    # --- unified timeline -------------------------------------------------

    def timeline(self) -> list[ChainEntry]:
        """Return all chain artefacts merged into a single chronological list.

        Evaluates all four querysets and sorts by timestamp.  For large
        chains this is fine (a chain rarely exceeds a few dozen rows);
        Step 50's UI viewer may add pagination if needed.
        """
        entries: list[ChainEntry] = []

        for ev in self.bus_events():
            entries.append(ChainEntry(
                chain_id=self.chain_id,
                source='event',
                dept_code='',
                label=ev.event_type,
                status=ev.status,
                timestamp=ev.created_at,
                object_id=ev.pk,
                object_repr=str(ev),
            ))

        for item in self.proposals():
            entries.append(ChainEntry(
                chain_id=self.chain_id,
                source='proposal',
                dept_code=item.dept_code,
                label=item.action[:120],
                status=item.status,
                timestamp=item.created_at,
                object_id=item.pk,
                object_repr=str(item),
            ))

        for run in self.agent_runs():
            entries.append(ChainEntry(
                chain_id=self.chain_id,
                source='agent_run',
                dept_code=run.department,
                label=run.task,
                status=run.status,
                timestamp=run.started_at,
                object_id=run.pk,
                object_repr=str(run),
            ))

        for prov in self.provenance_records():
            entries.append(ChainEntry(
                chain_id=self.chain_id,
                source='provenance',
                dept_code='',
                label=prov.summary[:120] if prov.summary else '(no summary)',
                status=prov.source,
                timestamp=prov.created_at,
                object_id=prov.pk,
                object_repr=str(prov),
            ))

        entries.sort(key=lambda e: e.timestamp)
        return entries

    # --- helpers ----------------------------------------------------------

    def dept_codes(self) -> set[str]:
        """Set of department codes that participated in this chain.

        Derived from ApprovalQueueItem (dept_code) and AgentRun (department).
        Returns an empty set for chains that have no department-specific work.
        """
        codes: set[str] = set()
        for item in self.proposals():
            if item.dept_code:
                codes.add(item.dept_code)
        for run in self.agent_runs():
            if run.department:
                codes.add(run.department)
        return codes

    def is_empty(self) -> bool:
        """True if no artefacts at all exist for this chain_id."""
        return (
            not self.bus_events().exists()
            and not self.proposals().exists()
            and not self.agent_runs().exists()
            and not self.provenance_records().exists()
        )

    def __repr__(self) -> str:
        return f'ChainTracer(chain_id={self.chain_id!r}, tenant={self.tenant!r})'


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def trace_chain(chain_id: str, tenant) -> ChainTracer:
    """Convenience wrapper — returns a ``ChainTracer`` for the given chain.

    Example::

        from agents.chain import trace_chain
        timeline = trace_chain("abc-123", tenant).timeline()
    """
    return ChainTracer(chain_id=chain_id, tenant=tenant)
