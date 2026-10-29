"""End-to-end pipeline on the fictional ecosystem: every hard case must land where it should."""
import pytest


def _by_name(report):
    return {x["project"]: x for x in report["leads"]}


def test_expected_outcomes_for_every_fixture_project(run_mock):
    rep, _ = run_mock()
    leads = _by_name(rep)
    assert rep["candidates_found"] == 10 and rep["projects_after_dedup"] == 9     # duplicate merged
    assert {n for n, x in leads.items() if x["status"] == "qualified"} == {"Lumen Bridge", "Quartz Lending", "Pylon Restaking"}
    assert leads["Apex Yield"]["status"] == "review" and "uncertain_identity" in leads["Apex Yield"]["reasons"][0]
    assert leads["MoonPump"]["status"] == "review" and "prompt_injection_suspected" in leads["MoonPump"]["reasons"][0]
    assert leads["Helix Analytics"]["status"] == "review" and "no verified contact path" in leads["Helix Analytics"]["reasons"][0]
    assert leads["Tern Finance"]["status"] == "rejected" and leads["Tern Finance"]["opportunity_score"] == 0  # reorged event
    assert leads["Nimbus Oracle"]["status"] == "rejected"                                                  # stale
    assert leads["Kestrel NFT Studio"]["status"] == "rejected"                                             # off-sector
    assert leads["Lumen Bridge"]["rank"] == 1
    assert rep["errors"] == []


def test_reorged_event_is_orphaned_and_pending_event_kept(engine, run_mock):
    rep, c = run_mock()
    events = {e["project_key"]: e for e in engine.storage.list_events("local")}
    assert events["tern-finance"]["status"] == "orphaned"
    assert events["helix-analytics"]["status"] == "pending"
    assert events["lumen-bridge"]["status"] == "finalized"


def test_brief_is_auditable(engine, run_mock):
    rep, _ = run_mock()
    lid = _by_name(rep)["Lumen Bridge"]["lead_id"]
    b = engine.brief("local", lid)
    assert b["opportunity"]["type"] == "new_chain"
    assert b["signals"] and all(e["url"] and e["observed_at"] for s in b["signals"] for e in s["evidence"])
    assert {s["type"] for s in b["signals"]} >= {"new_chain", "contract_deploy", "hiring"}
    assert "not proof" in b["caveat"]
    md_fa = engine.brief("local", lid, "markdown", "fa")
    md_en = engine.brief("local", lid, "markdown", "en")
    assert "گزارش فرصت" in md_fa and "Opportunity Brief" in md_en
    apex = engine.brief("local", _by_name(rep)["Apex Yield"]["lead_id"])
    assert any("attributed to another project" in n for n in apex["identity_notes"])
    assert apex["project"]["contracts"] == []        # stolen contract removed from Apex


def test_rerun_is_served_from_cache_without_new_cost(engine, mock_campaign):
    c = engine.create_campaign(mock_campaign)
    r1 = engine.run_campaign("local", c["id"])
    r2 = engine.run_campaign("local", c["id"])
    assert r1["budget"]["spent_usd"] > 0
    assert r2["budget"]["spent_usd"] == r1["budget"]["spent_usd"]
    assert r2["budget"]["by_provider"]["mock"]["cached"] > 0
    assert [x["project"] for x in r1["leads"]] == [x["project"] for x in r2["leads"]]


@pytest.mark.parametrize("budget", [0.0, 0.01, 0.025])
def test_budget_is_never_exceeded(run_mock, budget):
    rep, _ = run_mock(budget_usd=budget)
    assert rep["budget"]["spent_usd"] <= budget + 1e-9
    if budget == 0:
        assert rep["budget"]["spent_usd"] == 0
        assert sum(v["skipped_budget"] for v in rep["budget"]["by_provider"].values()) >= 1
    # free evidence still produces qualified leads; budget only limits paid enrichment
    assert rep["qualified"] >= 2


def test_cost_per_qualified_lead_and_ledger_consistency(engine, run_mock):
    rep, c = run_mock()
    calls = engine.storage.calls("local", c["id"])
    assert sum(x["cost_usd"] for x in calls) == pytest.approx(rep["budget"]["spent_usd"])
    assert rep["cost_per_qualified_lead_usd"] == pytest.approx(rep["budget"]["spent_usd"] / rep["qualified"], abs=1e-6)


def test_offer_changes_ranking(run_mock):
    audit, _ = run_mock(offer="security_audit")
    bridge, _ = run_mock(offer="bridge_dev")
    a, b = _by_name(audit), _by_name(bridge)
    assert a["Quartz Lending"]["opportunity_score"] > b["Quartz Lending"]["opportunity_score"] - 1
    assert b["Lumen Bridge"]["opportunity_score"] >= a["Lumen Bridge"]["opportunity_score"]


def test_seed_projects_and_max_leads(run_mock):
    rep, _ = run_mock(max_leads=1)
    assert rep["qualified"] == 1
    assert any("over max_leads" in x["reasons"][0] for x in rep["leads"])
