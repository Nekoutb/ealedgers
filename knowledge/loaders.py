"""Knowledge-fixture loader (Step 15).

Knowledge slices (K01–K20) are authored as JSON fixtures under
``knowledge/fixtures/`` and loaded idempotently by slug via
``update_or_create``. This is preferred over Django's ``loaddata`` because:

  - it's idempotent and re-runnable (re-encoding a slice just re-syncs);
  - it keys on the human-stable ``slug``, not auto PKs;
  - the same function serves both the data migrations (auto-load on deploy)
    and the ``load_knowledge`` management command (manual / local).

The loader takes the Rule model class explicitly so a migration can pass
its historical ``apps.get_model('knowledge', 'Rule')`` while the command
passes the real model.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

# Map slice id → fixture filename. Extend as slices are encoded (K02, …).
SLICE_FIXTURES = {
    "K01": "k01_syscohada_coa.json",
    "K15": "k15_syscohada_evaluation.json",
    "K20": "k20_syscohada_first_application.json",
    "K10": "k10_cgi_is.json",
}


def load_rules_from_file(RuleModel, fixture_path):
    """update_or_create Rule rows from one fixture file. Returns
    (created, updated)."""
    data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    created = updated = 0
    for item in data:
        slug = item["slug"]
        defaults = {k: v for k, v in item.items() if k != "slug"}
        _, was_created = RuleModel.objects.update_or_create(
            slug=slug, defaults=defaults,
        )
        created += int(was_created)
        updated += int(not was_created)
    return created, updated


def load_slice(RuleModel, slice_id):
    """Load a named slice (e.g. 'K01'). Returns (created, updated)."""
    if slice_id not in SLICE_FIXTURES:
        raise ValueError(f"Unknown knowledge slice {slice_id!r}. "
                         f"Known: {sorted(SLICE_FIXTURES)}")
    return load_rules_from_file(RuleModel, FIXTURE_DIR / SLICE_FIXTURES[slice_id])
