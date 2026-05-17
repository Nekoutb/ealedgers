"""Data migration: create the 'Elite Advisors' Tenant and backfill every
existing business-model row's tenant_id to point at it.

This runs after 0002_tenant_and_membership which adds the Tenant model and
the nullable tenant FK columns. After this migration, no row in any
business table has a NULL tenant. (A later migration will tighten the FK
to NOT NULL once the middleware + managers are in place — Phase 0.2.)

The migration is idempotent: re-running it does nothing if the tenant
already exists. Reverse direction sets tenant_id back to NULL but does NOT
delete the Tenant or Membership (avoid wiping user data on a rollback).
"""

from django.db import migrations


TENANT_SLUG = "elite-advisors"
TENANT_NAME = "Elite Advisors SARL"
TENANT_LEGAL = "Elite Advisors SARL"
TENANT_COUNTRY = "Cameroon"
TENANT_CURRENCY_CODE = "XAF"
TENANT_BUSINESS_TYPE = "services"
ADMIN_USERNAME = "admin"

BUSINESS_MODELS = (
    "Partner",
    "Account",
    "Journal",
    "JournalEntry",
    "JournalEntryLine",
    "FixedAsset",
    "DepreciationLine",
)


def backfill_tenant(apps, schema_editor):
    Tenant = apps.get_model("accounting", "Tenant")
    Membership = apps.get_model("accounting", "Membership")
    Currency = apps.get_model("accounting", "Currency")
    User = apps.get_model("auth", "User")

    xaf = Currency.objects.filter(code=TENANT_CURRENCY_CODE).first()
    admin_user = User.objects.filter(username=ADMIN_USERNAME).first()

    tenant, _created = Tenant.objects.get_or_create(
        slug=TENANT_SLUG,
        defaults={
            "name": TENANT_NAME,
            "legal_name": TENANT_LEGAL,
            "country": TENANT_COUNTRY,
            "currency": xaf,
            "business_type": TENANT_BUSINESS_TYPE,
            "fiscal_year_start_month": 1,
            "plan": "free",
            "owner": admin_user,
            "active": True,
        },
    )

    # Make sure the admin user has an owner membership.
    if admin_user is not None:
        Membership.objects.get_or_create(
            user=admin_user,
            tenant=tenant,
            defaults={"role": "owner", "active": True},
        )
        if tenant.owner_id is None:
            tenant.owner = admin_user
            tenant.save(update_fields=["owner"])

    # Backfill tenant_id on every business model row that doesn't have one.
    for model_name in BUSINESS_MODELS:
        Model = apps.get_model("accounting", model_name)
        Model.objects.filter(tenant__isnull=True).update(tenant=tenant)


def unbackfill_tenant(apps, schema_editor):
    """Reverse: just NULL out tenant_id. Don't delete the tenant or
    memberships — those are user data."""
    for model_name in BUSINESS_MODELS:
        Model = apps.get_model("accounting", model_name)
        Model.objects.update(tenant=None)


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0002_tenant_and_membership"),
    ]

    operations = [
        migrations.RunPython(backfill_tenant, unbackfill_tenant),
    ]
