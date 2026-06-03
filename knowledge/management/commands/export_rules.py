"""Export the rule-review CSV to a file (Step 27 enablement).

    python manage.py export_rules --out rules_review.csv
    python manage.py export_rules --framework CGI-2025 --out cgi_review.csv

The same CSV the explorer's "Export for expert review" button downloads —
this is for offline / repeatable generation (e.g. emailing the fiscaliste).
"""

from django.core.management.base import BaseCommand

from knowledge.export import build_rules_review_csv
from knowledge.models import Rule


class Command(BaseCommand):
    help = "Export rules as a reviewer CSV for the Step-27 expert sign-off."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="rules_review.csv")
        parser.add_argument("--framework", default=None)

    def handle(self, *args, **options):
        rules = Rule.objects.all().order_by(
            "framework", "knowledge_slice", "slug")
        if options["framework"]:
            rules = rules.filter(framework=options["framework"])
        csv_text = build_rules_review_csv(rules)
        with open(options["out"], "w", encoding="utf-8", newline="") as fh:
            fh.write(csv_text)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {rules.count()} rules to {options['out']}"))
