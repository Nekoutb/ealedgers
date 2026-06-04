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
                    order=None, context=None):
        self.search_calls.append({
            "model": model, "domain": domain, "fields": fields,
            "limit": limit, "order": order, "context": context})
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
            {"id": 5, "code": "601000", "name": "Achats",
             "account_type": "expense", "active": True},
            {"id": 9, "code": "701000", "name": "Ventes",
             "account_type": "income", "active": True},
        ]
        c = self._conn(rows=rows)
        out = c.lookup_chart_of_accounts()
        self.assertEqual(out[0], {
            "external_id": 5, "code": "601000", "name": "Achats",
            "account_type": "expense", "active": True, "raw": rows[0]})
        call = c.client.search_calls[0]
        self.assertEqual(call["model"], "account.account")
        # No deprecated filter (the field was removed in Odoo 17+); the default
        # active_test already excludes archived accounts.
        self.assertEqual(call["domain"], [])
        self.assertEqual(call["fields"],
                         ["code", "name", "account_type", "active"])
        self.assertEqual(call["order"], "code")
        self.assertIsNone(call["context"])

    def test_lookup_all_when_not_active_only(self):
        c = self._conn(rows=[])
        c.lookup_chart_of_accounts(active_only=False)
        call = c.client.search_calls[0]
        self.assertEqual(call["domain"], [])
        # Archived accounts are included by disabling active_test via context.
        self.assertEqual(call["context"], {"active_test": False})

    def test_coa_never_references_deprecated_field(self):
        """Regression guard: Odoo 17+ has no account.account.deprecated, so it
        must never appear in the default fields, the domain, or the output."""
        c = self._conn(rows=[
            {"id": 1, "code": "101100", "name": "Capital",
             "account_type": "equity", "active": True}])
        out = c.lookup_chart_of_accounts()
        self.assertNotIn("deprecated", OdooConnector.DEFAULT_COA_FIELDS)
        call = c.client.search_calls[0]
        self.assertNotIn("deprecated", call["fields"])
        self.assertNotIn("deprecated", str(call["domain"]))
        self.assertNotIn("deprecated", out[0])

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


class MoveFakeClient:
    """Model-aware fake for CAP.03: resolves journals/accounts, records the
    account.move create and any action_post call."""

    def __init__(self, accounts=None, journals=None, create_id=777):
        self.accounts = accounts if accounts is not None else {
            "601000": 5, "401000": 8}
        self.journals = journals if journals is not None else {"MISC": 3}
        self.create_id = create_id
        self.created = []
        self.kw_calls = []

    def search_read(self, model, domain=None, fields=None, limit=None,
                    order=None, context=None):
        if model == "account.journal":
            code = domain[0][2]
            return ([{"id": self.journals[code], "code": code,
                      "type": "general"}] if code in self.journals else [])
        if model == "account.account":
            wanted = domain[0][2]  # [["code", "in", [...]]]
            return [{"id": self.accounts[c], "code": c}
                    for c in wanted if c in self.accounts]
        return []

    def create(self, model, values):
        self.created.append({"model": model, "values": values})
        return self.create_id

    def execute_kw(self, model, method, args=None, kwargs=None):
        self.kw_calls.append({"model": model, "method": method, "args": args})
        return True


class OdooConnectorJournalEntryTests(SimpleTestCase):
    """CAP.03 — post journal entry (mocked; no live Odoo)."""

    def _conn(self, **kw):
        return OdooConnector(config={}, client=MoveFakeClient(**kw))

    _BAL = [  # a simple balanced pair
        {"account_code": "601000", "debit": 1000, "credit": 0, "name": "Exp"},
        {"account_code": "401000", "debit": 0, "credit": 1000, "name": "Pay"},
    ]

    def test_declares_cap03(self):
        self.assertIn("CAP.03", self._conn().capabilities)
        self.assertTrue(self._conn().supports("CAP.03"))

    def test_draft_by_default_resolves_and_builds_move(self):
        c = self._conn()
        res = c.post_journal_entry(
            lines=self._BAL, date="2026-06-04", ref="T1", journal_code="MISC")
        self.assertEqual(res["external_id"], 777)
        self.assertEqual(res["state"], "draft")
        self.assertFalse(res["posted"])
        self.assertTrue(res["balanced"])
        self.assertEqual(res["total_debit"], 1000)
        self.assertEqual(res["total_credit"], 1000)
        self.assertEqual(res["line_count"], 2)
        move = c.client.created[0]
        self.assertEqual(move["model"], "account.move")
        self.assertEqual(move["values"]["move_type"], "entry")
        self.assertEqual(move["values"]["journal_id"], 3)
        self.assertEqual(move["values"]["date"], "2026-06-04")
        cmds = move["values"]["line_ids"]
        self.assertEqual(cmds[0][0], 0)            # [0, 0, {...}] create-cmd
        self.assertEqual(cmds[0][2]["account_id"], 5)   # 601000 -> 5
        self.assertEqual(cmds[1][2]["account_id"], 8)   # 401000 -> 8
        self.assertEqual(c.client.kw_calls, [])    # draft -> NOT posted

    def test_post_true_calls_action_post(self):
        c = self._conn()
        res = c.post_journal_entry(
            lines=self._BAL, date="2026-06-04", ref="T2",
            journal_code="MISC", post=True)
        self.assertEqual(res["state"], "posted")
        self.assertTrue(res["posted"])
        self.assertEqual(c.client.kw_calls[0]["model"], "account.move")
        self.assertEqual(c.client.kw_calls[0]["method"], "action_post")
        self.assertEqual(c.client.kw_calls[0]["args"], [[777]])

    def test_accepts_explicit_ids_without_lookup(self):
        c = self._conn()
        c.post_journal_entry(
            journal_id=11, date="2026-06-04",
            lines=[{"account_id": 5, "debit": 50, "credit": 0},
                   {"account_id": 8, "debit": 0, "credit": 50}])
        self.assertEqual(c.client.created[0]["values"]["journal_id"], 11)

    def test_unbalanced_rejected_before_write(self):
        c = self._conn()
        with self.assertRaises(ValueError):
            c.post_journal_entry(
                date="2026-06-04", journal_code="MISC",
                lines=[{"account_code": "601000", "debit": 100, "credit": 0},
                       {"account_code": "401000", "debit": 0, "credit": 90}])
        self.assertEqual(c.client.created, [])     # nothing written

    def test_zero_total_rejected(self):
        c = self._conn()
        with self.assertRaises(ValueError):
            c.post_journal_entry(
                date="2026-06-04", journal_code="MISC",
                lines=[{"account_code": "601000", "debit": 0, "credit": 0},
                       {"account_code": "401000", "debit": 0, "credit": 0}])
        self.assertEqual(c.client.created, [])

    def test_unknown_account_rejected_before_write(self):
        c = self._conn()
        with self.assertRaises(ValueError):
            c.post_journal_entry(
                date="2026-06-04", journal_code="MISC",
                lines=[{"account_code": "999999", "debit": 10, "credit": 0},
                       {"account_code": "401000", "debit": 0, "credit": 10}])
        self.assertEqual(c.client.created, [])

    def test_missing_journal_rejected_before_write(self):
        c = self._conn()
        with self.assertRaises(ValueError):
            c.post_journal_entry(lines=self._BAL, date="2026-06-04")
        self.assertEqual(c.client.created, [])

    def test_unknown_journal_code_rejected(self):
        c = self._conn()
        with self.assertRaises(ValueError):
            c.post_journal_entry(
                lines=self._BAL, date="2026-06-04", journal_code="NOPE")
        self.assertEqual(c.client.created, [])

    def test_empty_lines_rejected(self):
        with self.assertRaises(ValueError):
            self._conn().post_journal_entry(
                lines=[], date="2026-06-04", journal_code="MISC")

    def test_line_without_account_reference_rejected(self):
        c = self._conn()
        with self.assertRaises(ValueError):
            c.post_journal_entry(
                date="2026-06-04", journal_code="MISC",
                lines=[{"debit": 10, "credit": 0},          # no account at all
                       {"account_code": "401000", "debit": 0, "credit": 10}])
        self.assertEqual(c.client.created, [])
