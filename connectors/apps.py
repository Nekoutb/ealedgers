from django.apps import AppConfig


class ConnectorsConfig(AppConfig):
    """L6 — ERP capability layer (outbound).

    The bridge between the agents and each tenant's ERP. Holds the
    IERPConnector contract + capability registry (CAP.01–CAP.23, Step 28),
    the Odoo connector (Step 29+), and later Sage / SAP / Dynamics. The
    ERPConnection / ERPOperation models already live in `accounting`
    (Step 7); this app holds the connector code that drives them.

    Scaffolded empty in Step 11.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'connectors'
    verbose_name = 'Connectors (ERP capability layer)'

    def ready(self):
        # Import connector modules for their @register_connector side effect
        # so the factory can resolve each vendor (Step 30+).
        from connectors.odoo import connector  # noqa: F401
