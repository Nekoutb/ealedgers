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
  - Step 54 — APClassifier (account-code suggestion)      ✓ done
  - Step 55 — APProposer (SYSCOHADA JE builder)           ✓ done (agents/ap/proposer.py)
  - Step 56 — APReviewer (adversarial citation check)     ✓ done (agents/ap/reviewer.py)
  - Step 58 — APDepartment.execute() (ERP CAP.05 + CAP.03) ✓ done (this file)
  - Step 59 — AP emits ``bill.posted`` event on a successful post ✓ done
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


class ConnectorExecutionError(Exception):
    """Raised when posting an approved AP proposal to the ERP fails (Step 58).

    Distinct from validation errors (``ValueError``): the proposal was sound
    but the ERP write itself failed or escalated after retries.
    """


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

    Deterministic — no LLM call.  Takes the merged extractor + classifier
    context and assembles a balanced double-entry vendor-bill JE, resolving the
    supplier (401x), deductible-VAT (445x) and purchase-journal accounts from
    the tenant's own chart of accounts (see :mod:`agents.ap.proposer`).

    Input keys (built up from APClassifier / APExtractor output):
        classified_lines — from APClassifier (each with suggested_account, amount)
        vendor_name, vendor_vat, invoice_date, invoice_number,
        currency, tax_amount, total — from APExtractor

    Output keys on success:
        proposed_je — {
            date, ref, journal_code,
            lines: [{account, label, debit, credit, partner_vat}]
        }
        debit_total, credit_total, balanced (bool),
        currency, extracted_total, total_matches (bool),
        issues (list[str]), needs_review (bool)

    The specialist never raises — failures (no usable lines, no supplier
    account) are returned as ``SpecialistResult(ok=False, error=…)`` so the
    pipeline handles them through the normal stop_on_failure mechanism.
    """

    specialist_type = "proposer"

    def __init__(self, tenant, context=None, *, proposer=None):
        """
        Args:
            tenant:   The Tenant instance (passed to DepartmentSpecialist).
            context:  Optional context dict.
            proposer: Injectable builder for testing.  When None, a
                      :class:`~agents.ap.proposer.SyscohadaBillProposer` is used.
        """
        super().__init__(tenant, context)
        if proposer is None:
            from agents.ap.proposer import SyscohadaBillProposer
            proposer = SyscohadaBillProposer()
        self._proposer = proposer

    def run(self, input_data: dict) -> SpecialistResult:
        """Build the candidate SYSCOHADA journal entry.

        Returns SpecialistResult(ok=True, output={...}) on success.
        Returns SpecialistResult(ok=False, error=...) on any failure — never raises.
        """
        from agents.ap.proposer import ProposerError

        try:
            output = self._proposer.build(input_data, self.tenant)
        except ProposerError as exc:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Proposal failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — pipeline must not crash
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Unexpected proposer error: {type(exc).__name__}: {exc}",
            )

        return SpecialistResult(
            specialist_type=self.specialist_type,
            output=output,
            ok=True,
        )


class APReviewer(DepartmentSpecialist):
    """Adversarially check the proposed JE against SYSCOHADA / CGI rules (Step 56).

    Two layers (see :mod:`agents.ap.reviewer`):
      1. Structural form checks (pure Python — balance, signs, accounts, date).
      2. Adversarial citation check: rules retrieved from the knowledge base
         are handed to the Anthropic API, which must cite them by slug; an
         anti-hallucination guard drops citations to rules it was not given
         and re-reads every ``source_ref`` from the database.

    Input keys (built up from APProposer / earlier specialists):
        proposed_je — the candidate journal entry built by APProposer
        issues, classified_lines, vendor_name, vendor_vat,
        currency, total, tax_amount — supporting context

    Output keys on success:
        approved          — bool (True only when no cited rule fails)
        citations         — list of {rule_slug, source_ref, verdict, notes}
        review_notes      — human-readable summary for the approver
        structural_issues — form problems found before any API call

    A *rejection* is a successful review (``ok=True, approved=False``).
    ``ok=False`` is reserved for the reviewer itself failing (missing API
    key, API error, unparseable response) — never raises either way.
    """

    specialist_type = "reviewer"

    def __init__(self, tenant, context=None, *, reviewer_client=None, reviewer_model=None):
        """
        Args:
            tenant:          The Tenant instance (passed to DepartmentSpecialist).
            context:         Optional context dict.
            reviewer_client: Injectable Anthropic client for testing (Mock).
            reviewer_model:  Model ID override for testing.
        """
        super().__init__(tenant, context)
        from agents.ap.reviewer import AnthropicJEReviewer
        self._reviewer = AnthropicJEReviewer(
            client=reviewer_client,
            model=reviewer_model,
        )

    def run(self, input_data: dict) -> SpecialistResult:
        """Adversarially review the proposed journal entry.

        Returns SpecialistResult(ok=True, output={approved, citations, …})
        whether the verdict is approve or reject.
        Returns SpecialistResult(ok=False, error=...) only when the review
        itself could not run — never raises.
        """
        from agents.ap.reviewer import ReviewerError

        try:
            output = self._reviewer.review(input_data, self.tenant)
        except ReviewerError as exc:
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Review failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — pipeline must not crash
            return SpecialistResult(
                specialist_type=self.specialist_type,
                output={},
                ok=False,
                error=f"Unexpected reviewer error: {type(exc).__name__}: {exc}",
            )

        return SpecialistResult(
            specialist_type=self.specialist_type,
            output=output,
            ok=True,
        )


# ---------------------------------------------------------------------------
# AP Manager — wires specialists into the pipeline
# ---------------------------------------------------------------------------

class APManager(DepartmentManager):
    """The D01 AP department pipeline manager.

    Default pipeline order (matches the bill-processing lifecycle)::

        APExtractor  — extract structured fields from raw document  (Step 53 ✓)
        APClassifier — suggest SYSCOHADA account codes per line     (Step 54 ✓)
        APProposer   — build candidate journal entry                (Step 55 ✓)
        APReviewer   — adversarial citation check against K10/K12  (Step 56 ✓)

    ``stop_on_failure=True`` means the pipeline halts at the first failing
    specialist.  All four specialists are implemented — the pipeline runs
    end-to-end from raw document to reviewed candidate entry.

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

        Posting strategy (Step 58):
          - **CAP.05** (create/post a vendor-bill object) is used when the
            tenant's connector advertises it.
          - Otherwise the balanced journal entry the proposer built is posted
            directly via **CAP.03** — the documented "manual-JE-via-CAP.03"
            gap-fallback (decision P00.10).  This is the live path today, since
            neither the Odoo nor the local-GL connector implements CAP.05 yet.

        The write is wrapped in the saga (:func:`accounting.saga.run_erp_operation`)
        when a real ERP connection exists, so it gets retry/backoff + an
        ``ERPOperation`` audit row; standalone (local-GL) tenants post directly
        against their own ledger.

        Returns a result dict::

            {posted, capability, connector, external_id, state,
             operation_id, result}

        Raises ``ValueError`` when the proposal carries no journal entry, and
        ``ConnectorExecutionError`` when the ERP write fails or escalates.
        On success a ``bill.posted`` event is emitted (Step 59) so downstream
        departments can react; the saved BusEvent id is returned as
        ``event_id`` (``None`` if emission failed — the post still succeeded).
        """
        proposed_je = self._extract_proposed_je(proposal)
        je_lines = proposed_je.get("lines") or []
        if not je_lines:
            raise ValueError("Cannot execute: the proposal has no journal-entry lines.")

        connector_lines = [self._to_connector_line(ln) for ln in je_lines]
        connector = self.connector
        capability, call = self._build_post_call(connector, proposed_je, connector_lines)
        connection = self._active_connection()

        if connection is not None:
            from accounting.saga import run_erp_operation
            outcome = run_erp_operation(
                connection=connection,
                capability=capability,
                func=call,
                method="post_journal_entry",
                tenant=self.tenant,
                request={
                    "ref": proposed_je.get("ref", ""),
                    "journal_code": proposed_je.get("journal_code", ""),
                    "line_count": len(connector_lines),
                },
            )
            if not outcome.ok:
                kind = "escalated" if outcome.escalated else "failed"
                raise ConnectorExecutionError(
                    f"ERP post {kind} after {outcome.attempts} attempt(s): "
                    f"{outcome.operation.error}"
                )
            result = outcome.result or {}
            operation_id = outcome.operation.id
        else:
            result = call() or {}
            operation_id = None

        outcome_dict = {
            "posted": True,
            "capability": capability,
            "connector": getattr(connector, "vendor", type(connector).__name__),
            "external_id": result.get("external_id"),
            "state": result.get("state"),
            "operation_id": operation_id,
            "result": result,
        }

        # Announce the post so downstream departments (D06 Tax, D04 Fixed
        # Assets, D03 Treasury) can react (Step 59). Best-effort: the bill is
        # already in the ledger, so a failure to emit must NOT fail execute().
        try:
            bus_event = self._emit_bill_posted(proposal, proposed_je, outcome_dict)
            outcome_dict["event_id"] = bus_event.id
        except Exception:  # noqa: BLE001 — emission is best-effort
            outcome_dict["event_id"] = None

        return outcome_dict

    def _emit_bill_posted(self, proposal: Proposal, proposed_je: dict, result: dict):
        """Emit the ``bill.posted`` BusEvent for a successful post (Step 59).

        Threads the proposal's ``chain_id`` so the event joins the same causal
        chain as the originating ``bill.received`` event and the proposal
        (Steps 44 + 50).  Returns the saved BusEvent.
        """
        from agents.events import Event, emit

        extractor = next(
            (r.output for r in proposal.specialist_results
             if r.specialist_type == "extractor" and r.ok),
            {},
        )
        inputs = proposal.inputs or {}
        payload = {
            "dept_code": self.dept_code,
            "external_id": result.get("external_id"),
            "capability": result.get("capability"),
            "connector": result.get("connector"),
            "state": result.get("state"),
            "operation_id": result.get("operation_id"),
            "ref": proposed_je.get("ref", ""),
            "journal_code": proposed_je.get("journal_code", ""),
            "vendor_name": extractor.get("vendor_name", ""),
            "vendor_vat": extractor.get("vendor_vat", ""),
            "total": extractor.get("total"),
            "currency": extractor.get("currency", ""),
            "ap_document_id": inputs.get("ap_document_id") or inputs.get("document_id"),
        }
        return emit(Event(
            event_type="bill.posted",
            tenant=self.tenant,
            payload=payload,
            chain_id=proposal.chain_id,
        ))

    # ----- execution helpers --------------------------------------------------

    @staticmethod
    def _extract_proposed_je(proposal: Proposal) -> dict:
        """Pull the proposer's ``proposed_je`` out of a Proposal's results."""
        for r in proposal.specialist_results:
            if r.specialist_type == "proposer" and r.ok:
                return (r.output or {}).get("proposed_je") or {}
        raise ValueError(
            "Cannot execute: the proposal has no successful proposer output."
        )

    @staticmethod
    def _to_connector_line(line: dict) -> dict:
        """Map a proposed-JE line to the connector's line schema.

        Proposer line keys (``account``/``label``) → connector keys
        (``account_code``/``name``); debit/credit pass through as strings
        (both connectors cast them).
        """
        return {
            "account_code": line.get("account"),
            "name": line.get("label") or "",
            "debit": line.get("debit") or "0",
            "credit": line.get("credit") or "0",
            "partner_vat": line.get("partner_vat") or "",
        }

    def _build_post_call(self, connector, proposed_je: dict, connector_lines: list):
        """Return ``(capability, zero_arg_callable)`` for posting the bill.

        Prefers CAP.05 (a richer vendor-bill object) when the connector
        supports it; otherwise posts the balanced JE via CAP.03.
        """
        date = proposed_je.get("date") or None
        ref = proposed_je.get("ref", "")
        journal_code = proposed_je.get("journal_code") or None

        if connector.supports("CAP.05"):
            def _call():
                return connector.execute(
                    "CAP.05", lines=connector_lines, date=date, ref=ref,
                    journal_code=journal_code, post=True,
                )
            return "CAP.05", _call

        def _call():
            return connector.post_journal_entry(
                lines=connector_lines, date=date, ref=ref,
                journal_code=journal_code, post=True,
            )
        return "CAP.03", _call

    def _active_connection(self):
        """The tenant's active, non-manual ERP connection (or None = standalone)."""
        from accounting.models import ERPConnection
        return (
            ERPConnection.objects
            .filter(tenant=self.tenant, is_active=True)
            .exclude(vendor="manual")
            .order_by("-is_primary", "name")
            .first()
        )

    # ----- approval-queue bridge ----------------------------------------------

    def execute_queue_item(self, item) -> dict:
        """Post an APPROVED :class:`ApprovalQueueItem` to the ERP and advance it.

        Rebuilds a :class:`~agents.department.Proposal` from the stored
        specialist results, calls :meth:`execute`, then transitions the item to
        ``executed`` (recording the ERP external id in ``metadata``) or
        ``execution_failed`` on error.  Re-raises the failure so the caller can
        surface it; the item's status is already persisted either way.
        """
        if item.status not in ("approved", "auto_approved"):
            raise ValueError(
                f"Only approved items can be posted; item #{item.pk} is "
                f"'{item.status}'."
            )

        proposal = self._proposal_from_item(item)
        try:
            result = self.execute(proposal)
        except Exception as exc:  # noqa: BLE001 — record + re-raise
            item.mark_execution_failed(f"{type(exc).__name__}: {exc}")
            raise

        item.metadata = {
            **(item.metadata or {}),
            "execution": {
                "external_id": result.get("external_id"),
                "capability": result.get("capability"),
                "connector": result.get("connector"),
                "operation_id": result.get("operation_id"),
            },
        }
        item.save(update_fields=["metadata"])
        item.mark_executed()
        return result

    def _proposal_from_item(self, item) -> Proposal:
        """Reconstruct a Proposal from an ApprovalQueueItem's stored results."""
        results = [
            SpecialistResult(
                specialist_type=r.get("specialist_type", ""),
                output=r.get("output") or {},
                ok=bool(r.get("ok")),
                error=r.get("error", ""),
            )
            for r in (item.specialist_results or [])
        ]
        return Proposal(
            dept_code=item.dept_code,
            tenant_id=self.tenant.id,
            action=item.action,
            inputs=item.inputs or {},
            specialist_results=results,
            chain_id=item.chain_id,
        )
