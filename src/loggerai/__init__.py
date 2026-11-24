"""
LoggerAI for Python: performance monitoring, errors, logs and health checks
for FastAPI, Starlette, Django, Flask and Celery.

    import loggerai
    loggerai.init()                      # reads LOGGERAI_KEY (sk_live_...)

    # FastAPI / Starlette
    from loggerai.asgi import LoggerAIMiddleware
    app.add_middleware(LoggerAIMiddleware)

Nothing is sent without a key, nothing ever raises into your app, and
secrets are masked before anything leaves it.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Dict, Iterator, Optional

from . import context
from ._version import __version__
from .client import Client, get_client, set_client
from .circuit import CircuitBreaker, CircuitOpenError, CircuitCapacityError
from .circuit_registry import CircuitRegistry

__all__ = ["init", "capture_exception", "set_user", "set_route", "measure", "track", "trace_headers", "flush", "health_checks", "Client", "__version__", "CircuitBreaker", "CircuitOpenError", "CircuitCapacityError", "CircuitRegistry"]


def init(key: Optional[str] = None, **options: Any) -> Client:
    """Start LoggerAI. Every option also has an environment variable:
    key (LOGGERAI_KEY), endpoint, service, environment, release, enabled.
    Others: logs_level ("WARNING", or None for no logs), capture_queries,
    capture_http, capture_redis, ignore_paths, health_checks={"database": fn},
    host_metrics (the server's CPU, memory and disk; LOGGERAI_HOST_METRICS, on by default),
    flush_interval (seconds), timeout, root (the app's folder, for stack traces)."""
    old = get_client()
    if old is not None:
        old.close(timeout=1.0)
    client = Client(key, **options)
    set_client(client)
    import logging
    if client.enabled:
        logging.getLogger("loggerai").info("LoggerAI: sending as %s (%s) to %s", client.service, client.environment, client.endpoint)
    elif not client.key:
        logging.getLogger("loggerai").info("LoggerAI: off (no LOGGERAI_KEY)")
    if client.enabled:
        from . import instrument
        instrument.all_from(client)
        if client.health_checks or client.host_metrics:
            client.start()  # the sender thread samples checks and server metrics even on a quiet app
    return client


def capture_exception(exc: Optional[BaseException] = None, **extra: Any) -> None:
    """Report an exception you handled but still want to see."""
    import sys
    exc = exc or sys.exc_info()[1]
    client = get_client()
    if exc is not None and client is not None:
        client.exception(exc, handled=True, extra=extra or None)


def set_user(user_id: Any) -> None:
    """Count the current request against a user. Only a one-way hash of the id is sent."""
    unit, client = context.current.get(), get_client()
    if unit is not None and client is not None:
        unit.user = client.user_ref(user_id)


def set_route(template: str) -> None:
    """Name the current request yourself (a template like /orders/{id}, never a raw path)."""
    unit = context.current.get()
    if unit is not None:
        unit.route = template


@contextlib.contextmanager
def measure(target: str, operation: str = "CALL") -> Iterator[None]:
    """Time a call LoggerAI can't see on its own, e.g. an SDK with its own HTTP client:

        with loggerai.measure("api.stripe.com", "POST"):
            stripe.PaymentIntent.confirm(...)
    """
    t = time.perf_counter()
    ok = False
    try:
        yield
        ok = True
    finally:
        context.dependency(target, operation, (time.perf_counter() - t) * 1000, ok)


@contextlib.contextmanager
def track(kind: str, name: str, headers: Optional[Any] = None) -> Iterator[context.Unit]:
    """Time a unit of work outside a web request: a job, a scheduled task, a script.

        with loggerai.track("job", "send_invoices"):
            ...

    For a message another service published with loggerai.trace_headers(), pass the message's
    headers (a dict, or Kafka's list of (key, bytes) pairs) so the job joins that trace:

        with loggerai.track("job", "order.created", headers=message.headers()):
            ...
    """
    client = get_client()
    k = kind if kind in ("job", "command", "http") else "job"
    h = _lower(headers)
    unit = context.Unit(k, "JOB" if k != "command" else "CLI", None, context.session_from(h.get("x-loggerai-session")),
                        context.parent_from(h.get("traceparent")), h.get("x-loggerai-caller"))
    if unit.caller and k == "job":
        unit.transport = "queue"
    unit.route = name
    token = context.current.set(unit)
    status = 200
    try:
        yield unit
    except Exception as e:
        status = 500
        if client is not None:
            client.exception(e, status=500)
        raise
    finally:
        context.current.reset(token)
        if client is not None:
            client.finish(unit, status)


def trace_headers(target: Optional[str] = None) -> Dict[str, str]:
    """Headers that carry the current trace to another service: put them on a message you publish
    (Kafka, RabbitMQ, SQS, Redis streams...) or a call LoggerAI doesn't see. target names where it
    goes (a queue or topic), recorded as a step of the current request.

        producer.produce("orders", value, headers=loggerai.trace_headers("orders"))
    """
    client, unit = get_client(), context.current.get()
    if client is None or not client.enabled or unit is None:
        return {}
    sid = context.span_id()
    if target:
        unit.dep(str(target)[:120], "PUBLISH", 0.0, True, span=sid, kind="producer", name=f"publish {str(target)[:100]}")
    return context.outgoing_headers(unit, sid, client.service)


def _lower(headers: Any) -> Dict[str, Any]:
    if not headers:
        return {}
    items = headers.items() if hasattr(headers, "items") else headers
    out: Dict[str, Any] = {}
    try:
        for k, v in items:
            out[(k.decode() if isinstance(k, (bytes, bytearray)) else str(k)).lower()] = v
    except (TypeError, ValueError):
        return {}
    return out


def flush(timeout: float = 5.0) -> bool:
    """Send everything now (e.g. at the end of a script or a serverless call)."""
    client = get_client()
    return client.flush(timeout) if client is not None else True


def health_checks(checks: Dict[str, Callable[[], Any]]) -> None:
    """Add health checks after init: each returns True/False, or (ok, detail)."""
    client = get_client()
    if client is not None:
        client.health_checks.update(checks)
        client.start()
