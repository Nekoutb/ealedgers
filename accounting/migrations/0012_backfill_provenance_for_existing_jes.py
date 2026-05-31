"""Step 9 — backfill a Provenance row for every existing JournalEntry.

Before v2, journal entries were created by humans through the admin / the
invoicing & bill flows. None of them have a Provenance row yet. This data
migration creates one ``source='manual'`` Provenance per existing JE so
the audit trail is complete from day one — every entry in the ledger can
answer "why is this here?", even the historical ones.

Idempotent: only creates a Provenance for JEs that don't already have one
(so re-running, or running after some entries already got provenance from
normal v2 flow, is safe).

Reversible: the reverse drops only the rows this migration created, which
we tag with ``extra = {'backfilled': True}``.

Works on both SQLite and Postgres. On Postgres the RLS policy is bypassed
here because migrations run with the table-owner role and RLS is keyed on
``app.current_tenant_id`` which migration connections don't set to a real
tenant — but we write tenant_id explicitly on every row, so the data is
correct regardless.
"""

from django.db import migrations


BACKFILL_TAG = {'backfilled': True, 'step': 9}


def backfill_provenance(apps, schema_editor):
    JournalEntry = apps.get_model('accounting', 'JournalEntry')
    Provenance = apps.get_model('accounting', 'Provenance')

    # JE ids that already have a provenance row — skip them.
    already = set(
        Provenance.objects.exclude(journal_entry__isnull=True)
        .values_list('journal_entry_id', flat=True)
    )

    to_create = []
    qs = JournalEntry.objects.all().only('id', 'tenant_id', 'name', 'ref', 'state')
    for je in qs.iterator():
        if je.id in already:
            continue
        label = je.name or je.ref or f'JE #{je.id}'
        to_create.append(Provenance(
            tenant_id=je.tenant_id,
            journal_entry_id=je.id,
            source='manual',
            chain_id='',
            summary=f'Backfilled provenance for {label} '
                    f'(pre-v2 entry, source assumed manual).',
            citations=[],
            extra=dict(BACKFILL_TAG),
        ))

    # bulk_create in batches to keep memory bounded on large ledgers.
    if to_create:
        Provenance.objects.bulk_create(to_create, batch_size=500)


def remove_backfilled_provenance(apps, schema_editor):
    Provenance = apps.get_model('accounting', 'Provenance')
    # Delete only the rows this migration created. We tagged them; match on
    # the source + the backfilled flag in extra. extra is JSON; do a Python
    # filter to stay DB-agnostic (SQLite JSON querying is limited).
    ids = [
        p.id for p in Provenance.objects.filter(source='manual').only('id', 'extra')
        if isinstance(p.extra, dict) and p.extra.get('backfilled') is True
    ]
    if ids:
        Provenance.objects.filter(id__in=ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0011_tenant_accounting_framework_tenant_agent_enabled_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_provenance, remove_backfilled_provenance),
    ]
