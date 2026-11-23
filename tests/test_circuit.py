import asyncio
import unittest
from loggerai.circuit import CircuitBreaker, CircuitOpenError, CircuitCapacityError


class CircuitTests(unittest.TestCase):
    def test_open_probe_recover(self):
        now = [0.0]
        b = CircuitBreaker("payments", minimum_requests=2, cooldown_seconds=1, clock=lambda: now[0])
        def fail(): raise ValueError("offline")
        for _ in range(2):
            with self.assertRaises(ValueError): b.call(fail)
        with self.assertRaises(CircuitOpenError): b.call(fail)
        now[0] = 1.1
        self.assertEqual(b.call(lambda: "ok"), "ok")
        self.assertEqual(b.snapshot()["state"], "closed")
        self.assertEqual(b.snapshot()["blocked"], 1)

    def test_concurrent_probe_and_stale_completion(self):
        async def run():
            now = [0.0]
            b = CircuitBreaker("api", minimum_requests=1, cooldown_seconds=1, clock=lambda: now[0])
            event = asyncio.Event()
            async def pending():
                await event.wait()
                return True
            old = asyncio.create_task(b.call_async(pending))
            await asyncio.sleep(0)
            with self.assertRaises(ValueError): b.call(lambda: (_ for _ in ()).throw(ValueError("offline")))
            event.set(); await old
            self.assertEqual(b.snapshot()["state"], "open")
            now[0] = 2
            event.clear()
            probe = asyncio.create_task(b.call_async(pending)); await asyncio.sleep(0)
            with self.assertRaises(CircuitOpenError): await b.call_async(pending)
            event.set(); await probe
            self.assertEqual(b.snapshot()["state"], "closed")
        asyncio.run(run())

    def test_capacity_and_result_classification(self):
        b = CircuitBreaker("api", minimum_requests=1, max_concurrent=1)
        def nested():
            with self.assertRaises(CircuitCapacityError): b.call(lambda: True)
            return 200
        self.assertEqual(b.call(nested), 200)
        self.assertEqual(b.snapshot()["failures"], 0)
        b.call(lambda: 503, is_success=lambda s: s < 500)
        self.assertEqual(b.snapshot()["state"], "open")

    def test_cancellation_releases_capacity(self):
        async def run():
            b = CircuitBreaker("api", max_concurrent=1)
            async def wait(): await asyncio.Event().wait()
            task = asyncio.create_task(b.call_async(wait)); await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(b.call(lambda: 42), 42)
        asyncio.run(run())

    def test_invalid_policy(self):
        with self.assertRaises(ValueError): CircuitBreaker("api", failure_ratio=0)


if __name__ == "__main__": unittest.main()
