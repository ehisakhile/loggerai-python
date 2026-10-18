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

Secrets (passwords, tokens, cookies, API keys, JWTs) are masked before
anything leaves your server, request and response bodies are never sent, and
users are counted by a one-way hash of their id. No dependencies; Python 3.8+.

## Install

```bash
pip install loggerai
```

Get your project's **secret key** (`sk_live_...`) from LoggerAI → your
project → **Connect**, and set it in the environment:

```bash
LOGGERAI_KEY=sk_live_...
LOGGERAI_RELEASE=$GIT_SHA      # optional: "since release X"
```

Without a key nothing is recorded or sent, so local development stays quiet.

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

## How it sends

Records are kept in memory and sent from a background thread every few
seconds, gzipped, with a 2-second timeout. Requests never wait on LoggerAI.
If it can't be reached, records are dropped instead of piling up. With
gunicorn or uWSGI, each worker process sends its own batches.

## Support

dev@loggerai.uk · https://loggerai.uk
