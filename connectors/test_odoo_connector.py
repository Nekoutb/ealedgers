"""OdooConnector tests (Steps 30-31) — mocked client, no network or creds.

CAP.01 (lookup chart of accounts) and CAP.02 (lookup/create partner) are
exercised against a fake OdooClient. Live verification against a real Odoo is
done once the owner connects an instance via the ERP-connection form.
"""

from django.test import SimpleTestCase

from connectors.base import CapabilityNotSupported, HealthStatus
from connectors.odoo.connector import OdooConnector
from connectors.registry import connector_for_vendor


class FakeClient:
    def __init__(self, rows=None, health=None, create_id=42):
        self.rows = rows if rows is not None else []
        self._health = health or HealthStatus(
            ok=True, state="ok", detail="authenticated")
        self.create_id = create_id
        self.search_calls = []
        self.create_calls = []

    def search_read(self, model, domain=None, fields=None, limit=None,
                    order=None):
        self.search_calls.append({
            "model": model, "domain": domain, "fields": fields,
            "limit": limit, "order": order})
        return self.rows

    def create(self, model, values):
        self.create_calls.append({"model": model, "values": values})
        return self.create_id

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


class OdooConnectorPartnerTests(SimpleTestCase):
    def _conn(self, rows=None, create_id=42):
        return OdooConnector(config={},
                             client=FakeClient(rows=rows, create_id=create_id))

    def test_declares_cap02(self):
        self.assertIn("CAP.02", self._conn().capabilities)

    def test_find_partner_by_vat(self):
        rows = [{"id": 3, "name": "Acme", "vat": "P012", "email": "a@x.cm",
                 "is_company": True}]
        c = self._conn(rows=rows)
        p = c.find_partner(vat="P012")
        self.assertEqual(p["external_id"], 3)
        self.assertEqual(p["name"], "Acme")
        call = c.client.search_calls[0]
        self.assertEqual(call["model"], "res.partner")
        self.assertEqual(call["domain"], [["vat", "=", "P012"]])
        self.assertEqual(call["limit"], 1)

    def test_find_partner_absent_returns_none(self):
        self.assertIsNone(self._conn(rows=[]).find_partner(name="Nobody"))

    def test_find_partner_needs_a_criterion(self):
        self.assertIsNone(self._conn().find_partner())

    def test_create_partner_builds_payload(self):
        c = self._conn(create_id=77)
        out = c.create_partner(name="Beta SARL", vat="P9", email="b@x.cm",
                               is_supplier=True)
        self.assertEqual(out["external_id"], 77)
        self.assertTrue(out["created"])
        vals = c.client.create_calls[0]["values"]
        self.assertEqual(vals["name"], "Beta SARL")
        self.assertEqual(vals["vat"], "P9")
        self.assertEqual(vals["supplier_rank"], 1)
        self.assertNotIn("customer_rank", vals)

    def test_upsert_returns_existing_without_creating(self):
        rows = [{"id": 5, "name": "Acme", "vat": "P1", "is_company": True}]
        c = self._conn(rows=rows)
        out = c.upsert_partner(name="Acme", vat="P1")
        self.assertEqual(out["external_id"], 5)
        self.assertFalse(out["created"])
        self.assertEqual(c.client.create_calls, [])  # nothing created

    def test_upsert_creates_when_absent(self):
        c = self._conn(rows=[], create_id=9)
        out = c.upsert_partner(name="New Co", is_customer=True)
        self.assertTrue(out["created"])
        self.assertEqual(out["external_id"], 9)
        self.assertEqual(c.client.create_calls[0]["values"]["customer_rank"], 1)
