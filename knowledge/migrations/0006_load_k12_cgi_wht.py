"""Step 20 — load the K12 CGI 2025 withholding-tax (retenues à la source) slice.

Encodes the Cameroon Code Général des Impôts 2025 withholding-tax regime as
17 cited Rule rows. Sources are spread across the IRPP chapter (Titre I,
Ch. II) and the common-provisions chapter (Ch. III):

  - IRCM rate 15 % / 30 % (paradis fiscal) / 10 % (dividendes PME ≤ 3 Md) and
    the +10 % CAC majoration (art. 70, 71);
  - IRCM taxable base, withholding-at-source mechanics, the 9-month deemed-
    distribution rule, foreign-source RCM (art. 35, 85, 86);
  - reduced BVMAC securities rates 10 % / 5 % (art. 111);
  - salary withholding (PAS) + the progressive IRPP scale 10/15/25/35 %
    (art. 69, 71, 81-83);
  - property-income withholding 15 %, the 10 % libératoire on out-of-scope
    rents, and 5 %/10 % on built-property gains (art. 87-90);
  - public-procurement / public-accountant retenue, the 5 % acompte, the
    10 % digital-platform retenue, the 10 % libératoire on non-salaried
    agents, the BNC libératoire 10 %/5 %, public-expenditure collection, and
    the mandatory withholding certificate (art. 92, 92 bis, 92 ter, 93 bis,
    116 ter, 93 bis A; rates anchored in art. 69 (3)).

SCOPE NOTE — TSR deferred: the *Taxe Spéciale sur le Revenu* (the dedicated
withholding on non-resident service payments) is referenced by the code but
its rate/base provisions sit at art. 225+, beyond the supplied PDF's art.-124
cutoff — the same missing tail that also omits the TVA section (art. 125-149).
Per the project's no-fabrication rule the TSR is NOT encoded here; it returns
to the queue together with K11 (TVA) once the later CGI pages are available.
Art. 70 (1)'s 30 % rate on tax-haven passive income IS captured (it is a
domestic IRCM penalty rate, distinct from the TSR).

Same idempotent pattern; reverse deletes only the K12 (CGI-2025) rules. All
rules ship review_status='needs_review' until the Step-27 expert-review gate.
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k12(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K12")


def unload_k12(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K12", framework="CGI-2025",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0005_load_k10_cgi_is"),
    ]

    operations = [
        migrations.RunPython(load_k12, unload_k12),
    ]
