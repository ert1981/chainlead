"""Decode raw EVM logs into structured ChainEvents for the small set of events that
matter commercially (upgrades, ownership/admin changes, initialisation, pauses).
Topic hashes are keccak256 of the canonical signature (verified in tests)."""
from __future__ import annotations

from datetime import datetime, timezone

from ..models import ChainEvent

# topic0 -> (name, [(arg, type, indexed)])
KNOWN_EVENTS: dict[str, tuple[str, list[tuple[str, str, bool]]]] = {
    "0xbc7cd75a20ee27fd9adebab32041f755214dbc6bffa90cc0225b39da2e5c2d3b":
        ("Upgraded", [("implementation", "address", True)]),
    "0x1cf3b03a6cf19fa2baba4df148e9dcabedea7f8a5c07840e207e5c089be95d3e":
        ("BeaconUpgraded", [("beacon", "address", True)]),
    "0x7e644d79422f17c01e4894b5f4f588d331ebfa28653d42ae832dc59e38c9798f":
        ("AdminChanged", [("previousAdmin", "address", False), ("newAdmin", "address", False)]),
    "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0":
        ("OwnershipTransferred", [("previousOwner", "address", True), ("newOwner", "address", True)]),
    "0x7f26b83ff96e1f2b6a682f133852f6798a09c465da95921460cefb3847402498":
        ("Initialized", [("version", "uint8", False)]),
    "0xc7f505b2f371ae2175ee4913f4499e1f2633a7b5936321eed1cdaeb6115181d2":
        ("Initialized", [("version", "uint64", False)]),
    "0x62e78cea01bee320cd4e420270b5ea74000d11b0c9f74754ebdbfc544b05a258":
        ("Paused", [("account", "address", False)]),
    "0x5db9ee0a495bf2e6ff9c91a7834c1ba4fdd244a5e8aa4e537bd38aeae4b073aa":
        ("Unpaused", [("account", "address", False)]),
}

EVENT_SIGNATURES = {
    "Upgraded": "Upgraded(address)",
    "BeaconUpgraded": "BeaconUpgraded(address)",
    "AdminChanged": "AdminChanged(address,address)",
    "OwnershipTransferred": "OwnershipTransferred(address,address)",
    "Initialized": "Initialized(uint8)",
    "Paused": "Paused(address)",
    "Unpaused": "Unpaused(address)",
}

# events that indicate "the deployed system changed" -> commercially meaningful
DEPLOYMENT_EVENTS = {"Upgraded", "BeaconUpgraded", "AdminChanged", "Initialized"}


def _word(hexdata: str, i: int) -> str:
    return hexdata[2 + 64 * i: 2 + 64 * (i + 1)]


def _decode(value_hex: str, typ: str) -> str | int:
    if len(value_hex) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value_hex):
        raise ValueError("bad abi word")
    if typ == "address":
        return "0x" + value_hex[-40:].lower()
    return int(value_hex, 16)


def decode_log(log: dict, chain_id: int, timestamp: datetime | None = None) -> ChainEvent | None:
    """Returns None for unknown or malformed logs (malicious/garbage input must never crash the run)."""
    try:
        topics = [t.lower() for t in log.get("topics") or []]
        if not topics or topics[0] not in KNOWN_EVENTS:
            return None
        name, spec = KNOWN_EVENTS[topics[0]]
        data = (log.get("data") or "0x").lower()
        args: dict[str, str | int] = {}
        t_i, d_i = 1, 0
        for arg, typ, indexed in spec:
            if indexed:
                args[arg] = _decode(topics[t_i][2:], typ)
                t_i += 1
            else:
                args[arg] = _decode(_word(data, d_i), typ)
                d_i += 1
        if log.get("removed"):
            status = "orphaned"
        else:
            status = "pending"
        return ChainEvent(
            chain_id=chain_id,
            tx_hash=log["transactionHash"].lower(),
            log_index=int(log["logIndex"], 16) if isinstance(log["logIndex"], str) else int(log["logIndex"]),
            block_number=int(log["blockNumber"], 16) if isinstance(log["blockNumber"], str) else int(log["blockNumber"]),
            block_hash=(log.get("blockHash") or "").lower(),
            address=log["address"].lower(),
            name=name, args=args, timestamp=timestamp, status=status,
        )
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return None


def block_timestamp(block: dict | None) -> datetime | None:
    if not block or "timestamp" not in block:
        return None
    ts = block["timestamp"]
    return datetime.fromtimestamp(int(ts, 16) if isinstance(ts, str) else int(ts), tz=timezone.utc)
