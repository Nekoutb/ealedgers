"""D01 — Accounts Payable department.

Concrete classes for the AP department:

    APExtractor   — extracts vendor / date / lines / totals from raw document
                    (Step 53 — Anthropic LLM extraction, IMPLEMENTED)
    APClassifier  — suggests SYSCOHADA account codes for each line (Step 54)
    APProposer    — builds candidate JE from classifier output (Step 55)
    APReviewer    — adversarial citation check (K10 IS, K12 WHT rules) (Step 56)
    APManager     — wires the specialist pipeline in order
    APDepartment  — the D01 BaseDepartment concrete class

LLM/OCR integrations status:
  - Step 52 — APDocument ingestion + BusEvent dispatch  ✓ done
  - Step 53 — APExtractor (Anthropic tool_use extraction) ✓ done (this file)
  - Step 54 — APClassifier (account-code suggestion)      stub → Step 54
  - Step 55 — APProposer (SYSCOHADA JE builder)           stub → Step 55
  - Step 56 — APReviewer (adversarial citation check)     stub → Step 56
  - Step 58 — APDepartment.execute() (CAP.05 + CAP.03)   stub → Step 58
  - Step 60 — auto-approval rule engine                   stub → Step 60

The pipeline, manager, and department classes are production-ready —
no changes to their wiring are expected as the remaining stubs are filled in.
"""

from __future__ import annotations

import os

from django.conf import settings as djsettings

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

    Reads the uploaded file from ``MEDIA_ROOT`` using the ``file_path`` key
    set by the ap_inbox view when the BusEvent is dispatched, then calls the
    Anthropic Messages API (tool_use) to extract structured fields.

    Input keys expected in ``input_data``:
        file_path      — path to the file, relative to MEDIA_ROOT
                         (also accepted: ``document_path`` as an alias)
        content_type   — MIME type (optional; inferred from extension if absent)

    Output keys on success:
        vendor_name, vendor_vat, invoice_date, due_date,
        invoice_number, currency, subtotal, tax_amount, total,
        lines: [{description, quantity, unit_price, amount}]

    The specialist never raises — failures are returned as
    ``SpecialistResult(ok=False, error=…)`` so the pipeline handles them
    through the normal stop_on_failure mechanism.
    """

    specialist_type = "extractor"

    # Extension → MIME type mapping (for content_type inference)
    _EXT_TO_TYPE: dict[str, str] = {
        ".pdf":  "application/pdf",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
        ".tif":  "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }

    def __init__(self, tenant, context=None, *, extractor_client=None, extractor_model=None):
        """
        Args:
            tenant:            The Tenant instance (passed to DepartmentSpecialist).
            context:           Optional context dict.
            extractor_client:  Injectable Anthropic client for testing (Mock).
                               When None, a real client is created from settings.
            extractor_model:   Model ID override for testing.
        """
        super().__init__(tenant, context)
        from agents.ap.extractor import AnthropicBillExtractor
        self._extractor = AnthropicBillExtractor(
            client=extractor_client,
            model=extractor_model,
        )

    def run(self, input_data: dict) -> SpecialistResult:
        """Extract vendor / date / lines / totals from the bill document.

        Returns SpecialistResult(ok=True, output={...}) on success.
        Returns SpecialistResult(ok=False, error=...) on any failure — never raises.
        """
        from agents.ap.extractor import ExtractorError

        # --- 1. Resolve file path ---
        rel_path = input_data.get("file_path") or input_data.get("document_path", "")
        if not rel_path:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error="No file_path or document_path in input_data.",
            )

        # Absolute paths (e.g. temp files in tests) bypass MEDIA_ROOT join
        if os.path.isabs(rel_path):
            abs_path = rel_path
        else:
            media_root = str(getattr(djsettings, "MEDIA_ROOT", ""))
            abs_path = os.path.join(media_root, rel_path)

        # --- 2. Check existence ---
        if not os.path.exists(abs_path):
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Document file not found: {abs_path!r}",
            )

        # --- 3. Read file ---
        try:
            with open(abs_path, "rb") as fh:
                file_bytes = fh.read()
        except OSError as exc:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Could not read document: {exc}",
            )

        # --- 4. Determine content type ---
        content_type = input_data.get("content_type", "")
        if not content_type:
            ext = os.path.splitext(rel_path)[1].lower()
            content_type = self._EXT_TO_TYPE.get(ext, "application/pdf")

        # --- 5. Call the LLM extractor ---
        try:
            extracted = self._extractor.extract(file_bytes, content_type)
        except ExtractorError as exc:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Extraction failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — pipeline must not crash
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Unexpected extractor error: {type(exc).__name__}: {exc}",
            )

        return SpecialistResult(
            specialist_type=self.specialist_type,
            output=extracted,
            ok=True,
        )


class APClassifier(DepartmentSpecialist):
    """Suggest SYSCOHADA account codes for each extracted bill line (Step 54).

    Receives the merged context from APExtractor and classifies each line
    against the tenant's chart of accounts via the Anthropic API.

    Input keys expected in ``input_data`` (from APExtractor output):
        lines — list of {description, quantity, unit_price, amount}

    Output keys on success:
        classified_lines — list of {
            description, quantity, unit_price, amount,
            suggested_account, suggested_account_name, confidence, reasoning
        }

    The specialist never raises — failures are returned as
    ``SpecialistResult(ok=False, error=…)`` so the pipeline handles them
    through the normal stop_on_failure mechanism.
    """

    specialist_type = "classifier"

    def __init__(self, tenant, context=None, *, classifier_client=None, classifier_model=None):
        """
        Args:
            tenant:            The Tenant instance (passed to DepartmentSpecialist).
            context:           Optional context dict.
            classifier_client: Injectable Anthropic client for testing (Mock).
            classifier_model:  Model ID override for testing.
        """
        super().__init__(tenant, context)
        from agents.ap.classifier import AnthropicLineClassifier
        self._classifier = AnthropicLineClassifier(
            client=classifier_client,
            model=classifier_model,
        )

    def run(self, input_data: dict) -> SpecialistResult:
        """Classify each bill line with a SYSCOHADA account code.

        Returns SpecialistResult(ok=True, output={'classified_lines': [...]}) on success.
        Returns SpecialistResult(ok=False, error=...) on any failure — never raises.
        """
        from agents.ap.classifier import ClassifierError

        lines = input_data.get("lines", [])
        # An empty lines list is a valid (if unusual) result — return ok=True
        if not lines:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={"classified_lines": []},
                ok=True,
            )

        try:
            classified = self._classifier.classify(lines, self.tenant)
        except ClassifierError as exc:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Classification failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — pipeline must not crash
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Unexpected classifier error: {type(exc).__name__}: {exc}",
            )

        return SpecialistResult(
            specialist_type=self.specialist_type,
            output={"classified_lines": classified},
            ok=True,
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

        APExtractor  — extract structured fields from raw document  (Step 53 ✓)
        APClassifier — suggest SYSCOHADA account codes per line     (Step 54 ✓)
        APProposer   — build candidate journal entry                (Step 55)
        APReviewer   — adversarial citation check against K10/K12  (Step 56)

    ``stop_on_failure=True`` means the pipeline halts at the first failing
    specialist.  APExtractor and APClassifier are implemented; APProposer and
    APReviewer are still stubs (Steps 55–56).

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
