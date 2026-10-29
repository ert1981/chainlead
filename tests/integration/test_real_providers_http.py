"""Exercise the REAL GitHub / website / JSON-RPC / explorer / The Graph providers over HTTP
against local fake servers (no internet needed)."""
import json
from datetime import timedelta

import pytest

from chainlead.chain.rpc import RpcClient, RpcError
from chainlead.config import Settings
from chainlead.engine import ChainLead
from chainlead.models import iso, utcnow
from chainlead.providers.block_explorer import BlockExplorerProvider
from chainlead.providers.github import GitHubProvider
from chainlead.providers.mock import MockRpcClient
from chainlead.providers.onchain import OnChainProvider
from chainlead.providers.public_web import PublicWebProvider, parse_html
from chainlead.providers.the_graph import TheGraphProvider
from chainlead.storage import Storage

ROUTER = "0x" + "c" * 36 + "0001"


def _fixture_chain():
    return {"projects": [], "chain": {
        "heads": {"8453": 30_000_000},
        "code": {f"8453:{ROUTER}": "0x6080604052"},
        "logs": [{"chain_id": 8453, "address": ROUTER, "event": "Initialized", "args": {"version": 1},
                  "days_ago": 4, "log_index": 0}]}}


def _rpc_server(fake, client: MockRpcClient):
    def handle(q, body):
        req = json.loads(body)
        m, p = req["method"], req["params"]
        if m == "eth_chainId":
            r = hex(8453)
        elif m == "eth_blockNumber":
            r = hex(client.block_number())
        elif m == "eth_getCode":
            r = client.get_code(p[0])
        elif m == "eth_getBlockByNumber":
            r = client.get_block(p[0])
        elif m == "eth_getLogs":
            f = p[0]
            span = int(f["toBlock"], 16) - int(f["fromBlock"], 16)
            if span >= 1_000_000:
                return 200, "application/json", {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32005, "message": "range too large"}}
            r = client.get_logs(f["address"], int(f["fromBlock"], 16), int(f["toBlock"], 16), f.get("topics"))
        else:
            return 200, "application/json", {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "nope"}}
        return 200, "application/json", {"jsonrpc": "2.0", "id": req["id"], "result": r}
    fake.routes[("POST", "/rpc")] = handle


def _github_and_site(fake, site):
    now = utcnow()
    fake.json("/search/repositories", {"items": [
        {"full_name": "orbit-labs/orbit-core", "html_url": "https://github.com/orbit-labs/orbit-core",
         "homepage": site, "description": "Cross-chain bridge router", "topics": ["bridge", "solidity"],
         "fork": False, "archived": False, "owner": {"login": "orbit-labs"}},
        {"full_name": "someone/forked", "html_url": "https://github.com/someone/forked", "fork": True,
         "owner": {"login": "someone"}}]})
    fake.json("/users/orbit-labs", {"type": "Organization", "name": "Orbit Labs", "blog": site,
                                    "email": "hello@orbit.test", "twitter_username": "orbitlabs"})
    fake.json("/repos/orbit-labs/orbit-core", {"html_url": "https://github.com/orbit-labs/orbit-core", "homepage": site,
                                               "fork": False, "archived": False, "topics": ["bridge"],
                                               "stargazers_count": 150, "description": "Cross-chain bridge router",
                                               "pushed_at": iso(now)})
    fake.json("/repos/orbit-labs/orbit-core/releases", [
        {"tag_name": "v3.0.0", "name": "v3.0.0", "draft": False, "prerelease": False,
         "published_at": iso(now - timedelta(days=6)), "html_url": "https://github.com/orbit-labs/orbit-core/releases/v3.0.0",
         "body": "Now live on Base mainnet. Adds cross-chain withdrawals."}])
    fake.json("/repos/orbit-labs/orbit-core/commits", [
        {"author": {"login": f"dev{i % 4}"}, "commit": {"committer": {"date": iso(now - timedelta(days=i))}}}
        for i in range(45)])
    fake.text("/repos/orbit-labs/orbit-core/readme",
              f"# Orbit\nRouter on Ethereum mainnet and Base.\n| Base | {ROUTER} |\nAddress without chain: 0x{'d'*40}\n",
              "text/plain")
    fake.text("/robots.txt", "User-agent: *\nDisallow: /private\n", "text/plain")
    fake.text("/site/", f"""<html><head><title>Orbit — bridge</title><script>evil()</script></head><body>
      <a href="https://github.com/orbit-labs">GitHub</a> <a href="mailto:partners@orbit.test">Contact</a>
      <a href="https://x.com/orbitlabs">X</a> <a href="/site/careers">Careers</a> <a href="/site/team">Team</a>
      <a href="/private/secret">x</a><p>Orbit is live on Base and Ethereum mainnet.</p>
      <p>We raised $6M seed round led by Example Ventures.</p></body></html>""")
    fake.text("/site/careers", "<html><body><h2>Open roles</h2><li>Senior Solidity Engineer</li><li>DevOps Engineer</li></body></html>")
    fake.text("/site/team", "<html><body><p>Lena Park — CTO</p><p>Omar Haddad, Co-founder</p></body></html>")


def test_full_campaign_with_real_providers(fake_http):
    site = fake_http.url + "/site/"
    _github_and_site(fake_http, site)
    _rpc_server(fake_http, MockRpcClient(_fixture_chain(), 8453))
    fake_http.json("/v2/api", {"status": "1", "result": [{"SourceCode": "contract X{}", "ContractName": "OrbitRouter",
                                                         "Proxy": "1", "Implementation": "0x" + "e" * 40}]})
    s = Settings(db_path=":memory:", rpc_urls={8453: fake_http.url + "/rpc"}, explorer_api_key="k",
                 api_keys={}, github_token="ghp_test")
    eng = ChainLead(Storage(":memory:"), s,
                    [GitHubProvider(s, api_base=fake_http.url), PublicWebProvider(s),
                     BlockExplorerProvider(s, api_url=fake_http.url + "/v2/api", cost_per_call=0.001)],
                    OnChainProvider(s, log_range_override=500_000))
    c = eng.create_campaign({"name": "live-ish", "offer": "bridge_dev", "github_queries": ["topic:bridge"],
                             "allowed_sources": ["github", "public_web", "rpc", "explorer"],
                             "target_chains": [8453], "budget_usd": 0.01, "freshness_days": 30})
    rep = eng.run_campaign("local", c["id"])
    assert rep["errors"] == [], rep["errors"]
    assert rep["projects_after_dedup"] == 1                       # fork filtered out
    lead = rep["leads"][0]
    assert lead["status"] == "qualified", lead
    b = eng.brief("local", lead["lead_id"])
    types = {s["type"] for s in b["signals"]}
    assert {"new_chain", "release", "hiring", "funding", "contract_deploy", "dev_activity"} <= types
    # only the address whose README line names its chain is linked; the chain-less one is ignored
    assert [(x["chain_id"], x["address"]) for x in b["project"]["contracts"]] == [(8453, ROUTER)]
    assert b["project"]["contracts"][0]["linked_by"] == "https://github.com/orbit-labs/orbit-core#readme"
    assert b["scores"]["identity_confidence"] >= 85
    names = {p["name"] for p in b["people"]}
    assert names == {"Lena Park", "Omar Haddad"}
    assert {x["type"] for x in b["contacts"] if x["official"]} >= {"email", "x"}
    # GitHub token was sent; robots.txt disallowed path never fetched; explorer charged against budget
    assert any(h[3].get("Authorization") == "Bearer ghp_test" for h in fake_http.hits if h[1].startswith("/repos"))
    assert not any(h[1].startswith("/private") for h in fake_http.hits)
    assert rep["budget"]["by_provider"]["explorer"]["cost_usd"] == pytest.approx(0.001)
    ev = eng.storage.list_events("local")
    assert len(ev) == 1 and ev[0]["status"] == "finalized" and ev[0]["name"] == "Initialized"


def test_rpc_client_is_read_only_and_reports_errors(fake_http):
    _rpc_server(fake_http, MockRpcClient(_fixture_chain(), 8453))
    cli = RpcClient(fake_http.url + "/rpc")
    assert cli.chain_id() == 8453 and cli.block_number() == 30_000_000
    for m in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_sign"):
        with pytest.raises(RpcError, match="read-only"):
            cli.call(m, [])
    with pytest.raises(RpcError, match="range too large"):
        cli.get_logs(ROUTER, 0, 2_000_000, max_range=5_000_000)
    assert len(cli.get_logs(ROUTER, 29_000_000, 30_000_000, max_range=100_000)) == 1    # paginated


def test_onchain_request_cap_and_missing_code(fake_http):
    client = MockRpcClient(_fixture_chain(), 8453)
    _rpc_server(fake_http, client)
    s = Settings(db_path=":memory:", rpc_urls={8453: fake_http.url + "/rpc"}, api_keys={})
    prov = OnChainProvider(s, max_log_requests_per_contract=3)
    b = prov.inspect({"chain_id": 8453, "address": ROUTER}, {"freshness_days": 120})
    scan = b["facts"]["scan"][f"8453:{ROUTER}"]
    assert scan["requests"] == 3 and scan["complete"] is False             # capped, newest-first
    b2 = prov.inspect({"chain_id": 8453, "address": "0x" + "9" * 40}, {"freshness_days": 30})
    assert b2["evidence"][0]["data"]["has_code"] is False and b2["events"] == []


def test_the_graph_provider_is_read_only(fake_http):
    fake_http.json("/api/KEY/subgraphs/id/SG1", {"data": {"deployments": [{"id": 1}, {"id": 2}]}}, method="POST")
    s = Settings(db_path=":memory:", graph_api_key="KEY", api_keys={})
    g = TheGraphProvider(s, gateway=fake_http.url + "/api")
    b = g.activity({"subgraph_id": "SG1", "query": "{ deployments { id } }", "count_path": "deployments",
                    "claim": "Deployments indexed"})
    assert b["evidence"][0]["claim"].endswith("(n=2)")
    with pytest.raises(ValueError):
        g.query("SG1", "mutation { drain }")


def test_github_rate_limit_is_reported_not_crashing(fake_http):
    fake_http.json("/search/repositories", {"message": "API rate limit exceeded"}, status=403)
    s = Settings(db_path=":memory:", api_keys={})
    eng = ChainLead(Storage(":memory:"), s, [GitHubProvider(s, api_base=fake_http.url)], None)
    c = eng.create_campaign({"name": "x", "offer": "bridge_dev", "github_queries": ["q"], "allowed_sources": ["github"]})
    rep = eng.run_campaign("local", c["id"])
    assert rep["projects_after_dedup"] == 0 and "403" in rep["errors"][0]


def test_html_parser_survives_hostile_markup():
    title, text, links = parse_html("<html><title>T</title><a href='x'>y<script>alert(1)</script></a>"
                                    "<<<>>><div unclosed <p>hello‮world")
    assert title == "T" and "alert" not in text
