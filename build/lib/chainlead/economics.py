"""Budget, cost ledger and provider router.

Rules enforced here (not in any LLM):
  * every paid call is reserved against the campaign budget atomically BEFORE it runs;
    if it does not fit, it is not made (BudgetExceeded);
  * identical requests are served from cache (cost 0) and logged as cached;
  * failed calls are refunded and logged with their error;
  * free sources are always preferred over paid ones for the same capability (spec §4)."""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from .config import Settings
from .models import stable_hash
from .storage import Storage

log = logging.getLogger("chainlead.economics")


class BudgetExceeded(Exception):
    pass


class ProviderError(Exception):
    pass


class ProviderRouter:
    def __init__(self, storage: Storage, settings: Settings, tenant: str, campaign_id: str,
                 providers: Iterable[Any]) -> None:
        self.storage = storage
        self.settings = settings
        self.tenant = tenant
        self.campaign_id = campaign_id
        self.providers = {p.name: p for p in providers}

    # -------------------------------------------------------------- selection
    def available(self, capability: str, allowed: Iterable[str] | None = None) -> list[Any]:
        allowed_set = set(allowed) if allowed is not None else None
        cands = [p for p in self.providers.values()
                 if capability in p.capabilities and p.available()
                 and (allowed_set is None or p.name in allowed_set)]
        # free first, then cheapest
        return sorted(cands, key=lambda p: (p.cost(capability), p.name))

    # -------------------------------------------------------------- execution
    def call(self, provider: Any, op: str, params: dict, fn: Callable[[], Any],
             cacheable: bool = True) -> Any:
        key = stable_hash(self.tenant, provider.name, op, params)
        if cacheable:
            hit = self.storage.cache_get(key, self.settings.cache_ttl_seconds)
            if hit is not None:
                self.storage.record_call(self.tenant, self.campaign_id, provider.name, op, key, 0.0, "ok", True)
                return hit
        cost = float(provider.cost(op))
        if cost > 0 and not self.storage.try_spend(self.tenant, self.campaign_id, cost):
            self.storage.record_call(self.tenant, self.campaign_id, provider.name, op, key, 0.0,
                                     "skipped_budget", False, "budget limit reached")
            raise BudgetExceeded(f"{provider.name}.{op} costs ${cost:.4f}; budget exhausted")
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - every provider failure is logged & refunded
            if cost > 0:
                self.storage.try_spend(self.tenant, self.campaign_id, -cost)
            self.storage.record_call(self.tenant, self.campaign_id, provider.name, op, key, 0.0, "error",
                                     False, f"{type(e).__name__}: {e}"[:300])
            raise ProviderError(f"{provider.name}.{op} failed: {e}") from e
        if cacheable:
            self.storage.cache_put(key, provider.name, result)
        self.storage.record_call(self.tenant, self.campaign_id, provider.name, op, key, cost, "ok", False)
        return result

    # -------------------------------------------------------------- reporting
    def summary(self) -> dict:
        calls = self.storage.calls(self.tenant, self.campaign_id)
        b = self.storage.budget(self.tenant, self.campaign_id) or {"limit_usd": 0, "spent_usd": 0}
        by_provider: dict[str, dict] = {}
        for c in calls:
            s = by_provider.setdefault(c["provider"], {"calls": 0, "cached": 0, "errors": 0,
                                                       "skipped_budget": 0, "cost_usd": 0.0})
            s["calls"] += 1
            s["cached"] += c["cached"]
            s["errors"] += c["status"] == "error"
            s["skipped_budget"] += c["status"] == "skipped_budget"
            s["cost_usd"] = round(s["cost_usd"] + c["cost_usd"], 6)
        return {"limit_usd": b["limit_usd"], "spent_usd": round(b["spent_usd"], 6),
                "remaining_usd": round(b["limit_usd"] - b["spent_usd"], 6), "by_provider": by_provider,
                "total_calls": len(calls)}
