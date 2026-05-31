"""Load one or all knowledge slices from their fixtures (idempotent).

Usage:
    python manage.py load_knowledge            # load every known slice
    python manage.py load_knowledge --slice K01

Idempotent — re-running re-syncs rules by slug. Safe to run any time an
encoding fixture changes. (Data migrations also auto-load slices on deploy;
this command is for manual/local use and ad-hoc re-syncs.)
"""

from django.core.management.base import BaseCommand, CommandError

from knowledge.loaders import SLICE_FIXTURES, load_slice
from knowledge.models import Rule


class Command(BaseCommand):
    help = "Load knowledge slices (K01–K20) from their JSON fixtures."

    def add_arguments(self, parser):
        parser.add_argument(
            "--slice", dest="slice_id", default=None,
            help="Load only this slice id (e.g. K01). Omit to load all.",
        )

    def handle(self, *args, **options):
        slice_id = options.get("slice_id")
        targets = [slice_id] if slice_id else list(SLICE_FIXTURES.keys())

        total_created = total_updated = 0
        for sid in targets:
            try:
                created, updated = load_slice(Rule, sid)
            except ValueError as exc:
                raise CommandError(str(exc))
            total_created += created
            total_updated += updated
            self.stdout.write(
                f"  {sid}: {created} created, {updated} updated"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Knowledge load complete: {total_created} created, "
            f"{total_updated} updated."
        ))
