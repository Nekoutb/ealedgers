"""Drift detector + reconciliation report (Step 37).

Compares what EA Ledgers BELIEVES it wrote to the ERP (successful
``ERPOperation``s carrying an external move id) against the ERP's CURRENT
state, and classifies each:

  - matched          — we recorded it and it's still posted in the ERP  ✓
  - missing_in_erp    — we recorded posting it, but the ERP has no such move  ⚠ drift
  - mismatched        — the move exists but isn't posted (reset to draft / cancelled)  ⚠ drift
  - untracked_in_erp  — posted moves in the ERP with no recorded operation
                        (expected for human-posted entries — informational, NOT drift)

``drift`` = missing + mismatched, and should be ZERO on a healthy tenant.
Read-only against the ERP. The ERP reads are injectable (``read_states`` /
``list_posted_ids``) so the detector is testable offline and connector-agnostic;
the defaults read Odoo's ``account.move``.
"""

from dataclasses import dataclass, field

from accounting.models import ERPOperation


@dataclass
class ReconciliationReport:
    connection_id: int
    checked: int = 0
    matched: int = 0
    missing_in_erp: list = field(default_factory=list)
    mismatched: list = field(default_factory=list)
    untracked_in_erp: list = field(default_factory=list)

    @property
    def drift_count(self):
        return len(self.missing_in_erp) + len(self.mismatched)

    @property
    def clean(self):
        return self.drift_count == 0

    def summary(self):
        return (f"checked={self.checked} matched={self.matched} "
                f"missing={len(self.missing_in_erp)} "
                f"mismatched={len(self.mismatched)} "
                f"untracked={len(self.untracked_in_erp)} "
                f"drift={self.drift_count}")


def _recorded_moves(connection):
    """``external_id -> ERPOperation`` for this connection's successful writes
    that produced an ERP move id."""
    out = {}
    qs = (ERPOperation.objects
          .filter(connection=connection, status="success")
          .order_by("started_at"))
    for op in qs:
        eid = (op.external_ids or {}).get("external_id")
        if eid is not None:
            out[eid] = op
    return out


def reconcile_connection(connection, *, read_states=None, list_posted_ids=None):
    """Build a :class:`ReconciliationReport` for one connection.

    ``read_states(ids) -> {id: state_or_None}`` reads the ERP's current state
    for the given move ids (defaults to reading Odoo ``account.move``). Only
    called when there is something recorded to verify. ``list_posted_ids() ->
    [id, …]`` lists the ERP's posted move ids (to surface untracked entries);
    omit to skip that pass.
    """
    recorded = _recorded_moves(connection)
    report = ReconciliationReport(
        connection_id=connection.id, checked=len(recorded))

    if recorded:
        if read_states is None:
            read_states = _odoo_state_reader(connection)
        states = read_states(list(recorded.keys()))
        for eid in recorded:
            state = states.get(eid)
            if state is None:
                report.missing_in_erp.append(eid)
            elif state != "posted":
                report.mismatched.append({"external_id": eid, "state": state})
            else:
                report.matched += 1

    if list_posted_ids is not None:
        erp_ids = set(list_posted_ids())
        report.untracked_in_erp = sorted(erp_ids - set(recorded.keys()))

    return report


# ----- default Odoo readers (read-only) -----------------------------------

def _odoo_state_reader(connection):
    from connectors.registry import build_connector

    def _read(ids):
        client = build_connector(connection).client
        rows = client.read("account.move", ids, ["state"])
        return {r["id"]: r.get("state") for r in rows}
    return _read


def odoo_posted_id_lister(connection):
    from connectors.registry import build_connector

    def _list():
        client = build_connector(connection).client
        rows = client.search_read(
            "account.move", domain=[["state", "=", "posted"]], fields=["id"])
        return [r["id"] for r in rows]
    return _list
