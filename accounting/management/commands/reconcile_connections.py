"""Drift / reconciliation report across ERP connections (Step 37 — nightly).

    python manage.py reconcile_connections
    python manage.py reconcile_connections --tenant elite-advisors

For each connection, compares EA Ledgers' recorded ERP writes against the
ERP's current state and prints a drift report. Read-only against the ERP. A
healthy tenant reports drift=0. Intended to run nightly from cron alongside
healthcheck_connections; one bad connection never aborts the run.
"""

from django.core.management.base import BaseCommand

from accounting.models import ERPConnection
from accounting.reconciliation import (
    odoo_posted_id_lister, reconcile_connection,
)


class Command(BaseCommand):
    help = ("Reconcile recorded ERP writes against the ERP's state and report "
            "drift (nightly). Read-only.")

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None,
                            help="Limit to one tenant slug.")

    def handle(self, *args, **options):
        qs = ERPConnection.objects.all().select_related("tenant")
        if options["tenant"]:
            qs = qs.filter(tenant__slug=options["tenant"])

        total_drift = 0
        for conn in qs.order_by("tenant__slug", "name"):
            label = f"{conn.tenant.slug}/{conn.name}"
            # Only an Odoo connection that's currently healthy can be read.
            can_read = conn.vendor == "odoo" and conn.health == "ok"
            lister = odoo_posted_id_lister(conn) if can_read else None
            try:
                report = reconcile_connection(conn, list_posted_ids=lister)
            except Exception as exc:   # noqa: BLE001 — never abort the batch
                self.stderr.write(
                    f"  {label}: reconcile error: {type(exc).__name__}: {exc}")
                continue
            total_drift += report.drift_count
            flag = "OK" if report.clean else "DRIFT"
            self.stdout.write(f"  [{flag}] {label}: {report.summary()}")
            for m in report.missing_in_erp:
                self.stdout.write(f"      missing in ERP: move {m}")
            for mm in report.mismatched:
                self.stdout.write(
                    f"      mismatched: move {mm['external_id']} "
                    f"is '{mm['state']}' (expected posted)")

        style = self.style.SUCCESS if total_drift == 0 else self.style.ERROR
        self.stdout.write(style(f"Total drift across connections: {total_drift}"))
