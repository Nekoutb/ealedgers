"""Smoke tests confirming the agents app is installed and importable.
Real tests arrive with the department runtime from Step 41."""

from django.apps import apps
from django.test import SimpleTestCase


class AgentsAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('agents'))

    def test_app_config_loads(self):
        config = apps.get_app_config('agents')
        self.assertEqual(config.name, 'agents')
