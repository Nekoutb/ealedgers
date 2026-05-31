"""Step 18 (swapped with planned Step 19) — load the K10 CGI 2025 IS slice.

Encodes the Cameroon Code Général des Impôts 2025, Livre Premier, Titre I,
Chapitre I — Impôt sur les sociétés (articles 2 à 23): scope and exempt
entities, territoriality, the net-asset-comparison taxable-profit base, the
full deductibility regime (remuneration, head-office & technical-fee caps,
royalty/commission caps, taxes & fines, insurance, donations, thin-cap),
depreciation principles + the rate schedule, provisions and bad debts,
cash/invoice and tax-haven disallowances, capital gains on cessation, the
4-year (6-year for credit institutions) loss carry-forward, the parent-
subsidiary regime, the 30 %/25 % rates, transfer pricing, the monthly
acompte, the minimum de perception, and the annual-return deadlines.

NOTE ON STEP ORDER: the EXECUTION_PLAN sequenced K11 (TVA) as Step 18 and
K10 (IS) as Step 19. The CGI 2025 PDF supplied ends at article 124 and does
not contain the TVA section (articles 125 sqq.), so K11 cannot yet be
encoded from the authoritative source. Per the project's no-fabrication
rule, K10 (IS) — fully present in the PDF, articles 2–23 — is encoded first;
K11 (TVA) returns to the queue once its source pages are available.

Same idempotent pattern as the SYSCOHADA slices; reverse deletes only the
K10 (CGI-2025) rules. All rules ship review_status='needs_review' until the
Step-27 expert-review gate.
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k10(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K10")


def unload_k10(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K10", framework="CGI-2025",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0004_load_k20_syscohada_first_application"),
    ]

    operations = [
        migrations.RunPython(load_k10, unload_k10),
    ]
