"""ASGI middleware: FastAPI, Starlette, and any ASGI app.

    from loggerai.asgi import LoggerAIMiddleware
    app.add_middleware(LoggerAIMiddleware)
"""
from __future__ import annotations

from . import context
from .client import get_client


def _route(scope) -> str | None:
    route = scope.get("route")
    if route is not None:
        path = getattr(route, "path_format", None) or getattr(route, "path", None)
        if path:
            # Routes inside a mounted sub-app are relative to the mount.
            mount = scope.get("app_root_path") or ""
            return (mount.rstrip("/") + path) if mount and not path.startswith(mount) else path
    endpoint = scope.get("endpoint")
    app = scope.get("app")
    if endpoint is not None and app is not None:  # older Starlette: find the route by its endpoint
        for r in getattr(app, "routes", []) or []:
            if getattr(r, "endpoint", None) is endpoint:
                return getattr(r, "path_format", None) or getattr(r, "path", None)
    return None


class LoggerAIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        client = get_client()
        if scope["type"] != "http" or client is None or not client.enabled or client.ignored(scope.get("path", "")):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        unit = context.Unit("http", scope.get("method", "GET"), context.trace_from(headers.get("traceparent")),
                            context.session_from(headers.get("x-loggerai-session")))
        token = context.current.set(unit)
        status = 500

        async def _send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except BaseException as e:
            unit.route = unit.route or _route(scope)
            if isinstance(e, Exception):
                client.exception(e, status=500)
            status = 500
            raise
        finally:
            unit.route = unit.route or _route(scope)
            context.current.reset(token)
            client.finish(unit, status)
