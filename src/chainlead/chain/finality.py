"""Confirmation status and reorg handling.

An event seen at block N is:  pending (< safe_depth confirmations) -> safe -> finalized.
On every sync we re-check stored, not-yet-final events: if the canonical block hash at
that height changed, the event is marked `orphaned` and is excluded from scoring."""
from __future__ import annotations

from typing import Callable

from ..storage import Storage
from .registry import Chain


def confirmation_status(chain: Chain, event_block: int, head: int) -> str:
    conf = head - event_block
    if conf >= chain.finalized_depth:
        return "finalized"
    if conf >= chain.safe_depth:
        return "safe"
    return "pending"


def reconcile_reorgs(storage: Storage, tenant: str, chain: Chain, head: int,
                     canonical_hash: Callable[[int], str | None]) -> dict:
    """Re-validate events inside the non-final window. Returns counts per action."""
    window_start = max(0, head - chain.finalized_depth)
    stats = {"checked": 0, "orphaned": 0, "promoted": 0}
    cache: dict[int, str | None] = {}
    for ev in storage.events_from_block(tenant, chain.chain_id, window_start):
        if ev["status"] == "finalized":
            continue
        stats["checked"] += 1
        bn = ev["block_number"]
        if bn not in cache:
            cache[bn] = (canonical_hash(bn) or "").lower() or None
        current = cache[bn]
        if current is not None and current != (ev["block_hash"] or "").lower():
            storage.set_event_status(tenant, chain.chain_id, ev["tx_hash"], ev["log_index"], "orphaned")
            stats["orphaned"] += 1
            continue
        new_status = confirmation_status(chain, bn, head)
        if new_status != ev["status"]:
            storage.set_event_status(tenant, chain.chain_id, ev["tx_hash"], ev["log_index"], new_status)
            stats["promoted"] += 1
    return stats
