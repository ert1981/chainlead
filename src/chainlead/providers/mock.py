"""Offline provider backed by a FICTIONAL fixture ecosystem (data/mock_ecosystem.json).

Used for demos, tests and CI. It deliberately contains hard cases: a duplicate project,
an identity-ambiguous project that claims another project's contract, a prompt-injection
/ release-spam project, a reorged event, a stale project, an off-sector project and a
project with no public contact path. `MockRpcClient` speaks the same interface as the real
read-only RPC client so the on-chain code path is exercised end-to-end."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from importlib import resources

from ..chain.event_decoder import KNOWN_EVENTS
from ..chain.registry import get_chain
from ..models import iso, utcnow
from ..safety import sanitize_text
from .base import Provider, empty_bundle

_NAME_TO_TOPIC = {}
for _topic, (_name, _spec) in KNOWN_EVENTS.items():
    _NAME_TO_TOPIC.setdefault(_name, (_topic, _spec))   # first = uint8 Initialized


def load_fixture(path: str | None = None) -> dict:
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.loads(resources.files("chainlead").joinpath("data/mock_ecosystem.json").read_text("utf-8"))


def _days(d):
    return None if d is None else iso(utcnow() - timedelta(days=d))


class MockProvider(Provider):
    name = "mock"
    capabilities = frozenset({"discover", "evidence", "contacts", "verify"})
    costs = {"discover": 0.0, "evidence": 0.0, "contacts": 0.02, "verify": 0.005}

    def __init__(self, fixture: dict | None = None) -> None:
        self.data = fixture or load_fixture()
        self._by_key = {p["key"]: p for p in self.data["projects"]}

    def discover(self, campaign: dict) -> list[dict]:
        out = []
        for p in self.data["projects"]:
            desc, _ = sanitize_text(p.get("description", ""), 300)
            out.append({"name": p["name"], "github_org": p.get("github_org"), "repos": list(p.get("repos", [])),
                        "website": p.get("website"), "description": desc, "sectors": list(p.get("sectors", [])),
                        "chains": list(p.get("chains", [])), "source_url": f"mock://{p['key']}",
                        "mock_key": p["key"]})
        return out[: int(campaign.get("max_candidates", 100))]

    def _src(self, candidate: dict) -> dict:
        return self._by_key.get(candidate.get("mock_key", ""), {})

    def evidence(self, candidate: dict, campaign: dict) -> dict:
        p = self._src(candidate)
        b = empty_bundle()
        if not p:
            return b
        _, dflags = sanitize_text(p.get("description", ""))
        for e in p.get("evidence", []):
            data = dict(e.get("data", {}))
            data["kind"] = e["kind"]
            _, fl = sanitize_text(data.get("excerpt", "") + " " + e["claim"])
            if fl:
                data["flags"] = fl
            for i in range(int(e.get("repeat", 1))):
                b["evidence"].append({
                    "source_type": e["source_type"], "url": e["url"] + (f"#{i}" if e.get("repeat") else ""),
                    "claim": e["claim"] + (f" (#{i + 1})" if e.get("repeat") else ""),
                    "occurred_at": _days(e.get("days_ago")), "official": e.get("official", False),
                    "verified": e.get("verified", False), "data": data})
        b["contracts"] = [dict(c) for c in p.get("contracts", [])]
        b["people"] = [dict(x) for x in p.get("people", [])]
        b["contacts"] = [dict(x) for x in p.get("contacts", [])]
        b["links"] = json.loads(json.dumps(p.get("links", {})))
        facts = dict(p.get("facts", {}))
        flags = set(facts.get("flags", [])) | set(dflags)
        for e in b["evidence"]:
            flags |= set(e["data"].get("flags", []))
        if flags:
            facts["flags"] = sorted(flags)
        b["facts"] = facts
        return b

    def contacts(self, candidate: dict, campaign: dict) -> dict:
        b = empty_bundle()
        b["contacts"] = [dict(x) for x in self._src(candidate).get("paid_contacts", [])]
        return b

    def verify(self, contract: dict, campaign: dict) -> dict:
        b = empty_bundle()
        key = f"{contract['chain_id']}:{contract['address'].lower()}"
        for p in self.data["projects"]:
            if key in p.get("verify", {}):
                ok = p["verify"][key]
                b["evidence"].append({
                    "source_type": "explorer", "url": f"mock-explorer://{key}",
                    "claim": f"Source code of {contract['address']} is {'verified' if ok else 'NOT verified'} on the explorer",
                    "occurred_at": None, "official": False, "verified": True,
                    "data": {"kind": "contract_verification", "chain_id": contract["chain_id"],
                             "address": contract["address"].lower(), "verified_source": ok}})
                b["facts"]["verified_contracts"] = {key: ok}
                break
        return b


class MockRpcClient:
    """In-memory EVM node for one chain, built from the fixture."""

    def __init__(self, fixture: dict, chain_id: int) -> None:
        self.chain = get_chain(chain_id)
        c = fixture["chain"]
        self.head = int(c["heads"].get(str(chain_id), 1_000_000))
        self.code = {k.split(":", 1)[1]: v for k, v in c["code"].items() if k.startswith(f"{chain_id}:")}
        self.now = utcnow()
        self.logs = []
        for i, lg in enumerate(x for x in c["logs"] if x["chain_id"] == chain_id):
            ago = lg.get("blocks_ago")
            if ago is None:
                ago = int(lg["days_ago"] * 86400 / self.chain.block_time)
            bn = self.head - ago
            topic0, spec = _NAME_TO_TOPIC[lg["event"]]
            topics, data = [topic0], "0x"
            for arg, typ, indexed in spec:
                v = lg["args"][arg]
                word = (v[2:].rjust(64, "0") if typ == "address" else format(int(v), "064x"))
                if indexed:
                    topics.append("0x" + word)
                else:
                    data += word
            bh = self.block_hash(bn)
            if lg.get("reorged"):
                bh = "0x" + hashlib.sha256(f"stale:{bn}".encode()).hexdigest()
            self.logs.append({
                "address": lg["address"].lower(), "topics": topics, "data": data,
                "blockNumber": hex(bn), "blockHash": bh,
                "transactionHash": "0x" + hashlib.sha256(f"tx:{chain_id}:{i}:{lg['address']}".encode()).hexdigest(),
                "logIndex": hex(lg.get("log_index", 0)), "removed": False})
        self.calls: dict[str, int] = {}

    def _count(self, m: str) -> None:
        self.calls[m] = self.calls.get(m, 0) + 1

    def block_hash(self, n: int) -> str:
        return "0x" + hashlib.sha256(f"{self.chain.chain_id}:{n}".encode()).hexdigest()

    def chain_id(self) -> int:
        return self.chain.chain_id

    def block_number(self) -> int:
        self._count("eth_blockNumber")
        return self.head

    def get_code(self, address: str, block: str = "latest") -> str:
        self._count("eth_getCode")
        return self.code.get(address.lower(), "0x")

    def get_block(self, number):
        self._count("eth_getBlockByNumber")
        n = int(number, 16) if isinstance(number, str) else int(number)
        if n > self.head:
            return None
        ts = self.now - timedelta(seconds=(self.head - n) * self.chain.block_time)
        return {"number": hex(n), "hash": self.block_hash(n), "timestamp": hex(int(ts.timestamp()))}

    def get_logs(self, address, from_block: int, to_block: int, topics=None, max_range: int = 0):
        self._count("eth_getLogs")
        addrs = {a.lower() for a in (address if isinstance(address, list) else [address])}
        wanted = set(topics[0]) if topics and isinstance(topics[0], list) else None
        return [lg for lg in self.logs
                if lg["address"] in addrs and from_block <= int(lg["blockNumber"], 16) <= to_block
                and (wanted is None or lg["topics"][0] in wanted)]
