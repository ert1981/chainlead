"""Opportunity Brief (spec §8): an auditable, agent-readable report per lead.
Every claim carries its source URL and observation time; observations, inferences and the
commercial hypothesis are kept in separate sections."""
from __future__ import annotations

from .models import Lead, iso, utcnow
from .signals import INFERENCES_FA

TITLES = {
    "new_chain": ("Multi-chain expansion", "توسعه چندزنجیره‌ای"),
    "contract_deploy": ("Recent contract deployment / upgrade", "استقرار یا ارتقای اخیر قرارداد"),
    "hiring": ("Engineering capacity need", "نیاز به ظرفیت مهندسی"),
    "audit_published": ("Post-audit security follow-up", "پیگیری امنیتی پس از ممیزی"),
    "release": ("New protocol release", "انتشار نسخه جدید پروتکل"),
    "funding": ("Newly funded team", "تیم تازه تأمین مالی‌شده"),
    "integration": ("New integration", "یکپارچه‌سازی جدید"),
    "dev_activity": ("Active engineering team", "تیم توسعه فعال"),
}
STATUS_FA = {"qualified": "واجد شرایط", "review": "نیازمند بررسی انسانی", "rejected": "رد شده"}
CAVEAT_EN = ("Signals indicate a *possible* need. They are not proof of budget or purchase intent. "
             "Verify the sources before any outreach; do not mass-contact founders.")
CAVEAT_FA = ("نشانه‌ها فقط یک نیاز «احتمالی» را نشان می‌دهند و اثبات بودجه یا قصد خرید نیستند. "
             "پیش از هر تماس، منابع را بررسی کن و از تماس انبوه با بنیان‌گذاران خودداری کن.")


def build_brief(lead: Lead, offer_title: str, rank_signals) -> dict:
    p = lead.project
    sigs = rank_signals(lead.signals)
    top = sigs[0].type if sigs else None
    ev_by_id = {e.id: e for e in lead.evidence}
    official = [x for x in p.contact_channels if x.get("official")]
    if lead.status == "qualified":
        who = f"{p.people[0].name} ({p.people[0].role})" if p.people else "the technical/partnerships owner"
        who_fa = f"{p.people[0].name} ({p.people[0].role})" if p.people else "مسئول فنی یا همکاری‌های پروژه"
        via = official[0]["type"] + ": " + official[0]["value"] if official else "n/a"
        action = (f"Read the official sources below, then contact {who} via {via} with a specific "
                  f"'{offer_title}' proposal tied to the top signal.")
        action_fa = (f"ابتدا منابع رسمی زیر را بررسی کن، سپس از طریق {via} با {who_fa} تماس بگیر و پیشنهادی "
                     f"مشخص درباره «{offer_title}» مرتبط با مهم‌ترین نشانه ارائه کن.")
    elif lead.status == "review":
        action = "Human review required before any outreach: " + "; ".join(lead.status_reasons)
        action_fa = "پیش از هر تماس، بررسی انسانی لازم است: " + "؛ ".join(lead.status_reasons)
    else:
        action = "No action: " + "; ".join(lead.status_reasons)
        action_fa = "اقدامی لازم نیست: " + "؛ ".join(lead.status_reasons)
    return {
        "lead_id": lead.id, "campaign_id": lead.campaign_id, "generated_at": iso(utcnow()),
        "project": {"key": p.key, "name": p.name, "website": p.website, "github_org": p.github_org,
                    "repos": p.repos, "chains": p.chains, "sectors": p.sectors,
                    "contracts": [{"chain_id": c.chain_id, "address": c.address, "label": c.label,
                                   "linked_by": c.linked_by} for c in p.contracts]},
        "status": lead.status, "status_reasons": lead.status_reasons, "rank": lead.rank,
        "opportunity": {"type": top, "title_en": TITLES.get(top, ("No current opportunity", ""))[0],
                        "title_fa": TITLES.get(top, ("", "فرصتی شناسایی نشد"))[1], "offer": offer_title},
        "scores": {
            "lead": {"total": lead.lead_score.total, **lead.lead_score.components},
            "opportunity": {"total": lead.opportunity_score.total, **lead.opportunity_score.components},
            "data_confidence": lead.data_confidence, "identity_confidence": p.identity_confidence,
            "lead_reasons": lead.lead_score.reasons, "weights": {"lead": lead.lead_score.weights,
                                                                 "opportunity": lead.opportunity_score.weights}},
        "identity_notes": p.identity_notes,
        "signals": [{"type": s.type, "observation": s.observation, "inference": s.inference,
                     "inference_fa": INFERENCES_FA.get(s.type, ""),
                     "strength": s.strength, "quality": s.quality, "occurred_at": iso(s.occurred_at),
                     "evidence": [{"url": ev_by_id[i].url, "source_type": ev_by_id[i].source_type,
                                   "observed_at": iso(ev_by_id[i].observed_at), "official": ev_by_id[i].official,
                                   "verified": ev_by_id[i].verified} for i in s.evidence_ids if i in ev_by_id]}
                    for s in sigs],
        "contacts": [{"type": x["type"], "value": x["value"], "source_url": x.get("source_url"),
                      "official": x.get("official", False)} for x in p.contact_channels],
        "people": [{"name": x.name, "role": x.role, "source_url": x.source_url} for x in p.people],
        "evidence_count": len(lead.evidence),
        "suggested_action": action,
        "suggested_action_fa": action_fa,
        "cost_usd": round(lead.cost_usd, 6),
        "caveat": CAVEAT_EN,
    }


def to_markdown(b: dict, lang: str = "fa") -> str:
    fa = lang == "fa"
    s, p, o = b["scores"], b["project"], b["opportunity"]
    L = []
    if fa:
        L += [f"# گزارش فرصت: {p['name']}", "",
              f"**وضعیت:** {STATUS_FA[b['status']]}  ·  **رتبه:** {b['rank']}", "",
              f"## فرصت احتمالی: {o['title_fa']}", f"خدمت پیشنهادی: {o['offer']}", "",
              f"- امتیاز لید تجاری (L): **{s['lead']['total']}/100** — F={s['lead']['F']} B={s['lead']['B']} "
              f"C={s['lead']['C']} D={s['lead']['D']} R={s['lead']['R']}",
              f"- امتیاز فرصت (O): **{s['opportunity']['total']}/100** — N={s['opportunity']['N']} "
              f"T={s['opportunity']['T']} E={s['opportunity']['E']} Q={s['opportunity']['Q']}",
              f"- اطمینان داده: **{s['data_confidence']}/100**  ·  اطمینان هویت: {s['identity_confidence']}/100", "",
              "## مشاهده‌ها و استنباط‌ها (شواهد)"]
    else:
        L += [f"# Opportunity Brief: {p['name']}", "", f"**Status:** {b['status']}  ·  **Rank:** {b['rank']}", "",
              f"## Possible opportunity: {o['title_en']}", f"Offer: {o['offer']}", "",
              f"- Lead score (L): **{s['lead']['total']}/100**", f"- Opportunity score (O): **{s['opportunity']['total']}/100**",
              f"- Data confidence: **{s['data_confidence']}/100**", "", "## Observations & inferences"]
    for sig in b["signals"]:
        src = ", ".join(f"[{e['source_type']}]({e['url']}) (observed {e['observed_at']})" for e in sig["evidence"])
        L.append(f"- **{sig['type']}** — {sig['observation']}  ")
        L.append(f"  {'استنباط' if fa else 'Inference'}: {sig.get('inference_fa') if fa else sig['inference']}  ")
        L.append(f"  {'منبع' if fa else 'Source'}: {src}")
    if not b["signals"]:
        L.append("- —")
    L += ["", "## " + ("دلایل تصمیم" if fa else "Decision reasons")] + [f"- {r}" for r in b["status_reasons"]]
    L += ["", "## " + ("هویت پروژه" if fa else "Identity")] + [f"- {n}" for n in b["identity_notes"]]
    L += ["", "## " + ("کانال‌های تماس عمومی" if fa else "Public contact channels")]
    L += [f"- {c['type']}: {c['value']} ({'رسمی' if fa and c['official'] else 'official' if c['official'] else 'unverified'}) — {c['source_url']}"
          for c in b["contacts"]] or ["- —"]
    L += [f"- {x['name']} — {x['role']} ({x['source_url']})" for x in b["people"]]
    L += ["", "## " + ("اقدام پیشنهادی" if fa else "Suggested action"),
          b.get("suggested_action_fa", b["suggested_action"]) if fa else b["suggested_action"], "",
          f"> {CAVEAT_FA if fa else CAVEAT_EN}", ""]
    return "\n".join(L)
