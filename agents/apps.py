from django.apps import AppConfig


class AgentsConfig(AppConfig):
    """L4 — Reasoning layer (the virtual finance departments).

    Houses the multi-agent runtime: the department base classes
    (BaseDepartment / DepartmentManager / DepartmentSpecialist, Step 41),
    the event bus (Step 43), the dispatcher (Step 45), and one sub-package
    per department (agents/ap, agents/tax, …) standing them up from Step 51.

    Note: the agent-RUNTIME audit models (AgentRun, AgentToolCall) already
    live in the `accounting` app (Step 7) so they sit next to the ledger
    they describe; this app holds the orchestration code, not those models.

    Scaffolded empty in Step 11.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agents'
    verbose_name = 'Agents (virtual finance departments)'

    def ready(self):
        # Subscribe the dispatcher to inbound work events so a received
        # document (Step 52) is routed to its department's queue on the bus.
        # Idempotent — safe across worker reloads (subscribe de-duplicates).
        from agents.dispatcher import register_dispatcher
        register_dispatcher()
