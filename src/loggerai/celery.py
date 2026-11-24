"""Celery: every task timed like a request (kind "job"), failures reported.

    import loggerai, loggerai.celery
    loggerai.init()
    loggerai.celery.instrument()

Call instrument() where tasks are sent and where they run (usually the same code). A task then
joins the trace of the request that sent it, and the service map shows which service queued it:
the trace and the sender's name travel in the task message's headers.
"""
from __future__ import annotations

from . import context
from .client import get_client

_tokens = {}


def _header(request, name):
    """A custom message header: Celery puts them on the task's request context."""
    v = getattr(request, name, None)
    if v is None:
        headers = getattr(request, "headers", None)
        if isinstance(headers, dict):
            v = headers.get(name)
    return v


def instrument() -> None:
    from celery import signals

    @signals.before_task_publish.connect(weak=False)
    def _publish(sender=None, headers=None, routing_key=None, **kw):
        client, unit = get_client(), context.current.get()
        if client is None or not client.enabled or unit is None or not isinstance(headers, dict):
            return
        sid = context.span_id()
        target = str(routing_key or sender or "celery")[:120]
        unit.dep(target, "PUBLISH", 0.0, True, span=sid, kind="producer", name=f"publish {sender or target}"[:255])
        for k, v in context.outgoing_headers(unit, sid, client.service).items():
            headers.setdefault(k, v)

    @signals.task_prerun.connect(weak=False)
    def _start(task_id=None, task=None, **kw):
        client = get_client()
        if client is None or not client.enabled:
            return
        req = getattr(task, "request", None)
        parent = context.parent_from(_header(req, "traceparent")) if req is not None else None
        caller = _header(req, "x-loggerai-caller") if req is not None else None
        unit = context.Unit("job", "JOB", None, context.session_from(_header(req, "x-loggerai-session")) if req is not None else None, parent, caller)
        if unit.caller:
            unit.transport = "queue"
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
