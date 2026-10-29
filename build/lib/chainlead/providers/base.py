"""Provider interface.

Providers are swappable data sources (spec §13 "قابل تعویض بودن منابع داده"). They return
plain JSON-serialisable dicts so results can be cached and costed by the router.

Capabilities:
  discover   – produce candidate projects for a campaign
  evidence   – collect public off-chain evidence for one candidate
  onchain    – read contract code / events for linked contracts (read-only)
  verify     – verify contract source / creation on an explorer
  contacts   – enrich public contact channels (often paid; only for top candidates)

Candidate dict:  {name, github_org, repos[], website, description, sectors[], chains[], source_url}
Bundle dict (evidence/onchain/verify/contacts):
  {"evidence": [ {source_type,url,claim,occurred_at,official,verified,data:{kind,...}} ],
   "contracts": [ {chain_id,address,label,linked_by} ],
   "people":    [ {name,role,source_url} ],
   "contacts":  [ {type,value,source_url} ],
   "links":     {"site_links_github": [...], "repo_homepages": [...], "site_url": str},
   "facts":     {...},
   "events":    [ ChainEvent-like dicts ] }
"""
from __future__ import annotations

from typing import Any


class Provider:
    name: str = "base"
    capabilities: frozenset[str] = frozenset()
    costs: dict[str, float] = {}

    def available(self) -> bool:
        return True

    def cost(self, op: str) -> float:
        return float(self.costs.get(op, 0.0))

    # Implementations override what they support ---------------------------
    def discover(self, campaign: dict) -> list[dict]:
        raise NotImplementedError

    def evidence(self, candidate: dict, campaign: dict) -> dict:
        raise NotImplementedError

    def contacts(self, candidate: dict, campaign: dict) -> dict:
        raise NotImplementedError

    def verify(self, contract: dict, campaign: dict) -> dict:
        raise NotImplementedError


def empty_bundle() -> dict[str, Any]:
    return {"evidence": [], "contracts": [], "people": [], "contacts": [], "links": {}, "facts": {},
            "events": []}


def merge_bundles(*bundles: dict) -> dict:
    out = empty_bundle()
    for b in bundles:
        if not b:
            continue
        for k in ("evidence", "contracts", "people", "contacts", "events"):
            out[k].extend(b.get(k, []))
        for k, v in (b.get("links") or {}).items():
            if isinstance(v, list):
                out["links"].setdefault(k, [])
                out["links"][k].extend(x for x in v if x not in out["links"][k])
            else:
                out["links"][k] = v
        out["facts"].update(b.get("facts") or {})
    return out
