"""Step 16 — load the K15 SYSCOHADA evaluation & determination-of-result slice.

Encodes SYSCOHADA Acte Uniforme Titre I Ch.4 (Articles 35–51): the base
conventions (historical cost / prudence / going concern), acquisition and
production cost composition, the component approach, permanence of methods,
closing valuation, inventory valuation (FIFO/WAC), depreciation, impairment,
provisions, and FX-on-entry.

Same idempotent pattern as 0002 (update_or_create by slug); reverse deletes
only the K15 rules.
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k15(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K15")


def unload_k15(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K15", framework="SYSCOHADA-2017",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0002_load_k01_syscohada_coa"),
    ]

    operations = [
        migrations.RunPython(load_k15, unload_k15),
    ]
