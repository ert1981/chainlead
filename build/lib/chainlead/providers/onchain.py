"""On-chain provider: read-only contract checks through any EVM JSON-RPC endpoint (BYOK).

For each contract that an *official* source tied to a project, it checks:
  * does bytecode exist on that chain?  (claims of a deployment that has no code are refuted)
  * recent deployment-related events (Upgraded / AdminChanged / Initialized / OwnershipTransferred)
    inside the campaign freshness window, scanned newest-first under a request cap.
Every event keeps chain_id + tx_hash + log_index (+ block number/hash) for reorg handling."""
from __future__ import annotations

from typing import Callable

from ..chain.event_decoder import DEPLOYMENT_EVENTS, KNOWN_EVENTS, block_timestamp, decode_log
from ..chain.finality import confirmation_status
from ..chain.registry import get_chain
from ..chain.rpc import RpcClient
from ..config import Settings
from ..models import iso
from .base import Provider, empty_bundle

WATCH_TOPICS = [t for t, (name, _) in KNOWN_EVENTS.items() if name in DEPLOYMENT_EVENTS | {"OwnershipTransferred"}]


class OnChainProvider(Provider):
    name = "rpc"
    capabilities = frozenset({"onchain"})

    def __init__(self, settings: Settings, client_factory: Callable[[int], object] | None = None,
                 cost_per_contract: float = 0.0, max_log_requests_per_contract: int = 25,
                 log_range_override: int | None = None) -> None:
        self.settings = settings
        self.costs = {"onchain": cost_per_contract}
        self.max_requests = max_log_requests_per_contract
        self.log_range_override = log_range_override
        self._factory = client_factory
        self._clients: dict[int, object] = {}

    def client(self, chain_id: int):
        if chain_id not in self._clients:
            if self._factory is not None:
                self._clients[chain_id] = self._factory(chain_id)
            elif chain_id in self.settings.rpc_urls:
                self._clients[chain_id] = RpcClient(self.settings.rpc_urls[chain_id], self.settings.http_timeout,
                                                    self.settings.user_agent)
            else:
                return None
        return self._clients[chain_id]

    def available(self) -> bool:
        return self._factory is not None or bool(self.settings.rpc_urls)

    def supports(self, chain_id: int) -> bool:
        return self.client(chain_id) is not None

    def canonical_hash(self, chain_id: int, block_number: int) -> str | None:
        blk = self.client(chain_id).get_block(block_number)
        return blk.get("hash") if blk else None

    def head(self, chain_id: int) -> int:
        return self.client(chain_id).block_number()

    def inspect(self, contract: dict, campaign: dict) -> dict:
        b = empty_bundle()
        chain_id = int(contract["chain_id"])
        cli = self.client(chain_id)
        if cli is None:
            return b
        chain = get_chain(chain_id)
        addr = contract["address"].lower()
        code = cli.get_code(addr)
        has_code = bool(code) and code not in ("0x", "0x0")
        b["evidence"].append({
            "source_type": "rpc", "url": f"evm://{chain_id}/{addr}",
            "claim": (f"Contract {addr} has deployed bytecode on {chain.name}" if has_code
                      else f"No bytecode at {addr} on {chain.name} (claimed deployment not found)"),
            "occurred_at": None, "official": False, "verified": True,
            "data": {"kind": "contract_code", "chain_id": chain_id, "address": addr, "has_code": has_code,
                     "code_size": max(0, (len(code) - 2) // 2) if code else 0},
        })
        if not has_code:
            return b
        head = cli.block_number()
        lookback = int(campaign.get("freshness_days", 120) * 86400 / chain.block_time)
        step = self.log_range_override or chain.max_log_range
        start_floor = max(0, head - lookback)
        to_block, requests, logs = head, 0, []
        while to_block >= start_floor and requests < self.max_requests:
            frm = max(start_floor, to_block - step + 1)
            logs.extend(cli.get_logs(addr, frm, to_block, topics=[WATCH_TOPICS], max_range=step))
            requests += 1
            to_block = frm - 1
        b["facts"].setdefault("scan", {})[f"{chain_id}:{addr}"] = {
            "head": head, "scanned_from": to_block + 1, "requests": requests,
            "complete": to_block < start_floor}
        blocks: dict[int, dict | None] = {}
        for raw in logs:
            ev = decode_log(raw, chain_id)
            if ev is None:
                continue
            if ev.block_number not in blocks:
                blocks[ev.block_number] = cli.get_block(ev.block_number)
            blk = blocks[ev.block_number]
            ev.timestamp = block_timestamp(blk)
            if ev.status != "orphaned":
                ev.status = confirmation_status(chain, ev.block_number, head)
            b["events"].append({
                "chain_id": chain_id, "tx_hash": ev.tx_hash, "log_index": ev.log_index,
                "block_number": ev.block_number, "block_hash": ev.block_hash, "address": ev.address,
                "name": ev.name, "args": ev.args, "timestamp": iso(ev.timestamp), "status": ev.status})
        return b
