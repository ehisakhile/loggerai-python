"""Flask:

    import loggerai, loggerai.flask
    loggerai.init()
    loggerai.flask.instrument(app)
"""
from __future__ import annotations

import re

from . import context
from .client import get_client
from .wsgi import LoggerAIMiddleware

_CONVERTER = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")


def route_template(rule: str) -> str:
    """'/orders/<int:order_id>' -> '/orders/{order_id}'"""
    return _CONVERTER.sub(r"{\1}", rule)


def instrument(app) -> None:
    from flask import got_request_exception, request

    if getattr(app, "_loggerai", False):
        return
    app._loggerai = True
    app.wsgi_app = LoggerAIMiddleware(app.wsgi_app)

    @app.before_request
    def _loggerai_route():
        unit = context.current.get()
        if unit is not None and request.url_rule is not None:
            unit.route = route_template(request.url_rule.rule)

    def _on_exception(sender, exception, **extra):
        client = get_client()
        if client is not None:
            client.exception(exception, status=500)

    got_request_exception.connect(_on_exception, app, weak=False)
