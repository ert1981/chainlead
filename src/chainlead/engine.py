"""The ChainLead pipeline (spec §5):

  1. offer definition      -> Campaign.offer
  2. discover projects     -> discover providers (+ explicit seeds), deduplicated
  3. collect evidence      -> GitHub / official website / fixtures (free first)
  4. resolve identity      -> bidirectional links, contract ownership conflicts
  5. on-chain checks       -> bytecode + deployment events, finality & reorg reconciliation
  6. signals & scoring     -> L, O, data confidence, decision
  7. budgeted enrichment   -> paid verify/contact calls only for the best candidates
  8. rank, persist, brief  -> auditable Opportunity Briefs for other agents
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .brief import build_brief, to_markdown
from .chain.finality import reconcile_reorgs
from .chain.registry import get_chain
from .config import Settings, load_settings
from .economics import BudgetExceeded, ProviderError, ProviderRouter
from .identity import build_project, deduplicate, identity_confidence, resolve_contract_conflicts
from .models import Campaign, ChainEvent, Evidence, Lead, Project, parse_dt, slugify
from .providers.base import empty_bundle, merge_bundles
from .providers.onchain import OnChainProvider
from .safety import assert_no_secrets, sanitize_text
from .scoring import commercial_fit, data_confidence, decide, opportunity, rank_key
from .signals import detect_signals
from .storage import Storage

log = logging.getLogger("chainlead")


class ChainLead:
    def __init__(self, storage: Storage | None = None, settings: Settings | None = None,
                 providers: list[Any] | None = None, onchain: OnChainProvider | None = None,
                 evidence_workers: int = 4) -> None:
        self.evidence_workers = max(1, int(evidence_workers))
        self.settings = settings or load_settings()
        self.storage = storage or Storage(self.settings.db_path)
        self.providers = providers or []
        self.onchain = onchain

    # ------------------------------------------------------------------ factory
    @classmethod
    def build(cls, settings: Settings | None = None, storage: Storage | None = None,
              mock: bool = False, fixture: dict | None = None) -> "ChainLead":
        """Default wiring. mock=True -> fictional offline ecosystem incl. a mock EVM node."""
        settings = settings or load_settings()
        from .providers.block_explorer import BlockExplorerProvider
        from .providers.github import GitHubProvider
        from .providers.public_web import PublicWebProvider
        from .providers.the_graph import TheGraphProvider
        providers: list[Any] = [GitHubProvider(settings), PublicWebProvider(settings), BlockExplorerProvider(settings),
                                TheGraphProvider(settings)]
        onchain = OnChainProvider(settings) if settings.rpc_urls else None
        if mock:
            from .providers.mock import MockProvider, MockRpcClient, load_fixture
            data = fixture or load_fixture()
            providers.append(MockProvider(data))
            onchain = OnChainProvider(settings, client_factory=lambda cid: MockRpcClient(data, cid),
                                      log_range_override=50_000_000)
        return cls(storage, settings, providers, onchain)

    # ------------------------------------------------------------------ campaigns
    def create_campaign(self, spec: dict, tenant: str = "local") -> dict:
        if not isinstance(spec, dict):
            raise ValueError("campaign spec must be a JSON object")
        assert_no_secrets(spec)
        spec = dict(spec)
        server_fields = sorted(set(spec) & SERVER_ASSIGNED)
        if server_fields:
            # A caller-chosen id would overwrite (and re-budget) an existing campaign.
            raise ValueError(f"fields {server_fields} are assigned by the server")
        spec["tenant"] = tenant
        if "target_chains" in spec:
            if not isinstance(spec["target_chains"], list):
                raise ValueError("target_chains must be a list of chain ids")
            spec["target_chains"] = [int(x) for x in spec["target_chains"]]
            for cid in spec["target_chains"]:
                get_chain(cid)
        spec["budget_usd"] = check_budget(spec.get("budget_usd", 1.0))
        for k, (lo, hi) in INT_LIMITS.items():
            if k in spec:
                v = spec[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v:
                    raise ValueError(f"{k} must be an integer")
                if not lo <= int(v) <= hi:
                    raise ValueError(f"{k} must be between {lo} and {hi}")
                spec[k] = int(v)
        for k in FLOAT_THRESHOLDS:
            if k in spec:
                v = spec[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 100:
                    raise ValueError(f"{k} must be a number between 0 and 100")
        for k in LIST_FIELDS:
            if k in spec and (not isinstance(spec[k], list) or len(spec[k]) > LIST_FIELDS[k]):
                raise ValueError(f"{k} must be a list of at most {LIST_FIELDS[k]} items")
        c = Campaign.from_dict(spec)
        self.storage.save_campaign(tenant, c.to_dict())
        self.storage.ensure_budget(tenant, c.id, c.budget_usd)
        return c.to_dict()

    def get_campaign(self, tenant: str, campaign_id: str) -> Campaign:
        d = self.storage.get_campaign(tenant, campaign_id)
        if not d:
            raise KeyError(f"campaign {campaign_id} not found")
        return Campaign.from_dict(d)

    def set_budget(self, tenant: str, campaign_id: str, limit_usd: float) -> dict:
        c = self.get_campaign(tenant, campaign_id)
        c.budget_usd = check_budget(limit_usd)
        self.storage.save_campaign(tenant, c.to_dict())
        self.storage.ensure_budget(tenant, campaign_id, c.budget_usd)
        return self.storage.budget(tenant, campaign_id)

    # ------------------------------------------------------------------ run
    def run_campaign(self, tenant: str, campaign_id: str, deadline: float | None = None) -> dict:
        """deadline: optional time.monotonic() value; when reached, no new evidence/on-chain calls are
        started and the run finishes with what it has (used by the synchronous paid HTTP endpoint)."""
        c = self.get_campaign(tenant, campaign_id)
        try:
            return self._run(tenant, c, deadline)
        except Exception:
            c.status = "failed"
            self.storage.save_campaign(tenant, c.to_dict())
            raise

    def _run(self, tenant: str, c: Campaign, deadline: float | None) -> dict:
        t0 = time.time()
        campaign_id = c.id
        cd = c.to_dict()
        c.status = "running"
        self.storage.save_campaign(tenant, c.to_dict())
        router = ProviderRouter(self.storage, self.settings, tenant, c.id,
                                self.providers + ([self.onchain] if self.onchain else []))
        errors: list[str] = []

        timed_out: list[str] = []

        def out_of_time() -> bool:
            if deadline is not None and time.monotonic() >= deadline:
                if not timed_out:
                    timed_out.append("time budget reached; remaining candidates were scored on the evidence "
                                     "collected so far")
                return True
            return False

        def safe_call(provider, op, params, fn, cacheable=True):
            try:
                return router.call(provider, op, params, fn, cacheable=cacheable)
            except ProviderError as e:
                errors.append(str(e)[:300])
                return None
            except BudgetExceeded as e:     # a paid call outside enrichment must not abort the run
                errors.append(str(e)[:300])
                return None

        # 2. discovery ---------------------------------------------------------
        disc_params = {k: cd[k] for k in ("github_queries", "target_sectors", "target_chains", "max_candidates")}
        candidates: list[dict] = []
        for p in router.available("discover", c.allowed_sources):
            res = safe_call(p, "discover", disc_params, lambda p=p: p.discover(cd))
            candidates.extend(res or [])
        for seed in c.seed_projects:
            s = dict(seed)
            s.setdefault("name", s.get("github_org") or s.get("website") or "seed")
            s.setdefault("repos", [])
            s.setdefault("sectors", [])
            s.setdefault("chains", [])
            s["source_url"] = s.get("source_url") or "seed://campaign"
            s["description"] = sanitize_text(s.get("description", ""), 300)[0]
            candidates.append(s)
        merged = deduplicate(candidates)[: c.max_candidates]

        # 3-4. evidence + identity -------------------------------------------
        projects: list[tuple[Project, dict, dict]] = []

        def collect(m: dict) -> list:
            parts = []
            for member in m["members"]:
                if out_of_time():
                    break
                for p in router.available("evidence", c.allowed_sources):
                    if not _applies(p.name, member):
                        continue
                    params = {"member": {k: member.get(k) for k in ("name", "github_org", "repos", "website", "mock_key")}}
                    parts.append(safe_call(p, "evidence", params, lambda p=p, member=member: p.evidence(member, cd)))
                for spec in member.get("graph_queries", []) or []:
                    g = router.providers.get("the_graph")
                    if g and g.available():
                        parts.append(safe_call(g, "graph", spec, lambda g=g, spec=spec: g.activity(spec)))
            return parts

        # Candidates are independent, so their evidence is fetched concurrently (results keep input order).
        if self.evidence_workers > 1 and len(merged) > 1:
            with ThreadPoolExecutor(max_workers=min(self.evidence_workers, len(merged)),
                                    thread_name_prefix="chainlead-evidence") as pool:
                all_parts = list(pool.map(collect, merged))
        else:
            all_parts = [collect(m) for m in merged]
        for m, parts in zip(merged, all_parts):
            bundle = merge_bundles(*[x for x in parts if x])
            proj = build_project(m, bundle)
            if m.get("topics"):
                bundle["facts"]["topics"] = sorted(set(bundle["facts"].get("topics", [])) | set(m["topics"]))
            projects.append((proj, bundle, m))
        for proj, bundle, m in projects:
            proj.identity_confidence, proj.identity_notes = identity_confidence(proj, bundle, len(m["members"]))
        resolve_contract_conflicts([p for p, _, _ in projects], self.storage, tenant)

        # 5. on-chain ----------------------------------------------------------
        heads: dict[int, int] = {}
        if self.onchain and ("rpc" in c.allowed_sources or "mock" in c.allowed_sources):
            for proj, bundle, m in projects:
                penalty_notes = [n for n in proj.identity_notes if n.startswith("-35")]
                for ct in proj.contracts[:MAX_CONTRACTS_PER_PROJECT]:
                    if out_of_time():
                        break
                    if ct.chain_id not in c.target_chains or not self.onchain.supports(ct.chain_id):
                        continue
                    params = {"chain_id": ct.chain_id, "address": ct.address, "fresh": c.freshness_days}
                    res = safe_call(self.onchain, "onchain", params,
                                    lambda ct=ct: self.onchain.inspect({"chain_id": ct.chain_id, "address": ct.address}, cd),
                                    cacheable=False)
                    if res:
                        bundle["evidence"].extend(res["evidence"])
                        for ev in res["events"]:
                            self.storage.upsert_event(tenant, _event(ev), proj.key)
                        for k, v in res["facts"].get("scan", {}).items():
                            heads[int(k.split(":")[0])] = max(heads.get(int(k.split(":")[0]), 0), v["head"])
                if any(e["data"].get("kind") == "contract_code" for e in bundle["evidence"]):
                    conf, notes = identity_confidence(proj, bundle, len(m["members"]))
                    proj.identity_confidence = max(0.0, conf - 35 * len(penalty_notes))
                    proj.identity_notes = notes + penalty_notes
            for chain_id, head in heads.items():
                try:
                    stats = reconcile_reorgs(self.storage, tenant, get_chain(chain_id), head,
                                             lambda bn, cid=chain_id: self.onchain.canonical_hash(cid, bn))
                    log.info("reorg reconcile chain %s: %s", chain_id, stats)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"reorg reconcile {chain_id}: {e}")

        # 6. signals & scores --------------------------------------------------
        leads: list[tuple[Lead, dict, dict]] = []
        for proj, bundle, m in projects:
            lead = self._score(c, proj, bundle, tenant)
            leads.append((lead, bundle, m))

        # 7. budgeted enrichment of the best candidates --------------------------
        enrich_pool = sorted([x for x in leads if x[0].status in ("qualified", "review")
                              and x[0].project.identity_confidence >= c.min_identity_confidence
                              and not any("untrusted" in r for r in x[0].status_reasons)],
                             key=lambda x: rank_key(x[0]))[: c.max_leads]
        budget_stopped = False
        for i, (lead, bundle, m) in enumerate(enrich_pool):
            if budget_stopped or out_of_time():
                break
            before = (self.storage.budget(tenant, c.id) or {}).get("spent_usd", 0)
            extra = []
            try:
                for p in router.available("verify", c.allowed_sources):
                    for ct in lead.project.contracts:
                        if ct.chain_id in c.target_chains:
                            extra.append(safe_call(p, "verify", {"c": ct.key}, lambda p=p, ct=ct: p.verify(
                                {"chain_id": ct.chain_id, "address": ct.address}, cd)))
                need_contacts = not lead.project.people or not any(
                    x.get("type") == "email" and x.get("official") for x in lead.project.contact_channels)
                if need_contacts:
                    for p in router.available("contacts", c.allowed_sources):
                        for member in m["members"]:
                            if _applies(p.name, member):
                                r = safe_call(p, "contacts", {"m": member.get("mock_key") or member.get("github_org")},
                                              lambda p=p, member=member: p.contacts(member, cd))
                                if r:
                                    for x in r["contacts"]:
                                        x["enriched"] = True
                                extra.append(r)
            except BudgetExceeded as e:
                errors.append(str(e))
                budget_stopped = True
            extra = [x for x in extra if x]
            if extra:
                nb = merge_bundles(bundle, *extra)
                rebuilt = build_project(m, nb)
                lead.project.contact_channels = rebuilt.contact_channels
                lead.project.people = rebuilt.people
                bundle["evidence"] = nb["evidence"]
                after = (self.storage.budget(tenant, c.id) or {}).get("spent_usd", 0)
                new_lead = self._score(c, lead.project, nb, tenant)
                new_lead.cost_usd = after - before
                idx = next(j for j, x in enumerate(leads) if x[0].project.key == lead.project.key)
                leads[idx] = (new_lead, nb, m)

        # 8. rank, persist, briefs -------------------------------------------
        ordered = sorted([x[0] for x in leads], key=rank_key)
        qualified_n = 0
        for i, lead in enumerate(ordered, 1):
            lead.rank = i
            if lead.status == "qualified":
                qualified_n += 1
                if qualified_n > c.max_leads:
                    lead.status = "review"
                    lead.status_reasons = [f"over max_leads={c.max_leads}; kept for later"]
            self.storage.save_project(tenant, lead.project)
            for ev in lead.evidence:
                self.storage.save_evidence(tenant, ev)
            self.storage.save_signals(tenant, c.id, lead.project.key, lead.signals)
            self.storage.save_lead(tenant, lead, build_brief(lead, c.offer.title, _rank_signals(c)))
        summary = router.summary()
        q = [lead for lead in ordered if lead.status == "qualified"]
        c.status = "completed"
        self.storage.save_campaign(tenant, c.to_dict())
        return {
            "campaign_id": c.id, "status": "completed", "offer": c.offer.title,
            "candidates_found": len(candidates), "projects_after_dedup": len(merged),
            "qualified": len(q), "review": sum(1 for x in ordered if x.status == "review"),
            "rejected": sum(1 for x in ordered if x.status == "rejected"),
            "leads": [{"lead_id": x.id, "project": x.project.name, "status": x.status, "rank": x.rank,
                       "lead_score": x.lead_score.total, "opportunity_score": x.opportunity_score.total,
                       "data_confidence": x.data_confidence, "reasons": x.status_reasons} for x in ordered],
            "budget": summary,
            "cost_per_qualified_lead_usd": round(summary["spent_usd"] / len(q), 6) if q else None,
            "errors": errors + timed_out, "duration_s": round(time.time() - t0, 2),
            "complete": not timed_out,
        }

    def _score(self, c: Campaign, proj: Project, bundle: dict, tenant: str) -> Lead:
        evidence = [_evidence(proj.key, e) for e in bundle["evidence"]]
        for row in self.storage.list_events(tenant, proj.key):
            evidence.append(_event_evidence(proj.key, row))
        uniq = {e.id: e for e in evidence}
        evidence = list(uniq.values())
        signals, sflags = detect_signals(evidence, c, set(proj.chains))
        flags = sorted(set(bundle["facts"].get("flags", [])) | set(sflags))
        L = commercial_fit(proj, evidence, signals, c, bundle["facts"])
        O = opportunity(signals, c)
        conf = data_confidence(proj, signals, flags)
        status, reasons = decide(proj, L, O, conf, signals, evidence, flags, c)
        if flags:
            proj.identity_notes = proj.identity_notes + [f"content flags: {', '.join(flags)}"]
        return Lead(c.id, proj, L, O, conf, signals, evidence, status, reasons)

    # ------------------------------------------------------------------ queries
    def leads(self, tenant: str, campaign_id: str, status: str | None = None) -> list[dict]:
        return self.storage.list_leads(tenant, campaign_id, status)

    def brief(self, tenant: str, lead_id: str, fmt: str = "json", lang: str = "fa") -> Any:
        b = self.storage.get_lead(tenant, lead_id)
        if b is None:
            raise KeyError(f"lead {lead_id} not found")
        return to_markdown(b, lang) if fmt == "markdown" else b

    def budget(self, tenant: str, campaign_id: str) -> dict:
        self.get_campaign(tenant, campaign_id)
        router = ProviderRouter(self.storage, self.settings, tenant, campaign_id, [])
        return router.summary()


# ---------------------------------------------------------------------- helpers
SERVER_ASSIGNED = {"id", "tenant", "status", "created_at"}
INT_LIMITS = {"max_candidates": (1, 200), "max_leads": (1, 100), "freshness_days": (1, 730)}
FLOAT_THRESHOLDS = ("min_identity_confidence", "min_lead_score", "min_opportunity_score", "min_data_confidence")
LIST_FIELDS = {"github_queries": 20, "seed_projects": 100, "target_sectors": 30, "target_chains": 10,
               "allowed_sources": 10}
MAX_BUDGET_USD = 1_000_000.0
MAX_CONTRACTS_PER_PROJECT = 10


def check_budget(value: Any) -> float:
    """Budgets must be finite and >= 0. NaN would slip past every `>`/`<` policy check (and is stored as
    NULL by SQLite), inf would make the cap meaningless."""
    if isinstance(value, bool):
        raise ValueError("budget_usd must be a number")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("budget_usd must be a number")
    if not math.isfinite(v) or v < 0 or v > MAX_BUDGET_USD:
        raise ValueError(f"budget_usd must be a finite number between 0 and {MAX_BUDGET_USD:g}")
    return v


def _applies(provider_name: str, member: dict) -> bool:
    if provider_name == "mock":
        return bool(member.get("mock_key"))
    if member.get("mock_key"):
        return False                         # fictional fixture projects never hit real services
    if provider_name == "github":
        return bool(member.get("github_org") or member.get("repos"))
    if provider_name == "public_web":
        return bool(member.get("website"))
    return True


def _rank_signals(c: Campaign):
    def f(signals):
        return sorted(signals, key=lambda s: s.strength * c.offer.signal_relevance.get(s.type, 0) * s.quality,
                      reverse=True)
    return f


def _evidence(project_key: str, d: dict) -> Evidence:
    return Evidence(project_key=project_key, source_type=d["source_type"], url=d["url"], claim=d["claim"],
                    occurred_at=parse_dt(d.get("occurred_at")), official=bool(d.get("official")),
                    verified=bool(d.get("verified")), data=dict(d.get("data") or {}))


def _event(d: dict) -> ChainEvent:
    return ChainEvent(chain_id=d["chain_id"], tx_hash=d["tx_hash"], log_index=d["log_index"],
                      block_number=d["block_number"], block_hash=d["block_hash"], address=d["address"],
                      name=d["name"], args=d.get("args", {}), timestamp=parse_dt(d.get("timestamp")),
                      status=d.get("status", "pending"))


def _event_evidence(project_key: str, row: dict) -> Evidence:
    chain = get_chain(row["chain_id"])
    return Evidence(
        project_key=project_key, source_type="rpc",
        url=f"evm://{row['chain_id']}/tx/{row['tx_hash']}#log{row['log_index']}",
        claim=f"{row['name']} event on {chain.name} contract {row['address']} at block {row['block_number']} "
              f"({row['status']})",
        occurred_at=parse_dt(row["ts"]), official=False, verified=row["status"] in ("safe", "finalized"),
        data={"kind": "contract_event", "name": row["name"], "chain_id": row["chain_id"],
              "address": row["address"], "status": row["status"], "block_number": row["block_number"],
              "block_hash": row["block_hash"], "tx_hash": row["tx_hash"], "log_index": row["log_index"]})


__all__ = ["ChainLead", "slugify"]
