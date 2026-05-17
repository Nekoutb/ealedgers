"""Install PostgreSQL Row-Level Security policies on tenant-scoped tables.

This migration is a **no-op on SQLite** (which has no RLS support) so the
test suite continues to pass and dev machines can keep using the bundled
SQLite database. On PostgreSQL it:

1. Enables RLS on every tenant-scoped table.
2. Adds a single policy per table that filters rows by
   ``tenant_id = current_setting('app.current_tenant_id')::bigint``.
3. Sets ``app.current_tenant_id = '0'`` as the database default so
   migrations / management commands without a request context see
   no rows by default — explicit ``set_config('app.current_tenant_id', X)``
   is required to see data.

The ``app.current_tenant_id`` session variable is pinned by
``accounting.middleware.TenantContextMiddleware`` on every request, so the
RLS policies kick in automatically for normal HTTP traffic.

Superusers and migration jobs that legitimately need cross-tenant access
can either:
  - ``ALTER ROLE ealedgers SET row_security = off;`` (per-role escape hatch), or
  - ``SET LOCAL app.current_tenant_id = '<tenant_id>';`` inside a transaction.

This is **defence in depth**, not the primary tenant filter. The ORM-level
``Model.objects.for_tenant(tenant)`` calls in views and the
``TenantAwareAdmin`` mixin remain the first line of defence; RLS catches
cases where that filter is forgotten somewhere.
"""

from django.db import migrations


# Tables we install RLS on. These are exactly the tenant-scoped models
# (anything with a `tenant_id` FK). Keep this list in sync with new models
# as they get added to accounting/models.py.
TENANT_TABLES = [
    'accounting_membership',
    'accounting_partner',
    'accounting_account',
    'accounting_journal',
    'accounting_journalentry',
    'accounting_journalentryline',
    'accounting_fixedasset',
    'accounting_depreciationline',
    'accounting_customerinvoice',
    'accounting_customerinvoiceline',
]


def _enable_rls(table):
    """SQL to enable RLS on a single table + add the tenant filter policy."""
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::bigint);
    """


def _disable_rls(table):
    return f"""
        DROP POLICY IF EXISTS tenant_isolation ON {table};
        ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
    """


def install_rls(apps, schema_editor):
    """Forward: enable RLS on every tenant-scoped table. Postgres only."""
    if schema_editor.connection.vendor != 'postgresql':
        return
    cursor = schema_editor.connection.cursor()
    # Default the session variable so migrations run in this same
    # connection don't error on missing setting. Per-request middleware
    # later overrides this with set_config(..., is_local=true).
    cursor.execute("SELECT set_config('app.current_tenant_id', '0', false)")
    for table in TENANT_TABLES:
        cursor.execute(_enable_rls(table))


def uninstall_rls(apps, schema_editor):
    """Reverse: drop policies and disable RLS."""
    if schema_editor.connection.vendor != 'postgresql':
        return
    cursor = schema_editor.connection.cursor()
    for table in TENANT_TABLES:
        cursor.execute(_disable_rls(table))


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0005_customerinvoice_customerinvoiceline_and_more'),
    ]

    operations = [
        migrations.RunPython(install_rls, uninstall_rls, elidable=False),
    ]
