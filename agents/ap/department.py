"""D01 — Accounts Payable department (scaffold, Step 51).

Concrete classes for the AP department:

    APExtractor   — extracts vendor / date / lines / totals from raw document
    APClassifier  — suggests SYSCOHADA account codes for each line
    APProposer    — builds candidate JE from classifier output
    APReviewer    — adversarial citation check (K10 IS, K12 WHT rules)
    APManager     — wires the specialist pipeline in order
    APDepartment  — the D01 BaseDepartment concrete class

All specialists are stubs in this step; LLM/OCR integrations land in:
  - Step 52 — bill-received document ingestion handler
  - Step 53 — APExtractor (OCR + LLM structured extraction)
  - Step 54 — APClassifier (account-code suggestion via knowledge retrieval)
  - Step 55 — APProposer (build candidate SYSCOHADA JE)
  - Step 56 — APReviewer (adversarial citation check)
  - Step 58 — APDepartment.execute() (ERP CAP.05 + CAP.03)
  - Step 60 — auto-approval rule engine (can_auto_approve override)

The pipeline, manager, and department classes are production-ready —
no changes to their wiring are expected as the stubs are filled in.
"""

from __future__ import annotations

from agents.department import (
    BaseDepartment,
    DepartmentDisabledError,
    DepartmentManager,
    DepartmentSpecialist,
    Proposal,
    SpecialistResult,
)


# ---------------------------------------------------------------------------
# Specialist stubs
# ---------------------------------------------------------------------------

class APExtractor(DepartmentSpecialist):
    """Extract structured fields from a raw vendor-bill document (Step 53).

    Input keys expected in ``input_data``:
        document_path  — local file path or URL to the PDF / image
        raw_text       — optional pre-extracted text (skips OCR when provided)

    Output keys on success:
        vendor_name, vendor_vat, invoice_date, due_date,
        invoice_number, currency, subtotal, tax_amount, total,
        lines: [{description, quantity, unit_price, amount}]
    """

    specialist_type = "extractor"

    def run(self, input_data: dict) -> SpecialistResult:  # pragma: no cover
        """Stub — OCR + LLM extraction implemented in Step 53."""
        raise NotImplementedError(
            "APExtractor.run() is not yet implemented. "
            "LLM/OCR extraction lands in Step 53."
        )


class APClassifier(DepartmentSpecialist):
    """Suggest SYSCOHADA account codes for each extracted bill line (Step 54).

    Input keys (built up from APExtractor output):
        lines — list of {description, quantity, unit_price, amount}

    Output keys on success:
        classified_lines — list of {
            description, quantity, unit_price, amount,
            suggested_account, suggested_account_name, confidence
        }
    """

    specialist_type = "classifier"

    def run(self, input_data: dict) -> SpecialistResult:  # pragma: no cover
        """Stub — knowledge-retrieval classification implemented in Step 54."""
        raise NotImplementedError(
            "APClassifier.run() is not yet implemented. "
            "Account-code classification lands in Step 54."
        )


class APProposer(DepartmentSpecialist):
    """Build a candidate SYSCOHADA journal entry from classifier output (Step 55).

    Input keys (built up from APClassifier output):
        classified_lines — from APClassifier
        vendor_name, invoice_date, total — from APExtractor

    Output keys on success:
        proposed_je — {
            date, ref, journal_code,
            lines: [{account, label, debit, credit, partner_vat}]
        }
        debit_total, credit_total, balanced — bool
    """

    specialist_type = "proposer"

    def run(self, input_data: dict) -> SpecialistResult:  # pragma: no cover
        """Stub — JE builder implemented in Step 55."""
        raise NotImplementedError(
            "APProposer.run() is not yet implemented. "
            "Candidate JE construction lands in Step 55."
        )


class APReviewer(DepartmentSpecialist):
    """Adversarially check the proposed JE against SYSCOHADA / CGI rules (Step 56).

    Input keys (built up from APProposer output):
        proposed_je — the candidate journal entry built by APProposer

    Output keys on success:
        approved     — bool (True if all citations pass)
        citations    — list of {rule_slug, source_ref, verdict, notes}
        review_notes — human-readable summary for the approver
    """

    specialist_type = "reviewer"

    def run(self, input_data: dict) -> SpecialistResult:  # pragma: no cover
        """Stub — adversarial citation check implemented in Step 56."""
        raise NotImplementedError(
            "APReviewer.run() is not yet implemented. "
            "Adversarial citation check lands in Step 56."
        )


# ---------------------------------------------------------------------------
# AP Manager — wires specialists into the pipeline
# ---------------------------------------------------------------------------

class APManager(DepartmentManager):
    """The D01 AP department pipeline manager.

    Default pipeline order (matches the bill-processing lifecycle)::

        APExtractor  — extract structured fields from raw document
        APClassifier — suggest SYSCOHADA account codes per line
        APProposer   — build candidate journal entry
        APReviewer   — adversarial citation check against K10/K12 rules

    ``stop_on_failure=True`` means the pipeline halts at the first failing
    specialist. While all four are stubs (Steps 53–56 fill them in), the
    first specialist (APExtractor) will always raise NotImplementedError,
    so ``run()`` returns a single failed SpecialistResult in the interim.

    Once all specialists are implemented the pipeline runs end-to-end
    without any changes to this class.
    """

    def __init__(self, department: "APDepartment", *, tenant=None):
        _tenant = tenant or department.tenant
        super().__init__(
            department=department,
            specialists=[
                APExtractor(_tenant),
                APClassifier(_tenant),
                APProposer(_tenant),
                APReviewer(_tenant),
            ],
            stop_on_failure=True,
        )


# ---------------------------------------------------------------------------
# APDepartment — D01 Accounts Payable
# ---------------------------------------------------------------------------

class APDepartment(BaseDepartment):
    """D01 Accounts Payable — the first live accounting agent department.

    Full lifecycle (Steps 52–60)::

        bill.received event
            → APExtractor  (OCR + LLM extraction,           Step 53)
            → APClassifier (account-code suggestion,         Step 54)
            → APProposer   (build candidate SYSCOHADA JE,   Step 55)
            → APReviewer   (adversarial citation check,      Step 56)
            → ApprovalQueueItem (human review,               Step 57 UI)
            → execute()    (ERP CAP.05 + CAP.03,             Step 58)
            → bill.posted event emitted                     (Step 59)

    Auto-approval rule engine with per-tenant caps added in Step 60
    (``can_auto_approve`` override + auto-action routing in handle).

    Class attributes::

        dept_code          = 'D01'
        dept_name          = 'AP — Accounts Payable'
        capabilities_needed = frozenset({'CAP.03', 'CAP.05'})
    """

    dept_code = "D01"
    dept_name = "AP — Accounts Payable"
    capabilities_needed = frozenset({"CAP.03", "CAP.05"})

    def __init__(self, tenant, connector=None):
        super().__init__(tenant=tenant, connector=connector)
        self._manager = APManager(self)

    @property
    def manager(self) -> APManager:
        """The APManager that orchestrates this department's specialist pipeline."""
        return self._manager

    # ----- lifecycle ----------------------------------------------------------

    def handle(self, event: dict) -> Proposal:
        """Process an incoming AP work event; return a Proposal.

        Gate check: raises ``DepartmentDisabledError`` if the tenant has no
        active D01 subscription (``TenantDepartmentSubscription`` row absent
        or ``active=False``).

        The ``chain_id`` key in ``event`` is threaded through to the returned
        Proposal so the full causal chain is traceable via the audit trail
        (Steps 44 + 50).  If absent, the manager generates a fresh UUID.

        Returns a Proposal whose ``all_ok`` is False while the specialist
        stubs are in place (Steps 53–56 flip this to True as each one
        ships).
        """
        if not self.is_enabled():
            raise DepartmentDisabledError(
                f"D01 AP is not enabled for tenant {self.tenant!r}. "
                "Enable it by adding an active TenantDepartmentSubscription "
                "for department='D01' in tenant settings."
            )
        chain_id = event.get("chain_id", "")
        return self._manager.propose(event, chain_id=chain_id)

    def execute(self, proposal: Proposal) -> dict:
        """Execute an APPROVED AP proposal — post the vendor bill to the ERP.

        Delegates to:
          - CAP.05 (vendor-bill create in Odoo / local GL)
          - CAP.03 (post the resulting journal entry)

        Emits a ``bill.posted`` BusEvent for downstream consumers (D06 Tax,
        D04 Fixed Assets, D03 Treasury) — Steps 58–59 implement this.

        Raises ``NotImplementedError`` until Step 58.
        """
        raise NotImplementedError(
            "APDepartment.execute() is not yet implemented. "
            "ERP execution (CAP.05 + CAP.03) lands in Step 58."
        )
