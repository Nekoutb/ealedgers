"""Connector contract (Step 28).

``IConnector`` is the single interface every ERP adapter implements — Odoo
(Step 29+), later Sage / SAP / Dynamics, and the standalone local-GL
connector (Step 38). The agents depend only on this contract and on the
``CAP.NN`` capability codes, never on a vendor's API surface.

Design rules:
  - a connector **declares** the capabilities it supports (``capabilities``);
  - every operation is **capability-gated** — call ``require(cap)`` first, so
    an unsupported call fails fast and predictably (the orchestrator then
    applies the gap-fallback: escalate / manual-JE-via-CAP.03 / refuse);
  - operations are thin: validate inputs, call the vendor, return a plain
    dict, and let the caller persist an ``ERPOperation`` audit row.

This module ships the contract plus ``NullConnector`` — the safe default for
a tenant with no ERP configured (it supports nothing and is always "ok").
The *functional* standalone connector that posts to a local GL is Step 38.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from . import capabilities as caps


class ConnectorError(Exception):
    """Base class for connector-layer errors."""


class CapabilityNotSupported(ConnectorError):
    """Raised when an operation needs a capability the connector lacks."""

    def __init__(self, vendor, code):
        self.vendor = vendor
        self.code = code
        super().__init__(
            f"{vendor!r} connector does not support {code} "
            f"({caps.CAPABILITIES[code].name if caps.is_valid(code) else 'unknown'})."
        )


class ConnectorNotImplemented(ConnectorError):
    """Raised by the factory for a vendor that has no connector yet."""


@dataclass(frozen=True)
class HealthStatus:
    """Result of a connector health check. Maps onto ERPConnection.health."""

    ok: bool
    state: str = "ok"            # ok | degraded | down | unconfigured
    detail: str = ""
    capabilities: tuple = field(default_factory=tuple)


class IConnector(ABC):
    """Abstract ERP connector. Subclasses set ``vendor`` and implement
    ``capabilities`` + ``health_check`` + whichever operations they support."""

    vendor: str = ""

    def __init__(self, config=None):
        # ERPConnection.config: URL, db, env-var names for secrets, etc.
        # Connectors read their settings from here; never store raw secrets.
        self.config = config or {}

    # ----- capability machinery ------------------------------------------
    @property
    @abstractmethod
    def capabilities(self):
        """A frozenset of supported CAP.NN codes."""

    def supports(self, code):
        return code in self.capabilities

    def require(self, code):
        """Guard at the top of every operation. Raises if unsupported."""
        if not caps.is_valid(code):
            raise KeyError(f"Unknown capability code {code!r}")
        if code not in self.capabilities:
            raise CapabilityNotSupported(self.vendor, code)

    @abstractmethod
    def health_check(self):
        """Return a HealthStatus. Must not raise for an expected outage —
        report ``ok=False`` instead."""

    def describe(self):
        """Serialisable summary (for the capability-matrix UI, Step 35)."""
        return {
            "vendor": self.vendor,
            "capabilities": sorted(self.capabilities),
            "n_capabilities": len(self.capabilities),
        }

    # ----- operations ----------------------------------------------------
    # Named entry points for the capabilities P03 builds first (Steps 30-33).
    # Each guards on its capability then defers to the concrete connector.
    # Connectors override the ones they declare; the rest stay unsupported.

    def lookup_chart_of_accounts(self, **kwargs):
        self.require("CAP.01")
        raise NotImplementedError

    def upsert_partner(self, **kwargs):
        self.require("CAP.02")
        raise NotImplementedError

    def post_journal_entry(self, **kwargs):
        self.require("CAP.03")
        raise NotImplementedError

    def fetch_trial_balance(self, **kwargs):
        self.require("CAP.17")
        raise NotImplementedError

    def poll_posted_entries(self, **kwargs):
        self.require("CAP.22")
        raise NotImplementedError

    def execute(self, code, **params):
        """Generic capability dispatch for codes without a named method.
        Concrete connectors override this for the capabilities they add."""
        self.require(code)
        raise NotImplementedError(
            f"{self.vendor!r} declares {code} but provides no handler.")


class NullConnector(IConnector):
    """No-ERP default: supports nothing, always healthy. Assigned to a
    tenant in standalone mode until a real connector is configured. The
    functional local-GL standalone connector lands in Step 38."""

    vendor = "manual"

    @property
    def capabilities(self):
        return frozenset()

    def health_check(self):
        return HealthStatus(
            ok=True, state="unconfigured",
            detail="No ERP configured (standalone mode).",
            capabilities=(),
        )
