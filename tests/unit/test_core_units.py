from datetime import timedelta

import pytest

from chainlead.chain.event_decoder import EVENT_SIGNATURES, KNOWN_EVENTS, decode_log
from chainlead.chain.finality import confirmation_status, reconcile_reorgs
from chainlead.chain.registry import chains_mentioned, get_chain
from chainlead.identity import (build_project, deduplicate, domain, identity_confidence,
                                resolve_contract_conflicts)
from chainlead.models import (OFFER_CATALOG, Campaign, ChainEvent, Contract, Evidence, Project, Signal,
                              ScoreBreakdown, utcnow)
from chainlead.scoring import LEAD_WEIGHTS, OPP_WEIGHTS, commercial_fit, decide, freshness, opportunity
from chainlead.signals import detect_signals
from chainlead.storage import Storage


# ------------------------------------------------------------------ event decoding
def _keccak(sig: str) -> str | None:
    try:
        from Crypto.Hash import keccak
    except ImportError:
        return None
    k = keccak.new(digest_bits=256)
    k.update(sig.encode())
    return "0x" + k.hexdigest()


@pytest.mark.parametrize("topic,entry", list(KNOWN_EVENTS.items()))
def test_topic_hashes_are_keccak_of_signature(topic, entry):
    name, spec = entry
    sig = f"{name}({','.join(t for _, t, _ in spec)})"
    h = _keccak(sig)
    if h is None:
        pytest.skip("pycryptodome not installed")
    assert h == topic, sig


def test_decode_upgraded_and_malformed_logs():
    topic = next(t for t, (n, _) in KNOWN_EVENTS.items() if n == "Upgraded")
    log = {"address": "0xABC0000000000000000000000000000000000001", "topics": [topic, "0x" + "0" * 24 + "a" * 40],
           "data": "0x", "blockNumber": "0x10", "blockHash": "0xBB", "transactionHash": "0xCC", "logIndex": "0x2"}
    ev = decode_log(log, 8453)
    assert ev.name == "Upgraded" and ev.args["implementation"] == "0x" + "a" * 40
    assert ev.key == "8453:0xcc:2" and ev.block_number == 16
    assert decode_log({"topics": ["0xdeadbeef"]}, 1) is None
    assert decode_log({**log, "topics": [topic, "0xzz"]}, 1) is None          # garbage never crashes
    assert decode_log({**log, "removed": True}, 1).status == "orphaned"
    assert set(EVENT_SIGNATURES) <= {n for n, _ in KNOWN_EVENTS.values()}


# ------------------------------------------------------------------ registry / finality
def test_chain_mentions_avoid_false_positives():
    assert chains_mentioned("We are live on Base and Arbitrum") == {8453, 42161}
    assert 8453 not in chains_mentioned("database layer and code base improvements")
    assert chains_mentioned("Deployed to Ethereum mainnet") == {1}
    assert chains_mentioned("") == set()


def test_confirmation_status_and_reorg_reconcile():
    base = get_chain(8453)
    assert confirmation_status(base, 100, 110) == "pending"
    assert confirmation_status(base, 100, 100 + base.safe_depth) == "safe"
    assert confirmation_status(base, 100, 100 + base.finalized_depth) == "finalized"
    st = Storage(":memory:")
    good = ChainEvent(8453, "0xaa", 0, 1000, "0xgood", "0x1", "Upgraded", status="pending")
    bad = ChainEvent(8453, "0xbb", 0, 1001, "0xstale", "0x1", "Upgraded", status="pending")
    assert st.upsert_event("t", good, "p") is True
    assert st.upsert_event("t", good, "p") is False             # (chain, tx, logIndex) is the identity
    st.upsert_event("t", bad, "p")
    stats = reconcile_reorgs(st, "t", base, 1000 + base.safe_depth, lambda n: "0xgood" if n == 1000 else "0xnew")
    assert stats["orphaned"] == 1 and stats["promoted"] == 1
    status = {e["tx_hash"]: e["status"] for e in st.list_events("t")}
    assert status == {"0xaa": "safe", "0xbb": "orphaned"}


# ------------------------------------------------------------------ identity
def test_domain_and_dedup():
    assert domain("https://www.Foo.example/path") == "foo.example"
    cands = [{"name": "A", "github_org": "a-org", "website": "https://a.example", "repos": ["a-org/x"]},
             {"name": "A dir", "github_org": None, "website": "https://www.a.example/", "repos": []},
             {"name": "B", "github_org": "b-org", "website": None, "repos": ["b-org/y"]},
             {"name": "A2", "github_org": "A-ORG", "website": None, "repos": ["a-org/z"]}]
    merged = deduplicate(cands)
    assert len(merged) == 2
    a = next(m for m in merged if m["github_org"] and m["github_org"].lower() == "a-org")
    assert set(a["repos"]) == {"a-org/x", "a-org/z"} and len(a["members"]) == 3


def test_identity_requires_bidirectional_links_and_rejects_prefix_spoof():
    cand = {"name": "Lumen", "github_org": "lumen", "website": "https://lumen.example", "repos": []}
    strong = {"links": {"site_links_github": ["lumen"], "repo_homepages": ["https://lumen.example"]},
              "contracts": [{"chain_id": 8453, "address": "0x" + "1" * 40, "linked_by": "https://github.com/lumen/core#readme"}],
              "evidence": [], "facts": {}, "people": [], "contacts": []}
    p = build_project(cand, strong)
    conf, notes = identity_confidence(p, strong, 1)
    assert conf == 90
    spoof = dict(strong, contracts=[{"chain_id": 8453, "address": "0x" + "2" * 40,
                                     "linked_by": "https://github.com/lumen-evil/x#readme"}],
                 links={"site_links_github": [], "repo_homepages": []},
                 contacts=[{"type": "email", "value": "x@evil.example", "source_url": "https://github.com/lumen-evil"}])
    p2 = build_project(cand, spoof)
    conf2, _ = identity_confidence(p2, spoof, 1)
    assert conf2 == 30
    assert p2.contact_channels[0]["official"] is False


def test_contract_conflict_penalises_weaker_claimant():
    a = Project("A", github_org="a", contracts=[Contract(8453, "0x" + "1" * 40)])
    b = Project("B", github_org="b", contracts=[Contract(8453, "0x" + "1" * 40)])
    a.identity_confidence, b.identity_confidence = 90, 70
    resolve_contract_conflicts([a, b])
    assert len(a.contracts) == 1 and b.contracts == [] and b.identity_confidence == 35


# ------------------------------------------------------------------ signals
def _camp(**kw):
    return Campaign(name="t", offer=OFFER_CATALOG["bridge_dev"], **kw)


def _ev(kind, days=None, **data):
    return Evidence("p", "github", f"https://x/{kind}/{days}/{data}", f"{kind} {days} {data}",
                    occurred_at=(utcnow() - timedelta(days=days)) if days is not None else None,
                    official=True, verified=True, data={"kind": kind, **data})


def test_signals_freshness_flood_cap_and_orphans():
    c = _camp(freshness_days=60)
    evs = [_ev("release", 5, chains=[8453]), _ev("release", 400, chains=[8453]),
           _ev("contract_event", 2, name="Upgraded", status="orphaned", chain_id=8453),
           _ev("contract_event", 2, name="Initialized", status="finalized", chain_id=8453)]
    sigs, flags = detect_signals(evs, c, {1, 8453})
    types = sorted(s.type for s in sigs)
    assert types == ["contract_deploy", "new_chain", "new_chain", "release"]
    assert flags == []
    spam = [_ev("release", 1, n=i) for i in range(30)]
    sigs, flags = detect_signals(spam, c, {8453})
    assert flags == ["signal_flood"] and len([s for s in sigs if s.type == "release"]) == 3


def test_release_is_not_new_chain_for_single_chain_project():
    sigs, _ = detect_signals([_ev("release", 3, chains=[8453])], _camp(), {8453})
    assert [s.type for s in sigs] == ["release"]


# ------------------------------------------------------------------ scoring
def test_score_formulas_match_spec():
    assert LEAD_WEIGHTS == {"F": 0.30, "B": 0.25, "C": 0.20, "D": 0.15, "R": 0.10}
    assert OPP_WEIGHTS == {"N": 0.35, "T": 0.25, "E": 0.20, "Q": 0.20}
    c = _camp()
    p = Project("P", sectors=["defi"], chains=[8453], description="bridge for cross-chain")
    p.contact_channels = [{"type": "email", "value": "a@p.example", "official": True}]
    evs = [_ev("hiring", 3, title="Solidity Engineer", keywords=["solidity"])]
    sigs, _ = detect_signals(evs, c, {8453})
    L = commercial_fit(p, evs, sigs, c, {})
    assert L.total == round(sum(LEAD_WEIGHTS[k] * L.components[k] for k in LEAD_WEIGHTS), 1)
    O = opportunity(sigs, c)
    assert O.total == round(sum(OPP_WEIGHTS[k] * O.components[k] for k in OPP_WEIGHTS), 1)
    assert opportunity([], c).total == 0
    assert freshness(None, 90) == 30 and freshness(utcnow(), 90) == 100
    assert freshness(utcnow() - timedelta(days=200), 90) == 0


def test_decision_rule_high_score_is_not_enough():
    c = _camp()
    hi = ScoreBreakdown(95, {"C": 80}, {})
    sig = Signal("hiring", "o", "i", ["e1"], utcnow(), 90, 90)
    ev = Evidence("p", "github", "u", "c", official=True, verified=True)
    sig.evidence_ids = [ev.id]
    p = Project("P")
    p.contact_channels = [{"type": "email", "value": "x", "official": True}]
    p.identity_confidence = 40
    assert decide(p, hi, hi, 95, [sig], [ev], [], c)[0] == "review"                      # uncertain identity
    p.identity_confidence = 95
    assert decide(p, hi, hi, 95, [sig], [ev], ["prompt_injection_suspected"], c)[0] == "review"
    assert decide(p, hi, hi, 30, [sig], [ev], [], c)[0] == "review"                      # low data confidence
    assert decide(p, hi, hi, 95, [sig], [ev], [], c)[0] == "qualified"
    p.contact_channels = []
    assert decide(p, hi, hi, 95, [sig], [ev], [], c)[0] == "review"                      # no contact path
    lo = ScoreBreakdown(10, {"C": 0}, {})
    assert decide(p, lo, lo, 95, [sig], [ev], [], c)[0] == "rejected"


def test_uncertain_identity_with_failing_scores_is_rejected_not_reviewed():
    from chainlead.models import Campaign, Project, ScoreBreakdown, resolve_offer
    from chainlead.scoring import decide
    c = Campaign(name="t", offer=resolve_offer("bridge_dev"))
    p = Project(name="weak", identity_confidence=15)
    low = ScoreBreakdown(20.0, {"C": 0}, {})
    status, reasons = decide(p, low, ScoreBreakdown(0.0, {}, {}), 30, [], [], [], c)
    assert status == "rejected" and any("uncertain_identity" in r for r in reasons)
    status, _ = decide(p, low, ScoreBreakdown(80.0, {}, {}), 30, [], [], [], c)
    assert status == "review"
    status, _ = decide(p, low, ScoreBreakdown(0.0, {}, {}), 30, [], [], ["prompt_injection_suspected"], c)
    assert status == "review"
