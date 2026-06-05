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

    def test_undeclared_capability_raises(self):
        c = self._conn()
        # Declares CAP.01/02/03/17; any other capability is gated off.
        self.assertFalse(c.supports("CAP.05"))
        with self.assertRaises(CapabilityNotSupported):
            c.require("CAP.05")

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


class TBFakeClient:
    """Model-aware fake for CAP.17. ``grouped`` provides formatted_read_group
    output; if it's None, the method raises (to exercise the search_read
    fallback) and ``lines`` is aggregated instead."""

    def __init__(self, grouped=None, lines=None, accounts=None,
                 raise_grouped=False):
        self.grouped = grouped
        self.lines = lines or []
        self.accounts = accounts or []
        self.raise_grouped = raise_grouped
        self.calls = []

    def formatted_read_group(self, model, domain=None, groupby=None,
                             aggregates=None, context=None):
        self.calls.append({"method": "formatted_read_group", "domain": domain,
                           "groupby": groupby, "aggregates": aggregates})
        if self.raise_grouped or self.grouped is None:
            raise RuntimeError("formatted_read_group unavailable")
        return self.grouped

    def search_read(self, model, domain=None, fields=None, limit=None,
                    order=None, context=None):
        self.calls.append({"method": "search_read", "model": model,
                           "domain": domain, "fields": fields})
        if model == "account.account":
            ids = domain[0][2]
            return [a for a in self.accounts if a["id"] in ids]
        if model == "account.move.line":
            return self.lines
        return []


class OdooConnectorTrialBalanceTests(SimpleTestCase):
    """CAP.17 — trial balance (mocked; no live Odoo)."""

    ACCOUNTS = [
        {"id": 5, "code": "601000", "name": "Achats", "account_type": "expense"},
        {"id": 9, "code": "701000", "name": "Ventes", "account_type": "income"},
    ]
    GROUPED = [
        {"account_id": [5, "601000 Achats"],
         "debit:sum": 1000.0, "credit:sum": 0.0, "balance:sum": 1000.0},
        {"account_id": [9, "701000 Ventes"],
         "debit:sum": 0.0, "credit:sum": 1000.0, "balance:sum": -1000.0},
        {"account_id": False,  # the "no account" bucket — must be skipped
         "debit:sum": 5.0, "credit:sum": 5.0, "balance:sum": 0.0},
    ]

    def _conn(self, **kw):
        kw.setdefault("accounts", self.ACCOUNTS)
        return OdooConnector(config={}, client=TBFakeClient(**kw))

    def test_declares_cap17(self):
        self.assertIn("CAP.17", self._conn(grouped=self.GROUPED).capabilities)

    def test_trial_balance_via_formatted_read_group(self):
        c = self._conn(grouped=self.GROUPED)
        tb = c.fetch_trial_balance(date_from="2026-01-01", date_to="2026-12-31")
        self.assertEqual(tb["account_count"], 2)            # False bucket skipped
        self.assertEqual(tb["total_debit"], 1000)
        self.assertEqual(tb["total_credit"], 1000)
        self.assertTrue(tb["balanced"])
        first = tb["accounts"][0]                            # sorted by code
        self.assertEqual(first["code"], "601000")
        self.assertEqual(first["name"], "Achats")
        self.assertEqual(first["account_type"], "expense")
        self.assertEqual(first["debit"], 1000)
        self.assertEqual(first["balance"], 1000)

    def test_domain_reflects_filters(self):
        c = self._conn(grouped=[])
        c.fetch_trial_balance(date_from="2026-02-01", date_to="2026-02-28",
                              journal_ids=[3, 4])
        dom = c.client.calls[0]["domain"]
        self.assertIn(["parent_state", "=", "posted"], dom)
        self.assertIn(["date", ">=", "2026-02-01"], dom)
        self.assertIn(["date", "<=", "2026-02-28"], dom)
        self.assertIn(["journal_id", "in", [3, 4]], dom)

    def test_posted_only_false_drops_state_filter(self):
        c = self._conn(grouped=[])
        c.fetch_trial_balance(posted_only=False)
        dom = c.client.calls[0]["domain"]
        self.assertNotIn(["parent_state", "=", "posted"], dom)

    def test_falls_back_to_search_read_when_grouping_unavailable(self):
        lines = [
            {"account_id": [5, "601000 Achats"], "debit": 600, "credit": 0,
             "balance": 600},
            {"account_id": [5, "601000 Achats"], "debit": 400, "credit": 0,
             "balance": 400},
            {"account_id": [9, "701000 Ventes"], "debit": 0, "credit": 1000,
             "balance": -1000},
        ]
        c = self._conn(raise_grouped=True, lines=lines)
        tb = c.fetch_trial_balance()
        # fallback summed the two 601000 lines: 600 + 400 = 1000
        acc = {r["code"]: r for r in tb["accounts"]}
        self.assertEqual(acc["601000"]["debit"], 1000)
        self.assertEqual(tb["total_debit"], 1000)
        self.assertEqual(tb["total_credit"], 1000)
        self.assertTrue(tb["balanced"])
        methods = [x["method"] for x in c.client.calls]
        self.assertIn("search_read", methods)               # used the fallback

    def test_empty_trial_balance(self):
        c = self._conn(grouped=[])
        tb = c.fetch_trial_balance()
        self.assertEqual(tb["accounts"], [])
        self.assertEqual(tb["account_count"], 0)
        self.assertTrue(tb["balanced"])                     # 0 == 0


class EventFakeClient:
    """Fake for CAP.22: returns account.move rows; records the search domain."""

    def __init__(self, moves=None):
        self.moves = moves or []
        self.calls = []

    def search_read(self, model, domain=None, fields=None, limit=None,
                    order=None, context=None):
        self.calls.append({"model": model, "domain": domain, "fields": fields,
                           "limit": limit, "order": order})
        return self.moves


class OdooConnectorPolledEventsTests(SimpleTestCase):
    """CAP.22 — detect posted entries by polling (mocked; no live Odoo)."""

    MOVES = [
        {"id": 31, "name": "MISC/2026/06/0001", "ref": "", "date": "2026-06-04",
         "journal_id": [3, "Miscellaneous Operations"], "move_type": "entry",
         "amount_total": 1000.0, "write_date": "2026-06-04 15:00:00",
         "write_uid": [2, "Nekout Boma"]},
        {"id": 32, "name": "BILL/2026/06/0001", "ref": "EAL:auto",
         "date": "2026-06-04", "journal_id": [11, "Purchases"],
         "move_type": "in_invoice", "amount_total": 20000.0,
         "write_date": "2026-06-04 15:08:37", "write_uid": [2, "Nekout Boma"]},
    ]

    def _conn(self, moves=None):
        return OdooConnector(config={},
                             client=EventFakeClient(moves=self.MOVES
                                                    if moves is None else moves))

    def test_declares_cap22(self):
        self.assertIn("CAP.22", self._conn().capabilities)
        self.assertTrue(self._conn().supports("CAP.22"))

    def test_poll_normalises_and_sets_watermark(self):
        c = self._conn()
        out = c.poll_posted_entries(since="2026-06-04 14:00:00")
        self.assertEqual(out["count"], 2)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["watermark"], "2026-06-04 15:08:37")  # max wdate
        e = out["entries"][0]
        self.assertEqual(e["external_id"], 31)
        self.assertEqual(e["name"], "MISC/2026/06/0001")
        self.assertEqual(e["move_type"], "entry")
        self.assertEqual(e["journal_id"], 3)
        self.assertEqual(e["journal"], "Miscellaneous Operations")
        self.assertEqual(e["write_uid"], 2)
        self.assertEqual(e["write_user"], "Nekout Boma")

    def test_domain_only_posted_and_since(self):
        c = self._conn(moves=[])
        c.poll_posted_entries(since="2026-06-04 14:00:00")
        dom = c.client.calls[0]["domain"]
        self.assertIn(["state", "=", "posted"], dom)
        self.assertIn(["write_date", ">", "2026-06-04 14:00:00"], dom)
        self.assertEqual(c.client.calls[0]["order"], "write_date asc, id asc")

    def test_no_since_omits_write_date_filter(self):
        c = self._conn(moves=[])
        c.poll_posted_entries()
        dom = c.client.calls[0]["domain"]
        self.assertEqual(dom, [["state", "=", "posted"]])

    def test_journal_and_move_type_filters(self):
        c = self._conn(moves=[])
        c.poll_posted_entries(journal_ids=[3], move_types=["entry"])
        dom = c.client.calls[0]["domain"]
        self.assertIn(["journal_id", "in", [3]], dom)
        self.assertIn(["move_type", "in", ["entry"]], dom)

    def test_exclude_ref_prefix_drops_ea_posted(self):
        c = self._conn()
        out = c.poll_posted_entries(exclude_ref_prefix="EAL:")
        # the EAL:auto entry (id 32) is dropped...
        ids = [e["external_id"] for e in out["entries"]]
        self.assertEqual(ids, [31])
        # ...but the watermark still advances past it (id 32's write_date)
        self.assertEqual(out["watermark"], "2026-06-04 15:08:37")

    def test_exclude_ids_drops_known_moves(self):
        c = self._conn()
        out = c.poll_posted_entries(exclude_ids=[31])
        ids = [e["external_id"] for e in out["entries"]]
        self.assertEqual(ids, [32])

    def test_truncated_when_limit_hit(self):
        c = self._conn()
        out = c.poll_posted_entries(limit=2)   # fake returns exactly 2
        self.assertTrue(out["truncated"])

    def test_empty_keeps_since_as_watermark(self):
        c = self._conn(moves=[])
        out = c.poll_posted_entries(since="2026-06-01 00:00:00")
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["watermark"], "2026-06-01 00:00:00")
        self.assertFalse(out["truncated"])
