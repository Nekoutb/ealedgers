"""Step 17 — load the K20 SYSCOHADA first-application (transition) slice.

Encodes SYSCOHADA Titre VIII Ch.41 (Première application du SYSCOHADA
révisé): the change-of-method / retrospective principle, the opening
balance sheet, the compte 475 transitional mechanism (4751/4752), capital
protection, conformity declaration, and the per-item transition treatments
(charges immobilisées, component approach, leases, investment property,
retirement commitments, pro-forma).

Same idempotent pattern; reverse deletes only the K20 rules.
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k20(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K20")


def unload_k20(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K20", framework="SYSCOHADA-2017",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0003_load_k15_syscohada_evaluation"),
    ]

    operations = [
        migrations.RunPython(load_k20, unload_k20),
    ]
