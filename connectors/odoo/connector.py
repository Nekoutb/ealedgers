"""OdooConnector — the IConnector implementation for Odoo (Step 30).

Wraps the low-level ``OdooClient`` (Step 29) in the capability contract. It
declares the capabilities it has *implemented* (CAP.01 now; CAP.02/03/17 land
in Steps 31-33) and self-registers under vendor ``"odoo"`` so the factory
resolves it.

CAP.01 (lookup chart of accounts) is implemented here. It reads ``code`` and
``name`` — universal across Odoo versions — plus ``deprecated``; the richer
account-type field is intentionally left out of the default field set because
its name differs by Odoo version (``account_type`` in 17, ``user_type_id``
earlier). Override the field set per tenant via ``config['coa_fields']`` once
the version is known.

Live verification (pulls a real CoA) needs the tenant's Odoo instance; the
logic here is exercised offline against a fake client.
"""

from connectors.base import HealthStatus, IConnector
from connectors.odoo.client import OdooClient, OdooConfigError
from connectors.registry import register_connector


@register_connector("odoo")
class OdooConnector(IConnector):
    vendor = "odoo"

    # Default account.account fields that are safe across Odoo versions.
    DEFAULT_COA_FIELDS = ["code", "name", "deprecated"]

    def __init__(self, config=None, client=None):
        super().__init__(config)
        self._client = client  # injected in tests; built lazily otherwise

    # ----- capabilities (only what's actually implemented) ----------------
    DEFAULT_PARTNER_FIELDS = ["name", "email", "vat", "is_company"]

    @property
    def capabilities(self):
        # Grows as Steps 32-33 add CAP.03 (post JE), CAP.17 (trial balance).
        # Declaring == implemented keeps it honest.
        return frozenset({"CAP.01", "CAP.02"})

    # ----- client (lazy) --------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            self._client = OdooClient.from_config(self.config)
        return self._client

    # ----- health ---------------------------------------------------------
    def health_check(self):
        try:
            client = self.client
        except OdooConfigError as exc:
            return HealthStatus(
                ok=False, state="unconfigured", detail=str(exc),
                capabilities=tuple(sorted(self.capabilities)))
        base = client.health_check()
        return HealthStatus(
            ok=base.ok, state=base.state, detail=base.detail,
            capabilities=tuple(sorted(self.capabilities)))

    # ----- CAP.01: lookup chart of accounts -------------------------------
    def lookup_chart_of_accounts(self, active_only=True):
        """Return the ERP's accounts as normalised dicts:
        ``{external_id, code, name, deprecated, raw}``."""
        self.require("CAP.01")
        fields = self.config.get("coa_fields") or self.DEFAULT_COA_FIELDS
        domain = [["deprecated", "=", False]] if active_only else []
        rows = self.client.search_read(
            "account.account", domain=domain, fields=fields, order="code")
        return [self._normalise_account(r) for r in rows]

    @staticmethod
    def _normalise_account(row):
        return {
            "external_id": row.get("id"),
            "code": row.get("code"),
            "name": row.get("name"),
            "deprecated": row.get("deprecated", False),
            "raw": row,
        }

    # ----- CAP.02: lookup / create partner --------------------------------
    def find_partner(self, *, vat=None, name=None):
        """Return the first matching partner (by VAT, else exact name), or
        None. Read-only."""
        self.require("CAP.02")
        if vat:
            domain = [["vat", "=", vat]]
        elif name:
            domain = [["name", "=", name]]
        else:
            return None
        fields = self.config.get("partner_fields") or self.DEFAULT_PARTNER_FIELDS
        rows = self.client.search_read(
            "res.partner", domain=domain, fields=fields, limit=1)
        return self._normalise_partner(rows[0]) if rows else None

    def create_partner(self, *, name, email=None, vat=None, is_company=True,
                        is_customer=False, is_supplier=False, extra=None):
        """Create a res.partner; returns the partner dict with external_id.
        This is a WRITE — only invoked against a live (ideally test) Odoo."""
        self.require("CAP.02")
        values = {"name": name, "is_company": bool(is_company)}
        if email:
            values["email"] = email
        if vat:
            values["vat"] = vat
        if is_customer:
            values["customer_rank"] = 1
        if is_supplier:
            values["supplier_rank"] = 1
        if extra:
            values.update(extra)
        new_id = self.client.create("res.partner", values)
        return {"external_id": new_id, "created": True, **values}

    def upsert_partner(self, *, name, email=None, vat=None, is_company=True,
                       is_customer=False, is_supplier=False, extra=None):
        """Find a partner (by VAT, else name) or create it. Returns the
        partner dict with ``created`` True/False."""
        self.require("CAP.02")
        existing = self.find_partner(vat=vat, name=name)
        if existing:
            return {**existing, "created": False}
        return self.create_partner(
            name=name, email=email, vat=vat, is_company=is_company,
            is_customer=is_customer, is_supplier=is_supplier, extra=extra)

    @staticmethod
    def _normalise_partner(row):
        return {
            "external_id": row.get("id"),
            "name": row.get("name"),
            "email": row.get("email") or "",
            "vat": row.get("vat") or "",
            "is_company": row.get("is_company", True),
            "raw": row,
        }
