"""SYSCOHADA account-code classification for AP bill lines — Step 54.

``AnthropicLineClassifier.classify(lines, tenant)`` uses the Anthropic Messages
API (tool_use) to suggest the most appropriate SYSCOHADA account code for each
extracted bill line item.

Architecture
------------
  1. ``_load_expense_accounts(tenant)`` queries ``accounting.Account`` for the
     tenant's expense / cost accounts, returning ``[{code, name, type}]``.
  2. ``AnthropicLineClassifier.classify(lines, tenant)``
     a. Loads the account list.
     b. Formats the accounts and lines as readable text.
     c. Calls claude-3-5-haiku once with ALL lines (not one call per line).
     d. Parses the ``classify_lines`` tool_use block.
     e. Enriches each original line dict with ``suggested_account``,
        ``suggested_account_name``, ``confidence``, and ``reasoning``.

Design notes
------------
• All expense and expense_direct_cost accounts are passed to Claude.  For a
  typical OHADA chart this is ~240 accounts (~14 KB) — well within the model's
  context window and significantly cheaper than one call per line.
• Tool-use is mandatory (tool_choice="classify_lines") so output is guaranteed
  valid JSON against the declared schema.
• ``tenant`` is required to scope the account lookup — each tenant owns their
  own copy of the chart of accounts.
• ``_client`` / ``_model`` are injectable for testing (Mock avoids real API calls).
"""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings


# ---------------------------------------------------------------------------
# Classification tool schema
# ---------------------------------------------------------------------------

CLASSIFICATION_TOOL: dict = {
    "name": "classify_lines",
    "description": (
        "Assign the most appropriate SYSCOHADA account code to each bill line "
        "item.  Choose from the provided list of accounts for this tenant.  "
        "For each line return: the zero-based line_index, the 6-digit account "
        "code, the account name, a confidence level, and a brief reasoning."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "classified_lines": {
                "type": "array",
                "description": "One entry per input line item, in the same order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_index": {
                            "type": "integer",
                            "description": "Zero-based index into the input lines array.",
                        },
                        "suggested_account": {
                            "type": "string",
                            "description": "6-digit SYSCOHADA account code, e.g. '628100'",
                        },
                        "suggested_account_name": {
                            "type": "string",
                            "description": "Name of the suggested account.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": (
                                "high = obvious match; "
                                "medium = plausible but ambiguous; "
                                "low = best guess, human review recommended"
                            ),
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "One-sentence explanation for the choice.",
                        },
                    },
                    "required": [
                        "line_index",
                        "suggested_account",
                        "suggested_account_name",
                        "confidence",
                    ],
                },
            }
        },
        "required": ["classified_lines"],
    },
}

# ---------------------------------------------------------------------------
# Prompts (bilingual context for OHADA bills)
# ---------------------------------------------------------------------------

_CLASSIFICATION_SYSTEM = (
    "You are an expert SYSCOHADA accountant classifying vendor bill line items "
    "for a Cameroonian professional services firm.  You will be given:\n\n"
    "  1. A list of available SYSCOHADA accounts (code — name [type]).\n"
    "  2. The extracted line items from a vendor bill.\n\n"
    "Your task is to assign the most appropriate account code from the list to "
    "each line item.  Rules:\n"
    "• Only use accounts from the provided list — do NOT invent codes.\n"
    "• For general operating expenses use class 6 accounts.\n"
    "• For capital expenditures (long-lived assets) use class 2 accounts if available.\n"
    "• If multiple accounts are equally plausible, prefer the more specific code "
    "  (e.g. 628100 over 629800).\n"
    "• Set confidence='low' when the line description is vague or ambiguous.\n"
    "• Bills may be in French, English, or both — handle both languages.\n"
    "• Call the classify_lines tool — do not reply with plain text."
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class ClassifierError(Exception):
    """Raised when line classification cannot proceed.

    Covers no accounts, API key missing, API errors, and response parse errors.
    """


# ---------------------------------------------------------------------------
# Account loader
# ---------------------------------------------------------------------------

# Account types to include as classification candidates.
_EXPENSE_TYPES = ("expense", "expense_direct_cost")


def _load_expense_accounts(tenant) -> list[dict[str, str]]:
    """Return [{code, name, type}] for all non-deprecated expense accounts.

    Queries ``accounting.Account`` scoped to the given tenant.
    Returns an empty list when the tenant has no accounts (handled upstream).
    """
    from accounting.models import Account  # noqa: PLC0415 — avoid circular import

    qs = Account.objects.filter(
        tenant=tenant,
        type__in=_EXPENSE_TYPES,
        deprecated=False,
    ).order_by("code")

    return [{"code": a.code, "name": a.name, "type": a.type} for a in qs]


def _format_accounts(accounts: list[dict]) -> str:
    """Format the account list as compact text for the Claude prompt."""
    lines = []
    for a in accounts:
        type_label = (
            "direct cost" if a["type"] == "expense_direct_cost" else "expense"
        )
        lines.append(f"{a['code']} — {a['name']} [{type_label}]")
    return "\n".join(lines)


def _format_lines(lines: list[dict]) -> str:
    """Format extracted bill lines as numbered text for the Claude prompt."""
    parts = []
    for i, line in enumerate(lines):
        desc = line.get("description", "(no description)")
        qty = line.get("quantity", "")
        unit_price = line.get("unit_price", "")
        amount = line.get("amount", "")
        detail = f"  description: {desc}"
        if qty:
            detail += f"  |  qty: {qty}"
        if unit_price:
            detail += f"  |  unit price: {unit_price}"
        if amount:
            detail += f"  |  amount: {amount}"
        parts.append(f"Line {i} (index {i}):\n{detail}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# AnthropicLineClassifier
# ---------------------------------------------------------------------------

class AnthropicLineClassifier:
    """Classify bill lines against the SYSCOHADA chart of accounts via Anthropic API.

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
            raise ClassifierError(
                "The 'anthropic' package is not installed. "
                "Run: pip install anthropic"
            ) from exc
        api_key = (
            getattr(settings, "ANTHROPIC_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not api_key:
            raise ClassifierError(
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

    def classify(self, lines: list[dict], tenant) -> list[dict[str, Any]]:
        """Classify each line item against the tenant's SYSCOHADA accounts.

        Args:
            lines:  List of line dicts (``{description, quantity, unit_price, amount}``).
                    Typically the ``lines`` key from APExtractor output.
            tenant: The Tenant instance — used to scope the account lookup.

        Returns:
            A list of enriched line dicts, one per input line, each containing
            the original keys plus:
                suggested_account      — 6-digit code, e.g. '628100'
                suggested_account_name — account name
                confidence             — 'high' | 'medium' | 'low'
                reasoning              — brief justification (optional)

        Raises:
            ClassifierError — on config failure, API error, or parse failure.
        """
        # 1. Load accounts
        accounts = _load_expense_accounts(tenant)
        if not accounts:
            raise ClassifierError(
                f"No expense accounts found for tenant {tenant!r}.  "
                "Ensure the SYSCOHADA chart of accounts is loaded for this tenant."
            )

        # 2. Build API request
        account_text = _format_accounts(accounts)
        lines_text = _format_lines(lines)
        user_message = (
            f"## Available SYSCOHADA expense accounts\n\n"
            f"{account_text}\n\n"
            f"## Bill line items to classify\n\n"
            f"{lines_text}\n\n"
            "Call the classify_lines tool to assign an account code to each line."
        )

        # 3. Call the API
        try:
            response = self._api_client.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                system=_CLASSIFICATION_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
                tools=[CLASSIFICATION_TOOL],
                tool_choice={"type": "tool", "name": "classify_lines"},
            )
        except ClassifierError:
            raise
        except Exception as exc:
            raise ClassifierError(
                f"Anthropic API call failed: {type(exc).__name__}: {exc}"
            ) from exc

        # 4. Parse response
        raw_classifications = self._parse_response(response)

        # 5. Merge original line data with classifications
        return self._merge_results(lines, raw_classifications)

    # ----- internals ---------------------------------------------------------

    @staticmethod
    def _parse_response(response) -> list[dict]:
        """Extract the classify_lines tool_use input from the response.

        Raises ClassifierError if no matching tool_use block is present.
        """
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "classify_lines"
            ):
                return list(block.input.get("classified_lines", []))
        raise ClassifierError(
            f"Anthropic API did not return a classify_lines tool_use block. "
            f"stop_reason={getattr(response, 'stop_reason', 'unknown')!r}"
        )

    @staticmethod
    def _merge_results(
        original_lines: list[dict],
        classifications: list[dict],
    ) -> list[dict[str, Any]]:
        """Merge classification results back into the original line dicts.

        Uses ``line_index`` from the classification to align with the original
        list.  Lines that Claude did not classify receive a low-confidence
        placeholder so the output always has the same length as the input.
        """
        # Index classifications by line_index for O(1) lookup
        by_index: dict[int, dict] = {
            c.get("line_index", i): c for i, c in enumerate(classifications)
        }
        result = []
        for i, line in enumerate(original_lines):
            cls = by_index.get(i, {})
            result.append({
                **line,
                "suggested_account": cls.get("suggested_account", ""),
                "suggested_account_name": cls.get("suggested_account_name", ""),
                "confidence": cls.get("confidence", "low"),
                "reasoning": cls.get("reasoning", ""),
            })
        return result
