"""Connector registry + factory (Step 28).

Maps an ``ERPConnection.vendor`` to the connector class that drives it. New
connectors self-register with ``@register_connector("odoo")`` (Step 29+), so
the factory stays open for extension without edits here.

At Step 28 only the standalone ``NullConnector`` ("manual") is registered;
asking for an unregistered vendor raises ``ConnectorNotImplemented`` with a
clear message rather than failing obscurely.
"""

from .base import ConnectorNotImplemented, IConnector, NullConnector


CONNECTOR_REGISTRY = {}


def register_connector(vendor):
    """Class decorator: register a connector under an ERPConnection vendor."""
    def _register(cls):
        if not issubclass(cls, IConnector):
            raise TypeError(f"{cls!r} must subclass IConnector")
        CONNECTOR_REGISTRY[vendor] = cls
        return cls
    return _register


# Standalone / no-ERP is available from day one.
register_connector("manual")(NullConnector)


def connector_for_vendor(vendor, config=None):
    """Instantiate the connector for a vendor string (no DB needed).

    ``config`` is the ERPConnection.config dict (URL, db, env-var names for
    secrets). Connectors read their settings from it.
    """
    cls = CONNECTOR_REGISTRY.get(vendor)
    if cls is None:
        raise ConnectorNotImplemented(
            f"No connector registered for vendor {vendor!r}. "
            f"Available: {', '.join(sorted(CONNECTOR_REGISTRY)) or '(none)'}."
        )
    return cls(config=config or {})


def build_connector(connection):
    """Instantiate the connector for an ``ERPConnection`` model instance,
    injecting its decrypted API key into the config so the connector can
    authenticate. The key is never persisted in config — only passed in
    transit to the connector."""
    config = dict(connection.config or {})
    key = connection.get_api_key()
    if key:
        config["api_key"] = key
    return connector_for_vendor(connection.vendor, config)
