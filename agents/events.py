"""Persistent event bus — Step 43.

Events are named signals emitted when something significant happens in a
department or in the system: a bill is posted, an invoice is sent, a payment
is registered. Other departments *subscribe* to these event types and react.

Architecture
------------
  emit(event)          → persists a BusEvent row → queues a Django-Q2 task
  _dispatch(bus_id)    → Django-Q2 task: loads the row, calls all handlers
  subscribe(type, fn)  → adds fn to the in-process registry

The bus is process-local for handler *registration* (same pattern as Django
signals) — gunicorn workers all run the same application code, so each
worker registers the same set of handlers at startup.

The ``BusEvent`` row is the durability layer: even if a worker crashes mid-
dispatch, the row is in the DB (status='queued') and Django-Q2's built-in
retry (max_attempts=3) will re-run it.

Synchronous mode
----------------
Pass ``synchronous=True`` to ``emit()``, or set ``Q_CLUSTER = {'sync': True}``
in Django settings.  Handlers run in the same call, no Q2 worker needed.
Tests always use this path via the ``sync`` setting in the test overrides or
by passing ``synchronous=True`` directly.

Handler signature
-----------------
    def handler(event_type: str, payload: dict, tenant, chain_id: str) -> None: ...

Handlers must not raise (exceptions are caught and stored on the BusEvent row;
the next handler always runs — fail-open behaviour).

Typical registration (in a department's AppConfig.ready or at module level)::

    from agents.events import subscribe
    subscribe('bill.posted', ap_bill_posted_handler)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# Registry (process-local, populated at startup)
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, list[Callable]] = {}


def subscribe(event_type: str, handler: Callable) -> None:
    """Register ``handler`` to be called whenever ``event_type`` is emitted.

    Calling ``subscribe()`` multiple times with the same handler is
    idempotent — duplicate registrations are silently de-duplicated so that
    calling ``AppConfig.ready()`` on every hot-reload does not stack handlers.
    """
    bucket = _REGISTRY.setdefault(event_type, [])
    if handler not in bucket:
        bucket.append(handler)


def subscriptions_for(event_type: str) -> list[Callable]:
    """Return the (shallow-copied) list of handlers for this event type."""
    return list(_REGISTRY.get(event_type, []))


def clear_subscriptions(event_type: str | None = None) -> None:
    """Remove registrations. Pass ``event_type`` to target one type; omit to
    clear everything. Used in tests to prevent cross-test pollution."""
    if event_type is None:
        _REGISTRY.clear()
    else:
        _REGISTRY.pop(event_type, None)


# ---------------------------------------------------------------------------
# Event value object
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """A named occurrence in the system.

    ``event_type`` follows the ``noun.verb`` convention::

        'bill.posted'           # AP just posted a vendor bill
        'invoice.posted'        # AR just posted a customer invoice
        'invoice.sent'          # AR sent the invoice to the customer
        'payment.registered'    # Treasury registered a payment
        'period.closed'         # Controller closed a period

    ``chain_id`` threads this event to every other event in the same causal
    chain (Step 44 wires this to the Provenance / AgentRun audit trail).
    Auto-generated as a UUID when not supplied.
    """
    event_type: str                 # 'bill.posted', 'invoice.sent', …
    tenant: object                  # Tenant model instance
    payload: dict                   # event-specific data (vendor_id, amount, …)
    chain_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def emit(event: Event, *, synchronous: bool = False) -> object:
    """Persist the event and schedule (or immediately run) handlers.

    Returns the saved ``BusEvent`` DB row.

    ``synchronous=True`` runs handlers inline (no Q2 worker needed).
    Otherwise, the decision follows ``Q_CLUSTER.get('sync', False)`` from
    Django settings — useful for test environments.
    """
    from accounting.models import BusEvent

    bus_event = BusEvent.objects.create(
        tenant=event.tenant,
        event_type=event.event_type,
        payload=event.payload,
        chain_id=event.chain_id,
        metadata=event.metadata,
        status='queued',
    )

    if synchronous or _is_sync_mode():
        _dispatch(bus_event.id)
    else:
        from django_q.tasks import async_task
        task_id = async_task('agents.events._dispatch', bus_event.id, group='events') or ''
        bus_event.task_id = str(task_id)
        bus_event.save(update_fields=['task_id'])

    bus_event.refresh_from_db()
    return bus_event


def _is_sync_mode() -> bool:
    """True when Q_CLUSTER['sync'] == True (test/dev mode)."""
    from django.conf import settings
    return bool(getattr(settings, 'Q_CLUSTER', {}).get('sync', False))


# ---------------------------------------------------------------------------
# Dispatch (called by Django-Q2 worker OR inline in synchronous mode)
# ---------------------------------------------------------------------------

def _dispatch(bus_event_id: int) -> None:
    """Load the BusEvent, run every registered handler, persist the result.

    Called by the Django-Q2 ORM broker as a background task, or directly by
    ``emit()`` in synchronous mode.

    Handler errors are *caught* and stored on the row — they do not prevent
    other handlers from running (fail-open). The BusEvent is marked 'failed'
    only if at least one handler errored.
    """
    from django.utils import timezone
    from accounting.models import BusEvent

    try:
        bus_event = BusEvent.objects.select_related('tenant').get(pk=bus_event_id)
    except BusEvent.DoesNotExist:
        return  # already deleted / race condition

    handlers = _REGISTRY.get(bus_event.event_type, [])
    errors: list[str] = []

    for handler in handlers:
        try:
            handler(
                bus_event.event_type,
                bus_event.payload,
                bus_event.tenant,
                bus_event.chain_id,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open
            errors.append(f'{type(exc).__name__}@{handler.__name__}: {exc}')

    bus_event.status = 'failed' if errors else 'dispatched'
    bus_event.handler_count = len(handlers)
    bus_event.error = '\n'.join(errors)
    bus_event.dispatched_at = timezone.now()
    bus_event.save(update_fields=[
        'status', 'handler_count', 'error', 'dispatched_at'])
