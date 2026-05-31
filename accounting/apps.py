"""Accounting app config + post-migrate hooks.

Hooks installed:

- ``post_migrate`` for this app fires ``ensure_admin`` so the ``admin/admin``
  rule (R4 in ``docs/EXECUTION_PLAN.md``) is re-asserted on every deploy.
  Override the credentials via the ``EA_ADMIN_*`` env vars (see the
  command's docstring).
"""

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _run_ensure_admin(sender, **kwargs):
    """post_migrate handler — re-assert the admin user after every migrate.

    Guarded so a failure here can never break ``migrate`` itself; if the
    auth tables haven't been created yet (e.g. on the very first migrate
    in a brand-new DB), we silently skip and pick it up on the next
    invocation.
    """
    # Only fire for this app's migrations — otherwise we'd run once per
    # contrib app each migrate and spam the log.
    if sender.name != 'accounting':
        return
    # Lazy import — Django apps registry must be ready before we touch ORM.
    from django.core.management import call_command
    try:
        call_command('ensure_admin', quiet=True)
    except Exception as exc:  # pragma: no cover — defensive, hard to repro
        # We do not want to ever crash a migrate. Surface the issue but
        # keep going.
        import sys
        sys.stderr.write(f'[accounting.apps] ensure_admin failed: {exc}\n')


class AccountingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounting'

    def ready(self):
        post_migrate.connect(_run_ensure_admin, sender=self)
