import logging
import os

import pytest

import loggerai
from loggerai import context


def test_fastapi(client, ingest):
    import httpx
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from loggerai.asgi import LoggerAIMiddleware

    engine = create_engine("sqlite://")
    app = FastAPI()
    app.add_middleware(LoggerAIMiddleware)

    @app.get("/orders/{order_id}")
    def order(order_id: int):  # sync: runs in a thread pool
        with engine.connect() as c:
            for _ in range(3):
                c.execute(text("SELECT 1"))
        httpx.get(ingest.url + "/stripe/ok")
        loggerai.set_user(42)
        return {"id": order_id}

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        async with httpx.AsyncClient() as h:
            await h.get(ingest.url + "/fail")
        return {}

    @app.get("/boom")
    def boom():
        raise ZeroDivisionError("token=abc Bearer abcdefghijklmnopqrstuvwx")

    @app.get("/missing")
    def missing():
        raise HTTPException(404)

    @app.get("/health")
    def health():
        return {}

    tc = TestClient(app, raise_server_exceptions=False)
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    tc.get("/orders/1842", headers={"traceparent": f"00-{trace}-00f067aa0ba902b7-01", "x-loggerai-session": "sess-1"})
    tc.get("/items/abc")
    assert tc.get("/boom").status_code == 500
    tc.get("/missing")
    tc.get("/nope")
    tc.get("/health")
    assert loggerai.flush()

    reqs = {(r["method"], r["route"]): r for r in ingest.all("requests")}
    o = reqs[("GET", "/orders/{order_id}")]
    assert o["status"] == 200 and o["trace_id"] == trace and o["session_id"] == "sess-1"
    deps = {(d["target"], d["op"]): d for d in o["deps"]}
    assert deps[("sqlite", "SELECT")]["count"] == 3
    assert deps[("127.0.0.1", "GET")]["count"] == 1 and deps[("127.0.0.1", "GET")]["errors"] == 0
    assert o["user"] and o["user"] != "42"
    i = reqs[("GET", "/items/{item_id}")]
    assert i["deps"][0]["errors"] == 1  # the 503
    assert reqs[("GET", "/boom")]["status"] == 500
    assert reqs[("GET", "/missing")]["status"] == 404
    assert ("GET", "(unmatched)") in reqs
    assert not any(r["route"] == "/health" for r in ingest.all("requests"))

    errs = ingest.all("errors")
    assert len(errs) == 1, errs  # once, though uvicorn/starlette also log it
    e = errs[0]
    assert e["type"] == "ZeroDivisionError" and e["route"] == "/boom" and e["status"] == 500
    assert "abcdefghijklmnop" not in e["message"]
    assert e["frames"][0]["in_app"] and e["frames"][0]["file"] == os.path.join("tests", "test_sdk.py")
    assert e["frames"][0]["function"] == "boom"
    b = ingest.batches[0]
    assert b["sdk"]["name"] == "loggerai-python" and b["service"] == "api" and b["release"] == "v1" and ingest.keys[0] == "sk_live_test"


def test_flask(client, ingest):
    from flask import Flask

    import loggerai.flask

    app = Flask(__name__)
    loggerai.flask.instrument(app)

    @app.route("/users/<int:user_id>/posts/<slug>")
    def post(user_id, slug):
        return "ok"

    @app.route("/crash")
    def crash():
        raise KeyError("missing")

    tc = app.test_client()
    tc.get("/users/7/posts/hello")
    assert tc.get("/crash").status_code == 500
    loggerai.flush()
    routes = {r["route"]: r for r in ingest.all("requests")}
    assert routes["/users/{user_id}/posts/{slug}"]["status"] == 200
    assert routes["/crash"]["status"] == 500
    assert [e["type"] for e in ingest.all("errors")] == ["KeyError"]


def test_django(client, ingest):
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=False, SECRET_KEY="x", ROOT_URLCONF=__name__, ALLOWED_HOSTS=["*"],
            MIDDLEWARE=["loggerai.django.LoggerAIMiddleware"],
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
            INSTALLED_APPS=[], LOGGING_CONFIG=None,
        )
        django.setup()
    from django.test import Client as DjangoClient

    DjangoClient().get("/orders/5/")
    DjangoClient(raise_request_exception=False).get("/explode/")
    loggerai.flush()
    routes = {r["route"]: r for r in ingest.all("requests")}
    o = routes["/orders/{pk}/"]
    assert o["status"] == 200 and o["deps"][0]["target"] == "sqlite" and o["deps"][0]["count"] == 2
    assert routes["/explode/"]["status"] == 500
    assert [e["type"] for e in ingest.all("errors")] == ["RuntimeError"]


def _orders(request, pk):
    from django.db import connection
    from django.http import HttpResponse

    with connection.cursor() as c:
        c.execute("SELECT 1")
        c.execute("SELECT 2")
    return HttpResponse("ok")


def _explode(request):
    raise RuntimeError("nope")


try:
    from django.urls import path

    urlpatterns = [path("orders/<int:pk>/", _orders), path("explode/", _explode)]
except ImportError:  # pragma: no cover
    urlpatterns = []


def test_logs_jobs_and_checks(ingest):
    c = loggerai.init("sk_live_test", endpoint=ingest.url + "/ingest", flush_interval=1, health_checks={
        "database": lambda: True, "queue": lambda: (False, "12 stuck"), "cache": lambda: 1 / 0})
    try:
        log = logging.getLogger("app.payments")
        log.warning("retrying with Bearer abcdefghijklmnopqrstuv")
        log.info("not sent")
        with loggerai.track("job", "send_invoices"):
            log.error("invoice 9 failed")
        with pytest.raises(ValueError):
            with loggerai.track("job", "bad_job"):
                raise ValueError("bad")
        try:
            raise KeyError("x")
        except KeyError:
            log.exception("handled one")
        import time
        time.sleep(1.5)  # let the health checks run once
        loggerai.flush()
        logs = ingest.all("logs")
        assert [(l["level"], l["logger"]) for l in logs] == [("WARNING", "app.payments"), ("ERROR", "app.payments")]
        assert "abcdefghijk" not in logs[0]["message"] and logs[1]["route"] == "send_invoices"
        jobs = {r["route"]: r for r in ingest.all("requests")}
        assert jobs["send_invoices"]["kind"] == "job" and jobs["send_invoices"]["status"] == 200 and jobs["bad_job"]["status"] == 500
        errs = {e["type"]: e for e in ingest.all("errors")}
        assert errs["ValueError"]["route"] == "bad_job" and errs["KeyError"]["handled"] is True
        checks = {k["name"]: k for k in ingest.all("checks")}
        assert checks["database"]["ok"] and not checks["queue"]["ok"] and checks["queue"]["detail"] == "12 stuck"
        assert not checks["cache"]["ok"] and "ZeroDivisionError" in checks["cache"]["detail"]
    finally:
        c.close()


def test_keys(ingest):
    assert not loggerai.init("pk_live_public", endpoint=ingest.url + "/ingest").enabled
    assert not loggerai.init(None, endpoint=ingest.url + "/ingest").enabled
    c = loggerai.init("sk_live_revoked", endpoint=ingest.url + "/ingest", flush_interval=1)
    ingest.answer = 401
    with loggerai.track("job", "x"):
        pass
    loggerai.flush()
    assert not c.enabled  # stops sending after a 401
    c.close()


def test_trace_parsing():
    assert context.trace_from("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert context.trace_from("garbage") is None
    assert context.trace_from("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None
    assert context.session_from("abc-123") == "abc-123" and context.session_from("<script>") is None
