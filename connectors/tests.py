"""Connector contract + capability registry tests (Step 28)."""

from django.apps import apps
from django.test import SimpleTestCase, TestCase

from connectors import capabilities as caps
from connectors.base import (
    CapabilityNotSupported, ConnectorNotImplemented, HealthStatus,
    IConnector, NullConnector,
)
from connectors.registry import (
    CONNECTOR_REGISTRY, build_connector, connector_for_vendor,
    register_connector,
)


class ConnectorsAppScaffoldTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertTrue(apps.is_installed('connectors'))

    def test_app_config_loads(self):
        config = apps.get_app_config('connectors')
        self.assertEqual(config.name, 'connectors')


class CapabilityRegistryTests(SimpleTestCase):
    def test_registry_has_23_capabilities(self):
        self.assertEqual(len(caps.CAPABILITIES), 23)

    def test_codes_are_cap_01_to_23(self):
        expected = {f"CAP.{n:02d}" for n in range(1, 24)}
        self.assertEqual(set(caps.CAPABILITIES), expected)

    def test_plan_referenced_capabilities_present(self):
        # Every CAP the EXECUTION_PLAN's P03/P05+ steps name must exist.
        for code in ("CAP.01", "CAP.02", "CAP.03", "CAP.05", "CAP.06",
                     "CAP.07", "CAP.08", "CAP.09", "CAP.10", "CAP.11",
                     "CAP.12", "CAP.15", "CAP.17", "CAP.22"):
            self.assertIn(code, caps.CAPABILITIES)

    def test_known_anchors(self):
        self.assertEqual(caps.get("CAP.03").name, "Post journal entry")
        self.assertEqual(caps.get("CAP.01").direction, "read")
        self.assertEqual(caps.get("CAP.22").direction, "event")

    def test_directions_are_valid(self):
        for c in caps.CAPABILITIES.values():
            self.assertIn(c.direction, {"read", "write", "event"})
            self.assertTrue(c.name and c.cycle and c.description)

    def test_read_write_partition(self):
        self.assertTrue(set(caps.READ_CODES).isdisjoint(caps.WRITE_CODES))

    def test_get_unknown_raises(self):
        with self.assertRaises(KeyError):
            caps.get("CAP.99")
        self.assertFalse(caps.is_valid("CAP.99"))


class IConnectorContractTests(SimpleTestCase):
    def test_iconnector_is_abstract(self):
        with self.assertRaises(TypeError):
            IConnector()

    def test_null_connector_supports_nothing(self):
        c = NullConnector()
        self.assertEqual(c.capabilities, frozenset())
        self.assertFalse(c.supports("CAP.03"))
        self.assertEqual(c.describe()["n_capabilities"], 0)

    def test_null_connector_health_ok(self):
        h = NullConnector().health_check()
        self.assertIsInstance(h, HealthStatus)
        self.assertTrue(h.ok)
        self.assertEqual(h.state, "unconfigured")

    def test_require_raises_capability_not_supported(self):
        c = NullConnector()
        with self.assertRaises(CapabilityNotSupported):
            c.require("CAP.03")

    def test_require_unknown_code_raises_keyerror(self):
        with self.assertRaises(KeyError):
            NullConnector().require("CAP.99")

    def test_operation_methods_gate_on_capability(self):
        c = NullConnector()
        for op in (c.lookup_chart_of_accounts, c.upsert_partner,
                   c.post_journal_entry, c.fetch_trial_balance):
            with self.assertRaises(CapabilityNotSupported):
                op()
        with self.assertRaises(CapabilityNotSupported):
            c.execute("CAP.01")

    def test_supported_capability_falls_through_to_not_implemented(self):
        # A connector that *declares* a cap but hasn't implemented the op
        # should reach NotImplementedError, not CapabilityNotSupported.
        class PartialConnector(IConnector):
            vendor = "partial"

            @property
            def capabilities(self):
                return frozenset({"CAP.03"})

            def health_check(self):
                return HealthStatus(ok=True)

        with self.assertRaises(NotImplementedError):
            PartialConnector().post_journal_entry()


class ConnectorRegistryTests(SimpleTestCase):
    def test_manual_vendor_yields_null_connector(self):
        self.assertIsInstance(connector_for_vendor("manual"), NullConnector)

    def test_unregistered_vendor_raises(self):
        # sap_b1 is a known ERP vendor with no connector yet (Odoo is now
        # registered as of Step 30).
        with self.assertRaises(ConnectorNotImplemented):
            connector_for_vendor("sap_b1")

    def test_odoo_vendor_resolves(self):
        from connectors.odoo.connector import OdooConnector
        self.assertIsInstance(connector_for_vendor("odoo", {}), OdooConnector)

    def test_register_connector_rejects_non_connector(self):
        with self.assertRaises(TypeError):
            @register_connector("bogus")
            class NotAConnector:
                pass

    def test_register_and_resolve(self):
        @register_connector("test_vendor")
        class TmpConnector(NullConnector):
            vendor = "test_vendor"
        try:
            self.assertIsInstance(
                connector_for_vendor("test_vendor"), TmpConnector)
        finally:
            CONNECTOR_REGISTRY.pop("test_vendor", None)


class BuildConnectorFromModelTests(TestCase):
    def test_build_connector_from_erpconnection(self):
        from accounting.models import Currency, ERPConnection, Tenant
        xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        t = Tenant.objects.create(slug="conn-t", name="Conn T", currency=xaf)
        conn = ERPConnection.objects.create(
            tenant=t, name="Standalone", vendor="manual")
        self.assertIsInstance(build_connector(conn), NullConnector)

    def test_build_connector_unimplemented_vendor(self):
        from accounting.models import Currency, ERPConnection, Tenant
        xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        t = Tenant.objects.create(slug="conn-t2", name="Conn T2", currency=xaf)
        conn = ERPConnection.objects.create(
            tenant=t, name="Future SAP", vendor="sap_b1")
        with self.assertRaises(ConnectorNotImplemented):
            build_connector(conn)


class ConnectorKeyInjectionTests(TestCase):
    """build_connector decrypts the stored API key and injects it into the
    connector's config (so the connector can authenticate)."""

    def test_decrypted_key_injected_into_config(self):
        from accounting.models import Currency, ERPConnection, Tenant
        xaf = Currency.objects.create(code="XAF", name="CFA", decimal_places=0)
        t = Tenant.objects.create(slug="conn-key", name="Conn Key", currency=xaf)
        conn = ERPConnection.objects.create(
            tenant=t, name="Odoo", vendor="odoo",
            config={"url": "https://x.odoo.com", "db": "x", "username": "u"})
        conn.set_api_key("secret-key")
        conn.save()
        connector = build_connector(conn)           # OdooConnector
        self.assertEqual(connector.config.get("api_key"), "secret-key")
        self.assertEqual(connector.config.get("url"), "https://x.odoo.com")
