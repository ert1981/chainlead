"""Core domain models.

The engine keeps three ideas strictly apart (see spec §3):
  * Observation  – something that verifiably happened (a release, a deploy, a job post).
  * Inference    – what that observation *might* indicate.
  * Opportunity  – a commercial hypothesis; only proposed when evidence is sufficient,
                   and always flagged for human/agent review, never treated as proven intent.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- offers
SIGNAL_TYPES = (
    "release",            # new protocol / software version
    "new_chain",          # deployment or announced support on a new network
    "contract_deploy",    # new / upgraded contract observed on-chain
    "hiring",             # relevant job post (e.g. Solidity engineer)
    "audit_published",    # new security audit report
    "integration",        # new integration announced
    "funding",            # funding announcement
    "dev_activity",       # sustained development activity
)


@dataclass
class Offer:
    """What the user wants to sell. Without it, 'a good lead' has no meaning (spec §5.1)."""
    key: str
    title: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    # relevance (0-100) of each signal type for this offer -> drives E and R
    signal_relevance: dict[str, float] = field(default_factory=dict)


OFFER_CATALOG: dict[str, Offer] = {
    "smart_contract_dev": Offer(
        "smart_contract_dev", "Smart-contract development",
        "Solidity/EVM contract engineering, upgrades and tooling.",
        ["solidity", "smart contract", "evm", "foundry", "hardhat", "upgrade", "vyper"],
        {"release": 70, "new_chain": 75, "contract_deploy": 85, "hiring": 95, "audit_published": 50,
         "integration": 55, "funding": 60, "dev_activity": 50},
    ),
    "bridge_dev": Offer(
        "bridge_dev", "Cross-chain / bridge integration",
        "Multi-chain expansion, bridges, messaging and chain integrations.",
        ["bridge", "cross-chain", "crosschain", "multichain", "multi-chain", "layerzero", "ccip",
         "wormhole", "hyperlane", "l2", "rollup", "interop"],
        {"release": 55, "new_chain": 95, "contract_deploy": 70, "hiring": 65, "audit_published": 35,
         "integration": 85, "funding": 55, "dev_activity": 40},
    ),
    "security_audit": Offer(
        "security_audit", "Security audit",
        "Smart-contract audits, reviews, invariant testing and monitoring.",
        ["audit", "security", "invariant", "fuzz", "formal verification", "bug bounty", "exploit"],
        {"release": 85, "new_chain": 70, "contract_deploy": 90, "hiring": 45, "audit_published": 60,
         "integration": 55, "funding": 70, "dev_activity": 45},
    ),
    "onchain_analytics": Offer(
        "onchain_analytics", "On-chain analytics & monitoring",
        "Dashboards, indexers, subgraphs and alerting.",
        ["analytics", "subgraph", "indexer", "dashboard", "monitoring", "dune", "the graph", "metrics"],
        {"release": 55, "new_chain": 70, "contract_deploy": 60, "hiring": 55, "audit_published": 30,
         "integration": 70, "funding": 60, "dev_activity": 40},
    ),
}


def resolve_offer(offer: str | dict | Offer) -> Offer:
    if isinstance(offer, Offer):
        return offer
    if isinstance(offer, str):
        if offer not in OFFER_CATALOG:
            raise ValueError(f"unknown offer '{offer}'. Known: {sorted(OFFER_CATALOG)}")
        return OFFER_CATALOG[offer]
    base = OFFER_CATALOG.get(offer.get("key", ""), None)
    rel = dict(base.signal_relevance) if base else {t: 50.0 for t in SIGNAL_TYPES}
    rel.update(offer.get("signal_relevance", {}))
    return Offer(
        key=offer.get("key", "custom"),
        title=offer.get("title", base.title if base else "Custom offer"),
        description=offer.get("description", base.description if base else ""),
        keywords=list(offer.get("keywords", base.keywords if base else [])),
        signal_relevance=rel,
    )


# --------------------------------------------------------------------------- campaign
@dataclass
class Campaign:
    name: str
    offer: Offer
    id: str = field(default_factory=lambda: new_id("cmp"))
    tenant: str = "local"
    target_sectors: list[str] = field(default_factory=lambda: ["defi", "infrastructure"])
    target_chains: list[int] = field(default_factory=lambda: [8453])          # Base by default
    allowed_sources: list[str] = field(default_factory=lambda: ["mock"])
    # discovery inputs
    github_queries: list[str] = field(default_factory=list)                 # GitHub search queries
    seed_projects: list[dict] = field(default_factory=list)                 # explicit seeds
    max_candidates: int = 25
    # economics
    budget_usd: float = 1.0
    max_leads: int = 10
    # decision thresholds (0-100)
    min_identity_confidence: float = 60
    min_lead_score: float = 55
    min_opportunity_score: float = 50
    min_data_confidence: float = 55
    freshness_days: int = 120
    status: str = "draft"
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = iso(self.created_at)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Campaign":
        d = dict(d)
        d["offer"] = resolve_offer(d.get("offer", "smart_contract_dev"))
        if isinstance(d.get("created_at"), str):
            d["created_at"] = parse_dt(d["created_at"]) or utcnow()
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown campaign fields: {sorted(unknown)}")
        return cls(**d)


# --------------------------------------------------------------------------- entities
@dataclass
class Evidence:
    project_key: str
    source_type: str          # github | website | rpc | explorer | graph | mock | manual
    url: str
    claim: str                # what was observed, in plain words
    observed_at: datetime = field(default_factory=utcnow)
    occurred_at: datetime | None = None   # when the underlying event happened
    official: bool = False    # published by the project itself (repo/site/its contract)
    verified: bool = False    # cross-checked by an independent fetch
    data: dict = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "ev_" + stable_hash(self.project_key, self.source_type, self.url, self.claim)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["observed_at"] = iso(self.observed_at)
        d["occurred_at"] = iso(self.occurred_at)
        return d


@dataclass
class Contract:
    chain_id: int
    address: str
    label: str = ""
    verified_source: bool = False
    linked_by: str = ""        # how the contract was tied to the project (url of proof)

    def __post_init__(self) -> None:
        self.address = self.address.lower()

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.address}"


@dataclass
class ChainEvent:
    chain_id: int
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    address: str
    name: str
    args: dict = field(default_factory=dict)
    timestamp: datetime | None = None
    status: str = "pending"   # pending | safe | finalized | orphaned

    @property
    def key(self) -> str:   # logical unique key (spec §7)
        return f"{self.chain_id}:{self.tx_hash.lower()}:{self.log_index}"


@dataclass
class Person:
    name: str
    role: str
    source_url: str
    public_handle: str = ""   # only handles published by the project itself


@dataclass
class Project:
    name: str
    key: str = ""
    website: str | None = None
    description: str = ""
    sectors: list[str] = field(default_factory=list)
    chains: list[int] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)          # "owner/repo"
    github_org: str | None = None
    contracts: list[Contract] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    contact_channels: list[dict] = field(default_factory=list)   # public, project-published
    sources: list[str] = field(default_factory=list)
    identity_confidence: float = 0.0
    identity_notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.key:
            self.key = slugify(self.github_org or self.name)


@dataclass
class Signal:
    type: str
    observation: str
    inference: str
    evidence_ids: list[str]
    occurred_at: datetime | None
    strength: float            # 0-100, how strong the observation itself is
    quality: float             # 0-100, quality of the backing evidence
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "sig_" + stable_hash(self.type, self.observation, sorted(self.evidence_ids))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["occurred_at"] = iso(self.occurred_at)
        return d


@dataclass
class ScoreBreakdown:
    total: float
    components: dict[str, float]
    weights: dict[str, float]
    reasons: list[str] = field(default_factory=list)


@dataclass
class Lead:
    campaign_id: str
    project: Project
    lead_score: ScoreBreakdown
    opportunity_score: ScoreBreakdown
    data_confidence: float
    signals: list[Signal]
    evidence: list[Evidence]
    status: str                 # qualified | review | rejected
    status_reasons: list[str]
    rank: int = 0
    cost_usd: float = 0.0
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = "lead_" + stable_hash(self.campaign_id, self.project.key)
