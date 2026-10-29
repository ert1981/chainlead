"""Settings of the paid tool server, from environment variables. No secret is needed for payments:
the payee is a public address, and the facilitator (PayAI) needs no key."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import payments

DEFAULT_BASE_RPC = "https://base-rpc.publicnode.com"


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_env(name, str(default)))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


@dataclass
class ToolSettings:
    public_base_url: str = field(default_factory=lambda: _env("CHAINLEAD_PUBLIC_BASE_URL").rstrip("/"))
    payee_address: str = field(default_factory=lambda: _env("CHAINLEAD_PAYEE_ADDRESS"))
    creator_address: str = field(default_factory=lambda: _env("CHAINLEAD_CREATOR_ADDRESS").lower())
    price_usdc: str = field(default_factory=lambda: _env("CHAINLEAD_PRICE_USDC", "0.50"))
    x402_network: str = field(default_factory=lambda: _env("CHAINLEAD_X402_NETWORK", "base"))
    facilitator_url: str = field(default_factory=lambda: _env("CHAINLEAD_FACILITATOR_URL",
                                                              payments.PAYAI_FACILITATOR_URL))
    # Synchronous run limits (fit Render free and OpenSea review: one call answers well under a minute).
    time_budget_s: int = field(default_factory=lambda: _int("CHAINLEAD_TOOL_TIME_BUDGET_S", 35, 5, 110))
    max_candidates: int = field(default_factory=lambda: _int("CHAINLEAD_TOOL_MAX_CANDIDATES", 8, 1, 20))
    max_concurrent: int = field(default_factory=lambda: _int("CHAINLEAD_TOOL_MAX_CONCURRENT", 2, 1, 8))
    probe_rate_per_min: int = field(default_factory=lambda: _int("CHAINLEAD_TOOL_RATE_PER_MIN", 60, 1, 10_000))
    registry_rpc_url: str = field(default_factory=lambda: _env("CHAINLEAD_RPC_8453", DEFAULT_BASE_RPC))
    db_path: str = field(default_factory=lambda: _env("CHAINLEAD_DB", "/tmp/chainlead-tool.db"))

    @property
    def paid_enabled(self) -> bool:
        return payments.valid_payee(self.payee_address)

    def x402(self) -> payments.X402Config | None:
        if not self.paid_enabled:
            return None
        return payments.X402Config(payee=self.payee_address, price_usdc=self.price_usdc, network=self.x402_network)

    def manifest_problem(self) -> str | None:
        """None when the ERC-8257 manifest can be served; otherwise what is missing."""
        p = urlparse(self.public_base_url)
        if p.scheme != "https" or not p.netloc or p.path not in ("", "/") or p.query or p.fragment:
            return "CHAINLEAD_PUBLIC_BASE_URL must be a bare https:// origin"
        if not re.fullmatch(r"0x[0-9a-f]{40}", self.creator_address or ""):
            return "CHAINLEAD_CREATOR_ADDRESS must be a 0x + 40 hex address"
        if not self.paid_enabled:
            return "CHAINLEAD_PAYEE_ADDRESS must be set (the tool is paid via x402)"
        try:
            self.x402()
        except ValueError as e:
            return f"x402 configuration: {e}"
        return None
