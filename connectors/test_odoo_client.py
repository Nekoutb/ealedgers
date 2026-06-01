"""OdooClient tests (Step 29) — fully mocked, no network or real creds.

A fake XML-RPC transport stands in for ``xmlrpc.client.ServerProxy`` so we
exercise auth, the execute_kw envelope, the CRUD helpers and health-check
offline. The live "health-check passes against test Odoo" verification needs
a real instance and is done once the owner provides connection details.
"""

from unittest import mock

from django.test import SimpleTestCase

from connectors.base import HealthStatus
from connectors.odoo.client import OdooAuthError, OdooClient, OdooConfigError


class FakeCommon:
    def __init__(self, version_ret=None, auth_ret=7,
                 raise_version=None, raise_auth=None):
        self.version_ret = version_ret or {"server_version": "17.0"}
        self.auth_ret = auth_ret
        self.raise_version = raise_version
        self.raise_auth = raise_auth
        self.auth_calls = []
        self.version_calls = 0

    def version(self):
        self.version_calls += 1
        if self.raise_version:
            raise self.raise_version
        return self.version_ret

    def authenticate(self, db, username, key, opts):
        self.auth_calls.append((db, username, key, opts))
        if self.raise_auth:
            raise self.raise_auth
        return self.auth_ret


class FakeObject:
    def __init__(self, ret=None):
        self.ret = ret
        self.calls = []

    def execute_kw(self, db, uid, key, model, method, args, kwargs):
        self.calls.append({
            "db": db, "uid": uid, "key": key, "model": model,
            "method": method, "args": args, "kwargs": kwargs,
        })
        if callable(self.ret):
            return self.ret(model, method, args, kwargs)
        return self.ret


def transport(common, obj):
    def factory(endpoint):
        if endpoint.endswith("/xmlrpc/2/common"):
            return common
        if endpoint.endswith("/xmlrpc/2/object"):
            return obj
        raise AssertionError(f"unexpected endpoint {endpoint}")
    return factory


def make_client(common=None, obj=None, **kw):
    common = common or FakeCommon()
    obj = obj or FakeObject()
    kw.setdefault("api_key", "secret-key")
    return OdooClient("https://demo.odoo.com/", "demo", "agent@demo",
                      transport_factory=transport(common, obj), **kw), common, obj


class OdooClientConfigTests(SimpleTestCase):
    def test_requires_core_fields(self):
        with self.assertRaises(OdooConfigError):
            OdooClient("", "db", "user")

    def test_from_config_reports_missing(self):
        with self.assertRaises(OdooConfigError):
            OdooClient.from_config({"url": "https://x", "db": "d"})  # no username

    def test_from_config_builds(self):
        c = OdooClient.from_config(
            {"url": "https://x.odoo.com", "db": "d", "username": "u",
             "api_key_env": "ODOO_KEY"})
        self.assertEqual(c.db, "d")
        self.assertEqual(c.url, "https://x.odoo.com")

    def test_url_is_trimmed(self):
        c, _, _ = make_client()
        self.assertEqual(c.url, "https://demo.odoo.com")


class OdooClientAuthTests(SimpleTestCase):
    def test_version_no_auth(self):
        c, common, _ = make_client()
        self.assertEqual(c.version()["server_version"], "17.0")
        self.assertEqual(common.version_calls, 1)
        self.assertEqual(common.auth_calls, [])  # version is unauthenticated

    def test_authenticate_caches_uid(self):
        c, common, _ = make_client()
        self.assertEqual(c.uid, 7)
        _ = c.uid  # second access
        self.assertEqual(len(common.auth_calls), 1)  # authenticated once

    def test_authenticate_passes_credentials(self):
        c, common, _ = make_client(api_key="my-key")
        c.authenticate()
        self.assertEqual(common.auth_calls[0], ("demo", "agent@demo", "my-key", {}))

    def test_authentication_failure_raises(self):
        c, _, _ = make_client(FakeCommon(auth_ret=False))
        with self.assertRaises(OdooAuthError):
            c.authenticate()


class OdooClientExecuteTests(SimpleTestCase):
    def test_execute_kw_envelope(self):
        c, _, obj = make_client(api_key="k")
        c.execute_kw("res.partner", "read", [[1]], {"fields": ["name"]})
        call = obj.calls[0]
        self.assertEqual(call["db"], "demo")
        self.assertEqual(call["uid"], 7)
        self.assertEqual(call["key"], "k")
        self.assertEqual(call["model"], "res.partner")
        self.assertEqual(call["method"], "read")

    def test_search_read_builds_kwargs(self):
        obj = FakeObject(ret=[{"id": 1, "code": "601000"}])
        c, _, _ = make_client(obj=obj)
        rows = c.search_read("account.account",
                             domain=[["deprecated", "=", False]],
                             fields=["code", "name"], limit=5)
        self.assertEqual(rows[0]["code"], "601000")
        call = obj.calls[0]
        self.assertEqual(call["method"], "search_read")
        self.assertEqual(call["args"], [[["deprecated", "=", False]]])
        self.assertEqual(call["kwargs"], {"fields": ["code", "name"], "limit": 5})

    def test_create_and_write_helpers(self):
        obj = FakeObject(ret=42)
        c, _, _ = make_client(obj=obj)
        self.assertEqual(c.create("res.partner", {"name": "Acme"}), 42)
        self.assertEqual(obj.calls[-1]["method"], "create")
        self.assertEqual(obj.calls[-1]["args"], [{"name": "Acme"}])
        c.write("res.partner", [42], {"name": "Acme SARL"})
        self.assertEqual(obj.calls[-1]["method"], "write")
        self.assertEqual(obj.calls[-1]["args"], [[42], {"name": "Acme SARL"}])


class OdooClientApiKeyTests(SimpleTestCase):
    def test_explicit_key(self):
        c = OdooClient("https://x", "d", "u", api_key="abc",
                       transport_factory=transport(FakeCommon(), FakeObject()))
        self.assertEqual(c.api_key, "abc")

    def test_key_from_env(self):
        c = OdooClient("https://x", "d", "u", api_key_env="ODOO_TEST_KEY",
                       transport_factory=transport(FakeCommon(), FakeObject()))
        with mock.patch.dict("os.environ", {"ODOO_TEST_KEY": "from-env"}):
            self.assertEqual(c.api_key, "from-env")

    def test_env_var_missing_raises(self):
        c = OdooClient("https://x", "d", "u", api_key_env="ODOO_MISSING_KEY",
                       transport_factory=transport(FakeCommon(), FakeObject()))
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(OdooConfigError):
                _ = c.api_key

    def test_no_key_configured_raises(self):
        c = OdooClient("https://x", "d", "u",
                       transport_factory=transport(FakeCommon(), FakeObject()))
        with self.assertRaises(OdooConfigError):
            _ = c.api_key


class OdooClientHealthTests(SimpleTestCase):
    def test_health_ok(self):
        c, _, _ = make_client()
        h = c.health_check()
        self.assertIsInstance(h, HealthStatus)
        self.assertTrue(h.ok)
        self.assertEqual(h.state, "ok")
        self.assertIn("17.0", h.detail)

    def test_health_down_when_unreachable(self):
        c, _, _ = make_client(FakeCommon(raise_version=OSError("conn refused")))
        h = c.health_check()
        self.assertFalse(h.ok)
        self.assertEqual(h.state, "down")

    def test_health_degraded_when_auth_fails(self):
        c, _, _ = make_client(FakeCommon(auth_ret=False))
        h = c.health_check()
        self.assertFalse(h.ok)
        self.assertEqual(h.state, "degraded")
