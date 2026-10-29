"""REST API on the standard library (ThreadingHTTPServer). JSON in, JSON out.

Auth: when CHAINLEAD_API_KEYS="tenantA:keyA,tenantB:keyB" is set, every request needs
`Authorization: Bearer <key>` (or `X-API-Key`), and the key selects the tenant. Without keys
the server runs single-tenant ("local") and binds to 127.0.0.1 by default."""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .engine import ChainLead, check_budget
from .models import OFFER_CATALOG
from .safety import SecretMaterialRejected
from .workers import JobRunner

MAX_BODY = 1_000_000
SOCKET_TIMEOUT = 30          # seconds a client may take to send its request (slowloris guard)


class RateLimiter:
    """Sliding one-minute window per key (tenant or client IP). In-memory, per process."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = max(1, int(per_minute))
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self.hits) > 10_000:
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60]:
                    self.hits.pop(k, None)
            return True


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def make_handler(engine: ChainLead, jobs: JobRunner, rate_limit_per_minute: int | None = None):
    import os
    limiter = RateLimiter(rate_limit_per_minute or int(os.environ.get("CHAINLEAD_RATE_LIMIT_PER_MIN", "120")))
    run_limiter = RateLimiter(int(os.environ.get("CHAINLEAD_RUN_LIMIT_PER_MIN", "10")))
    routes: list[tuple[str, re.Pattern, Any]] = []

    def route(method: str, pattern: str):
        def deco(fn):
            routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    @route("GET", r"/health")
    def health(tenant, body, q):
        return 200, {"status": "ok", "version": __version__,
                     "providers": {p.name: p.available() for p in engine.providers},
                     "onchain": bool(engine.onchain)}

    @route("GET", r"/offers")
    def offers(tenant, body, q):
        return 200, [{"key": o.key, "title": o.title, "description": o.description, "keywords": o.keywords}
                     for o in OFFER_CATALOG.values()]

    @route("POST", r"/campaigns")
    def create(tenant, body, q):
        if not isinstance(body, dict):
            raise ApiError(400, "JSON object required")
        return 201, engine.create_campaign(body, tenant)

    @route("GET", r"/campaigns")
    def list_c(tenant, body, q):
        return 200, engine.storage.list_campaigns(tenant)

    @route("GET", r"/campaigns/(?P<cid>[\w-]+)")
    def get_c(tenant, body, q, cid):
        return 200, engine.get_campaign(tenant, cid).to_dict()

    @route("POST", r"/campaigns/(?P<cid>[\w-]+)/run")
    def run(tenant, body, q, cid):
        if not run_limiter.allow(tenant):
            raise ApiError(429, "too many campaign runs; try again in a minute")
        if q.get("async", ["false"])[0].lower() in ("1", "true", "yes"):
            return 202, jobs.submit(tenant, cid)
        return 200, engine.run_campaign(tenant, cid)

    @route("GET", r"/jobs/(?P<jid>[\w-]+)")
    def job(tenant, body, q, jid):
        j = jobs.get(tenant, jid)
        if not j:
            raise ApiError(404, "job not found")
        return 200, j

    @route("GET", r"/campaigns/(?P<cid>[\w-]+)/leads")
    def leads(tenant, body, q, cid):
        engine.get_campaign(tenant, cid)
        return 200, engine.leads(tenant, cid, q.get("status", [None])[0])

    @route("GET", r"/leads/(?P<lid>[\w-]+)/brief")
    def brief(tenant, body, q, lid):
        fmt = q.get("format", ["json"])[0]
        res = engine.brief(tenant, lid, fmt, q.get("lang", ["fa"])[0])
        return 200, ({"markdown": res} if fmt == "markdown" else res)

    @route("GET", r"/campaigns/(?P<cid>[\w-]+)/budget")
    def get_budget(tenant, body, q, cid):
        return 200, engine.budget(tenant, cid)

    @route("PUT", r"/campaigns/(?P<cid>[\w-]+)/budget")
    def put_budget(tenant, body, q, cid):
        if not isinstance(body, dict) or "limit_usd" not in body:
            raise ApiError(400, "body must be {\"limit_usd\": number}")
        try:
            limit = check_budget(body["limit_usd"])           # finite, >= 0
        except ValueError as e:
            raise ApiError(400, str(e))
        return 200, engine.set_budget(tenant, cid, limit)

    @route("GET", r"/campaigns/(?P<cid>[\w-]+)/calls")
    def calls(tenant, body, q, cid):
        engine.get_campaign(tenant, cid)
        return 200, engine.storage.calls(tenant, cid)

    @route("GET", r"/projects")
    def projects(tenant, body, q):
        return 200, engine.storage.list_projects(tenant)

    @route("GET", r"/projects/(?P<key>[\w.-]+)")
    def project(tenant, body, q, key):
        p = engine.storage.get_project(tenant, key)
        if not p:
            raise ApiError(404, "project not found")
        p["evidence"] = engine.storage.list_evidence(tenant, key)
        p["chain_events"] = engine.storage.list_events(tenant, key)
        return 200, p

    class Handler(BaseHTTPRequestHandler):
        server_version = f"ChainLead/{__version__}"
        timeout = SOCKET_TIMEOUT

        def log_message(self, fmt, *args):   # quiet by default
            pass

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _tenant(self) -> str:
            keys = engine.settings.api_keys
            if not keys:
                return "local"
            auth = self.headers.get("Authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("X-API-Key", "")
            for k, tenant in keys.items():
                if token and hmac.compare_digest(token, k):
                    return tenant
            raise ApiError(401, "missing or invalid API key")

        def _handle(self, method: str) -> None:
            try:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path.rstrip("/") or "/"
                q = urllib.parse.parse_qs(parsed.query)
                tenant = "public" if path == "/health" else self._tenant()
                if not limiter.allow(f"t:{tenant}" if tenant not in ("public", "local") else f"ip:{self.client_address[0]}"):
                    raise ApiError(429, "rate limit exceeded")
                body: Any = None
                raw_len = (self.headers.get("Content-Length") or "0").strip()
                if not raw_len.isdigit():               # negative or junk -> read(-1) would block the thread
                    raise ApiError(400, "invalid Content-Length")
                n = int(raw_len)
                if n > MAX_BODY:
                    raise ApiError(413, "request body too large")
                if n:
                    try:
                        body = json.loads(self.rfile.read(n).decode("utf-8"), parse_constant=_reject_constant)
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        raise ApiError(400, "invalid JSON body")
                for m, rx, fn in routes:
                    mt = rx.match(path)
                    if mt and m == method:
                        status, payload = fn(tenant, body, q, **mt.groupdict())
                        return self._send(status, payload)
                if any(rx.match(path) for _, rx, _ in routes):
                    raise ApiError(405, "method not allowed")
                raise ApiError(404, "not found")
            except ApiError as e:
                self._send(e.status, {"error": str(e)})
            except SecretMaterialRejected as e:
                self._send(400, {"error": str(e), "code": "secret_material_rejected"})
            except KeyError as e:
                self._send(404, {"error": str(e).strip("'\"")})
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"internal error: {type(e).__name__}"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

    return Handler


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def create_server(engine: ChainLead, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    jobs = JobRunner(engine)
    srv = ThreadingHTTPServer((host, port), make_handler(engine, jobs))
    srv.jobs = jobs  # type: ignore[attr-defined]
    return srv


def serve(engine: ChainLead, host: str = "127.0.0.1", port: int = 8787) -> None:
    if host not in ("127.0.0.1", "localhost", "::1") and not engine.settings.api_keys:
        raise SystemExit("Refusing to listen on a public interface without CHAINLEAD_API_KEYS set.")
    srv = create_server(engine, host, port)
    print(f"ChainLead API listening on http://{host}:{port}  (tenants: {sorted(set(engine.settings.api_keys.values())) or ['local']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
