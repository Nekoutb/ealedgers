"""Ensure the ``admin`` superuser exists with the configured credentials.

Per execution-plan rule **R4** (see ``docs/EXECUTION_PLAN.md`` §0):

> ``admin`` / ``admin`` login always exists in every environment
> (dev + sandbox + prod-v2).

This command is **idempotent** — safe to run on every deploy. It is hooked
into the ``post_migrate`` signal in ``accounting/apps.py`` so every
successful ``migrate`` (including every GitHub-Actions deploy) finishes
by re-asserting the admin user.

Env-var overrides — useful on production if you want a different password
than the literal ``admin``:

==============================  ==========================================
``EA_ADMIN_USERNAME``           Defaults to ``admin``.
``EA_ADMIN_PASSWORD``           Defaults to ``admin``. **Set this on prod**
                                if you don't want the production admin
                                password rotated to ``admin`` on next
                                deploy.
``EA_ADMIN_EMAIL``              Defaults to ``admin@ealedgers.com``.
``EA_ADMIN_SKIP``               If set to a truthy value, the command is
                                a no-op — useful in tests that don't want
                                the side effect.
==============================  ==========================================

The password is **re-set on every run** so the rule "admin/admin always
works" stays true even after a manual rotation. If you want the password
to persist on production, set ``EA_ADMIN_PASSWORD`` in the systemd
EnvironmentFile to whatever the production password should be.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


def _is_truthy(value):
    return (value or '').strip().lower() in ('1', 'true', 'yes', 'on')


class Command(BaseCommand):
    help = 'Ensure the admin superuser exists with the configured password (R4).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--quiet',
            action='store_true',
            help='Suppress all output except errors.',
        )

    def handle(self, *args, **options):
        quiet = options.get('quiet', False)

        if _is_truthy(os.environ.get('EA_ADMIN_SKIP')):
            if not quiet:
                self.stdout.write('EA_ADMIN_SKIP set — skipping ensure_admin.')
            return

        username = os.environ.get('EA_ADMIN_USERNAME', 'admin')
        password = os.environ.get('EA_ADMIN_PASSWORD', 'admin')
        email = os.environ.get('EA_ADMIN_EMAIL', 'admin@ealedgers.com')

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                'email': email,
                'is_superuser': True,
                'is_staff': True,
                'is_active': True,
            },
        )

        # Re-assert flags + email + password on every run (idempotent).
        user.email = email
        user.is_superuser = True
        user.is_staff = True
        user.is_active = True
        user.set_password(password)
        user.save()

        if not quiet:
            verb = 'Created' if created else 'Refreshed'
            self.stdout.write(self.style.SUCCESS(
                f'{verb} superuser {username!r} (R4).'
            ))
