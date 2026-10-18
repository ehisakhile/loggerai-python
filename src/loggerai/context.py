"""The unit of work in progress (an HTTP request, a job, a command): what it
waited on, grouped by target and operation, until it finishes.

Kept in a ContextVar, so it follows the request into async code and into the
thread pools frameworks run sync code in."""
from __future__ import annotations

import os
import re
import time
from contextvars import ContextVar
from typing import Dict, Optional

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


class Unit:
    __slots__ = ("kind", "method", "route", "trace_id", "session_id", "user", "ts", "started", "deps")

    def __init__(self, kind: str, method: str, trace_id: Optional[str] = None, session_id: Optional[str] = None):
        self.kind = kind
        self.method = method
        self.route: Optional[str] = None
        self.trace_id = trace_id or os.urandom(16).hex()
        self.session_id = session_id
        self.user: Optional[str] = None
        self.ts = time.time()
        self.started = time.perf_counter()
        self.deps: Dict[tuple, list] = {}

    def dep(self, target: str, op: str, ms: float, ok: bool = True) -> None:
        d = self.deps.get((target, op))
        if d is None:
            d = self.deps[(target, op)] = [0, 0.0, 0]
        d[0] += 1
        d[1] += ms
        if not ok:
            d[2] += 1

    def record(self, status: int) -> dict:
        return {
            "ts": round(self.ts, 3),
            "kind": self.kind,
            "method": self.method,
            "route": self.route or "(unmatched)",
            "status": int(status),
            "duration_ms": round((time.perf_counter() - self.started) * 1000, 2),
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user": self.user,
            "deps": [{"target": t, "op": o, "count": c, "ms": round(ms, 2), "errors": e} for (t, o), (c, ms, e) in self.deps.items()],
        }


current: ContextVar[Optional[Unit]] = ContextVar("loggerai_unit", default=None)


def trace_from(traceparent: Optional[str]) -> Optional[str]:
    """The trace id from a W3C traceparent header, if it's well formed."""
    m = _TRACEPARENT.match((traceparent or "").strip().lower())
    return m.group(1) if m and m.group(1) != "0" * 32 else None


def session_from(value: Optional[str]) -> Optional[str]:
    v = (value or "").strip()
    return v[:64] if v and re.fullmatch(r"[A-Za-z0-9._:-]+", v) else None


def dependency(target: str, op: str, ms: float, ok: bool = True) -> None:
    """Record a call made during the current unit (ignored outside one)."""
    unit = current.get()
    if unit is not None:
        unit.dep(target, op[:20], ms, ok)


def sql_verb(sql: str) -> str:
    for word in (sql or "").lstrip(" (\n\t").split(None, 1)[:1]:
        return word.upper()[:20]
    return "?"
