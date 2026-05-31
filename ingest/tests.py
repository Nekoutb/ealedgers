"""Smoke tests confirming the ingest app is installed and importable.
Real tests arrive with the ingestion pipeline from Step 52."""

from django.apps import apps
from django.test import SimpleTestCase


class IngestAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('ingest'))

    def test_app_config_loads(self):
        config = apps.get_app_config('ingest')
        self.assertEqual(config.name, 'ingest')
