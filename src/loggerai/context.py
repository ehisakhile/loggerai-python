"""The unit of work in progress (an HTTP request, a job, a command): what it
waited on, grouped by target and operation, until it finishes.

Kept in a ContextVar, so it follows the request into async code and into the
thread pools frameworks run sync code in.

Traces cross services with W3C traceparent: a unit continues the trace it was
called with (its parent is the caller's span), calls to other services are
spans of their own, and calls to the project's other services carry the trace
on, with this service's name (x-loggerai-caller)."""
from __future__ import annotations

import os
import re
import time
from contextvars import ContextVar
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")
_CALLER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,59}$")
MAX_SPANS = 200  # the server keeps this many steps per request
_DBS = {"postgres", "mysql", "mariadb", "sqlite", "mongodb", "sqlserver", "mssql", "oracle"}
_CACHES = {"redis", "memcached"}


def span_id() -> str:
    return os.urandom(8).hex()


def step_kind(target: str) -> str:
    t = (target or "").lower()
    return "db" if t in _DBS else "cache" if t in _CACHES else "client"


class Unit:
    __slots__ = ("kind", "method", "route", "trace_id", "span_id", "parent_span_id", "caller", "transport", "session_id", "user", "ts",
                 "started", "deps", "spans")

    def __init__(self, kind: str, method: str, trace_id: Optional[str] = None, session_id: Optional[str] = None,
                 parent: Optional[Tuple[str, str]] = None, caller: Optional[str] = None):
        self.kind = kind
        self.method = method
        self.route: Optional[str] = None
        self.trace_id = trace_id or (parent[0] if parent else None) or os.urandom(16).hex()
        self.span_id = span_id()
        self.parent_span_id = parent[1] if parent and parent[0] == self.trace_id else None
        self.caller = caller_from(caller)
        self.transport: Optional[str] = None
        self.session_id = session_id
        self.user: Optional[str] = None
        self.ts = time.time()
        self.started = time.perf_counter()
        self.deps: Dict[tuple, list] = {}
        self.spans: List[dict] = []

    def dep(self, target: str, op: str, ms: float, ok: bool = True, span: Optional[str] = None, kind: Optional[str] = None,
            status: Optional[int] = None, name: Optional[str] = None) -> None:
        d = self.deps.get((target, op))
        if d is None:
            d = self.deps[(target, op)] = [0, 0.0, 0]
        d[0] += 1
        d[1] += ms
        if not ok:
            d[2] += 1
        # Calls to other services and queues one by one (they link the trace across services);
        # database and cache calls stay as totals per target, which the server turns into steps.
        kind = kind or step_kind(target)
        if kind in ("client", "producer") and len(self.spans) < MAX_SPANS:
            end = (time.perf_counter() - self.started) * 1000
            s = {"span_id": span or span_id(), "name": name or f"{op} {target}", "kind": kind, "target": target,
                 "start_ms": round(max(0.0, end - ms), 2), "duration_ms": round(ms, 2), "ok": bool(ok)}
            if status:
                s["status"] = int(status)
            self.spans.append(s)

    def traceparent(self, child: str) -> str:
        return f"00-{self.trace_id}-{child}-01"

    def record(self, status: int) -> dict:
        r = {
            "ts": round(self.ts, 3),
            "kind": self.kind,
            "method": self.method,
            "route": self.route or "(unmatched)",
            "status": int(status),
            "duration_ms": round((time.perf_counter() - self.started) * 1000, 2),
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "session_id": self.session_id,
            "user": self.user,
            "deps": [{"target": t, "op": o, "count": c, "ms": round(ms, 2), "errors": e} for (t, o), (c, ms, e) in self.deps.items()],
        }
        if self.parent_span_id:
            r["parent_span_id"] = self.parent_span_id
        if self.caller:
            r["caller"] = self.caller
        if self.transport:
            r["transport"] = self.transport
        if self.spans:
            r["spans"] = self.spans
        return r


current: ContextVar[Optional[Unit]] = ContextVar("loggerai_unit", default=None)


def _text(v) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("latin-1", "replace")
    if isinstance(v, (list, tuple)):
        return _text(v[0]) if v else ""
    return str(v or "")


def parent_from(traceparent) -> Optional[Tuple[str, str]]:
    """(trace id, parent span id) from a W3C traceparent header, if it's well formed."""
    m = _TRACEPARENT.match(_text(traceparent).strip().lower())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None
    return m.group(1), m.group(2)


def trace_from(traceparent: Optional[str]) -> Optional[str]:
    """The trace id from a W3C traceparent header, if it's well formed."""
    p = parent_from(traceparent)
    return p[0] if p else None


def caller_from(value) -> Optional[str]:
    v = _text(value).strip()[:60]
    return v if v and _CALLER.match(v) else None


def session_from(value: Optional[str]) -> Optional[str]:
    v = _text(value).strip()
    return v[:64] if v and re.fullmatch(r"[A-Za-z0-9._:-]+", v) else None


def dependency(target: str, op: str, ms: float, ok: bool = True, **extra) -> None:
    """Record a call made during the current unit (ignored outside one)."""
    unit = current.get()
    if unit is not None:
        unit.dep(target, op[:20], ms, ok, **extra)


def sql_verb(sql: str) -> str:
    for word in (sql or "").lstrip(" (\n\t").split(None, 1)[:1]:
        return word.upper()[:20]
    return "?"


# ---- where the trace is carried on ---------------------------------------------------------

_PRIVATE_V4 = re.compile(r"^(10\.|127\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)")
_INTERNAL_SUFFIX = re.compile(r"\.(local|internal|svc|cluster\.local|lan|home\.arpa|consul|localdomain)$", re.I)


def is_internal_host(host: Optional[str]) -> bool:
    """The project's own services by default: names without a dot (Docker and Kubernetes service
    names), private addresses and internal DNS suffixes. Outside APIs never get your trace ids or
    service names unless you list them."""
    h = (host or "").lower().strip("[]")
    if not h:
        return False
    if h in ("localhost", "::1") or _PRIVATE_V4.match(h) or re.match(r"^f[cd][0-9a-f]{2}:", h):
        return True
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", h):
        return False
    return "." not in h or bool(_INTERNAL_SUFFIX.search(h))


def propagation_rule(option: Union[None, bool, Iterable[str], str] = None, env_value: Optional[str] = None) -> Callable[[str], bool]:
    """propagate: True (every call), False (none), or hosts and patterns ("api.acme.io", "*.acme.io")
    besides the internal ones. LOGGERAI_PROPAGATE=all|none|host,*.domain does the same."""
    v = option
    if v is None and env_value:
        s = env_value.strip().lower()
        v = True if s in ("all", "true") else False if s in ("none", "false") else env_value.split(",")
    if isinstance(v, bool):
        return lambda host: v
    if isinstance(v, str):
        v = v.split(",")
    patterns = [str(p).strip().lower() for p in (v or ()) if str(p).strip()]

    def rule(host: str) -> bool:
        h = (host or "").lower()
        if is_internal_host(h):
            return True
        return any(h.endswith(p[1:]) if p.startswith("*.") else h == p for p in patterns)
    return rule


def outgoing_headers(unit: Unit, child: str, service: Optional[str]) -> Dict[str, str]:
    h = {"traceparent": unit.traceparent(child)}
    if service:
        h["x-loggerai-caller"] = service
    if unit.session_id:
        h["x-loggerai-session"] = unit.session_id
    return h
