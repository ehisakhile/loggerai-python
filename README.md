# LoggerAI for Python

Performance monitoring, errors, logs and health checks for FastAPI,
Starlette, Django, Flask and Celery, with AI that explains what went wrong.

- **Every endpoint** timed under its route template (`/orders/{order_id}`):
  p50/p95/p99, Apdex, status codes, slow and failed requests.
- **What each request waits on**: database queries (SQLAlchemy and Django),
  outgoing HTTP (`httpx`, `requests`), Redis.
- **Exceptions**, grouped by cause, with stack traces that show your code first.
- **Logs** at warning and above.
- **Health checks** you define (database, cache, a queue).
- **Background work** (Celery tasks, scripts) timed like requests.
- **Browser sessions** recorded by LoggerAI's browser script link to the
  server request each click caused (`traceparent`, `x-loggerai-session`).

The package is `loggerai-sdk`; you import it as `loggerai`.

Secrets (passwords, tokens, cookies, API keys, JWTs) are masked before
anything leaves your server, request and response bodies are never sent, and
users are counted by a one-way hash of their id. No dependencies; Python 3.8+.

## Install

```bash
pip install loggerai-sdk
```

Get your project's **secret key** (`sk_live_...`) from LoggerAI → your
project → **Connect**, and set it in the environment:

```bash
LOGGERAI_KEY=sk_live_...
LOGGERAI_RELEASE=$GIT_SHA      # optional: "since release X"
```

Without a key nothing is recorded or sent, so local development stays quiet.

Check it can reach LoggerAI from your server (or container):

```bash
python -m loggerai test
```

### FastAPI / Starlette

```python
import loggerai
from loggerai.asgi import LoggerAIMiddleware

loggerai.init()
app = FastAPI()
app.add_middleware(LoggerAIMiddleware)
```

### Django

```python
# settings.py
import loggerai
loggerai.init()

MIDDLEWARE = [
    "loggerai.django.LoggerAIMiddleware",   # first
    ...
]
```

### Flask

```python
import loggerai, loggerai.flask

loggerai.init()
app = Flask(__name__)
loggerai.flask.instrument(app)
```

### Celery

```python
import loggerai, loggerai.celery

loggerai.init(service="worker")
loggerai.celery.instrument()
```

### Any other WSGI app

```python
from loggerai.wsgi import LoggerAIMiddleware
application = LoggerAIMiddleware(application)
```

## Health checks

```python
def database():
    with engine.connect() as c:
        c.execute(text("SELECT 1"))
    return True               # or (False, "why")

loggerai.init(health_checks={"database": database, "redis": lambda: r.ping()})
```

They run every minute in the background and show on the project's health
page; alert on them from the dashboard.

## Server CPU and memory

The SDK also sends the server's CPU, memory, swap and disk every 30
seconds, so LoggerAI can tell when the server itself is why the app got slow
("CPU on web-1 hit 97%"). Inside Docker or Kubernetes it reports the
container's own CPU and memory limits. It reads Linux's `/proc`; install
`psutil` for macOS and Windows. Turn it off with `LOGGERAI_HOST_METRICS=false`.

## Microservices

Give each service its own name and use the same project key everywhere:

```bash
LOGGERAI_SERVICE=orders
```

Calls between services then link up on their own: a request is one trace from
the first service to the last, and LoggerAI's Service map shows which service
calls which. The middleware continues the trace each request is called with
(`traceparent`), and `httpx` and `requests` calls carry it on, with this
service's name (`x-loggerai-caller`). Only the project's own services get them: hosts without a dot (Docker and Kubernetes service names), private addresses, and `.internal`, `.local` and `.svc.cluster.local` names. Outside APIs (Stripe, OpenAI) never do. If your services call each other by a public name, list it (`loggerai.init(propagate=["api.acme.io", "*.acme.io"])`, or
`LOGGERAI_PROPAGATE=api.acme.io,*.acme.io`; `all` or `none` also work).

Celery tasks carry the trace on their own once `loggerai.celery.instrument()`
runs where tasks are sent and where they run. For other queues, put it on the
message and pass it back when you handle it:

```python
producer.produce("orders", value, headers=loggerai.trace_headers("orders"))

with loggerai.track("job", "order.created", headers=message.headers()):
    handle(message)
```

## More

```python
loggerai.set_user(request.user.id)          # only a one-way hash is sent

with loggerai.measure("api.stripe.com", "POST"):   # calls it can't see itself
    stripe.PaymentIntent.confirm(intent_id)

with loggerai.track("job", "send_invoices"):       # scripts, cron jobs
    send_invoices()

try:
    risky()
except ValueError as e:
    loggerai.capture_exception(e, order=order.id)  # handled, but worth seeing

loggerai.flush()     # at the end of a short script or a serverless call
```

## Settings

| Environment | `init()` | Default | |
|---|---|---|---|
| `LOGGERAI_KEY` | `key` | — | Secret key; off without it |
| `LOGGERAI_SERVICE` | `service` | `app` | Name this process (`api`, `worker`) |
| `LOGGERAI_ENVIRONMENT` | `environment` | `production` | Separates production from staging |
| `LOGGERAI_RELEASE` | `release` | — | Version or git SHA |
| `LOGGERAI_ENABLED` | `enabled` | `true` | Turn it off without removing the key |
| | `logs_level` | `"WARNING"` | Lowest log level sent; `None` for no logs |
| | `ignore_paths` | `/health`, `/up`, … | Paths not recorded |
| | `capture_queries`, `capture_http`, `capture_redis` | `True` | |
| | `flush_interval` | `5` | Seconds between batches |
| `LOGGERAI_HOST_METRICS` | `host_metrics` | `True` | The server's CPU, memory and disk |

## How it sends

Records are kept in memory and sent from a background thread every few
seconds, gzipped, with a 2-second timeout. Requests never wait on LoggerAI.
If it can't be reached, records are dropped instead of piling up. With
gunicorn or uWSGI, each worker process sends its own batches.

## Circuit breakers (opt in)

Register the exact dependency name, calling service, and environment used by
the policy in Reliability Lab. Only explicitly wrapped calls are protected.

```python
import os
import httpx
from loggerai import CircuitRegistry

registry = CircuitRegistry(os.environ["LOGGERAI_KEY"], service="checkout", environment="staging")
payments = registry.breaker("payments", minimum_requests=10, failure_ratio=.5)
registry.start()  # sync immediately, then every 30 seconds

response = payments.call(
    lambda: httpx.get("https://payments.example/status", timeout=3),
    is_success=lambda response: response.status_code < 500,
)

# At application shutdown:
registry.stop()
```

For async operations use `await payments.call_async(lambda: client.get(...))`
with an asynchronous HTTP client and an explicit timeout. Handle
`CircuitOpenError` and `CircuitCapacityError` in your application's response
path. Exceptions and results rejected by `is_success` count as failures. The
wrapper returns the original result and does not retry or add a fallback.

Calls use local state and retain the last configuration when synchronization
fails. Runtime acknowledgements describe observed processes, not proof that
every instance is instrumented. Start a registry per application process after
forking and stop it during shutdown. Automatic instrumentation is independent.

## Support

dev@loggerai.uk · https://loggerai.uk
