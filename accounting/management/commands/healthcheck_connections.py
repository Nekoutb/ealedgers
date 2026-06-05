"""Re-run the health-check on every ERP connection — the 'nightly health-check'.

    python manage.py healthcheck_connections
    python manage.py healthcheck_connections --tenant elite-advisors
    python manage.py healthcheck_connections --active-only

Refreshes each connection's health, discovered capability list, and
last-checked time (the same logic the ERP form runs on save). Read-only against
the ERP — it only updates the connection's own health metadata, never the
tenant's accounting data. Intended to run from cron nightly, e.g.:

    15 2 * * *  cd /opt/ealedgers/app && /opt/ealedgers/venv/bin/python \
                manage.py healthcheck_connections --active-only >> \
                /opt/ealedgers/logs/healthcheck.log 2>&1
"""

from django.core.management.base import BaseCommand

from accounting.models import ERPConnection
from accounting.views import _test_connection


class Command(BaseCommand):
    help = ("Re-health-check ERP connections (refresh health + capability "
            "matrix). Safe to run anytime.")

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default=None,
                            help="Limit to one tenant slug.")
        parser.add_argument("--active-only", action="store_true",
                            help="Only check connections marked is_active.")

    def handle(self, *args, **options):
        qs = ERPConnection.objects.all().select_related("tenant")
        if options["tenant"]:
            qs = qs.filter(tenant__slug=options["tenant"])
        if options["active_only"]:
            qs = qs.filter(is_active=True)

        checked = ok = 0
        for conn in qs.order_by("tenant__slug", "name"):
            _test_connection(conn)          # never raises; persists the result
            conn.refresh_from_db()
            checked += 1
            if conn.health == "ok":
                ok += 1
            self.stdout.write(
                f"  {conn.tenant.slug}/{conn.name} [{conn.vendor}] -> "
                f"{conn.health} ({len(conn.capabilities)} caps)")

        self.stdout.write(self.style.SUCCESS(
            f"Health-checked {checked} connection(s); {ok} ok."))
