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

__all__ = ["init", "capture_exception", "set_user", "set_route", "measure", "track", "flush", "health_checks", "Client", "__version__"]


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
def track(kind: str, name: str) -> Iterator[context.Unit]:
    """Time a unit of work outside a web request: a job, a scheduled task, a script.

        with loggerai.track("job", "send_invoices"):
            ...
    """
    client = get_client()
    unit = context.Unit(kind if kind in ("job", "command", "http") else "job", "JOB" if kind != "command" else "CLI")
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
