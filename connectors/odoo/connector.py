"""OdooConnector — the IConnector implementation for Odoo (Step 30).

Wraps the low-level ``OdooClient`` (Step 29) in the capability contract. It
declares the capabilities it has *implemented* — CAP.01 (read CoA), CAP.02
(lookup/create partner), CAP.03 (post journal entry), CAP.17 (trial balance),
CAP.22 (detect human-posted entries, via polling) — and self-registers under
vendor ``"odoo"`` so the factory resolves it.

CAP.01 (lookup chart of accounts) is implemented here. It reads ``code``,
``name``, ``account_type`` and ``active`` — all present on Odoo 17/18/19.
NOTE: Odoo 17 REMOVED ``account.account.deprecated`` (and pre-17 used
``user_type_id`` instead of ``account_type``); archiving is now the standard
``active`` field, so we never filter or read ``deprecated``. Verified live
against Odoo saas~19.3 (2026-06-04). Override the field set per tenant via
``config['coa_fields']`` for an older instance.

Live verification confirmed CAP.01 + CAP.02 against the tenant's Odoo; the
logic here is also exercised offline against a fake client.
"""

from connectors.base import HealthStatus, IConnector
from connectors.odoo.client import OdooClient, OdooConfigError
from connectors.registry import register_connector


@register_connector("odoo")
class OdooConnector(IConnector):
    vendor = "odoo"

    # Default account.account fields. Present on Odoo 17/18/19 (NOT
    # ``deprecated`` — removed in 17). Override via config['coa_fields'] for
    # an older Odoo (e.g. swap account_type -> user_type_id).
    DEFAULT_COA_FIELDS = ["code", "name", "account_type", "active"]

    def __init__(self, config=None, client=None):
        super().__init__(config)
        self._client = client  # injected in tests; built lazily otherwise

    # ----- capabilities (only what's actually implemented) ----------------
    DEFAULT_PARTNER_FIELDS = ["name", "email", "vat", "is_company"]

    @property
    def capabilities(self):
        # Declaring == implemented keeps it honest.
        return frozenset({"CAP.01", "CAP.02", "CAP.03", "CAP.17", "CAP.22"})

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
        ``{external_id, code, name, account_type, active, raw}``.

        Odoo 17+ dropped ``account.account.deprecated``; archiving uses the
        standard ``active`` field, which Odoo's default ``active_test`` already
        excludes. So ``active_only`` needs no domain filter — to INCLUDE
        archived accounts we disable ``active_test`` via context instead.
        """
        self.require("CAP.01")
        fields = self.config.get("coa_fields") or self.DEFAULT_COA_FIELDS
        context = None if active_only else {"active_test": False}
        rows = self.client.search_read(
            "account.account", domain=[], fields=fields, order="code",
            context=context)
        return [self._normalise_account(r) for r in rows]

    @staticmethod
    def _normalise_account(row):
        return {
            "external_id": row.get("id"),
            "code": row.get("code"),
            "name": row.get("name"),
            "account_type": row.get("account_type"),
            "active": row.get("active", True),
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

    # ----- CAP.03: post a journal entry -----------------------------------
    def post_journal_entry(self, *, lines, date, ref="", journal_code=None,
                           journal_id=None, post=False, move_type="entry"):
        """Create a balanced journal entry (``account.move``) from ``lines``.

        ``lines``: iterable of dicts, each with
          - ``account_code`` (str) OR ``account_id`` (int) — required
          - ``debit`` and ``credit`` (numbers; one is non-zero)
          - ``name`` (str, optional) — the line label
          - ``partner_id`` (int, optional)
        ``date``: ``"YYYY-MM-DD"``.  ``ref``: the entry's reference/label.
        ``journal_code`` / ``journal_id``: which journal (Odoo REQUIRES one for
        an ``account.move``); a code is resolved to its id.

        ``post`` defaults to **False** → the entry is created in **draft** for a
        human to review in Odoo; pass ``post=True`` to validate it
        (``action_post``). ``move_type="entry"`` is a general journal entry.

        Returns ``{external_id, state, posted, balanced, line_count,
        total_debit, total_credit}``. Raises ``ValueError`` (BEFORE any write)
        if the entry is empty, does not balance, or an account/journal can't be
        resolved. This is a WRITE — only runs against a live Odoo when invoked.
        """
        self.require("CAP.03")
        lines = list(lines)
        if not lines:
            raise ValueError("A journal entry needs at least one line.")

        # 1) Balance + non-zero check — fail fast, before touching Odoo.
        total_debit = round(sum(float(ln.get("debit") or 0) for ln in lines), 2)
        total_credit = round(
            sum(float(ln.get("credit") or 0) for ln in lines), 2)
        if total_debit != total_credit:
            raise ValueError(
                f"Journal entry does not balance: debit {total_debit} != "
                f"credit {total_credit}.")
        if total_debit == 0:
            raise ValueError("Journal entry total is zero — nothing to post.")

        # 2) Resolve the journal (required by Odoo) and any account codes.
        jid = journal_id or self._resolve_journal_id(journal_code)
        codes = []
        for ln in lines:
            if ln.get("account_id") is None:
                code = ln.get("account_code")
                if not code:
                    raise ValueError(
                        "Each line needs an account_code or account_id.")
                codes.append(code)
        account_ids = self._resolve_account_ids(codes)

        # 3) Build account.move.line create-commands ([0, 0, vals]).
        line_cmds = []
        for ln in lines:
            aid = ln.get("account_id")
            if aid is None:
                aid = account_ids[ln["account_code"]]
            vals = {
                "account_id": aid,
                "name": ln.get("name") or ref or "/",
                "debit": float(ln.get("debit") or 0),
                "credit": float(ln.get("credit") or 0),
            }
            if ln.get("partner_id"):
                vals["partner_id"] = ln["partner_id"]
            line_cmds.append([0, 0, vals])

        move_id = self.client.create("account.move", {
            "move_type": move_type,
            "journal_id": jid,
            "date": date,
            "ref": ref,
            "line_ids": line_cmds,
        })

        state = "draft"
        if post:
            # Validate the draft. Odoo re-checks the balance server-side.
            self.client.execute_kw(
                "account.move", "action_post", [[move_id]])
            state = "posted"

        return {
            "external_id": move_id,
            "state": state,
            "posted": bool(post),
            "balanced": True,
            "line_count": len(lines),
            "total_debit": total_debit,
            "total_credit": total_credit,
        }

    def _resolve_journal_id(self, journal_code):
        if not journal_code:
            raise ValueError(
                "post_journal_entry needs journal_code or journal_id "
                "(Odoo requires a journal for account.move).")
        rows = self.client.search_read(
            "account.journal", domain=[["code", "=", journal_code]],
            fields=["id", "code", "type"], limit=1)
        if not rows:
            raise ValueError(f"No Odoo journal with code {journal_code!r}.")
        return rows[0]["id"]

    def _resolve_account_ids(self, codes):
        """Map account codes → ids in one query. Raises if any are unknown."""
        codes = list(dict.fromkeys(codes))  # de-dup, preserve order
        if not codes:
            return {}
        rows = self.client.search_read(
            "account.account", domain=[["code", "in", codes]],
            fields=["id", "code"])
        found = {r["code"]: r["id"] for r in rows}
        missing = [c for c in codes if c not in found]
        if missing:
            raise ValueError(
                "Account code(s) not found in Odoo: " + ", ".join(missing))
        return found

    # ----- CAP.17: trial balance ------------------------------------------
    TB_FIELDS = ["debit", "credit", "balance"]

    def fetch_trial_balance(self, *, date_from=None, date_to=None,
                            posted_only=True, journal_ids=None):
        """Per-account debit/credit/balance totals — a trial balance.

        Aggregates ``account.move.line`` (the version-tolerant way; Odoo's
        report wizards differ per version). ``date_from`` / ``date_to`` bound
        the period (``"YYYY-MM-DD"``, inclusive); ``posted_only`` (default)
        excludes draft entries; ``journal_ids`` restricts to those journals.

        Returns ``{date_from, date_to, posted_only, accounts, account_count,
        total_debit, total_credit, balanced}`` where each account is
        ``{external_id, code, name, account_type, debit, credit, balance}``,
        sorted by code. A real ledger's TB balances (Σdebit == Σcredit).
        """
        self.require("CAP.17")
        domain = self._tb_domain(date_from, date_to, posted_only, journal_ids)
        sums = self._aggregate_move_lines(domain)
        rows = self._enrich_tb_rows(sums)
        total_debit = round(sum(r["debit"] for r in rows), 2)
        total_credit = round(sum(r["credit"] for r in rows), 2)
        return {
            "date_from": date_from,
            "date_to": date_to,
            "posted_only": posted_only,
            "accounts": rows,
            "account_count": len(rows),
            "total_debit": total_debit,
            "total_credit": total_credit,
            "balanced": total_debit == total_credit,
        }

    @staticmethod
    def _tb_domain(date_from, date_to, posted_only, journal_ids):
        domain = []
        if posted_only:
            domain.append(["parent_state", "=", "posted"])
        if date_from:
            domain.append(["date", ">=", date_from])
        if date_to:
            domain.append(["date", "<=", date_to])
        if journal_ids:
            domain.append(["journal_id", "in", list(journal_ids)])
        return domain

    def _aggregate_move_lines(self, domain):
        """Return ``{account_id: {debit, credit, balance}}``. Prefers Odoo
        17+ ``formatted_read_group``; falls back to reading the lines and
        summing in Python (equivalent result; works on any Odoo version)."""
        try:
            groups = self.client.formatted_read_group(
                "account.move.line", domain=domain, groupby=["account_id"],
                aggregates=[f"{f}:sum" for f in self.TB_FIELDS])
            out = {}
            for g in groups:
                acc = g.get("account_id")
                if not acc:
                    continue          # the "no account" bucket — skip
                out[acc[0]] = {f: g.get(f"{f}:sum") or 0.0
                               for f in self.TB_FIELDS}
            return out
        except Exception:
            # formatted_read_group absent (older Odoo) or refused — aggregate
            # the raw lines instead. Same numbers, just less efficient.
            return self._aggregate_via_search_read(domain)

    def _aggregate_via_search_read(self, domain):
        lines = self.client.search_read(
            "account.move.line", domain=domain,
            fields=["account_id"] + self.TB_FIELDS)
        out = {}
        for ln in lines:
            acc = ln.get("account_id")
            if not acc:
                continue
            slot = out.setdefault(
                acc[0], {f: 0.0 for f in self.TB_FIELDS})
            for f in self.TB_FIELDS:
                slot[f] += ln.get(f) or 0.0
        return out

    def _enrich_tb_rows(self, sums):
        """Attach code / name / account_type to each aggregated account."""
        if not sums:
            return []
        ids = list(sums.keys())
        accts = self.client.search_read(
            "account.account", domain=[["id", "in", ids]],
            fields=["id", "code", "name", "account_type"])
        meta = {a["id"]: a for a in accts}
        rows = []
        for aid, s in sums.items():
            a = meta.get(aid, {})
            rows.append({
                "external_id": aid,
                "code": a.get("code"),
                "name": a.get("name"),
                "account_type": a.get("account_type"),
                "debit": round(s["debit"], 2),
                "credit": round(s["credit"], 2),
                "balance": round(s["balance"], 2),
            })
        rows.sort(key=lambda r: (r["code"] or ""))
        return rows

    # ----- CAP.22: detect posted entries (event, via polling) -------------
    MOVE_EVENT_FIELDS = ["name", "ref", "date", "journal_id", "move_type",
                         "amount_total", "write_date", "write_uid"]

    def poll_posted_entries(self, *, since=None, journal_ids=None,
                            move_types=None, exclude_ref_prefix=None,
                            exclude_ids=None, limit=200):
        """Detect ``account.move`` records that are POSTED and changed since
        ``since`` (Odoo ``write_date``, ``"YYYY-MM-DD HH:MM:SS"`` UTC) — so the
        agents notice entries a human posted directly in the ERP. Read-only.

        Note: EA Ledgers authenticates as the same Odoo user a human may use,
        so ``write_uid`` can't tell agent from human — exclude EA-posted
        entries with ``exclude_ref_prefix`` (a marker CAP.03 can stamp) or
        ``exclude_ids`` (move ids we recorded). ``journal_ids`` / ``move_types``
        narrow the scan.

        Returns ``{since, watermark, count, truncated, entries:[...]}`` ordered
        by ``write_date`` asc; each entry is ``{external_id, name, ref, date,
        move_type, journal_id, journal, amount_total, write_date, write_uid,
        write_user, raw}``. Advance your stored watermark to ``watermark`` and
        re-poll with a small overlap, deduping by ``external_id``; if
        ``truncated`` (the ``limit`` was hit) poll again right away.
        """
        self.require("CAP.22")
        domain = [["state", "=", "posted"]]
        if since:
            domain.append(["write_date", ">", since])
        if journal_ids:
            domain.append(["journal_id", "in", list(journal_ids)])
        if move_types:
            domain.append(["move_type", "in", list(move_types)])
        moves = self.client.search_read(
            "account.move", domain=domain, fields=self.MOVE_EVENT_FIELDS,
            order="write_date asc, id asc", limit=limit)

        # Advance the watermark past EVERYTHING fetched (incl. excluded ones),
        # so excluded entries aren't re-scanned forever. moves are write_date
        # asc, so the last one carries the max write_date.
        watermark = moves[-1]["write_date"] if moves else since

        exclude_ids = set(exclude_ids or ())
        entries = []
        for m in moves:
            if m.get("id") in exclude_ids:
                continue
            ref = m.get("ref") or ""
            if exclude_ref_prefix and ref.startswith(exclude_ref_prefix):
                continue
            entries.append(self._normalise_move_event(m))

        return {
            "since": since,
            "watermark": watermark,
            "count": len(entries),
            "truncated": len(moves) == limit,
            "entries": entries,
        }

    @staticmethod
    def _normalise_move_event(m):
        jrnl = m.get("journal_id") or [None, None]
        wuid = m.get("write_uid") or [None, None]
        return {
            "external_id": m.get("id"),
            "name": m.get("name"),
            "ref": m.get("ref") or "",
            "date": m.get("date"),
            "move_type": m.get("move_type"),
            "journal_id": jrnl[0],
            "journal": jrnl[1],
            "amount_total": m.get("amount_total"),
            "write_date": m.get("write_date"),
            "write_uid": wuid[0],
            "write_user": wuid[1],
            "raw": m,
        }
