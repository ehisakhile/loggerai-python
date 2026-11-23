"""Opt-in local circuit protection. No network dependency, implicit retries or fallbacks."""
from __future__ import annotations
import math
import threading
import time
from collections import deque
from typing import Callable


class CircuitOpenError(RuntimeError):
    pass


class CircuitCapacityError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, target: str, *, clock: Callable = time.monotonic, **options):
        if not isinstance(target, str) or not target.strip() or len(target) > 200:
            raise ValueError("target must contain 1–200 characters")
        self.target, self._clock = target, clock
        self._lock = threading.Lock()
        self._state, self._generation, self._opened = "closed", 0, 0.0
        self._probe, self._inflight = False, 0
        self._samples = deque(maxlen=10000)
        self._calls = self._failures = self._blocked = self._capacity_blocked = 0
        self._options = {}
        self.configure(**options)

    def configure(self, **options):
        with self._lock:
            next_options = dict(minimum_requests=10, failure_ratio=.5, window_seconds=30, cooldown_seconds=30, max_concurrent=100)
            next_options.update(self._options)
            unknown = set(options) - set(next_options)
            if unknown:
                raise ValueError("Unknown circuit options: " + ", ".join(sorted(unknown)))
            next_options.update(options)
            for key, low, high, integer in (("minimum_requests", 1, 10000, True), ("failure_ratio", .01, 1, False),
                                          ("window_seconds", 1, 3600, False), ("cooldown_seconds", 1, 3600, False),
                                          ("max_concurrent", 1, 10000, True)):
                value = next_options[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
                    raise ValueError("Invalid " + key)
            self._options = next_options

    def _enter(self):
        with self._lock:
            now = self._clock()
            if self._state == "open" and now - self._opened >= self._options["cooldown_seconds"]:
                self._state = "half_open"
                self._generation += 1
            if self._state == "open" or (self._state == "half_open" and self._probe):
                self._blocked += 1
                raise CircuitOpenError("Circuit open for " + self.target)
            if self._inflight >= self._options["max_concurrent"]:
                self._capacity_blocked += 1
                raise CircuitCapacityError("Concurrency limit reached for " + self.target)
            if self._state == "half_open":
                self._probe = True
            self._inflight += 1
            self._calls += 1
            return self._generation

    def _finish(self, generation, ok):
        with self._lock:
            self._inflight -= 1
            if ok is False:
                self._failures += 1
            if generation != self._generation:
                return
            now = self._clock()
            if self._state == "half_open":
                self._probe = False
                self._generation += 1
                self._samples.clear()
                self._state = "closed" if ok else "open"
                self._opened = now
                return
            if ok is None:  # cancellation releases capacity without blaming the upstream
                return
            while self._samples and self._samples[0][0] <= now - self._options["window_seconds"]:
                self._samples.popleft()
            self._samples.append((now, ok))
            if len(self._samples) >= self._options["minimum_requests"] and sum(not s[1] for s in self._samples) / len(self._samples) >= self._options["failure_ratio"]:
                self._state, self._opened = "open", now
                self._generation += 1

    def call(self, fn: Callable, is_success: Callable = lambda result: True):
        generation, ok = self._enter(), None
        try:
            try:
                result = fn()
                ok = bool(is_success(result))
                return result
            except Exception:
                ok = False
                raise
        finally:
            self._finish(generation, ok)

    async def call_async(self, fn: Callable, is_success: Callable = lambda result: True):
        generation, ok = self._enter(), None
        try:
            try:
                result = await fn()
                ok = bool(is_success(result))
                return result
            except Exception:
                ok = False
                raise
        finally:
            self._finish(generation, ok)

    def snapshot(self):
        with self._lock:
            return dict(target=self.target, state=self._state, calls=self._calls, failures=self._failures,
                        blocked=self._blocked, capacity_blocked=self._capacity_blocked, inflight=self._inflight)
