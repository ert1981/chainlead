"""Minimal read-only EVM JSON-RPC client.

Deliberately exposes ONLY read methods. There is no eth_sendTransaction / eth_sign /
eth_sendRawTransaction here and no place to put a private key (spec §11)."""
from __future__ import annotations

import itertools
from typing import Any

from .. import http

READ_ONLY_METHODS = frozenset({
    "eth_chainId", "eth_blockNumber", "eth_getCode", "eth_getLogs", "eth_getBlockByNumber",
    "eth_getBlockByHash", "eth_getTransactionReceipt", "eth_call",
})


class RpcError(Exception):
    pass


class RpcClient:
    _ids = itertools.count(1)

    def __init__(self, url: str, timeout: float = 15, user_agent: str = "ChainLead/0.1") -> None:
        self.url = url
        self.timeout = timeout
        self.user_agent = user_agent

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        if method not in READ_ONLY_METHODS:
            raise RpcError(f"method {method} is not allowed: ChainLead is strictly read-only")
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        try:
            resp = http.post_json(self.url, body, timeout=self.timeout, user_agent=self.user_agent)
        except http.HttpError as e:
            raise RpcError(str(e)) from e
        if not isinstance(resp, dict):
            raise RpcError("malformed JSON-RPC response")
        if resp.get("error"):
            raise RpcError(f"{method}: {resp['error']}")
        return resp.get("result")

    # convenience -----------------------------------------------------------
    def chain_id(self) -> int:
        return int(self.call("eth_chainId"), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber"), 16)

    def get_code(self, address: str, block: str = "latest") -> str:
        return self.call("eth_getCode", [address, block]) or "0x"

    def get_block(self, number: int | str) -> dict | None:
        tag = hex(number) if isinstance(number, int) else number
        return self.call("eth_getBlockByNumber", [tag, False])

    def get_logs(self, address: str | list[str], from_block: int, to_block: int,
                 topics: list | None = None, max_range: int = 2_000) -> list[dict]:
        """Paginates over block ranges so public RPC range limits are respected."""
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + max_range - 1, to_block)
            flt: dict[str, Any] = {"address": address, "fromBlock": hex(start), "toBlock": hex(end)}
            if topics:
                flt["topics"] = topics
            out.extend(self.call("eth_getLogs", [flt]) or [])
            start = end + 1
        return out
