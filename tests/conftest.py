import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from chainlead.config import Settings
from chainlead.engine import ChainLead
from chainlead.storage import Storage


@pytest.fixture
def settings():
    return Settings(db_path=":memory:", api_keys={}, rpc_urls={})


@pytest.fixture
def engine(settings):
    return ChainLead.build(settings, Storage(":memory:"), mock=True)


@pytest.fixture
def mock_campaign():
    return {"name": "t", "offer": "bridge_dev", "allowed_sources": ["mock"], "target_chains": [8453],
            "target_sectors": ["defi", "infrastructure"], "budget_usd": 0.10}


@pytest.fixture
def run_mock(engine, mock_campaign):
    def _run(**over):
        spec = dict(mock_campaign, **over)
        c = engine.create_campaign(spec)
        return engine.run_campaign("local", c["id"]), c
    return _run


class FakeHttp:
    """Tiny router-based HTTP server for exercising real providers without the internet.
    routes: {(method, path): handler(query:str, body:bytes) -> (status, content_type, body:str|bytes|obj)}"""

    def __init__(self):
        self.routes = {}
        self.hits = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                path, _, query = self.path.partition("?")
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.hits.append((method, path, query, dict(self.headers)))
                fn = fake.routes.get((method, path))
                if fn is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"message":"Not Found"}')
                    return
                status, ctype, out = fn(query, body)
                if not isinstance(out, (bytes, str)):
                    out = json.dumps(out)
                if isinstance(out, str):
                    out = out.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def json(self, path, obj, method="GET", status=200):
        self.routes[(method, path)] = lambda q, b: (status, "application/json", obj)

    def text(self, path, text, ctype="text/html; charset=utf-8"):
        self.routes[("GET", path)] = lambda q, b: (200, ctype, text)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake_http():
    f = FakeHttp()
    yield f
    f.close()
