"""Microservices: a trace crosses services (HTTP and queues), with the caller's name."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

import loggerai
from loggerai import context

INCOMING = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture
def orders():
    """Another service: keeps the headers of every call it gets."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(502 if "fail" in self.path else 200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", seen=seen)
    server.shutdown()


def test_asgi_continues_the_trace_and_carries_it_on(client, ingest, orders):
    httpx = pytest.importorskip("httpx")
    requests = pytest.importorskip("requests")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from loggerai.asgi import LoggerAIMiddleware

    app = FastAPI()
    app.add_middleware(LoggerAIMiddleware)

    @app.get("/checkout")
    def checkout():
        with httpx.Client() as h:
            h.get(orders.url + "/orders", headers={"accept": "application/json"})
        requests.get(orders.url + "/fail")
        return {"ok": True}

    with TestClient(app) as tc:
        assert tc.get("/checkout", headers={"traceparent": INCOMING, "x-loggerai-caller": "web-bff"}).status_code == 200
    client.flush(3)
    r = next(x for x in ingest.all("requests") if x["route"] == "/checkout")
    assert r["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert r["parent_span_id"] == "00f067aa0ba902b7"
    assert r["caller"] == "web-bff"
    steps = [s for s in r["spans"] if s["kind"] == "client"]
    assert len(orders.seen) == 2 and len(steps) == 2
    for h in orders.seen:
        trace, span = context.parent_from(h["traceparent"])
        assert trace == r["trace_id"]
        assert span in {s["span_id"] for s in steps}
        assert h["x-loggerai-caller"] == "api"
    assert orders.seen[0]["accept"] == "application/json"
    assert any(s["status"] == 502 and s["ok"] is False for s in steps)


def test_outside_apis_get_nothing(client, ingest, orders):
    requests = pytest.importorskip("requests")
    client.propagates = context.propagation_rule(False)
    with loggerai.track("job", "charge"):
        requests.get(orders.url + "/v1/charges")
    assert "traceparent" not in orders.seen[0]
    assert "x-loggerai-caller" not in orders.seen[0]


def test_queues_by_hand(client, ingest):
    with loggerai.track("job", "checkout.publish"):
        headers = loggerai.trace_headers("orders")
    assert headers["x-loggerai-caller"] == "api"
    kafka = [(k, v.encode()) for k, v in headers.items()]  # Kafka hands headers over as (str, bytes)
    with loggerai.track("job", "order.created", headers=kafka):
        pass
    client.flush(3)
    reqs = ingest.all("requests")
    pub = next(x for x in reqs if x["route"] == "checkout.publish")
    job = next(x for x in reqs if x["route"] == "order.created")
    assert job["trace_id"] == pub["trace_id"]
    assert job["parent_span_id"] == pub["spans"][0]["span_id"]
    assert pub["spans"][0]["kind"] == "producer"
    assert (job["caller"], job["transport"]) == ("api", "queue")


def test_celery_messages_carry_the_trace(client, ingest):
    pytest.importorskip("celery")
    from celery import signals
    import loggerai.celery
    loggerai.celery.instrument()
    message_headers = {"task": "send_receipt", "id": "t-1"}
    with loggerai.track("http", "POST /orders"):
        signals.before_task_publish.send(sender="send_receipt", headers=message_headers, routing_key="emails")
    # The worker: Celery puts custom headers on the task's request.
    class Task:  # a signal's sender must be hashable
        name = "send_receipt"
        request = SimpleNamespace(**message_headers)
    task = Task()
    signals.task_prerun.send(sender=task, task_id="t-1", task=task)
    signals.task_postrun.send(sender=task, task_id="t-1", task=task, state="SUCCESS")
    client.flush(3)
    reqs = ingest.all("requests")
    sent = next(x for x in reqs if x["route"] == "POST /orders")
    job = next(x for x in reqs if x["route"] == "send_receipt")
    assert job["trace_id"] == sent["trace_id"]
    assert job["parent_span_id"] == sent["spans"][0]["span_id"]
    assert (job["caller"], job["transport"]) == ("api", "queue")


def test_which_hosts_get_the_trace():
    rule = context.propagation_rule()
    for h in ("orders", "payments.default.svc.cluster.local", "billing.internal", "10.1.2.3", "172.20.0.5", "localhost"):
        assert rule(h), h
    for h in ("api.stripe.com", "8.8.8.8", ""):
        assert not rule(h), h
    listed = context.propagation_rule(["*.acme.io"])
    assert listed("pay.acme.io") and not listed("acme.io.evil.com")
    assert context.propagation_rule(None, "all")("api.stripe.com")
    assert not context.propagation_rule(None, "none")("orders")
    assert context.caller_from("<img src=x>") is None
    assert context.parent_from("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None
