"""Tiny stdlib HTTP client with size limits, timeouts and simple retry on 429/5xx."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

MAX_BYTES = 2_000_000


class HttpError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.retry_after = retry_after


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
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
                return resp.status, dict(resp.headers), raw
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if e.headers else None
            last = HttpError(e.code, e.reason or "", float(ra) if ra and ra.isdigit() else None)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(float(ra) if ra and ra.isdigit() else 0.5 * (2 ** attempt), 5))
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
