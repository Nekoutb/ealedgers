"""Low-level Odoo external-API client (Step 29).

A thin wrapper over Odoo's XML-RPC endpoints — the reliable external API for
Odoo SaaS and self-hosted alike:

  - ``{url}/xmlrpc/2/common`` — ``version()`` (no auth) and ``authenticate()``
  - ``{url}/xmlrpc/2/object`` — ``execute_kw(db, uid, key, model, method, …)``

This is *transport only*: auth, the `execute_kw` envelope, and a few CRUD
helpers (``search_read`` / ``read`` / ``create`` / ``write``) + a connectivity
``health_check``. The capability-level ``OdooConnector`` (CAP.01 …) that
implements ``IConnector`` is Step 30 and builds on this.

Secrets: the API key is read from an **environment variable named in the
ERPConnection config** (``api_key_env``), never stored in the DB or passed
through chat. Tests inject a key directly and a fake transport, so no network
or real credentials are needed to exercise the wrapper.

``transport_factory(endpoint_url) -> proxy`` is injectable so tests can mock
``xmlrpc.client.ServerProxy``. The default builds a real proxy lazily, so
constructing an ``OdooClient`` never touches the network.
"""

import os
import xmlrpc.client

from connectors.base import HealthStatus


class OdooConfigError(Exception):
    """Missing/invalid connection config (URL, db, credentials)."""


class OdooAuthError(Exception):
    """Authentication against Odoo failed."""


def _default_transport(endpoint_url):
    # allow_none: Odoo accepts/returns None (XML-RPC nil) in some calls.
    return xmlrpc.client.ServerProxy(endpoint_url, allow_none=True)


class OdooClient:
    def __init__(self, url, db, username, api_key=None, *,
                 api_key_env=None, transport_factory=None):
        if not url or not db or not username:
            raise OdooConfigError(
                "OdooClient needs url, db and username.")
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self._api_key = api_key
        self._api_key_env = api_key_env
        self._uid = None
        self._transport = transport_factory or _default_transport

    @classmethod
    def from_config(cls, config, *, transport_factory=None):
        """Build from an ``ERPConnection.config`` dict:
        ``{url, db, username, api_key_env}``."""
        config = config or {}
        missing = [k for k in ("url", "db", "username") if not config.get(k)]
        if missing:
            raise OdooConfigError(
                f"ERPConnection.config missing: {', '.join(missing)}.")
        return cls(
            config["url"], config["db"], config["username"],
            api_key=config.get("api_key"),
            api_key_env=config.get("api_key_env"),
            transport_factory=transport_factory,
        )

    # ----- credentials ---------------------------------------------------
    @property
    def api_key(self):
        if self._api_key is not None:
            return self._api_key
        if self._api_key_env:
            key = os.environ.get(self._api_key_env)
            if not key:
                raise OdooConfigError(
                    f"Environment variable {self._api_key_env!r} "
                    f"(the Odoo API key) is not set.")
            return key
        raise OdooConfigError(
            "No Odoo API key: pass api_key or set api_key_env.")

    # ----- endpoints -----------------------------------------------------
    def _common(self):
        return self._transport(f"{self.url}/xmlrpc/2/common")

    def _models(self):
        return self._transport(f"{self.url}/xmlrpc/2/object")

    # ----- calls ---------------------------------------------------------
    def version(self):
        """Server version info. No authentication required — the cheapest
        connectivity probe."""
        return self._common().version()

    def authenticate(self):
        """Resolve and cache the uid. Raises OdooAuthError on failure."""
        uid = self._common().authenticate(
            self.db, self.username, self.api_key, {})
        if not uid:
            raise OdooAuthError(
                f"Odoo rejected the credentials for {self.username!r} "
                f"on db {self.db!r}.")
        self._uid = uid
        return uid

    @property
    def uid(self):
        if self._uid is None:
            self.authenticate()
        return self._uid

    def execute_kw(self, model, method, args=None, kwargs=None):
        """The Odoo RPC envelope: call ``method`` on ``model``."""
        return self._models().execute_kw(
            self.db, self.uid, self.api_key,
            model, method, list(args or []), dict(kwargs or {}))

    # ----- CRUD helpers --------------------------------------------------
    def search_read(self, model, domain=None, fields=None, limit=None,
                    order=None, context=None):
        kwargs = {}
        if fields is not None:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if order is not None:
            kwargs["order"] = order
        if context is not None:
            # Odoo reads context from kwargs (e.g. {"active_test": False} to
            # include archived records).
            kwargs["context"] = context
        return self.execute_kw(model, "search_read", [domain or []], kwargs)

    def read(self, model, ids, fields=None):
        kwargs = {"fields": fields} if fields else {}
        return self.execute_kw(model, "read", [ids], kwargs)

    def formatted_read_group(self, model, domain=None, groupby=None,
                             aggregates=None, context=None):
        """Grouped aggregation (Odoo 17+ public API; replaces ``read_group``,
        which was removed in Odoo 19). ``aggregates`` use the ``"field:agg"``
        form, e.g. ``["debit:sum", "credit:sum"]``; returns one dict per group
        with those keys plus the ``groupby`` fields."""
        kwargs = {"groupby": groupby or [], "aggregates": aggregates or []}
        if context is not None:
            kwargs["context"] = context
        return self.execute_kw(
            model, "formatted_read_group", [domain or []], kwargs)

    def create(self, model, values):
        return self.execute_kw(model, "create", [values])

    def write(self, model, ids, values):
        return self.execute_kw(model, "write", [ids, values])

    # ----- health --------------------------------------------------------
    def health_check(self):
        """Probe connectivity + auth. Never raises — reports ``ok=False``.

        Maps onto ERPConnection.health: 'down' if the server is unreachable,
        'degraded' if reachable but auth fails, 'ok' if authenticated.
        """
        try:
            version = self.version()
        except Exception as exc:  # noqa: BLE001 — report, don't propagate
            return HealthStatus(
                ok=False, state="down",
                detail=f"Cannot reach Odoo at {self.url}: {exc}")
        try:
            uid = self.authenticate()
        except Exception as exc:  # noqa: BLE001
            srv = version.get("server_version", "?") if isinstance(version, dict) else "?"
            return HealthStatus(
                ok=False, state="degraded",
                detail=f"Reached Odoo {srv} but authentication failed: {exc}")
        srv = version.get("server_version", "?") if isinstance(version, dict) else "?"
        return HealthStatus(
            ok=True, state="ok",
            detail=f"Authenticated to Odoo {srv} as uid {uid}.")
