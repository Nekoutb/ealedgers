"""OdooConnector tests (Step 30) — mocked client, no network or creds.

CAP.01 (lookup chart of accounts) is exercised against a fake OdooClient.
Live verification against a real Odoo is done once the owner provides
connection details.
"""

from django.test import SimpleTestCase

from connectors.base import CapabilityNotSupported, HealthStatus
from connectors.odoo.connector import OdooConnector
from connectors.registry import connector_for_vendor


class FakeClient:
    def __init__(self, rows=None, health=None):
        self.rows = rows if rows is not None else []
        self._health = health or HealthStatus(
            ok=True, state="ok", detail="authenticated")
        self.search_calls = []

    def search_read(self, model, domain=None, fields=None, limit=None,
                    order=None):
        self.search_calls.append({
            "model": model, "domain": domain, "fields": fields, "order": order})
        return self.rows

    def health_check(self):
        return self._health


class OdooConnectorTests(SimpleTestCase):
    def _conn(self, rows=None, health=None, config=None):
        return OdooConnector(config=config or {},
                             client=FakeClient(rows=rows, health=health))

    def test_registered_under_odoo_vendor(self):
        self.assertEqual(OdooConnector.vendor, "odoo")
        self.assertIsInstance(connector_for_vendor("odoo", {}), OdooConnector)

    def test_declares_cap01(self):
        c = self._conn()
        self.assertIn("CAP.01", c.capabilities)
        self.assertTrue(c.supports("CAP.01"))

    def test_lookup_chart_of_accounts_normalises_rows(self):
        rows = [
            {"id": 5, "code": "601000", "name": "Achats", "deprecated": False},
            {"id": 9, "code": "701000", "name": "Ventes", "deprecated": False},
        ]
        c = self._conn(rows=rows)
        out = c.lookup_chart_of_accounts()
        self.assertEqual(out[0], {
            "external_id": 5, "code": "601000", "name": "Achats",
            "deprecated": False, "raw": rows[0]})
        call = c.client.search_calls[0]
        self.assertEqual(call["model"], "account.account")
        self.assertEqual(call["domain"], [["deprecated", "=", False]])
        self.assertEqual(call["fields"], ["code", "name", "deprecated"])
        self.assertEqual(call["order"], "code")

    def test_lookup_all_when_not_active_only(self):
        c = self._conn(rows=[])
        c.lookup_chart_of_accounts(active_only=False)
        self.assertEqual(c.client.search_calls[0]["domain"], [])

    def test_coa_fields_overridable_via_config(self):
        c = OdooConnector(
            config={"coa_fields": ["code", "name", "account_type"]},
            client=FakeClient(rows=[]))
        c.lookup_chart_of_accounts()
        self.assertEqual(c.client.search_calls[0]["fields"],
                         ["code", "name", "account_type"])

    def test_undeclared_capabilities_raise(self):
        c = self._conn()
        with self.assertRaises(CapabilityNotSupported):
            c.post_journal_entry()   # CAP.03 — not until Step 32
        with self.assertRaises(CapabilityNotSupported):
            c.upsert_partner()       # CAP.02 — not until Step 31
        with self.assertRaises(CapabilityNotSupported):
            c.fetch_trial_balance()  # CAP.17 — not until Step 33

    def test_health_check_delegates_and_enriches(self):
        c = self._conn(health=HealthStatus(ok=True, state="ok", detail="up"))
        h = c.health_check()
        self.assertTrue(h.ok)
        self.assertEqual(h.state, "ok")
        self.assertIn("CAP.01", h.capabilities)

    def test_health_check_unconfigured_without_connection_details(self):
        # No client injected + empty config → OdooClient.from_config raises,
        # health_check catches it and reports 'unconfigured' (never raises).
        c = OdooConnector(config={})
        h = c.health_check()
        self.assertFalse(h.ok)
        self.assertEqual(h.state, "unconfigured")
