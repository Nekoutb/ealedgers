"""Adversarial citation review of a proposed JE — AP Reviewer specialist (Step 56).

``AnthropicJEReviewer.review(context, tenant)`` is the last specialist before
the human approval queue.  It takes the candidate journal entry built by the
proposer (Step 55) and adversarially checks it against the encoded SYSCOHADA /
CGI knowledge base, returning a confirm-or-reject verdict **with cited
reasons**.

Two layers, cheapest first
--------------------------
1. **Structural checks** (pure Python, no API call): the entry must exist,
   have lines, balance, contain a credit line, and carry no negative or
   double-sided amounts.  A structural failure is a *successful review that
   rejects* — it returns ``approved=False`` with reasons, without spending an
   API call on an entry that is broken on its face.

2. **Adversarial citation check** (Anthropic tool_use): relevant rules are
   retrieved from the knowledge base (:func:`knowledge.retrieval.retrieve`)
   using the entry's own labels and account codes as the query.  Claude is
   instructed to *look for reasons to reject* — deductibility caps (K10),
   withholding obligations (K12), wrong account class, VAT booking — citing
   only the rules it was given.

Anti-hallucination guard
------------------------
Citations whose ``rule_slug`` was not in the retrieved set are **dropped**,
and every surviving citation's ``source_ref`` is re-read from the database —
the model can choose *which* rules apply and *whether* they pass, but it
cannot invent rules or misquote references.

When the knowledge base yields nothing relevant the reviewer cannot perform a
citation check, so it conservatively returns ``approved=False`` ("could not
verify") — the human approver (Step 57) decides.  Infrastructure failures
(missing API key, API errors) raise :class:`ReviewerError`; the calling
``APReviewer.run()`` wraps those into ``SpecialistResult(ok=False, …)``.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings


# ---------------------------------------------------------------------------
# Review tool schema
# ---------------------------------------------------------------------------

REVIEW_TOOL: dict = {
    "name": "review_journal_entry",
    "description": (
        "Record your adversarial review verdict on the proposed journal entry. "
        "Cite ONLY rules from the provided list (by their exact rule_slug). "
        "verdict='reject' if any cited rule is violated or the entry is "
        "otherwise non-compliant; verdict='approve' only when you found no "
        "violation after actively looking for one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["approve", "reject"],
                "description": "Overall verdict on the proposed journal entry.",
            },
            "citations": {
                "type": "array",
                "description": (
                    "One entry per rule you checked the entry against "
                    "(at least the most relevant ones)."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "rule_slug": {
                            "type": "string",
                            "description": "Exact slug of a rule from the provided list.",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["pass", "fail", "warning"],
                            "description": (
                                "pass = entry complies with this rule; "
                                "fail = entry violates this rule; "
                                "warning = compliant but worth the approver's attention"
                            ),
                        },
                        "notes": {
                            "type": "string",
                            "description": "One or two sentences explaining the verdict.",
                        },
                    },
                    "required": ["rule_slug", "verdict"],
                },
            },
            "review_notes": {
                "type": "string",
                "description": (
                    "Human-readable summary for the approver: what was checked, "
                    "what passed, and exactly why the entry is rejected if it is."
                ),
            },
        },
        "required": ["verdict", "citations", "review_notes"],
    },
}


_REVIEW_SYSTEM = (
    "You are an adversarial SYSCOHADA / Cameroon-CGI compliance reviewer.  A "
    "junior agent has drafted a vendor-bill journal entry; your job is to find "
    "reasons to REJECT it before it reaches the human approver.\n\n"
    "You will be given:\n"
    "  1. The proposed journal entry (date, ref, journal, debit/credit lines).\n"
    "  2. Context from the bill (vendor, totals, line confidence, known issues).\n"
    "  3. A list of candidate rules retrieved from the knowledge base, each "
    "with a rule_slug, citation reference, and source text.\n\n"
    "Checks to perform against the cited rules where applicable:\n"
    "• Account-class correctness (class 6 for operating expenses, class 2 for "
    "capitalisable assets, 401 supplier credit, 445 deductible VAT).\n"
    "• CGI deductibility limits and conditions (caps, cash-payment "
    "disallowance, documentation requirements).\n"
    "• Withholding-tax obligations the entry may be missing.\n"
    "• Internal coherence (labels vs accounts, date plausibility).\n\n"
    "Rules of engagement:\n"
    "• Cite ONLY rules from the provided list, by their exact rule_slug — "
    "never invent a rule or a reference.\n"
    "• If a listed rule is irrelevant to this entry, simply omit it.\n"
    "• verdict='reject' if ANY cited rule fails; use 'warning' for points the "
    "approver should see that are not violations.\n"
    "• Bills and rules may be in French, English, or both.\n"
    "• Call the review_journal_entry tool — do not reply with plain text."
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class ReviewerError(Exception):
    """Raised when the review itself cannot run (config, API, parse failures).

    A *rejection* is not an error — it is a successful review outcome.
    """


# ---------------------------------------------------------------------------
# Structural checks (layer 1 — no API call)
# ---------------------------------------------------------------------------

def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def structural_issues(proposed_je: dict) -> list[str]:
    """Return the list of structural problems with the entry ([] = sound).

    These are form checks, not framework judgement: existence, balance,
    sign sanity, and the presence of both debit and credit sides.
    """
    issues: list[str] = []
    lines = proposed_je.get("lines") or []
    if not lines:
        return ["The proposed entry has no lines."]

    debit_total = Decimal("0")
    credit_total = Decimal("0")
    for i, line in enumerate(lines):
        debit = _to_decimal(line.get("debit")) or Decimal("0")
        credit = _to_decimal(line.get("credit")) or Decimal("0")
        if debit < 0 or credit < 0:
            issues.append(f"Line {i + 1} has a negative amount.")
        if debit > 0 and credit > 0:
            issues.append(f"Line {i + 1} has both a debit and a credit amount.")
        if not (line.get("account") or "").strip():
            issues.append(f"Line {i + 1} has no account code.")
        debit_total += debit
        credit_total += credit

    if debit_total != credit_total:
        issues.append(
            f"Entry is unbalanced: debits {debit_total} != credits {credit_total}."
        )
    if debit_total == 0 and credit_total == 0:
        issues.append("Entry total is zero.")
    if not (proposed_je.get("date") or "").strip():
        issues.append("Entry has no date.")
    return issues


# ---------------------------------------------------------------------------
# Rule retrieval + prompt formatting (layer 2 inputs)
# ---------------------------------------------------------------------------

# How many rules to retrieve and how much source text to quote per rule.
_RETRIEVE_K = 8
_SOURCE_TEXT_CHARS = 700


def _build_query(context: dict) -> str:
    """A retrieval query from the entry's own labels, accounts, and vendor."""
    je = context.get("proposed_je") or {}
    parts: list[str] = []
    for line in je.get("lines") or []:
        parts.append(str(line.get("label") or ""))
        parts.append(str(line.get("account") or ""))
    parts.append(str(context.get("vendor_name") or ""))
    parts.append("vendor bill deductibility withholding TVA charges déductibles")
    return " ".join(p for p in parts if p)


def _retrieve_rules(context: dict, tenant) -> list[dict]:
    """Top-K relevant knowledge-base entries for this journal entry."""
    from knowledge.retrieval import retrieve  # noqa: PLC0415 — avoid app-load cycles

    return retrieve(_build_query(context), tenant=tenant, k=_RETRIEVE_K)


def _format_rules(results: list[dict]) -> str:
    """Render retrieved rules as numbered prompt text (slug + ref + source)."""
    blocks = []
    for r in results:
        obj = r.get("object")
        source_text = (getattr(obj, "source_text", "") or
                       getattr(obj, "description", "") or "")
        if len(source_text) > _SOURCE_TEXT_CHARS:
            source_text = source_text[:_SOURCE_TEXT_CHARS] + " […]"
        ref = r.get("source_ref") or "(no citation reference)"
        blocks.append(
            f"rule_slug: {r['slug']}\n"
            f"reference: {ref}\n"
            f"title: {r['title']}\n"
            f"text: {source_text}"
        )
    return "\n\n---\n\n".join(blocks)


def _format_entry(context: dict) -> str:
    """Render the proposed entry + bill context as prompt text."""
    je = context.get("proposed_je") or {}
    lines_text = "\n".join(
        f"  {ln.get('account', '?'):<8} {ln.get('label', ''):<40} "
        f"DR {ln.get('debit', '0')}  CR {ln.get('credit', '0')}"
        for ln in je.get("lines") or []
    )
    prior_issues = context.get("issues") or []
    issues_text = (
        "\n".join(f"  - {i}" for i in prior_issues) if prior_issues else "  (none)"
    )
    confidences = [
        str(ln.get("confidence", "?")) for ln in context.get("classified_lines") or []
    ]
    return (
        f"date: {je.get('date', '')}\n"
        f"ref: {je.get('ref', '')}\n"
        f"journal: {je.get('journal_code', '')}\n"
        f"vendor: {context.get('vendor_name', '')} "
        f"(VAT id: {context.get('vendor_vat', '')})\n"
        f"currency: {context.get('currency', '')}\n"
        f"document total: {context.get('total', '')}  "
        f"tax amount: {context.get('tax_amount', '')}\n"
        f"lines:\n{lines_text}\n"
        f"classifier confidence per line: {', '.join(confidences) or '(unknown)'}\n"
        f"issues already flagged upstream:\n{issues_text}"
    )


# ---------------------------------------------------------------------------
# AnthropicJEReviewer
# ---------------------------------------------------------------------------

class AnthropicJEReviewer:
    """Adversarially review a proposed JE against the knowledge base.

    Args:
        client:  Injectable Anthropic client for testing (Mock avoids API calls).
                 When ``None`` a real client is created from settings.
        model:   Model ID override.  When ``None`` uses
                 ``settings.ANTHROPIC_EXTRACTION_MODEL``
                 (default: ``claude-3-5-haiku-20241022``).
    """

    DEFAULT_MODEL = "claude-3-5-haiku-20241022"
    MAX_TOKENS = 2048

    def __init__(self, *, client=None, model: str | None = None):
        self._client = client
        self._model = model

    # ----- client / model resolution ----------------------------------------

    @property
    def _api_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # noqa: PLC0415 — lazy import
        except ImportError as exc:
            raise ReviewerError(
                "The 'anthropic' package is not installed. "
                "Run: pip install anthropic"
            ) from exc
        api_key = (
            getattr(settings, "ANTHROPIC_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not api_key:
            raise ReviewerError(
                "ANTHROPIC_API_KEY is not configured.  "
                "Set the environment variable on the server."
            )
        return anthropic.Anthropic(api_key=api_key)

    @property
    def model(self) -> str:
        return (
            self._model
            or getattr(settings, "ANTHROPIC_EXTRACTION_MODEL", self.DEFAULT_MODEL)
        )

    # ----- main entry point --------------------------------------------------

    def review(self, context: dict, tenant) -> dict[str, Any]:
        """Review the proposed journal entry in ``context``.

        Returns a dict with:
            approved          — bool
            citations         — [{rule_slug, source_ref, verdict, notes}]
            review_notes      — human-readable summary for the approver
            structural_issues — form problems found before any API call

        Raises:
            ReviewerError — on config failure, API error, or parse failure
                            (never for a rejection — that is a normal result).
        """
        proposed_je = context.get("proposed_je") or {}
        if not proposed_je:
            return {
                "approved": False,
                "citations": [],
                "review_notes": (
                    "Rejected: no proposed journal entry was supplied to review."
                ),
                "structural_issues": ["No proposed_je in context."],
            }

        # --- Layer 1: structural form checks (free) ---------------------------
        form_issues = structural_issues(proposed_je)
        if form_issues:
            return {
                "approved": False,
                "citations": [],
                "review_notes": (
                    "Rejected on structural grounds before citation review: "
                    + " ".join(form_issues)
                ),
                "structural_issues": form_issues,
            }

        # --- Layer 2: retrieve rules ------------------------------------------
        retrieved = _retrieve_rules(context, tenant)
        if not retrieved:
            return {
                "approved": False,
                "citations": [],
                "review_notes": (
                    "Could not verify: no relevant rules were retrieved from "
                    "the knowledge base for this entry.  Manual review required."
                ),
                "structural_issues": [],
            }
        by_slug = {r["slug"]: r for r in retrieved}

        # --- Call the API ------------------------------------------------------
        user_message = (
            f"## Proposed journal entry\n\n{_format_entry(context)}\n\n"
            f"## Candidate rules (cite by exact rule_slug)\n\n"
            f"{_format_rules(retrieved)}\n\n"
            "Adversarially review the entry against these rules and call the "
            "review_journal_entry tool with your verdict."
        )
        try:
            response = self._api_client.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                system=_REVIEW_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
                tools=[REVIEW_TOOL],
                tool_choice={"type": "tool", "name": "review_journal_entry"},
            )
        except ReviewerError:
            raise
        except Exception as exc:
            raise ReviewerError(
                f"Anthropic API call failed: {type(exc).__name__}: {exc}"
            ) from exc

        verdict, raw_citations, review_notes = self._parse_response(response)

        # --- Anti-hallucination guard ------------------------------------------
        # Keep only citations whose slug was actually retrieved, and take the
        # source_ref from the database — never from the model.
        citations: list[dict] = []
        dropped = 0
        for c in raw_citations:
            slug = c.get("rule_slug", "")
            known = by_slug.get(slug)
            if known is None:
                dropped += 1
                continue
            citations.append({
                "rule_slug": slug,
                "source_ref": known.get("source_ref", ""),
                "verdict": c.get("verdict", "warning"),
                "notes": c.get("notes", ""),
            })
        if dropped:
            review_notes += (
                f"  ({dropped} citation(s) referencing unknown rules were "
                "discarded by the anti-hallucination guard.)"
            )

        any_fail = any(c["verdict"] == "fail" for c in citations)
        approved = verdict == "approve" and not any_fail

        return {
            "approved": approved,
            "citations": citations,
            "review_notes": review_notes,
            "structural_issues": [],
        }

    # ----- internals ---------------------------------------------------------

    @staticmethod
    def _parse_response(response) -> tuple[str, list[dict], str]:
        """Extract (verdict, citations, review_notes) from the tool_use block.

        Raises ReviewerError if no matching tool_use block is present.
        """
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "review_journal_entry"
            ):
                data = block.input
                return (
                    data.get("verdict", "reject"),
                    list(data.get("citations", [])),
                    data.get("review_notes", ""),
                )
        raise ReviewerError(
            f"Anthropic API did not return a review_journal_entry tool_use block. "
            f"stop_reason={getattr(response, 'stop_reason', 'unknown')!r}"
        )
