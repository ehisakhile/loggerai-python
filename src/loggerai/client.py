"""
The client: settings, the buffer, and a background thread that sends batches
to LoggerAI every few seconds. Nothing here ever raises into the app or makes
a request wait; when LoggerAI can't be reached, records are dropped rather
than piling up.
"""
from __future__ import annotations

import atexit
import gzip
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional

from . import context
from ._version import __version__
from .frames import frames, type_name
from .redact import redact, redact_string

log = logging.getLogger("loggerai")

DEFAULT_ENDPOINT = "https://loggerapp.ehigiator.com/api/v1/ingest/batch"
KINDS = ("requests", "errors", "logs", "checks")
MAX_BUFFER = 10_000          # per kind; beyond this the oldest go
MAX_PER_BATCH = 1_000        # the server's limit per kind
MAX_BATCH_BYTES = 900_000    # under the server's 1 MB


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _truthy(v) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


class Client:
    def __init__(
        self,
        key: Optional[str] = None,
        *,
        endpoint: Optional[str] = None,
        service: Optional[str] = None,
        environment: Optional[str] = None,
        release: Optional[str] = None,
        enabled: Optional[bool] = None,
        flush_interval: float = 5.0,
        timeout: float = 2.0,
        logs_level: Optional[str] = "WARNING",
        capture_queries: bool = True,
        capture_http: bool = True,
        capture_redis: bool = True,
        ignore_paths: tuple = ("/health", "/healthz", "/up", "/favicon.ico", "/metrics"),
        health_checks: Optional[Dict[str, Callable]] = None,
        health_interval: float = 60.0,
        root: Optional[str] = None,
    ):
        self.key = key or _env("LOGGERAI_KEY")
        self.endpoint = endpoint or _env("LOGGERAI_ENDPOINT", DEFAULT_ENDPOINT)
        self.service = service or _env("LOGGERAI_SERVICE", "app")
        self.environment = environment or _env("LOGGERAI_ENVIRONMENT") or _env("ENVIRONMENT") or _env("APP_ENV") or "production"
        self.release = release or _env("LOGGERAI_RELEASE")
        on = _truthy(_env("LOGGERAI_ENABLED", "true")) if enabled is None else enabled
        self.enabled = bool(on and self.key and self.key.startswith("sk_"))
        if on and self.key and not self.key.startswith("sk_"):
            log.warning("LoggerAI: servers need the secret key (sk_live_...); the public key is for browsers. Not sending.")
        self.flush_interval = max(1.0, float(flush_interval))
        self.timeout = timeout
        self.logs_level = logs_level
        self.capture_queries, self.capture_http, self.capture_redis = capture_queries, capture_http, capture_redis
        self.ignore_paths = tuple(ignore_paths or ())
        self.health_checks = dict(health_checks or {})
        self.health_interval = health_interval
        self.root = os.path.abspath(root or os.getcwd())
        self.host = socket.gethostname()

        self._lock = threading.Lock()
        self._buf: Dict[str, List[dict]] = {k: [] for k in KINDS}
        self._wake = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: Optional[threading.Thread] = None
        self._pid = os.getpid()
        self._stopped = False
        self._next_health = 0.0
        self.last_error: Optional[str] = None

    # ---- recording ------------------------------------------------------------------

    def ignored(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.ignore_paths)

    def add(self, kind: str, record: dict) -> None:
        if not self.enabled:
            return
        with self._lock:
            buf = self._buf[kind]
            buf.append(record)
            if len(buf) > MAX_BUFFER:
                del buf[: len(buf) - MAX_BUFFER]
        self._ensure_thread()

    def finish(self, unit: context.Unit, status: int) -> None:
        self.add("requests", unit.record(status))

    def exception(self, exc: BaseException, status: Optional[int] = None, handled: bool = False, extra: Optional[dict] = None) -> None:
        if not self.enabled or getattr(exc, "__loggerai_seen__", False):
            return
        try:
            exc.__loggerai_seen__ = True  # reported once, however many layers see it
        except Exception:
            pass
        unit = context.current.get()
        self.add("errors", {
            "ts": round(time.time(), 3),
            "type": type_name(exc),
            "message": redact_string(str(exc)[:1000]),
            "handled": handled,
            "route": unit.route if unit else None,
            "method": unit.method if unit else None,
            "status": status,
            "trace_id": unit.trace_id if unit else None,
            "frames": frames(exc, self.root),
            "context": redact(extra) if extra else {},
        })

    def log(self, level: str, logger: str, message: str) -> None:
        unit = context.current.get()
        self.add("logs", {
            "ts": round(time.time(), 3), "level": level, "logger": logger[:120], "message": redact_string(message[:8000]),
            "route": unit.route if unit else None, "trace_id": unit.trace_id if unit else None,
        })

    def user_ref(self, user_id) -> Optional[str]:
        """An opaque, stable id for a user: never the id itself, never an email."""
        if user_id in (None, ""):
            return None
        return hmac.new((self.key or "").encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:16]

    # ---- sending ---------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._pid != os.getpid():  # forked (gunicorn, uWSGI, Celery): the parent's thread didn't come along
            self._pid = os.getpid()
            self._thread = None
            with self._lock:
                self._buf = {k: [] for k in KINDS}
        if self._thread is None and not self._stopped:
            self._thread = threading.Thread(target=self._run, name="loggerai", daemon=True)
            self._thread.start()

    def start(self) -> None:
        """Starts the sender now (for health checks on a quiet app)."""
        if self.enabled:
            self._ensure_thread()

    def _run(self) -> None:
        while not self._stopped:
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            self._idle.clear()
            try:
                self._run_health()
                self._send_all()
            except Exception as e:  # never let the thread die
                log.debug("LoggerAI: send failed: %s", e)
            finally:
                self._idle.set()

    def _run_health(self) -> None:
        if not self.health_checks or time.monotonic() < self._next_health:
            return
        self._next_health = time.monotonic() + self.health_interval
        for name, fn in self.health_checks.items():
            t = time.perf_counter()
            try:
                r = fn()
                ok, detail = (r if isinstance(r, tuple) else (r is not False, None))
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"[:255]
            self.add("checks", {"ts": round(time.time(), 3), "name": name, "ok": bool(ok),
                                "ms": round((time.perf_counter() - t) * 1000, 2), "detail": detail})

    def _drain(self) -> Dict[str, List[dict]]:
        with self._lock:
            out = {k: v[:MAX_PER_BATCH] for k, v in self._buf.items() if v}
            for k in out:
                del self._buf[k][: len(out[k])]
        return out

    def _send_all(self) -> None:
        for _ in range(20):  # a burst goes out in several batches, then waits for the next tick
            parts = self._drain()
            if not parts or not self.enabled:
                return
            self._post(parts)

    def _post(self, parts: Dict[str, List[dict]]) -> None:
        body = json.dumps({
            "sdk": {"name": "loggerai-python", "version": __version__},
            "service": self.service, "environment": self.environment, "release": self.release, "host": self.host,
            **parts,
        }, separators=(",", ":"), default=str).encode()
        if len(body) > MAX_BATCH_BYTES and sum(len(v) for v in parts.values()) > 1:
            for half in _halves(parts):
                self._post(half)
            return
        req = urllib.request.Request(self.endpoint, data=gzip.compress(body), method="POST", headers={
            "Content-Type": "application/json", "Content-Encoding": "gzip", "X-LoggerAI-Key": self.key or "",
            "User-Agent": f"loggerai-python/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
            self.last_error = None
        except urllib.error.HTTPError as e:
            self.last_error = f"LoggerAI answered {e.code}"
            if e.code == 401:
                self.enabled = False  # a wrong or revoked key won't start working by retrying
                log.warning("LoggerAI: the key was refused (401). Check LOGGERAI_KEY; sending stopped.")
            elif e.code not in (429,) and e.code < 500:
                log.warning("LoggerAI: batch rejected (%s)", e.code)
        except Exception as e:
            self.last_error = f"couldn't reach LoggerAI: {type(e).__name__}"
            log.debug("LoggerAI: %s", self.last_error)

    def flush(self, timeout: float = 5.0) -> bool:
        """Sends everything recorded so far. True when the buffer is empty."""
        if not self.enabled:
            return True
        if self._thread is None or self._pid != os.getpid():
            self._send_all()  # no sender thread here (e.g. a short script): send in this thread
            return not any(self._buf.values())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._idle.wait(max(0.0, deadline - time.monotonic()))
            if not any(self._buf.values()):
                return True
            self._wake.set()
            time.sleep(0.05)
            self._idle.wait(max(0.0, deadline - time.monotonic()))
        return not any(self._buf.values())

    def close(self, timeout: float = 2.0) -> None:
        self.flush(timeout)
        self._stopped = True
        self._wake.set()


def _halves(parts: Dict[str, List[dict]]):
    a, b = {}, {}
    for k, v in parts.items():
        mid = len(v) // 2
        if v[:mid]:
            a[k] = v[:mid]
        if v[mid:]:
            b[k] = v[mid:]
    return [x for x in (a, b) if x]


_client: Optional[Client] = None


def get_client() -> Optional[Client]:
    return _client


def set_client(c: Optional[Client]) -> None:
    global _client
    _client = c


@atexit.register
def _at_exit() -> None:
    if _client is not None:
        _client.close(timeout=2.0)
