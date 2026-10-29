"""Supported networks. MVP focuses on one EVM chain (Base by default); others are listed so
campaigns can name them, but each must be explicitly given an RPC endpoint (BYOK)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chain:
    chain_id: int
    name: str
    slug: str
    safe_depth: int          # confirmations after which we call an event "safe"
    finalized_depth: int     # confirmations treated as final for scoring purposes
    max_log_range: int       # max block span per eth_getLogs request (public RPCs cap this)
    block_time: float        # average seconds per block, used to turn "days" into blocks
    aliases: tuple[str, ...] = ()


CHAINS: dict[int, Chain] = {c.chain_id: c for c in [
    Chain(1, "Ethereum", "ethereum", 12, 64, 2_000, 12.0, ("eth", "mainnet", "ethereum mainnet")),
    Chain(8453, "Base", "base", 30, 120, 5_000, 2.0, ("base mainnet",)),
    Chain(10, "OP Mainnet", "optimism", 30, 120, 5_000, 2.0, ("optimism", "op")),
    Chain(42161, "Arbitrum One", "arbitrum", 60, 240, 5_000, 0.25, ("arbitrum", "arb")),
    Chain(137, "Polygon PoS", "polygon", 64, 256, 2_000, 2.0, ("polygon", "matic")),
]}


def get_chain(chain_id: int) -> Chain:
    if chain_id not in CHAINS:
        raise KeyError(f"unsupported chain id {chain_id}; supported: {sorted(CHAINS)}")
    return CHAINS[chain_id]


def chain_by_name(text: str) -> Chain | None:
    t = text.strip().lower()
    for c in CHAINS.values():
        if t in (c.slug, c.name.lower(), *c.aliases):
            return c
    return None


def chains_mentioned(text: str) -> set[int]:
    """Chain ids whose names appear as whole words in free text (README, docs, announcements)."""
    import re
    found: set[int] = set()
    low = text.lower()
    for c in CHAINS.values():
        names = {c.slug, c.name.lower(), *c.aliases} - {"op", "eth", "mainnet", "base"}  # ambiguous words
        if c.chain_id == 8453 and re.search(
                r"\b(on|to|deployed on|live on|launch(?:ed|ing)? on)\s+base\b|\bbase\s+(mainnet|chain|network|l2|sepolia)\b",
                low):
            found.add(8453)
            continue
        for n in names:
            if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low):
                found.add(c.chain_id)
                break
    return found
