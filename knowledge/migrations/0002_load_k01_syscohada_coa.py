"""Step 15 — load the K01 SYSCOHADA chart-of-accounts knowledge slice.

Auto-loads the K01 rules (9 account classes + structural conventions)
on deploy via the shared loader, so production gets the knowledge without
a manual step. Idempotent (update_or_create by slug) — re-applying or a
future re-encoding just re-syncs.

Reverse deletes only the K01 / SYSCOHADA-2017 rules this slice owns.
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k01(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K01")


def unload_k01(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K01", framework="SYSCOHADA-2017",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(load_k01, unload_k01),
    ]
