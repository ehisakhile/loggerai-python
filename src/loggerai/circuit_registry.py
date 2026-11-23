"""Background policy sync; outbound calls use only local breaker state."""
from __future__ import annotations
import json
import threading
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import uuid4
from .circuit import CircuitBreaker


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CircuitRegistry:
    def __init__(self, key, *, service="app", environment="staging", endpoint="https://loggerapp.ehigiator.com/api/v1", instance_id=None):
        if not key or not key.startswith("sk_"):
            raise ValueError("CircuitRegistry requires a project secret key")
        url = urlparse(endpoint)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("Use HTTPS for the LoggerAI endpoint")
        self._key, self.service, self.environment = key, service, environment
        self.instance_id = instance_id or str(uuid4())
        self.endpoint = endpoint.rstrip("/") + "/obs/reliability/runtime"
        self._breakers, self._policies = {}, {}
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def breaker(self, target, **options):
        with self._lock:
            if target not in self._breakers:
                if len(self._breakers) >= 200:
                    raise ValueError("At most 200 circuit breakers per registry")
                self._breakers[target] = CircuitBreaker(target, **options)
            return self._breakers[target]

    def _request(self, path, body=None):
        request = Request(self.endpoint + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"})
        with build_opener(_NoRedirect).open(request, timeout=5) as response:
            return json.loads(response.read(1024 * 1024))

    def sync(self):
        with self._sync_lock:
            policies = self._request("/config?" + urlencode(dict(service=self.service, environment=self.environment)))
            if not isinstance(policies, list) or len(policies) > 200:
                raise ValueError("Invalid circuit policy response")
            with self._lock:
                for policy in policies:
                    breaker = self._breakers.get(policy["target"])
                    if breaker is None:
                        continue
                    breaker.configure(**{k: policy[k] for k in ("minimum_requests", "failure_ratio", "window_seconds", "cooldown_seconds", "max_concurrent")})
                    self._policies[policy["target"]] = policy
                circuits = [dict(self._breakers[target].snapshot(), policy_id=p["id"], revision=p["revision"]) for target, p in self._policies.items()]
            self._request("/observations", dict(instance_id=self.instance_id, service=self.service, environment=self.environment, circuits=circuits))
            return len(circuits)

    def start(self, on_error=None):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.sync()
                except Exception as error:
                    if on_error:
                        try:
                            on_error(error)
                        except Exception:
                            pass
                self._stop.wait(30)
        self._thread = threading.Thread(target=run, name="loggerai-circuits", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=11)
