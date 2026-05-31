"""Smoke tests confirming the knowledge app is installed and importable.
Real tests arrive with the models in Step 13."""

from django.apps import apps
from django.test import SimpleTestCase


class KnowledgeAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('knowledge'))

    def test_app_config_loads(self):
        config = apps.get_app_config('knowledge')
        self.assertEqual(config.name, 'knowledge')
