"""Saga runner for ERP operations — track, retry with backoff, escalate
(Step 36).

Every ERP *write* the agents make should be (a) auditable and (b) resilient to
transient failures. ``run_erp_operation`` wraps a connector call in an
``ERPOperation`` record and a retry loop:

    pending → in_flight → success
                       ↘ (RetryableError ×N, backing off) → escalated

- Raise ``RetryableError`` from the operation for a transient failure (network
  blip, lock, rate-limit) — it's retried up to ``max_attempts`` with
  exponential backoff, then the operation is **escalated** (terminal, flagged
  for a human).
- Any other exception is treated as permanent → **failed** immediately (no
  retry), so we don't hammer the ERP on a bad request.

Synchronous today (there's no background worker yet — see the SSH/Django-Q2
bundle). The retry loop takes an injectable ``sleep`` so it's deterministic in
tests and ready to move onto a worker later without API change.
"""

from dataclasses import dataclass
import time

from django.utils import timezone

from accounting.models import ERPOperation


class RetryableError(Exception):
    """A transient ERP failure worth retrying (network, lock, rate-limit)."""


class NonRetryableError(Exception):
    """A permanent ERP failure — do not retry (bad request, validation)."""


@dataclass
class SagaOutcome:
    operation: ERPOperation
    ok: bool
    escalated: bool
    attempts: int
    result: object = None


def compute_backoff(attempt, *, base=0.5, cap=30.0):
    """Exponential backoff for the Nth attempt (1-indexed), capped.
    attempt 1 → base, 2 → 2·base, 3 → 4·base, … (deterministic, no jitter)."""
    return min(cap, base * (2 ** (attempt - 1)))


def run_erp_operation(*, connection, capability, func, request=None, method="",
                      tenant=None, max_attempts=3, backoff_base=0.5,
                      backoff_cap=30.0, retryable=(RetryableError,),
                      sleep=time.sleep):
    """Run ``func()`` inside an ``ERPOperation`` with retry/backoff + escalation.

    ``func`` is a zero-arg callable performing the connector call; its return
    value is stored in ``response`` (and ``external_id``, if present, in
    ``external_ids``). Returns a ``SagaOutcome``. Never raises for an ERP
    failure — the failure is recorded on the operation and reported via the
    outcome (so callers branch on ``outcome.ok`` / ``outcome.escalated``).
    """
    op = ERPOperation.objects.create(
        tenant=tenant or connection.tenant,
        connection=connection,
        capability=capability,
        method=method,
        request=request or {},
        status="in_flight",
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            result = func()
        except retryable as exc:
            op.retry_count = attempt - 1   # retries performed so far
            op.error = f"{type(exc).__name__}: {exc}"
            if attempt >= max_attempts:
                op.status = "escalated"
                op.completed_at = timezone.now()
                op.save(update_fields=[
                    "status", "error", "retry_count", "completed_at"])
                return SagaOutcome(op, ok=False, escalated=True,
                                   attempts=attempt)
            op.save(update_fields=["error", "retry_count"])
            sleep(compute_backoff(attempt, base=backoff_base, cap=backoff_cap))
            continue
        except Exception as exc:           # permanent — fail fast, no retry
            op.status = "failed"
            op.error = f"{type(exc).__name__}: {exc}"
            op.retry_count = attempt - 1
            op.completed_at = timezone.now()
            op.save(update_fields=[
                "status", "error", "retry_count", "completed_at"])
            return SagaOutcome(op, ok=False, escalated=False, attempts=attempt)

        # success
        op.status = "success"
        op.response = result if isinstance(result, dict) else {"result": result}
        if isinstance(result, dict) and "external_id" in result:
            op.external_ids = {"external_id": result["external_id"]}
        op.retry_count = attempt - 1
        op.completed_at = timezone.now()
        op.save(update_fields=[
            "status", "response", "external_ids", "retry_count",
            "completed_at"])
        return SagaOutcome(op, ok=True, escalated=False, attempts=attempt,
                           result=result)
