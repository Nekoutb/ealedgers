"""Step 22 — load the K04 SYSCOHADA asset-impairment slice.

Encodes the SYSCOHADA impairment of fixed assets ("dépréciation des
immobilisations") as 11 cited Rule rows, authored from the SYSCOHADA Guide
d'application: the dépréciations section (1re partie, Ch.6 §3 — principle,
reversibility, accounts) and Deuxième partie (= Titre VIII) Ch.12
(Applications 44-47):

  - the impairment-test principle (annual indicator review → estimate valeur
    actuelle → compare with VNC; art. 46 AUDCIF) and the dépréciation (asset)
    vs provision (external liability) distinction (art. 46/48);
  - loss recognition (VNC − valeur actuelle), the post-impairment revised
    depreciation plan, and reversal capped at the historical-cost VNC (a
    reversal must never produce a revaluation);
  - reversibility mechanics and the dotation/dépréciation/reprise accounts
    (6913/6914, 6972, 853; 291-297; 7913/7914);
  - group impairment allocated first to goodwill (fonds commercial) then
    pro-rata to other assets' NBV, goodwill impairment never reversed;
  - impairment on a revalued asset (charged first against the revaluation
    surplus 1062, excess to 6914) and on a subsidised asset (net of the
    grant; two methods, same result).

K04 deepens K15's general closing-valuation rule (art. 42-45) with the
immobilisation-specific impairment mechanics. Same idempotent pattern;
reverse deletes only the K04 rows. All rules ship review_status='needs_review'
until the Step-27 expert-review gate.
"""

from django.db import migrations

from knowledge.loaders import load_slice


def load_k04(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    load_slice(Rule, "K04")


def unload_k04(apps, schema_editor):
    Rule = apps.get_model("knowledge", "Rule")
    Rule.objects.filter(
        knowledge_slice="K04", framework="SYSCOHADA-2017",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("knowledge", "0007_load_k02_syscohada_components"),
    ]

    operations = [
        migrations.RunPython(load_k04, unload_k04),
    ]
