"""Django: call loggerai.init() in settings.py (it reads LOGGERAI_KEY), and add
the middleware first in MIDDLEWARE:

    MIDDLEWARE = ["loggerai.django.LoggerAIMiddleware", ...]

Queries are timed through Django's own database layer."""
from __future__ import annotations

import re
import time

from . import context
from .client import get_client

_CONVERTER = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")
_NAMED = re.compile(r"\(\?P<([^>]+)>[^)]*\)")
_VENDORS = {"postgresql": "postgres", "microsoft": "sqlserver"}


def route_template(resolver_match) -> str | None:
    route = getattr(resolver_match, "route", None)
    if route is None:
        return None
    if route.startswith("^") or route.endswith("$"):  # re_path
        route = _NAMED.sub(r"{\1}", route.lstrip("^").rstrip("$"))
    return "/" + _CONVERTER.sub(r"{\1}", route).lstrip("/")


def _db_wrapper(execute, sql, params, many, ctx):
    if context.current.get() is None:
        return execute(sql, params, many, ctx)
    t = time.perf_counter()
    ok = False
    try:
        r = execute(sql, params, many, ctx)
        ok = True
        return r
    finally:
        vendor = ctx["connection"].vendor
        context.dependency(_VENDORS.get(vendor, vendor), context.sql_verb(sql), (time.perf_counter() - t) * 1000, ok)


def _install_db_timing():
    from django.db.backends.signals import connection_created

    def _on_connect(sender, connection, **kw):
        if _db_wrapper not in connection.execute_wrappers:
            connection.execute_wrappers.append(_db_wrapper)

    connection_created.connect(_on_connect, weak=False, dispatch_uid="loggerai")
    from django.db import connections
    for conn in connections.all(initialized_only=True) if _has_initialized_only() else []:
        _on_connect(None, conn)


def _has_initialized_only() -> bool:
    import inspect
    from django.db import connections
    return "initialized_only" in inspect.signature(connections.all).parameters


class LoggerAIMiddleware:
    sync_capable = True
    async_capable = False

    _db_done = False

    def __init__(self, get_response):
        self.get_response = get_response
        client = get_client()
        if client is not None and client.capture_queries and not LoggerAIMiddleware._db_done:
            LoggerAIMiddleware._db_done = True
            _install_db_timing()

    def __call__(self, request):
        client = get_client()
        if client is None or not client.enabled or client.ignored(request.path):
            return self.get_response(request)
        unit = context.Unit("http", request.method, context.trace_from(request.META.get("HTTP_TRACEPARENT")),
                            context.session_from(request.META.get("HTTP_X_LOGGERAI_SESSION")))
        token = context.current.set(unit)
        status = 500
        try:
            response = self.get_response(request)
            status = response.status_code
            return response
        finally:
            unit.route = route_template(getattr(request, "resolver_match", None)) or unit.route
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                unit.user = client.user_ref(getattr(user, "pk", None))
            context.current.reset(token)
            client.finish(unit, status)

    def process_exception(self, request, exception):
        client = get_client()
        if client is not None:
            unit = context.current.get()
            if unit is not None and unit.route is None:
                unit.route = route_template(getattr(request, "resolver_match", None))
            client.exception(exception, status=500)
        return None
