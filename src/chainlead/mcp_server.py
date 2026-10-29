"""MCP server over stdio (newline-delimited JSON-RPC 2.0), dependency-free.

Agents get discovery/scoring tools but NOT the power to raise budgets: campaigns created
through MCP are capped by CHAINLEAD_AGENT_MAX_BUDGET_USD (policy, default $1.00), and there
is no tool to change a budget. Only a human (CLI / REST with key) can do that."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from . import __version__
from .engine import ChainLead, check_budget
from .models import OFFER_CATALOG

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {"name": "chainlead_list_offers",
     "description": "List the service offers ChainLead can find Web3 leads for (smart_contract_dev, bridge_dev, security_audit, onchain_analytics).",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "chainlead_create_campaign",
     "description": "Create a lead-discovery campaign: what you sell (offer), target sectors/chains, sources, budget cap.",
     "inputSchema": {"type": "object", "required": ["name", "offer"], "additionalProperties": False, "properties": {
         "name": {"type": "string"},
         "offer": {"type": "string", "enum": sorted(OFFER_CATALOG)},
         "target_sectors": {"type": "array", "items": {"type": "string"}},
         "target_chains": {"type": "array", "items": {"type": "integer"}, "description": "EVM chain ids, e.g. 8453 (Base), 1 (Ethereum)"},
         "allowed_sources": {"type": "array", "items": {"type": "string", "enum": ["github", "public_web", "rpc", "explorer", "the_graph", "mock"]}},
         "github_queries": {"type": "array", "items": {"type": "string"}, "description": "GitHub repo search queries, e.g. 'topic:defi base'"},
         "seed_projects": {"type": "array", "items": {"type": "object"}},
         "budget_usd": {"type": "number", "minimum": 0},
         "max_leads": {"type": "integer", "minimum": 1, "maximum": 100},
         "max_candidates": {"type": "integer", "minimum": 1, "maximum": 200},
         "freshness_days": {"type": "integer", "minimum": 1, "maximum": 730},
         "min_lead_score": {"type": "number"}, "min_opportunity_score": {"type": "number"}}}},
    {"name": "chainlead_run_campaign",
     "description": "Run a campaign end-to-end (discover, evidence, on-chain checks, scoring, budgeted enrichment). Returns a summary with ranked leads and costs.",
     "inputSchema": {"type": "object", "required": ["campaign_id"], "additionalProperties": False,
                     "properties": {"campaign_id": {"type": "string"}}}},
    {"name": "chainlead_list_leads",
     "description": "List leads of a campaign with scores and status (qualified | review | rejected).",
     "inputSchema": {"type": "object", "required": ["campaign_id"], "additionalProperties": False, "properties": {
         "campaign_id": {"type": "string"}, "status": {"type": "string", "enum": ["qualified", "review", "rejected"]}}}},
    {"name": "chainlead_get_brief",
     "description": "Get the Opportunity Brief of a lead: evidence with source URLs and timestamps, scores, suggested action.",
     "inputSchema": {"type": "object", "required": ["lead_id"], "additionalProperties": False, "properties": {
         "lead_id": {"type": "string"}, "format": {"type": "string", "enum": ["json", "markdown"]},
         "lang": {"type": "string", "enum": ["fa", "en"]}}}},
    {"name": "chainlead_get_budget",
     "description": "Budget, spend and per-provider call ledger summary of a campaign (read-only).",
     "inputSchema": {"type": "object", "required": ["campaign_id"], "additionalProperties": False,
                     "properties": {"campaign_id": {"type": "string"}}}},
]


class McpServer:
    def __init__(self, engine: ChainLead, tenant: str = "local", max_budget: float | None = None) -> None:
        self.engine = engine
        self.tenant = tenant
        self.max_budget = float(max_budget if max_budget is not None
                                else os.environ.get("CHAINLEAD_AGENT_MAX_BUDGET_USD", "1.0"))

    # ------------------------------------------------------------------ tools
    def call_tool(self, name: str, args: dict) -> Any:
        e, t = self.engine, self.tenant
        if name == "chainlead_list_offers":
            return [{"key": o.key, "title": o.title, "description": o.description} for o in OFFER_CATALOG.values()]
        if name == "chainlead_create_campaign":
            spec = dict(args)
            spec.setdefault("budget_usd", min(1.0, self.max_budget))
            spec["budget_usd"] = check_budget(spec["budget_usd"])      # rejects NaN/inf/negative
            if spec["budget_usd"] > self.max_budget:
                raise PermissionError(f"policy: agents may not set budget above ${self.max_budget:.2f}; ask a human")
            return e.create_campaign(spec, t)
        if name == "chainlead_run_campaign":
            return e.run_campaign(t, args["campaign_id"])
        if name == "chainlead_list_leads":
            return [{"lead_id": x["lead_id"], "project": x["project"]["name"], "status": x["status"], "rank": x["rank"],
                     "opportunity": x["opportunity"]["title_en"], "lead_score": x["scores"]["lead"]["total"],
                     "opportunity_score": x["scores"]["opportunity"]["total"],
                     "data_confidence": x["scores"]["data_confidence"], "reasons": x["status_reasons"]}
                    for x in e.leads(t, args["campaign_id"], args.get("status"))]
        if name == "chainlead_get_brief":
            return e.brief(t, args["lead_id"], args.get("format", "json"), args.get("lang", "en"))
        if name == "chainlead_get_budget":
            return e.budget(t, args["campaign_id"])
        raise KeyError(f"unknown tool {name}")

    # ------------------------------------------------------------------ protocol
    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:          # notification (e.g. notifications/initialized)
            return None
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                ver = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result = {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "chainlead", "version": __version__},
                          "instructions": "Web3 lead & opportunity discovery. Create a campaign, run it, then read "
                                          "briefs. Signals are possible needs, not proof of purchase intent."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg.get("params") or {}
                try:
                    out = self.call_tool(p.get("name", ""), p.get("arguments") or {})
                    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                    if not isinstance(out, str):
                        result["structuredContent"] = out if isinstance(out, dict) else {"items": out}
                except Exception as ex:  # noqa: BLE001 - tool errors are reported to the agent
                    result = {"content": [{"type": "text", "text": f"{type(ex).__name__}: {ex}"}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as ex:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(ex)}}

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
                stdout.flush()
