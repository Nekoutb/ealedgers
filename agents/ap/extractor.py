"""LLM-based vendor-bill extraction — AP Extractor specialist (Step 53).

Implements ``AnthropicBillExtractor``, which sends a vendor-bill document
(PDF or image) to the Anthropic Messages API and uses a tool_use call to
extract structured fields.

Tool-use is used instead of free-text parsing because it forces Claude to
return a JSON object matching the declared schema — no regex or fragile
post-processing required.

Architecture
------------
``AnthropicBillExtractor.extract(file_bytes, content_type)``
    → builds a content block (document for PDF, image for JPEG/PNG/TIFF/WebP)
    → calls the Anthropic Messages API with tool_choice="extract_bill"
    → parses the tool_use block from the response
    → returns a dict with the extracted bill fields (raises ExtractorError on any failure)

``APExtractor.run(input_data)`` (in department.py)
    → resolves the file path from MEDIA_ROOT
    → reads the file bytes
    → delegates to AnthropicBillExtractor.extract()
    → returns SpecialistResult(ok=True/False)

Dependency injection
--------------------
``AnthropicBillExtractor`` accepts an optional ``client`` parameter for
testing — pass a Mock to avoid real API calls in automated tests.
The real ``anthropic.Anthropic`` client is created lazily so the module
can be imported even when the package is not installed or the key is absent.

Bilingual context
-----------------
The extraction prompt is bilingual (EN/FR) because OHADA vendor bills in
Cameroon appear in French, English, or both.  Amounts in XAF use integer
precision (0 decimal places per SYSCOHADA convention).
"""

from __future__ import annotations

import base64
import os
from typing import Any

from django.conf import settings


# ---------------------------------------------------------------------------
# Tool schema — JSON Schema for Claude's tool_use
# ---------------------------------------------------------------------------

BILL_EXTRACTION_TOOL: dict = {
    "name": "extract_bill",
    "description": (
        "Extract structured fields from a vendor bill or invoice document. "
        "Bills may be in French, English, or both languages.  For XAF amounts "
        "use integers (no decimal places — SYSCOHADA convention).  For dates "
        "use ISO 8601 format YYYY-MM-DD.  Only populate fields you can clearly "
        "identify in the document; omit fields that are absent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor_name": {
                "type": "string",
                "description": "Legal name of the vendor / fournisseur",
            },
            "vendor_vat": {
                "type": "string",
                "description": (
                    "VAT registration number, NIU (Numéro d'Identifiant Unique), "
                    "or other tax identifier of the vendor"
                ),
            },
            "invoice_date": {
                "type": "string",
                "description": "Invoice / bill date in YYYY-MM-DD format",
            },
            "due_date": {
                "type": "string",
                "description": "Payment due date in YYYY-MM-DD format",
            },
            "invoice_number": {
                "type": "string",
                "description": "Invoice reference number / numéro de facture",
            },
            "currency": {
                "type": "string",
                "description": "ISO 4217 currency code, e.g. XAF, EUR, USD",
            },
            "subtotal": {
                "type": "number",
                "description": "Subtotal before taxes (montant HT / hors taxes)",
            },
            "tax_amount": {
                "type": "number",
                "description": "Total tax amount (TVA + all other taxes)",
            },
            "total": {
                "type": "number",
                "description": "Total amount due including all taxes (TTC / montant total)",
            },
            "lines": {
                "type": "array",
                "description": "Individual line items from the bill",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "Line item description / désignation",
                        },
                        "quantity": {
                            "type": "number",
                            "description": "Quantity / quantité",
                        },
                        "unit_price": {
                            "type": "number",
                            "description": "Unit price / prix unitaire",
                        },
                        "amount": {
                            "type": "number",
                            "description": "Line total amount / montant ligne",
                        },
                    },
                    "required": ["description", "amount"],
                },
            },
        },
        "required": ["vendor_name", "invoice_date", "total", "lines"],
    },
}

# ---------------------------------------------------------------------------
# Bilingual extraction prompts
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM = (
    "You are an expert accounting assistant for SYSCOHADA-standard bookkeeping "
    "in Cameroon and Central Africa.  Your task is to extract structured data "
    "from vendor bills and invoices.  Bills may be in French, English, or both.\n\n"
    "Rules:\n"
    "• For XAF (CFA Franc) amounts use integers — no decimal places (SYSCOHADA).\n"
    "• For dates always use ISO 8601 format: YYYY-MM-DD.\n"
    "• Only populate fields you can clearly read in the document.  Do NOT guess.\n"
    "• The 'lines' array must contain every billable line item on the document.\n"
    "• Call the extract_bill tool — do not reply with plain text."
)

_EXTRACTION_USER = (
    "Please extract all structured fields from this vendor bill document and "
    "call the extract_bill tool with the results."
)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class ExtractorError(Exception):
    """Raised when extraction cannot proceed.

    Covers configuration failures (no API key, package not installed),
    I/O failures (file unreadable), and Anthropic API failures.
    The error message is safe to surface to an approver / in logs.
    """


# ---------------------------------------------------------------------------
# AnthropicBillExtractor
# ---------------------------------------------------------------------------

class AnthropicBillExtractor:
    """Extract vendor-bill fields from a document using the Anthropic API.

    Args:
        client:  Injectable Anthropic client for testing (pass a Mock here
                 to avoid real API calls).  When ``None`` a real
                 ``anthropic.Anthropic`` client is created from
                 ``settings.ANTHROPIC_API_KEY``.
        model:   Model ID override.  When ``None`` uses
                 ``settings.ANTHROPIC_EXTRACTION_MODEL``
                 (default: ``claude-3-5-haiku-20241022``).
    """

    DEFAULT_MODEL = "claude-3-5-haiku-20241022"
    MAX_TOKENS = 4096

    def __init__(self, *, client=None, model: str | None = None):
        self._client = client
        self._model = model

    # ----- client / model resolution ----------------------------------------

    @property
    def _api_client(self):
        """Return the Anthropic client, creating a real one lazily if needed."""
        if self._client is not None:
            return self._client
        try:
            import anthropic  # noqa: PLC0415 — intentional lazy import
        except ImportError as exc:
            raise ExtractorError(
                "The 'anthropic' package is not installed. "
                "Run: pip install anthropic"
            ) from exc
        api_key = (
            getattr(settings, "ANTHROPIC_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not api_key:
            raise ExtractorError(
                "ANTHROPIC_API_KEY is not configured.  "
                "Set the environment variable on the server to enable "
                "LLM-based bill extraction."
            )
        return anthropic.Anthropic(api_key=api_key)

    @property
    def model(self) -> str:
        return (
            self._model
            or getattr(settings, "ANTHROPIC_EXTRACTION_MODEL", self.DEFAULT_MODEL)
        )

    # ----- main entry point --------------------------------------------------

    def extract(self, file_bytes: bytes, content_type: str) -> dict[str, Any]:
        """Send ``file_bytes`` to the Anthropic API and return extracted fields.

        Returns a dict whose keys are a subset of those in ``BILL_EXTRACTION_TOOL``.
        Only fields Claude identified in the document are present — callers must
        use ``.get()`` for optional keys (vendor_vat, due_date, …).

        Raises:
            ExtractorError — on any configuration, I/O, or API failure.
        """
        content_block = self._build_content_block(file_bytes, content_type)
        try:
            response = self._api_client.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                system=_EXTRACTION_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            content_block,
                            {"type": "text", "text": _EXTRACTION_USER},
                        ],
                    }
                ],
                tools=[BILL_EXTRACTION_TOOL],
                tool_choice={"type": "tool", "name": "extract_bill"},
            )
        except ExtractorError:
            raise  # already wrapped — don't double-wrap
        except Exception as exc:
            raise ExtractorError(
                f"Anthropic API call failed: {type(exc).__name__}: {exc}"
            ) from exc

        return self._parse_response(response)

    # ----- helpers -----------------------------------------------------------

    @staticmethod
    def _build_content_block(file_bytes: bytes, content_type: str) -> dict:
        """Return a Messages-API content block for the given document.

        PDFs are sent as ``type: document`` (Anthropic native PDF support).
        All image types are sent as ``type: image``.
        """
        data_b64 = base64.standard_b64encode(file_bytes).decode("ascii")
        if content_type == "application/pdf":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": data_b64,
                },
            }
        # Images — normalise unknown types to image/jpeg as a safe fallback
        media_type = content_type if content_type.startswith("image/") else "image/jpeg"
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data_b64,
            },
        }

    @staticmethod
    def _parse_response(response) -> dict[str, Any]:
        """Extract the ``extract_bill`` tool_use input dict from the response.

        Raises:
            ExtractorError — if no matching tool_use block is present.
        """
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "extract_bill"
            ):
                return dict(block.input)
        raise ExtractorError(
            f"Anthropic API did not return an extract_bill tool_use block. "
            f"stop_reason={getattr(response, 'stop_reason', 'unknown')!r}"
        )
