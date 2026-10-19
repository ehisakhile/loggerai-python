"""python -m loggerai test: check this machine can send to LoggerAI."""
from __future__ import annotations

import os
import sys

import loggerai


def test() -> int:
    key = os.environ.get("LOGGERAI_KEY", "")
    print(f"loggerai {loggerai.__version__}")
    print(f"LOGGERAI_KEY: {key[:12] + '…' if key else '(not set)'}")
    client = loggerai.init(health_checks={"loggerai-test": lambda: True})
    print(f"endpoint: {client.endpoint}")
    print(f"service: {client.service} · environment: {client.environment} · release: {client.release or '(none)'}")
    if not client.enabled:
        print("Not sending: set LOGGERAI_KEY to the project's secret key (sk_live_…) from LoggerAI → Connect.")
        return 1
    with loggerai.track("command", "loggerai test"):
        pass
    client._run_health()
    sent = client.flush(10)
    if client.last_error or not sent:
        print(f"Failed: {client.last_error or 'timed out'}")
        return 1
    print("Sent. The project in LoggerAI shows as connected within a few seconds.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["test"]:
        print("usage: python -m loggerai test")
        sys.exit(2)
    sys.exit(test())
