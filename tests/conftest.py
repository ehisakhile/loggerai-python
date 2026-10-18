import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import loggerai


class Ingest:
    """A stand-in for LoggerAI's ingest endpoint: keeps every batch it gets."""

    def __init__(self):
        self.batches, self.keys, self.answer = [], [], 202
        ingest = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                if self.path.startswith("/ingest"):
                    ingest.keys.append(self.headers.get("X-LoggerAI-Key"))
                    ingest.batches.append(json.loads(body))
                    self.send_response(ingest.answer)
                else:  # an "outside service" for outgoing-call tests
                    self.send_response(503 if "fail" in self.path else 200)
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def all(self, kind):
        return [r for b in self.batches for r in b.get(kind, [])]


@pytest.fixture
def ingest():
    i = Ingest()
    yield i
    i.server.shutdown()


@pytest.fixture
def client(ingest):
    c = loggerai.init("sk_live_test", endpoint=ingest.url + "/ingest", flush_interval=1, service="api", environment="test", release="v1")
    yield c
    c.close()
