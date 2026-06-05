"""Standalone local-GL connector + tenant routing (Step 38).

A tenant with no ERP can still run the full GL loop: EA Ledgers posts to its
OWN double-entry ledger (the ``accounting`` models) instead of an external ERP.
``LocalGLConnector`` implements the same ``IConnector`` contract — CAP.01 (read
accounts), CAP.03 (post journal entry), CAP.17 (trial balance) — against the
local models, so the agents/sagas drive it exactly like the Odoo connector.

``connector_for_tenant(tenant)`` is the router: use a connected ERP if there is
one, otherwise fall back to the local GL (standalone mode).
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from connectors.base import HealthStatus, IConnector


class LocalGLConnector(IConnector):
    """Posts to the tenant's local double-entry ledger (no external ERP)."""

    vendor = "local"

    def __init__(self, tenant, config=None):
        super().__init__(config)
        self.tenant = tenant

    @property
    def capabilities(self):
        return frozenset({"CAP.01", "CAP.03", "CAP.17"})

    def health_check(self):
        return HealthStatus(
            ok=True, state="ok",
            detail="Standalone mode — posting to the local general ledger.",
            capabilities=tuple(sorted(self.capabilities)))

    # ----- CAP.01: read the local chart of accounts -----------------------
    def lookup_chart_of_accounts(self, active_only=True):
        self.require("CAP.01")
        from accounting.models import Account
        qs = Account.objects.for_tenant(self.tenant).order_by("code")
        if active_only:
            qs = qs.filter(deprecated=False)
        return [{
            "external_id": a.id, "code": a.code, "name": a.name,
            "account_type": a.type, "active": not a.deprecated, "raw": {},
        } for a in qs]

    # ----- CAP.03: post a journal entry to the local ledger ---------------
    def post_journal_entry(self, *, lines, date, ref="", journal_code=None,
                           journal_id=None, post=False, **_ignore):
        """Create a balanced JournalEntry (+ lines) in the local ledger.
        Defaults to DRAFT; ``post=True`` runs ``JournalEntry.post()`` (which
        re-validates the double-entry and assigns the journal sequence name).
        Validates BEFORE any write — raises ValueError on empty / unbalanced /
        zero-total / unknown account or journal."""
        self.require("CAP.03")
        from accounting.models import JournalEntry, JournalEntryLine
        lines = list(lines)
        if not lines:
            raise ValueError("A journal entry needs at least one line.")
        total_debit = round(sum(float(ln.get("debit") or 0) for ln in lines), 2)
        total_credit = round(sum(float(ln.get("credit") or 0) for ln in lines), 2)
        if total_debit != total_credit:
            raise ValueError(
                f"Journal entry does not balance: debit {total_debit} != "
                f"credit {total_credit}.")
        if total_debit == 0:
            raise ValueError("Journal entry total is zero — nothing to post.")

        journal = self._resolve_journal(journal_code, journal_id)
        accounts = self._resolve_accounts(lines)

        with transaction.atomic():
            entry = JournalEntry.objects.create(
                tenant=self.tenant, journal=journal, date=date, ref=ref,
                state="draft")
            for ln in lines:
                JournalEntryLine.objects.create(
                    tenant=self.tenant, entry=entry,
                    account=accounts[self._acct_key(ln)],
                    name=ln.get("name") or ref or "",
                    debit=Decimal(str(ln.get("debit") or 0)),
                    credit=Decimal(str(ln.get("credit") or 0)))
            if post:
                entry.post()         # re-validates + assigns name + posts
            entry.refresh_from_db()

        return {
            "external_id": entry.id, "state": entry.state,
            "posted": bool(post), "balanced": True, "line_count": len(lines),
            "total_debit": total_debit, "total_credit": total_credit,
        }

    # ----- CAP.17: local trial balance ------------------------------------
    def fetch_trial_balance(self, *, date_from=None, date_to=None,
                            posted_only=True, **_ignore):
        self.require("CAP.17")
        from accounting.models import JournalEntryLine
        qs = JournalEntryLine.objects.for_tenant(self.tenant)
        if posted_only:
            qs = qs.filter(entry__state="posted")
        if date_from:
            qs = qs.filter(entry__date__gte=date_from)
        if date_to:
            qs = qs.filter(entry__date__lte=date_to)
        grouped = (qs.values("account__id", "account__code",
                             "account__name", "account__type")
                   .annotate(debit=Sum("debit"), credit=Sum("credit"))
                   .order_by("account__code"))
        rows, td, tc = [], Decimal("0"), Decimal("0")
        for g in grouped:
            d = g["debit"] or Decimal("0")
            c = g["credit"] or Decimal("0")
            td += d
            tc += c
            rows.append({
                "external_id": g["account__id"], "code": g["account__code"],
                "name": g["account__name"], "account_type": g["account__type"],
                "debit": float(d), "credit": float(c), "balance": float(d - c),
            })
        return {
            "accounts": rows, "account_count": len(rows),
            "total_debit": float(td), "total_credit": float(tc),
            "balanced": td == tc,
        }

    # ----- resolution helpers ---------------------------------------------
    @staticmethod
    def _acct_key(ln):
        if ln.get("account_id") is not None:
            return ("id", ln["account_id"])
        return ("code", ln.get("account_code"))

    def _resolve_accounts(self, lines):
        from accounting.models import Account
        out = {}
        for ln in lines:
            key = self._acct_key(ln)
            if key in out:
                continue
            kind, val = key
            if not val:
                raise ValueError("Each line needs an account_code or account_id.")
            lookup = {"id": val} if kind == "id" else {"code": val}
            try:
                out[key] = Account.objects.for_tenant(self.tenant).get(**lookup)
            except Account.DoesNotExist:
                raise ValueError(
                    f"Account {val!r} not found in the local ledger.")
        return out

    def _resolve_journal(self, journal_code, journal_id):
        from accounting.models import Journal
        qs = Journal.objects.for_tenant(self.tenant)
        try:
            if journal_id is not None:
                return qs.get(id=journal_id)
            if journal_code:
                return qs.get(code=journal_code)
        except Journal.DoesNotExist:
            raise ValueError(
                f"No local journal {journal_code or journal_id!r}.")
        raise ValueError(
            "post_journal_entry needs journal_code or journal_id.")


def connector_for_tenant(tenant):
    """Route a tenant to its ERP connector if one is connected, else the
    standalone local GL. A ``manual`` connection is treated as no-ERP."""
    from accounting.models import ERPConnection
    from connectors.registry import build_connector
    conn = (ERPConnection.objects
            .filter(tenant=tenant, is_active=True)
            .exclude(vendor="manual")
            .order_by("-is_primary", "name")
            .first())
    if conn:
        return build_connector(conn)
    return LocalGLConnector(tenant)
