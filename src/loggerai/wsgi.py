"""WSGI middleware for any WSGI app. Flask users: use loggerai.flask.instrument(app),
which adds route names and exceptions on top of this."""
from __future__ import annotations

from . import context
from .client import get_client


class LoggerAIMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        client = get_client()
        if client is None or not client.enabled or client.ignored(environ.get("PATH_INFO", "")):
            return self.app(environ, start_response)
        unit = context.Unit("http", environ.get("REQUEST_METHOD", "GET"), context.trace_from(environ.get("HTTP_TRACEPARENT")),
                            context.session_from(environ.get("HTTP_X_LOGGERAI_SESSION")))
        token = context.current.set(unit)
        status = [500]

        def _start(code, headers, exc_info=None):
            try:
                status[0] = int(str(code).split(" ", 1)[0])
            except ValueError:
                pass
            return start_response(code, headers, exc_info) if exc_info is not None else start_response(code, headers)

        try:
            body = self.app(environ, _start)
            # Most apps return a list; for streamed bodies the time stops here, before the stream is read.
            return body
        except Exception as e:
            client.exception(e, status=500)
            status[0] = 500
            raise
        finally:
            context.current.reset(token)
            client.finish(unit, status[0])
