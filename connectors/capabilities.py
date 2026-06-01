"""ERP capability registry — CAP.01 … CAP.23 (Step 28).

Every action the agents can ask an ERP to perform is a *capability* with a
stable ``CAP.NN`` code. A connector (Odoo, Sage, …, or the standalone local
GL) declares which capabilities it supports; the agents never call a vendor
API directly — they request a capability, and the connector either fulfils
it or the gap-fallback kicks in (escalate / manual-JE-via-CAP.03 / refuse —
see EXECUTION_PLAN §P00.10).

Codes are an immutable contract: ``ERPConnection.capabilities`` and
``ERPOperation.capability`` store these strings, so a code's meaning must
never change once shipped. New abilities get new numbers.

`direction`:
  - ``read``  — pulls data out of the ERP (safe, idempotent)
  - ``write`` — mutates the ERP (needs provenance + is audited via ERPOperation)
  - ``event`` — inbound (the ERP notifies us, e.g. a human-posted entry)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    code: str          # "CAP.03"
    name: str          # "Post journal entry"
    direction: str     # read | write | event
    cycle: str         # GL | AP | AR | Treasury | FixedAssets | Tax | …
    description: str


def _cap(code, name, direction, cycle, description):
    return Capability(code, name, direction, cycle, description)


# The registry. Codes referenced by the plan's P03/P05+ steps are anchored at
# their exact numbers; the remaining slots round out the accounting cycles
# (these gap-fills are v1 design and may be refined as connectors are built).
CAPABILITIES = {c.code: c for c in [
    _cap("CAP.01", "Lookup chart of accounts", "read", "GL",
         "Fetch the ERP's accounts (code, name, type, parent)."),
    _cap("CAP.02", "Lookup / create partner", "write", "MasterData",
         "Find or create a customer/supplier (res.partner) record."),
    _cap("CAP.03", "Post journal entry", "write", "GL",
         "Create and post a balanced journal entry (account.move)."),
    _cap("CAP.04", "Read journal entries", "read", "GL",
         "Fetch posted moves / lines for a period or filter."),
    _cap("CAP.05", "Create / post vendor bill", "write", "AP",
         "Create a supplier bill from a processed invoice."),
    _cap("CAP.06", "Create / post customer invoice", "write", "AR",
         "Create and post a customer invoice."),
    _cap("CAP.07", "Register payment / receipt", "write", "Treasury",
         "Register a payment against a bill or a receipt against an invoice."),
    _cap("CAP.08", "Create fixed asset", "write", "FixedAssets",
         "Create a fixed-asset record (optionally componentised)."),
    _cap("CAP.09", "Run / post depreciation", "write", "FixedAssets",
         "Trigger the ERP's depreciation run and post the entries."),
    _cap("CAP.10", "Fetch depreciation schedule", "read", "FixedAssets",
         "Read an asset's depreciation board."),
    _cap("CAP.11", "Dispose fixed asset", "write", "FixedAssets",
         "Record a disposal/sale and the resulting gain or loss."),
    _cap("CAP.12", "Commit bank reconciliation", "write", "Treasury",
         "Post the matches of a bank reconciliation."),
    _cap("CAP.13", "Fetch bank statements", "read", "Treasury",
         "Pull bank-statement lines / a daily feed."),
    _cap("CAP.14", "Fetch tax configuration", "read", "Tax",
         "Read the ERP's tax codes and rates."),
    _cap("CAP.15", "Lock / close accounting period", "write", "Controller",
         "Lock a fiscal period so no further postings are allowed."),
    _cap("CAP.16", "Fetch account balances", "read", "GL",
         "Read balances for accounts over a date range."),
    _cap("CAP.17", "Fetch trial balance", "read", "Reporting",
         "Pull the trial balance for a period."),
    _cap("CAP.18", "Fetch financial statements", "read", "Reporting",
         "Pull balance sheet / income statement as the ERP computes them."),
    _cap("CAP.19", "Lookup / create product", "write", "Inventory",
         "Find or create an item-master (product) record."),
    _cap("CAP.20", "Post inventory move", "write", "Inventory",
         "Record a stock receipt/issue/adjustment."),
    _cap("CAP.21", "Fetch / set FX rates", "write", "Treasury",
         "Read or push currency exchange rates."),
    _cap("CAP.22", "Subscribe to ERP events", "event", "Platform",
         "Receive webhooks (e.g. a journal entry posted by a human in the ERP)."),
    _cap("CAP.23", "Upload / fetch attachment", "write", "Platform",
         "Attach or retrieve a source document (invoice PDF, etc.)."),
]}

ALL_CODES = tuple(CAPABILITIES.keys())

# Convenience for callers that want only safe, idempotent capabilities.
READ_CODES = tuple(c.code for c in CAPABILITIES.values() if c.direction == "read")
WRITE_CODES = tuple(c.code for c in CAPABILITIES.values() if c.direction == "write")


def get(code):
    """Return the Capability for a code, or raise KeyError with a clear msg."""
    try:
        return CAPABILITIES[code]
    except KeyError:
        raise KeyError(f"Unknown capability code {code!r}. "
                       f"Known: {', '.join(ALL_CODES)}")


def is_valid(code):
    return code in CAPABILITIES
