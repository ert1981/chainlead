"""Etherscan-V2-compatible explorer provider (one API key covers many chains via `chainid`).
Used only to verify contracts already tied to a project: verified source + creation info."""
from __future__ import annotations

import urllib.parse

from .. import http
from ..config import Settings
from .base import Provider, empty_bundle

DEFAULT_API = "https://api.etherscan.io/v2/api"


class BlockExplorerProvider(Provider):
    name = "explorer"
    capabilities = frozenset({"verify"})

    def __init__(self, settings: Settings, api_url: str = DEFAULT_API, cost_per_call: float = 0.0) -> None:
        self.settings = settings
        self.api_url = api_url
        self.costs = {"verify": cost_per_call}

    def available(self) -> bool:
        return bool(self.settings.explorer_api_key)

    def _q(self, **params) -> dict:
        params["apikey"] = self.settings.explorer_api_key
        url = f"{self.api_url}?{urllib.parse.urlencode(params)}"
        return http.get_json(url, timeout=self.settings.http_timeout, user_agent=self.settings.user_agent)

    def verify(self, contract: dict, campaign: dict) -> dict:
        b = empty_bundle()
        chain_id, addr = int(contract["chain_id"]), contract["address"].lower()
        src = self._q(chainid=chain_id, module="contract", action="getsourcecode", address=addr)
        res = (src.get("result") or [{}])
        res = res[0] if isinstance(res, list) and res else {}
        verified = bool(res.get("SourceCode"))
        name = res.get("ContractName") or ""
        b["facts"].setdefault("verified_contracts", {})[f"{chain_id}:{addr}"] = verified
        b["evidence"].append({
            "source_type": "explorer", "url": f"{self.api_url}?chainid={chain_id}&address={addr}",
            "claim": (f"Source code of {addr} is verified on the explorer as '{name}'" if verified
                      else f"Source code of {addr} is NOT verified on the explorer"),
            "occurred_at": None, "official": False, "verified": True,
            "data": {"kind": "contract_verification", "chain_id": chain_id, "address": addr,
                     "verified_source": verified, "contract_name": name,
                     "proxy": res.get("Proxy") == "1", "implementation": res.get("Implementation") or None},
        })
        try:
            cr = self._q(chainid=chain_id, module="contract", action="getcontractcreation", contractaddresses=addr)
            row = (cr.get("result") or [{}])[0] if isinstance(cr.get("result"), list) else {}
            ts = row.get("timestamp")
            if row.get("txHash"):
                from datetime import datetime, timezone
                occurred = datetime.fromtimestamp(int(ts), timezone.utc).isoformat() if ts and str(ts).isdigit() else None
                b["evidence"].append({
                    "source_type": "explorer", "url": f"{self.api_url}?chainid={chain_id}&tx={row['txHash']}",
                    "claim": f"Contract {addr} was created in tx {row['txHash'][:12]}…",
                    "occurred_at": occurred, "official": False, "verified": True,
                    "data": {"kind": "contract_creation", "chain_id": chain_id, "address": addr,
                             "tx_hash": row["txHash"], "creator": (row.get("contractCreator") or "").lower()},
                })
        except (http.HttpError, ValueError, KeyError, TypeError):
            pass
        return b
