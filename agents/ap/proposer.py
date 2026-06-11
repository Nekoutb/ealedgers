"""SYSCOHADA candidate journal-entry builder — AP Proposer specialist (Step 55).

``SyscohadaBillProposer.build(context, tenant)`` turns the merged
extractor + classifier output into a balanced double-entry vendor-bill
journal entry:

    DR   6xx / 2xx  expense (or capex) account   — per classified line (HT)
    DR   445x       TVA récupérable               — tax_amount (if any)
        CR   401x   Fournisseurs                  — total TTC

The proposer is **pure and deterministic** — no LLM call.  Account and
journal resolution comes from the tenant's own chart of accounts
(``accounting.Account`` / ``accounting.Journal``), so the suggested codes the
classifier produced are validated against real accounts before they reach the
draft entry.

The candidate entry is always internally balanced (the supplier credit is set
to the sum of the debits).  When the computed total differs from the total the
extractor read off the document, that is surfaced as an *issue* for the
reviewer (Step 56) and the human approver (Step 57) — it does not unbalance
the entry.

Failure modes that make a draft impossible (no usable line items, no supplier
account in the chart) raise :class:`ProposerError`; the calling
``APProposer.run()`` wraps that into ``SpecialistResult(ok=False, …)`` so the
pipeline never crashes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


# Two-decimal money quantum.  XAF carries 0 decimals in practice, but
# quantising to 2 places is safe for every currency and keeps the arithmetic
# exact regardless of how the extractor rendered the numbers.
_CENTS = Decimal("0.01")


class ProposerError(Exception):
    """Raised when a candidate journal entry cannot be constructed at all."""


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def _to_decimal(value: Any) -> Decimal | None:
    """Best-effort parse of an extractor/classifier number into Decimal.

    Returns ``None`` for missing/blank/un-parseable values so the caller can
    decide whether that line is usable.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value: Decimal) -> str:
    """Render a Decimal as a fixed 2-dp string (JSON-safe, precision-safe)."""
    return str(value.quantize(_CENTS))


# ---------------------------------------------------------------------------
# Chart-of-accounts resolution (all tenant-scoped)
# ---------------------------------------------------------------------------

def _resolve_supplier_account(tenant):
    """The 401x Fournisseurs payable account for ``tenant`` (or None)."""
    from accounting.models import Account  # noqa: PLC0415 — avoid circular import

    qs = Account.objects.for_tenant(tenant).filter(deprecated=False, type="payable")
    return (
        qs.filter(code__startswith="401").order_by("code").first()
        or qs.order_by("code").first()
    )


def _resolve_vat_account(tenant):
    """The 445x TVA-récupérable account for ``tenant`` (or None).

    Prefers 4452 (recoverable VAT on purchases); falls back to any 445x.
    """
    from accounting.models import Account  # noqa: PLC0415

    qs = Account.objects.for_tenant(tenant).filter(deprecated=False)
    return (
        qs.filter(code__startswith="4452").order_by("code").first()
        or qs.filter(code__startswith="445").order_by("code").first()
    )


def _resolve_purchase_journal_code(tenant) -> str:
    """The active purchase journal's code for ``tenant`` (default 'ACH')."""
    from accounting.models import Journal  # noqa: PLC0415

    journal = (
        Journal.objects.for_tenant(tenant)
        .filter(active=True, type="purchase")
        .order_by("code")
        .first()
    )
    return journal.code if journal else "ACH"


def _tenant_account_codes(tenant) -> set[str]:
    """All non-deprecated account codes for ``tenant`` (for validating lines)."""
    from accounting.models import Account  # noqa: PLC0415

    return set(
        Account.objects.for_tenant(tenant)
        .filter(deprecated=False)
        .values_list("code", flat=True)
    )


# ---------------------------------------------------------------------------
# Proposer
# ---------------------------------------------------------------------------

class SyscohadaBillProposer:
    """Build a balanced SYSCOHADA vendor-bill journal entry from pipeline context."""

    def build(self, context: dict, tenant) -> dict[str, Any]:
        """Construct the candidate journal entry.

        Args:
            context: Merged pipeline context — the extractor fields
                (``vendor_name``, ``vendor_vat``, ``invoice_date``,
                ``invoice_number``, ``currency``, ``tax_amount``, ``total``)
                plus ``classified_lines`` from the classifier.
            tenant:  The Tenant whose chart of accounts to resolve against.

        Returns:
            A dict with ``proposed_je``, ``debit_total``, ``credit_total``,
            ``balanced``, ``currency``, ``extracted_total``, ``total_matches``,
            ``issues`` and ``needs_review``.

        Raises:
            ProposerError: when no usable line items exist, or the tenant has
                no supplier (401x) account to credit.
        """
        classified_lines = context.get("classified_lines") or context.get("lines") or []
        if not classified_lines:
            raise ProposerError(
                "Cannot build a journal entry: no classified line items in context."
            )

        supplier = _resolve_supplier_account(tenant)
        if supplier is None:
            raise ProposerError(
                "Cannot build a journal entry: tenant has no 401x supplier "
                "(payable) account in its chart of accounts."
            )

        valid_codes = _tenant_account_codes(tenant)
        issues: list[str] = []
        je_lines: list[dict] = []
        expense_total = Decimal("0")
        low_confidence = False

        # --- 1. Expense / capex debit lines (one per classified line) ---------
        for i, line in enumerate(classified_lines):
            code = (line.get("suggested_account") or "").strip()
            amount = _to_decimal(line.get("amount"))
            desc = line.get("description") or f"Line {i + 1}"

            if amount is None or amount <= 0:
                issues.append(
                    f"Line {i + 1} ({desc!r}) skipped: missing or non-positive amount."
                )
                continue
            if not code:
                issues.append(
                    f"Line {i + 1} ({desc!r}) skipped: no account suggested by the classifier."
                )
                continue
            if code not in valid_codes:
                issues.append(
                    f"Line {i + 1} ({desc!r}) skipped: suggested account {code!r} "
                    "is not in the tenant's chart of accounts."
                )
                continue

            if (line.get("confidence") or "").lower() == "low":
                low_confidence = True

            expense_total += amount
            je_lines.append({
                "account": code,
                "label": desc,
                "debit": _money(amount),
                "credit": _money(Decimal("0")),
                "partner_vat": "",
            })

        if not je_lines:
            raise ProposerError(
                "Cannot build a journal entry: none of the classified lines "
                "produced a usable expense debit (see issues)."
            )

        # --- 2. Deductible-VAT debit line -------------------------------------
        tax = _to_decimal(context.get("tax_amount")) or Decimal("0")
        vat_booked = Decimal("0")
        if tax > 0:
            vat_account = _resolve_vat_account(tenant)
            if vat_account is None:
                issues.append(
                    f"Tax amount {_money(tax)} present but no 445x recoverable-VAT "
                    "account exists in the chart — VAT not booked separately."
                )
            else:
                vat_booked = tax
                je_lines.append({
                    "account": vat_account.code,
                    "label": "TVA récupérable",
                    "debit": _money(tax),
                    "credit": _money(Decimal("0")),
                    "partner_vat": "",
                })

        # --- 3. Supplier (payable) credit line --------------------------------
        # Credit = sum of debits, so the entry is always internally balanced.
        debit_total = expense_total + vat_booked
        je_lines.append({
            "account": supplier.code,
            "label": context.get("vendor_name") or "Fournisseur",
            "debit": _money(Decimal("0")),
            "credit": _money(debit_total),
            "partner_vat": context.get("vendor_vat") or "",
        })

        # --- 4. Reconcile with the extractor's stated total -------------------
        extracted_total = _to_decimal(context.get("total"))
        total_matches = (
            extracted_total is not None
            and extracted_total.quantize(_CENTS) == debit_total.quantize(_CENTS)
        )
        if extracted_total is not None and not total_matches:
            issues.append(
                f"Computed total {_money(debit_total)} differs from the extracted "
                f"document total {_money(extracted_total)} — please verify."
            )

        # --- 5. Assemble ------------------------------------------------------
        currency = context.get("currency") or self._tenant_currency_code(tenant)
        ref = (context.get("invoice_number") or "").strip()
        if not ref:
            vendor = context.get("vendor_name") or "vendor"
            ref = f"Bill — {vendor}"

        proposed_je = {
            "date": context.get("invoice_date") or "",
            "ref": ref,
            "journal_code": _resolve_purchase_journal_code(tenant),
            "lines": je_lines,
        }

        return {
            "proposed_je": proposed_je,
            "debit_total": _money(debit_total),
            "credit_total": _money(debit_total),
            "balanced": True,
            "currency": currency,
            "extracted_total": _money(extracted_total) if extracted_total is not None else None,
            "total_matches": total_matches,
            "issues": issues,
            "needs_review": bool(issues) or low_confidence,
        }

    @staticmethod
    def _tenant_currency_code(tenant) -> str:
        """Best-effort tenant currency code (empty string if unavailable)."""
        currency = getattr(tenant, "currency", None)
        return getattr(currency, "code", "") if currency else ""
