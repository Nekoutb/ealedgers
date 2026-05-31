"""Re-run the framework-conflict validator over tenant procedures (Step 25).

Procedures are validated on save, but a *framework* change (a cap, a rate,
a depreciation ceiling) can turn a previously-valid procedure into a
conflict. Run this after re-encoding rules to re-sync every procedure's
validation_status / notes. Idempotent.

Usage:
    python manage.py validate_procedures
    python manage.py validate_procedures --tenant 3
"""

from django.core.management.base import BaseCommand

from knowledge.models import TenantProcedure
from knowledge.validation import validate_and_apply


class Command(BaseCommand):
    help = "Re-validate tenant procedures against the current framework."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", dest="tenant_id", type=int, default=None,
            help="Limit to a single tenant id (default: all tenants).",
        )

    def handle(self, *args, **options):
        qs = TenantProcedure.objects.all()
        tid = options.get("tenant_id")
        if tid:
            qs = qs.filter(tenant_id=tid)

        counts = {"validated": 0, "conflict": 0}
        for proc in qs:
            validate_and_apply(proc)
            counts[proc.validation_status] = \
                counts.get(proc.validation_status, 0) + 1

        self.stdout.write(self.style.SUCCESS(
            f"Re-validated {sum(counts.values())} procedure(s): "
            f"{counts.get('validated', 0)} ok, "
            f"{counts.get('conflict', 0)} conflict."
        ))
