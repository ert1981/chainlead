"""Runtime configuration, read from environment variables (BYOK: bring your own keys)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: _env("CHAINLEAD_DB", "chainlead.db"))
    github_token: str | None = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    # JSON-RPC endpoints per chain id, e.g. CHAINLEAD_RPC_8453=https://mainnet.base.org
    rpc_urls: dict[int, str] = field(default_factory=dict)
    explorer_api_key: str | None = field(default_factory=lambda: _env("EXPLORER_API_KEY"))
    graph_api_key: str | None = field(default_factory=lambda: _env("GRAPH_API_KEY"))
    # Comma-separated "tenant:key" pairs for the REST API. Empty = local single-tenant mode.
    api_keys: dict[str, str] = field(default_factory=dict)
    # default_factory: read when Settings() is created, not once at import time
    http_timeout: float = field(default_factory=lambda: float(_env("CHAINLEAD_HTTP_TIMEOUT", "15") or 15))
    user_agent: str = "ChainLead/0.1 (+https://github.com/chainlead)"
    cache_ttl_seconds: int = field(default_factory=lambda: int(_env("CHAINLEAD_CACHE_TTL", "86400") or 86400))

    def __post_init__(self) -> None:
        for k, v in os.environ.items():
            if k.startswith("CHAINLEAD_RPC_") and v:
                try:
                    self.rpc_urls.setdefault(int(k.removeprefix("CHAINLEAD_RPC_")), v)
                except ValueError:
                    pass
        raw = _env("CHAINLEAD_API_KEYS", "") or ""
        for pair in raw.split(","):
            if ":" in pair:
                tenant, key = pair.split(":", 1)
                if key.strip() and tenant.strip():      # an empty key must never authenticate
                    self.api_keys[key.strip()] = tenant.strip()


def load_settings() -> Settings:
    return Settings()
