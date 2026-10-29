"""REST API (via the Python SDK) and MCP stdio server."""
import json
import os
import subprocess
import sys
import threading

import pytest

from chainlead.api import create_server
from chainlead.config import Settings
from chainlead.engine import ChainLead
from chainlead.mcp_server import McpServer
from chainlead.sdk import ChainLeadClient, ChainLeadError, LocalChainLead
from chainlead.storage import Storage

SPEC = {"name": "api", "offer": "bridge_dev", "allowed_sources": ["mock"], "target_chains": [8453], "budget_usd": 0.1}


@pytest.fixture
def server():
    s = Settings(db_path=":memory:", api_keys={"key-alice": "alice", "key-bob": "bob"})
    eng = ChainLead.build(s, Storage(":memory:"), mock=True)
    srv = create_server(eng, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield url, srv
    srv.shutdown()
    srv.server_close()


def test_rest_flow_via_sdk(server):
    url, srv = server
    alice = ChainLeadClient(url, "key-alice")
    assert alice.health()["status"] == "ok"
    assert {o["key"] for o in alice.offers()} >= {"bridge_dev", "security_audit"}
    c = alice.create_campaign(**SPEC)
    rep = alice.run_campaign(c["id"])
    assert rep["qualified"] == 3
    q = alice.leads(c["id"], "qualified")
    assert [x["project"]["name"] for x in q][0] == "Lumen Bridge"
    md = alice.brief(q[0]["lead_id"], "markdown", "fa")
    assert md.startswith("# گزارش فرصت")
    assert alice.budget(c["id"])["spent_usd"] > 0
    assert alice.set_budget(c["id"], 2.0)["limit_usd"] == 2.0
    # async run through the job runner
    job = alice.run_async(c["id"])
    srv.jobs.wait(job["job_id"])
    assert alice.job(job["job_id"])["status"] == "completed"


def test_rest_auth_tenant_isolation_and_errors(server):
    url, _ = server
    alice, bob = ChainLeadClient(url, "key-alice"), ChainLeadClient(url, "key-bob")
    c = alice.create_campaign(**SPEC)
    alice.run_campaign(c["id"])
    with pytest.raises(ChainLeadError) as e:
        ChainLeadClient(url, "wrong").offers()
    assert e.value.status == 401
    with pytest.raises(ChainLeadError) as e:
        ChainLeadClient(url).offers()
    assert e.value.status == 401
    with pytest.raises(ChainLeadError) as e:
        bob.leads(c["id"])                          # bob cannot see alice's campaign
    assert e.value.status == 404
    lead_id = alice.leads(c["id"])[0]["lead_id"]
    with pytest.raises(ChainLeadError) as e:
        bob.brief(lead_id)
    assert e.value.status == 404
    assert bob._req("GET", "/projects") == []
    with pytest.raises(ChainLeadError) as e:
        alice.create_campaign(name="x", offer="nope")
    assert e.value.status == 400
    with pytest.raises(ChainLeadError) as e:
        alice.set_budget(c["id"], -1)
    assert e.value.status == 400
    with pytest.raises(ChainLeadError) as e:
        alice._req("DELETE", "/campaigns")
    assert e.value.status in (404, 405, 501)


def test_rest_rejects_wallet_secrets(server):
    url, _ = server
    alice = ChainLeadClient(url, "key-alice")
    with pytest.raises(ChainLeadError) as e:
        alice.create_campaign(**SPEC, seed_projects=[{"name": "x", "private_key": "0x" + "a" * 64}])
    assert e.value.status == 400 and "never accepts" in str(e.value)


def test_mcp_in_process_policy_and_tools():
    eng = ChainLead.build(Settings(db_path=":memory:", api_keys={}), Storage(":memory:"), mock=True)
    mcp = McpServer(eng, max_budget=0.5)
    init = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert init["result"]["serverInfo"]["name"] == "chainlead"
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = {t["name"] for t in mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert "chainlead_set_budget" not in names and "chainlead_run_campaign" in names

    def call(tool, **args):
        r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        return r["result"]

    over = call("chainlead_create_campaign", name="x", offer="bridge_dev", budget_usd=5, allowed_sources=["mock"])
    assert over["isError"] and "policy" in over["content"][0]["text"]
    c = call("chainlead_create_campaign", name="x", offer="bridge_dev", allowed_sources=["mock"], target_chains=[8453])
    cid = c["structuredContent"]["id"]
    assert c["structuredContent"]["budget_usd"] == 0.5
    run = call("chainlead_run_campaign", campaign_id=cid)
    assert run["structuredContent"]["qualified"] == 3
    leads = call("chainlead_list_leads", campaign_id=cid, status="qualified")["structuredContent"]["items"]
    brief = call("chainlead_get_brief", lead_id=leads[0]["lead_id"], format="markdown", lang="en")
    assert brief["content"][0]["text"].startswith("# Opportunity Brief")
    assert mcp.handle({"jsonrpc": "2.0", "id": 9, "method": "bogus"})["error"]["code"] == -32601


def test_mcp_stdio_subprocess(tmp_path):
    env = dict(os.environ, CHAINLEAD_DB=str(tmp_path / "m.db"), CHAINLEAD_AGENT_MAX_BUDGET_USD="1")
    p = subprocess.Popen([sys.executable, "-m", "chainlead", "mcp", "--mock"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, env=env)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "chainlead_create_campaign",
             "arguments": {"name": "s", "offer": "security_audit", "allowed_sources": ["mock"], "target_chains": [8453]}}}]
    out, _ = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n", timeout=60)
    lines = [json.loads(x) for x in out.strip().splitlines()]
    assert lines[0]["result"]["protocolVersion"] == "2024-11-05"
    assert lines[1]["result"]["structuredContent"]["offer"]["key"] == "security_audit"
    assert lines[2]["error"]["code"] == -32700


def test_local_sdk(tmp_path):
    sdk = LocalChainLead(str(tmp_path / "l.db"), mock=True)
    c = sdk.create_campaign(**SPEC)
    rep = sdk.run_campaign(c["id"])
    assert rep["qualified"] == 3 and sdk.leads(c["id"])
