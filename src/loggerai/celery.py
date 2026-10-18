"""Celery: every task timed like a request (kind "job"), failures reported.

    import loggerai, loggerai.celery
    loggerai.init()
    loggerai.celery.instrument()
"""
from __future__ import annotations

from . import context
from .client import get_client

_tokens = {}


def instrument() -> None:
    from celery import signals

    @signals.task_prerun.connect(weak=False)
    def _start(task_id=None, task=None, **kw):
        client = get_client()
        if client is None or not client.enabled:
            return
        unit = context.Unit("job", "JOB")
        unit.route = getattr(task, "name", None) or "task"
        _tokens[task_id] = (unit, context.current.set(unit))

    @signals.task_failure.connect(weak=False)
    def _failed(task_id=None, exception=None, **kw):
        client = get_client()
        if client is not None and exception is not None:
            client.exception(exception, status=500)

    @signals.task_postrun.connect(weak=False)
    def _end(task_id=None, state=None, **kw):
        got = _tokens.pop(task_id, None)
        client = get_client()
        if got is None or client is None:
            return
        unit, token = got
        try:
            context.current.reset(token)
        except ValueError:
            context.current.set(None)
        client.finish(unit, 200 if state == "SUCCESS" else 500)
