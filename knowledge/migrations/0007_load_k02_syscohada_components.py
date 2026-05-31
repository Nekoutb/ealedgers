"""Step 21 — load the K02 SYSCOHADA component-approach slice.

Encodes the SYSCOHADA component approach ("approche par composants") for
property, plant and equipment as 8 cited Rule rows, authored from the
SYSCOHADA Guide d'application, Deuxième partie (= Titre VIII), chapters 4-6
(the decomposition cluster):

  - Ch.4 (Approche par composants, Application 33): the decomposition
    principle (structure vs components with their own useful life / renewal
    rhythm), separate depreciation of each element, the dedicated
    structure/component sub-accounts, and component replacement
    (derecognition of the old component's NBV via account 81 + capitalisation
    of the new one, which may raise the asset's gross value);
  - Ch.5 (Frais d'inspections ou de révisions majeures, Application 34): a
    major inspection / overhaul (and security/compliance spend) treated as a
    distinct component depreciated over the interval between revisions;
  - Ch.6 (Coût de démantèlement, Application 35): the discounted dismantling/
    restoration estimate carried as an "actif de démantèlement" component
    sub-account (provision 1984), with the discount unwinding posted to 6971.

Same idempotent pattern; reverse deletes only the K02 rows. All rules ship
review_status='needs_review' until the Step-27 expert-review gate. This
returns the encoding queue to SYSCOHADA (K11/TVA stays blocked on source).
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k02(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K02")


def unload_k02(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K02", framework="SYSCOHADA-2017",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0006_load_k12_cgi_wht"),
    ]

    operations = [
        migrations.RunPython(load_k02, unload_k02),
    ]
