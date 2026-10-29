"""Turn observations (evidence + chain events) into commercial signals (spec §8).

Each Signal keeps: the observation (what happened), the inference (what it MIGHT mean)
and the evidence ids behind it. Signals are never statements of purchase intent."""
from __future__ import annotations

from datetime import timedelta

from .chain.registry import CHAINS
from .models import Campaign, Evidence, Signal, utcnow

SOURCE_QUALITY = {"rpc": 90, "explorer": 85, "graph": 80, "github": 75, "website": 70, "news": 40,
                  "manual": 60, "mock": 60}
EVENT_STATUS_QUALITY = {"finalized": 95, "safe": 85, "pending": 55}
MAX_PER_TYPE = 3
FLOOD_THRESHOLD = 10        # > N signals of one type within FLOOD_WINDOW looks like spam
FLOOD_WINDOW = timedelta(days=2)

INFERENCES = {
    "release": "The team is actively shipping; new versions often bring integration, tooling or review needs.",
    "new_chain": "The project is expanding to another network, which may require integration or multi-chain work.",
    "contract_deploy": "Deployed contracts changed recently (new deployment or upgrade); this may create review, "
                       "monitoring or engineering needs.",
    "hiring": "The team is hiring for this skill, which may indicate capacity they could also source externally.",
    "audit_published": "A new audit was published; follow-up reviews or monitoring may be relevant.",
    "integration": "A new integration was announced; technical collaboration may be relevant.",
    "funding": "The project reports new funding; it may have budget for development, growth or infrastructure.",
    "dev_activity": "Sustained development activity indicates an active engineering team.",
}


INFERENCES_FA = {
    "release": "تیم به‌طور فعال نسخه منتشر می‌کند؛ نسخه‌های جدید اغلب نیاز به یکپارچه‌سازی، ابزار یا بازبینی ایجاد می‌کنند.",
    "new_chain": "پروژه در حال گسترش به شبکه‌ای دیگر است که ممکن است به یکپارچه‌سازی یا کار چندزنجیره‌ای نیاز داشته باشد.",
    "contract_deploy": "قراردادهای مستقر اخیراً تغییر کرده‌اند (استقرار یا ارتقای جدید)؛ این ممکن است نیاز به بازبینی، پایش یا مهندسی ایجاد کند.",
    "hiring": "تیم برای این مهارت نیرو استخدام می‌کند؛ ممکن است بخشی از این ظرفیت را از بیرون هم تأمین کند.",
    "audit_published": "گزارش ممیزی تازه‌ای منتشر شده؛ بازبینی تکمیلی یا پایش امنیتی ممکن است مرتبط باشد.",
    "integration": "یکپارچه‌سازی تازه‌ای اعلام شده؛ همکاری فنی ممکن است مرتبط باشد.",
    "funding": "پروژه از تأمین مالی جدید خبر داده؛ ممکن است برای توسعه، رشد یا زیرساخت بودجه داشته باشد.",
    "dev_activity": "فعالیت توسعه‌ای پیوسته نشان‌دهنده تیم مهندسی فعال است.",
}


def evidence_quality(ev: Evidence) -> float:
    q = SOURCE_QUALITY.get(ev.source_type, 50)
    q += 10 if ev.official else 0
    q += 10 if ev.verified else 0
    if not ev.official and not ev.verified:
        q -= 25
    if ev.data.get("flags"):
        q -= 40
    if ev.data.get("kind") == "contract_event":
        q = EVENT_STATUS_QUALITY.get(ev.data.get("status", "pending"), 55)
    return max(0.0, min(100.0, q))


def _chain_names(ids) -> str:
    return ", ".join(CHAINS[i].name if i in CHAINS else str(i) for i in ids)


def detect_signals(evidence: list[Evidence], campaign: Campaign, project_chains_before: set[int]) -> tuple[list[Signal], list[str]]:
    """Returns (signals, flags)."""
    now = utcnow()
    horizon = now - timedelta(days=campaign.freshness_days)
    targets = set(campaign.target_chains)
    offer_kw = [k.lower() for k in campaign.offer.keywords]
    raw: list[Signal] = []
    flags: list[str] = []

    def add(typ, obs, ev, strength, when=None):
        q = evidence_quality(ev)
        raw.append(Signal(type=typ, observation=obs, inference=INFERENCES[typ], evidence_ids=[ev.id],
                          occurred_at=when if when is not None else ev.occurred_at,
                          strength=max(0, min(100, strength)), quality=q))

    for ev in evidence:
        k = ev.data.get("kind")
        if ev.occurred_at and ev.occurred_at < horizon:
            continue                                  # history, not a current signal
        if k == "release":
            add("release", ev.claim, ev, 60 + (10 if not ev.data.get("prerelease") else 0))
            new = set(ev.data.get("chains", [])) & targets
            # only "new" if the project already lives on some other network
            if new and project_chains_before - new:
                add("new_chain", f"Release notes mention support for {_chain_names(sorted(new))}: {ev.claim}", ev, 75)
        elif k == "hiring":
            title = (ev.data.get("title") or "").lower()
            hit = any(kw in title for kw in offer_kw) or any(kw in offer_kw for kw in ev.data.get("keywords", []))
            add("hiring", ev.claim, ev, 85 if hit else 65)
        elif k == "audit_published":
            add("audit_published", ev.claim, ev, 65)
        elif k == "funding":
            s = 75 if (ev.official and ev.verified) else 55 if ev.official else 35
            add("funding", ev.claim, ev, s)
        elif k == "integration":
            add("integration", ev.claim, ev, 60)
        elif k == "dev_activity":
            c, a = ev.data.get("commits_90d", 0), ev.data.get("authors_90d", 0)
            if c >= 30 and a >= 3:
                add("dev_activity", ev.claim, ev, min(80, 40 + c / 3))
        elif k == "contract_event":
            st = ev.data.get("status")
            if st == "orphaned":
                continue
            name, chain_id = ev.data.get("name"), ev.data.get("chain_id")
            if name in ("Upgraded", "BeaconUpgraded", "AdminChanged"):
                add("contract_deploy", ev.claim, ev, 80)
            elif name == "Initialized":
                add("contract_deploy", ev.claim, ev, 85)
                if chain_id in targets and project_chains_before - {chain_id}:
                    add("new_chain", f"First initialisation of a project contract on {_chain_names([chain_id])} "
                                     f"(project already present on {_chain_names(sorted(project_chains_before - {chain_id}))})",
                        ev, 85)
            elif name == "OwnershipTransferred":
                add("contract_deploy", ev.claim, ev, 50)
        elif k == "contract_creation":
            if ev.occurred_at:
                add("contract_deploy", ev.claim, ev, 80)

    # flood / spam detection ---------------------------------------------------
    by_type: dict[str, list[Signal]] = {}
    for s in raw:
        by_type.setdefault(s.type, []).append(s)
    for typ, sigs in by_type.items():
        dated = sorted([s.occurred_at for s in sigs if s.occurred_at])
        for i in range(len(dated)):
            j = i
            while j < len(dated) and dated[j] - dated[i] <= FLOOD_WINDOW:
                j += 1
            if j - i > FLOOD_THRESHOLD:
                flags.append("signal_flood")
                break
    # diminishing returns: keep top-N per type, dedupe identical observations
    out: list[Signal] = []
    for typ, sigs in by_type.items():
        uniq: dict[str, Signal] = {}
        for s in sorted(sigs, key=lambda s: (s.strength * s.quality, s.occurred_at or horizon), reverse=True):
            uniq.setdefault(s.id, s)
        out.extend(list(uniq.values())[:MAX_PER_TYPE])
    return out, sorted(set(flags))
