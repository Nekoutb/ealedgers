"""Tenant-aware queryset and manager.

Attached as the default ``objects`` on every business model. The manager
keeps Django's normal API unchanged — ``Account.objects.all()`` returns
every row, same as before — and **adds** an explicit ``.for_tenant(tenant)``
filter that views and admins use to scope queries to the current tenant.

The choice of explicit filtering (rather than thread-local auto-filtering)
is deliberate: it's easier to grep for missing tenant filters in code review
than to diagnose a thread-local leak in async / Celery / test contexts.
Postgres Row-Level Security will be added as the database-level backstop
once we migrate off SQLite (Phase 0.3).
"""

from django.db import models


class TenantQuerySet(models.QuerySet):
    """QuerySet with a ``.for_tenant(tenant)`` method.

    ``for_tenant(None)`` returns an empty queryset on purpose so that a
    forgotten tenant value at a view's edge does NOT leak data. Always
    return ``self.none()`` when the tenant is missing.
    """

    def for_tenant(self, tenant):
        if tenant is None:
            return self.none()
        return self.filter(tenant=tenant)


# Django 5: Manager.from_queryset preserves the queryset API on the manager.
TenantManager = models.Manager.from_queryset(TenantQuerySet)
