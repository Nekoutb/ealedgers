"""Smoke tests confirming the connectors app is installed and importable.
Real tests arrive with the IERPConnector contract from Step 28."""

from django.apps import apps
from django.test import SimpleTestCase


class ConnectorsAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('connectors'))

    def test_app_config_loads(self):
        config = apps.get_app_config('connectors')
        self.assertEqual(config.name, 'connectors')
