"""Two independent, explainable scores (spec §5) + data confidence + decision rule.

  L = 0.30F + 0.25B + 0.20C + 0.15D + 0.10R      (commercial lead fit)
  O = 0.35N + 0.25T + 0.20E + 0.20Q              (opportunity: need, timing, relevance, quality)

All components are 0..100. Weights are starting points and can be overridden per call.
Decision rule: a high score alone is never enough. Uncertain identity, flagged content,
low data confidence, missing contact path or no first-party/verified signal => review."""
from __future__ import annotations

from datetime import timedelta

from .models import Campaign, Evidence, Project, ScoreBreakdown, Signal, utcnow
from .signals import evidence_quality

LEAD_WEIGHTS = {"F": 0.30, "B": 0.25, "C": 0.20, "D": 0.15, "R": 0.10}
OPP_WEIGHTS = {"N": 0.35, "T": 0.25, "E": 0.20, "Q": 0.20}
BLOCKING_FLAGS = {"prompt_injection_suspected", "hidden_characters", "signal_flood"}


def _clip(x: float) -> float:
    return round(max(0.0, min(100.0, x)), 1)


def _kw_hits(text: str, keywords: list[str]) -> int:
    t = text.lower()
    return sum(1 for k in keywords if k.lower() in t)


def freshness(dt, horizon_days: int) -> float:
    if dt is None:
        return 30.0
    age = (utcnow() - dt).total_seconds() / 86400
    return _clip(100 * (1 - age / max(1, horizon_days)))


def commercial_fit(p: Project, evidence: list[Evidence], signals: list[Signal], c: Campaign, facts: dict,
                   weights: dict | None = None) -> ScoreBreakdown:
    w = weights or LEAD_WEIGHTS
    reasons: list[str] = []
    # F – market fit -----------------------------------------------------------
    sector_hit = set(s.lower() for s in p.sectors) & set(s.lower() for s in c.target_sectors)
    chain_hit = set(p.chains) & set(c.target_chains)
    text = " ".join([p.description, " ".join(facts.get("topics", [])), " ".join(p.sectors)])
    kw = _kw_hits(text, c.offer.keywords)
    F = (60 if sector_hit else 10) + (25 if chain_hit else 0) + 15 * min(1, kw / 2)
    reasons.append(f"F: sectors {sorted(sector_hit) or 'none matched'}, target chains {sorted(chain_hit) or 'none'}, "
                   f"{kw} offer keywords in description/topics")
    # B – business model / budget likelihood --------------------------------
    types = {s.type: s for s in signals}
    B = 25.0
    if "funding" in types:
        B += 35 * types["funding"].quality / 100
        reasons.append("B: funding signal (weighted by evidence quality)")
    if "hiring" in types:
        B += 20
        reasons.append("B: hiring signal")
    if any(e.data.get("authors_90d", 0) >= 3 for e in evidence):
        B += 10
    if facts.get("stars", 0) >= 100:
        B += 10
    # C – credible access to a decision maker ----------------------------------
    off = [x for x in p.contact_channels if x.get("official")]
    C = 0.0
    if any(x["type"] == "email" for x in off):
        C += 35
    C += min(30, 15 * sum(1 for x in off if x["type"] in ("x", "discord", "telegram", "linkedin")))
    if p.people:
        C += 30
    if any(x.get("enriched") and x.get("official") for x in p.contact_channels):
        C += 15
    reasons.append(f"C: {len(off)} official contact channels, {len(p.people)} named people from official pages")
    # D – data quality & freshness -------------------------------------------
    qs = [evidence_quality(e) for e in evidence] or [0]
    newest = max((e.occurred_at for e in evidence if e.occurred_at), default=None)
    D = 0.6 * (sum(qs) / len(qs)) + 0.4 * freshness(newest, 90)
    # R – relevance of the offer to the project's needs ------------------------
    rel = max((c.offer.signal_relevance.get(s.type, 0) * s.strength / 100 for s in signals), default=0)
    R = 0.7 * rel + 0.3 * min(100, kw * 40)
    comps = {"F": _clip(F), "B": _clip(B), "C": _clip(C), "D": _clip(D), "R": _clip(R)}
    total = _clip(sum(w[k] * comps[k] for k in w))
    return ScoreBreakdown(total, comps, w, reasons)


def opportunity(signals: list[Signal], c: Campaign, weights: dict | None = None) -> ScoreBreakdown:
    w = weights or OPP_WEIGHTS
    if not signals:
        return ScoreBreakdown(0.0, {k: 0.0 for k in w}, w, ["no current signals"])
    rel = lambda s: c.offer.signal_relevance.get(s.type, 0)  # noqa: E731
    ranked = sorted(signals, key=lambda s: s.strength * rel(s) * s.quality, reverse=True)
    top = ranked[:3]
    # N – combined need evidence with diminishing returns
    prod = 1.0
    for s in ranked[:5]:
        prod *= 1 - 0.85 * (s.strength / 100) * (s.quality / 100)
    N = 100 * (1 - prod)
    T = freshness(top[0].occurred_at, c.freshness_days)
    E = sum(rel(s) for s in top[:2]) / len(top[:2])
    Q = sum(s.quality for s in top) / len(top)
    comps = {"N": _clip(N), "T": _clip(T), "E": _clip(E), "Q": _clip(Q)}
    total = _clip(sum(w[k] * comps[k] for k in w))
    return ScoreBreakdown(total, comps, w, [f"top signal: {top[0].type} — {top[0].observation[:120]}"])


def data_confidence(p: Project, signals: list[Signal], flags: list[str]) -> float:
    sq = sum(s.quality for s in signals) / len(signals) if signals else 30
    conf = 0.55 * p.identity_confidence + 0.45 * sq
    if set(flags) & BLOCKING_FLAGS:
        conf -= 40
    return _clip(conf)


def decide(p: Project, L: ScoreBreakdown, O: ScoreBreakdown, conf: float, signals: list[Signal],
           evidence: list[Evidence], flags: list[str], c: Campaign) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if p.identity_confidence < c.min_identity_confidence:
        return "review", [f"uncertain_identity ({p.identity_confidence:.0f} < {c.min_identity_confidence:.0f})"]
    blocking = sorted(set(flags) & BLOCKING_FLAGS)
    if blocking:
        return "review", [f"untrusted content flagged: {', '.join(blocking)} — scores may be manipulated"]
    if L.total < c.min_lead_score:
        reasons.append(f"lead score {L.total} < {c.min_lead_score}")
    if O.total < c.min_opportunity_score:
        reasons.append(f"opportunity score {O.total} < {c.min_opportunity_score}")
    if reasons:
        near_miss = (O.total >= c.min_opportunity_score and L.total >= c.min_lead_score - 10
                     and L.components.get("C", 0) == 0)
        if near_miss:
            return "review", [f"strong opportunity but no verified contact path (lead score {L.total})"]
        return "rejected", reasons
    by_id = {e.id: e for e in evidence}
    if not any(any(by_id[i].official or by_id[i].verified for i in s.evidence_ids if i in by_id) for s in signals):
        reasons.append("no first-party or verified signal")
    if conf < c.min_data_confidence:
        reasons.append(f"data confidence {conf} < {c.min_data_confidence}")
    if not any(x.get("official") for x in p.contact_channels):
        reasons.append("no verified public contact path")
    if reasons:
        return "review", reasons
    return "qualified", ["meets lead, opportunity, identity and data-confidence thresholds"]


def rank_key(lead) -> tuple:
    order = {"qualified": 0, "review": 1, "rejected": 2}[lead.status]
    return (order, -(0.5 * lead.lead_score.total + 0.5 * lead.opportunity_score.total), -lead.data_confidence)
