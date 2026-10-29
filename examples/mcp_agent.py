"""Minimal MCP client: launches `chainlead mcp --mock` over stdio and drives it like an agent would."""
import json
import subprocess
import sys

proc = subprocess.Popen([sys.executable, "-m", "chainlead", "mcp", "--mock"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, text=True)
_id = 0


def rpc(method, params=None, notify=False):
    global _id
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        _id += 1
        msg["id"] = _id
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return None if notify else json.loads(proc.stdout.readline())


def tool(tool_name, **args):
    r = rpc("tools/call", {"name": tool_name, "arguments": args})["result"]
    return r.get("structuredContent", r["content"][0]["text"])


rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "demo-agent"}})
rpc("notifications/initialized", notify=True)
print([t["name"] for t in rpc("tools/list")["result"]["tools"]])
camp = tool("chainlead_create_campaign", name="agent-run", offer="bridge_dev", allowed_sources=["mock"], target_chains=[8453])
print(tool("chainlead_run_campaign", campaign_id=camp["id"])["qualified"], "qualified leads")
top = tool("chainlead_list_leads", campaign_id=camp["id"], status="qualified")["items"][0]
print(tool("chainlead_get_brief", lead_id=top["lead_id"], format="markdown", lang="en"))
proc.stdin.close()
proc.wait()
