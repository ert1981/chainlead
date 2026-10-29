"""Python SDK.

    from chainlead.sdk import ChainLeadClient          # talk to a running REST server
    from chainlead.sdk import LocalChainLead           # embed the engine in-process

Both expose the same methods: offers, create_campaign, run_campaign, run_async, job,
leads, brief, budget."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .engine import ChainLead
from .models import OFFER_CATALOG
from .storage import Storage


class ChainLeadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class ChainLeadClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8787", api_key: str | None = None, timeout: float = 120):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be http(s)")
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # nosec B310 - http(s) checked in __init__
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", e.reason)
            except Exception:  # noqa: BLE001
                msg = e.reason
            raise ChainLeadError(e.code, msg) from None

    def health(self) -> dict:
        return self._req("GET", "/health")

    def offers(self) -> list[dict]:
        return self._req("GET", "/offers")

    def create_campaign(self, **spec: Any) -> dict:
        return self._req("POST", "/campaigns", spec)

    def run_campaign(self, campaign_id: str) -> dict:
        return self._req("POST", f"/campaigns/{campaign_id}/run")

    def run_async(self, campaign_id: str) -> dict:
        return self._req("POST", f"/campaigns/{campaign_id}/run?async=true")

    def job(self, job_id: str) -> dict:
        return self._req("GET", f"/jobs/{job_id}")

    def leads(self, campaign_id: str, status: str | None = None) -> list[dict]:
        return self._req("GET", f"/campaigns/{campaign_id}/leads" + (f"?status={status}" if status else ""))

    def brief(self, lead_id: str, format: str = "json", lang: str = "fa") -> Any:  # noqa: A002
        r = self._req("GET", f"/leads/{lead_id}/brief?format={format}&lang={lang}")
        return r["markdown"] if format == "markdown" else r

    def budget(self, campaign_id: str) -> dict:
        return self._req("GET", f"/campaigns/{campaign_id}/budget")

    def set_budget(self, campaign_id: str, limit_usd: float) -> dict:
        return self._req("PUT", f"/campaigns/{campaign_id}/budget", {"limit_usd": limit_usd})


class LocalChainLead:
    """Same surface as ChainLeadClient, without a server."""

    def __init__(self, db_path: str = "chainlead.db", mock: bool = False, tenant: str = "local",
                 settings: Settings | None = None):
        settings = settings or Settings(db_path=db_path)
        self.engine = ChainLead.build(settings, Storage(db_path), mock=mock)
        self.tenant = tenant

    def offers(self) -> list[dict]:
        return [{"key": o.key, "title": o.title} for o in OFFER_CATALOG.values()]

    def create_campaign(self, **spec: Any) -> dict:
        return self.engine.create_campaign(spec, self.tenant)

    def run_campaign(self, campaign_id: str) -> dict:
        return self.engine.run_campaign(self.tenant, campaign_id)

    def leads(self, campaign_id: str, status: str | None = None) -> list[dict]:
        return self.engine.leads(self.tenant, campaign_id, status)

    def brief(self, lead_id: str, format: str = "json", lang: str = "fa") -> Any:  # noqa: A002
        return self.engine.brief(self.tenant, lead_id, format, lang)

    def budget(self, campaign_id: str) -> dict:
        return self.engine.budget(self.tenant, campaign_id)
