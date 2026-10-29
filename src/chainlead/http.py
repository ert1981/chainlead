"""Tiny stdlib HTTP client with size limits, timeouts and simple retry on 429/5xx.

Two entry points:
  * request()/get_json()/post_json() - for operator-configured endpoints (GitHub API, the RPC
    URL in CHAINLEAD_RPC_*, the explorer and The Graph gateways).
  * fetch_untrusted() - for URLs that come from untrusted data (a project's website taken from a
    GitHub repo homepage or a campaign seed). It blocks SSRF: only http(s) on ports 80/443, no
    userinfo, the host must resolve ONLY to public (global) IPs, the connection is pinned to the
    validated IP (no DNS-rebinding window between check and connect), every redirect is re-checked,
    and the response is capped in size and in total wall-clock time."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_BYTES = 2_000_000
UNTRUSTED_MAX_BYTES = 1_000_000
UNTRUSTED_MAX_REDIRECTS = 3
UNTRUSTED_TOTAL_TIMEOUT = 12.0
ALLOWED_PORTS = {80, 443}


class HttpError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


class BlockedUrl(HttpError):
    """The URL points at a non-public address or is otherwise not allowed (SSRF guard)."""

    def __init__(self, message: str):
        super().__init__(0, f"blocked: {message}")


def _retry_after(value: str | None) -> float | None:
    return float(value) if value and value.isdigit() else None


def request(method: str, url: str, *, headers: dict | None = None, body: Any = None,
            timeout: float = 15, retries: int = 2, user_agent: str = "ChainLead/0.1") -> tuple[int, dict, bytes]:
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"refusing non-http url: {url!r}")
    data = None
    hdrs = {"User-Agent": user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    hdrs.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            # Scheme is restricted to http(s) above (bandit B310).
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
                raw = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
                return resp.status, dict(resp.headers), raw
        except urllib.error.HTTPError as e:
            ra = _retry_after(e.headers.get("Retry-After") if e.headers else None)
            last = HttpError(e.code, e.reason or "", ra)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(ra if ra is not None else 0.5 * (2 ** attempt), 5))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = HttpError(0, str(getattr(e, "reason", e)))
            if attempt < retries:
                time.sleep(0.3 * (2 ** attempt))
                continue
            raise last
    raise last or HttpError(0, "unknown")


def get_json(url: str, **kw: Any) -> Any:
    _, _, raw = request("GET", url, **kw)
    return json.loads(raw.decode("utf-8", "replace"))


def post_json(url: str, body: Any, **kw: Any) -> Any:
    _, _, raw = request("POST", url, body=body, **kw)
    return json.loads(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------------- SSRF-safe fetch
def is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if isinstance(addr, ipaddress.IPv6Address) and addr in ipaddress.ip_network("64:ff9b::/96"):
        return False  # NAT64 can reach IPv4-internal targets
    return bool(addr.is_global) and not addr.is_multicast


def _resolve_public(host: str, port: int, allow_private: bool) -> str:
    """Resolve once, require every address to be public, and return the one to connect to."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise HttpError(0, f"dns: {e}")
    ips = [i[4][0] for i in infos]
    if not ips:
        raise HttpError(0, "dns: no address")
    if not allow_private:
        bad = [ip for ip in ips if not is_public_ip(ip)]
        if bad:
            raise BlockedUrl(f"{host} resolves to a non-public address")
    return ips[0]


def check_url(url: str, allow_private: bool = False) -> urllib.parse.SplitResult:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedUrl("only http(s) urls are fetched")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedUrl("credentials in url")
    if not parts.hostname:
        raise BlockedUrl("missing host")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise BlockedUrl("bad port")
    if port not in ALLOWED_PORTS and not allow_private:
        raise BlockedUrl(f"port {port} not allowed")
    host = parts.hostname.rstrip(".").lower()
    if not allow_private and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise BlockedUrl("internal host name")
    return parts


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._ip, self.port), self.timeout)
        # SNI and certificate validation use the real host name; TCP goes to the validated IP.
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def fetch_untrusted(url: str, *, timeout: float = 10, user_agent: str = "ChainLead/0.1",
                    max_bytes: int = UNTRUSTED_MAX_BYTES, max_redirects: int = UNTRUSTED_MAX_REDIRECTS,
                    total_timeout: float = UNTRUSTED_TOTAL_TIMEOUT,
                    allow_private: bool = False) -> tuple[int, dict, bytes, str]:
    """GET an untrusted URL safely. Returns (status, headers, body, final_url).
    Raises BlockedUrl for SSRF attempts and HttpError for other failures (incl. 4xx/5xx)."""
    deadline = time.monotonic() + total_timeout
    current = url
    for _ in range(max_redirects + 1):
        parts = check_url(current, allow_private)
        host = parts.hostname.rstrip(".")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = _resolve_public(host, port, allow_private)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HttpError(0, "total time limit exceeded")
        cls = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
        conn = cls(host, ip, port, timeout=min(timeout, remaining))
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", path, headers={
                "Host": parts.netloc.split("@")[-1], "User-Agent": user_agent,
                "Accept": "text/html, text/plain;q=0.9, */*;q=0.1", "Accept-Encoding": "identity",
                "Connection": "close"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise HttpError(resp.status, "redirect without Location")
                current = urllib.parse.urljoin(current, loc)
                continue
            clen = resp.getheader("Content-Length")
            if clen and clen.isdigit() and int(clen) > max_bytes:
                raise HttpError(resp.status, "response too large")
            chunks, total = [], 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise HttpError(0, "total time limit exceeded")
                if conn.sock is not None:
                    conn.sock.settimeout(min(timeout, left))
                try:
                    # read1: return what has arrived instead of blocking for the full chunk (slow-drip guard)
                    chunk = resp.read1(min(65536, max_bytes + 1 - total))
                except (TimeoutError, socket.timeout):
                    raise HttpError(0, "total time limit exceeded")
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise HttpError(resp.status, "response too large")
            if resp.status >= 400:
                raise HttpError(resp.status, resp.reason or "")
            return resp.status, {k: v for k, v in resp.getheaders()}, b"".join(chunks), current
        except (OSError, http.client.HTTPException) as e:
            raise HttpError(0, f"{type(e).__name__}: {e}"[:200])
        finally:
            conn.close()
    raise BlockedUrl("too many redirects")
