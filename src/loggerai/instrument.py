"""Times what a request waits on, for whichever libraries are installed:
SQLAlchemy (any database), httpx and requests (outgoing HTTP), redis-py. And
a logging handler for warnings and above. Each is patched once and costs
nothing outside a request."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from . import context
from .client import get_client

_done = set()
_DIALECTS = {"postgresql": "postgres", "mssql": "sqlserver"}


def _once(name: str) -> bool:
    if name in _done:
        return False
    _done.add(name)
    return True


def sqlalchemy() -> None:
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError:
        return
    if not _once("sqlalchemy"):
        return

    @event.listens_for(Engine, "before_cursor_execute")
    def _before(conn, cursor, statement, params, ctx, many):
        conn.info.setdefault("loggerai_t", []).append(time.perf_counter())

    @event.listens_for(Engine, "after_cursor_execute")
    def _after(conn, cursor, statement, params, ctx, many):
        stack = conn.info.get("loggerai_t")
        if stack:
            ms = (time.perf_counter() - stack.pop()) * 1000
            name = conn.engine.dialect.name
            context.dependency(_DIALECTS.get(name, name), context.sql_verb(statement), ms)

    @event.listens_for(Engine, "handle_error")
    def _error(exc_ctx):
        conn = exc_ctx.connection
        stack = conn.info.get("loggerai_t") if conn is not None else None
        if stack:
            ms = (time.perf_counter() - stack.pop()) * 1000
            name = conn.engine.dialect.name
            context.dependency(_DIALECTS.get(name, name), context.sql_verb(exc_ctx.statement or ""), ms, ok=False)


def _host(url) -> str:
    try:
        return urlsplit(str(url)).hostname or "unknown"
    except ValueError:
        return "unknown"


def httpx() -> None:
    try:
        import httpx as _httpx
    except ImportError:
        return
    if not _once("httpx"):
        return
    send, asend = _httpx.Client.send, _httpx.AsyncClient.send

    def _send(self, request, *a, **kw):
        if context.current.get() is None:
            return send(self, request, *a, **kw)
        t = time.perf_counter()
        ok = False
        try:
            r = send(self, request, *a, **kw)
            ok = r.status_code < 500
            return r
        finally:
            context.dependency(_host(request.url), request.method, (time.perf_counter() - t) * 1000, ok)

    async def _asend(self, request, *a, **kw):
        if context.current.get() is None:
            return await asend(self, request, *a, **kw)
        t = time.perf_counter()
        ok = False
        try:
            r = await asend(self, request, *a, **kw)
            ok = r.status_code < 500
            return r
        finally:
            context.dependency(_host(request.url), request.method, (time.perf_counter() - t) * 1000, ok)

    _httpx.Client.send, _httpx.AsyncClient.send = _send, _asend


def requests() -> None:
    try:
        import requests as _requests
    except ImportError:
        return
    if not _once("requests"):
        return
    send = _requests.Session.send

    def _send(self, request, **kw):
        if context.current.get() is None:
            return send(self, request, **kw)
        t = time.perf_counter()
        ok = False
        try:
            r = send(self, request, **kw)
            ok = r.status_code < 500
            return r
        finally:
            context.dependency(_host(request.url), request.method or "GET", (time.perf_counter() - t) * 1000, ok)

    _requests.Session.send = _send


def redis() -> None:
    try:
        import redis as _redis
    except ImportError:
        return
    if not _once("redis"):
        return

    def wrap(cls, name, op=None):
        orig = getattr(cls, name, None)
        if orig is None:
            return

        def sync(self, *args, **kw):
            if context.current.get() is None:
                return orig(self, *args, **kw)
            t = time.perf_counter()
            ok = False
            try:
                r = orig(self, *args, **kw)
                ok = True
                return r
            finally:
                context.dependency("redis", op or str(args[0] if args else "?").upper(), (time.perf_counter() - t) * 1000, ok)

        async def asyn(self, *args, **kw):
            if context.current.get() is None:
                return await orig(self, *args, **kw)
            t = time.perf_counter()
            ok = False
            try:
                r = await orig(self, *args, **kw)
                ok = True
                return r
            finally:
                context.dependency("redis", op or str(args[0] if args else "?").upper(), (time.perf_counter() - t) * 1000, ok)

        import inspect
        setattr(cls, name, asyn if inspect.iscoroutinefunction(orig) else sync)

    wrap(_redis.Redis, "execute_command")
    wrap(_redis.client.Pipeline, "execute", "PIPELINE")
    try:
        import redis.asyncio as _ar
        wrap(_ar.Redis, "execute_command")
        wrap(_ar.client.Pipeline, "execute", "PIPELINE")
    except ImportError:
        pass


_LEVELS = {"WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL", "INFO": "INFO", "DEBUG": "DEBUG"}


class LoggerAIHandler(logging.Handler):
    """Sends log records at or above its level. A record carrying an exception
    is reported as an error with its stack trace instead (once)."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "loggerai" or record.name.startswith("loggerai."):
            return
        client = get_client()
        if client is None or not client.enabled:
            return
        try:
            if record.exc_info and record.exc_info[1] is not None:
                client.exception(record.exc_info[1], status=None, handled=True, extra={"logger": record.name, "log": record.getMessage()[:500]})
                return
            client.log(_LEVELS.get(record.levelname, "WARNING"), record.name, record.getMessage())
        except Exception:
            pass


def logging_handler(level: str) -> None:
    if not level or not _once("logging"):
        return
    handler = LoggerAIHandler(level=getattr(logging, str(level).upper(), logging.WARNING))
    logging.getLogger().addHandler(handler)
    # uvicorn and gunicorn log errors (with the exception) to their own loggers, which may not reach the root.
    for name in ("uvicorn.error", "gunicorn.error"):
        lg = logging.getLogger(name)
        if not lg.propagate:
            lg.addHandler(handler)


def all_from(client) -> None:
    if client.capture_queries:
        sqlalchemy()
    if client.capture_http:
        httpx()
        requests()
    if client.capture_redis:
        redis()
    logging_handler(client.logs_level)
