"""ERC-8257 tool manifest for listing ChainLead's lead-discovery endpoint on OpenSea Tools.

Served at /.well-known/ai-tool/<slug>.json from the same origin as the endpoint. Its JCS (RFC 8785)
keccak256 hash is what gets registered onchain, so its content must stay stable: changing
CHAINLEAD_PUBLIC_BASE_URL, CHAINLEAD_CREATOR_ADDRESS, CHAINLEAD_PAYEE_ADDRESS, CHAINLEAD_PRICE_USDC or
CHAINLEAD_X402_NETWORK (or this file) changes the hash and needs `updateToolMetadata` onchain."""
from __future__ import annotations

import json

from ..models import OFFER_CATALOG
from .config import ToolSettings
from .keccak import keccak_hex

TOOL_SLUG = "chainlead-web3-lead-discovery"
TOOL_PATH = "/v1/leads/discover"
MANIFEST_PATH = f"/.well-known/ai-tool/{TOOL_SLUG}.json"
ICON_PATH = "/static/icon.png"
MANIFEST_TYPE = "https://ercs.ethereum.org/ERCS/erc-8257#tool-manifest-v1"
SERVICE_NAME = "ChainLead"
TOOL_VERSION = "0.2.0"
SUPPORTED_CHAINS = [1, 10, 137, 8453, 42161]

INPUTS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["offer", "github_queries"],
    "properties": {
        "offer": {"type": "string", "enum": sorted(OFFER_CATALOG),
                  "description": "What you sell; it defines what a good lead is."},
        "github_queries": {"type": "array", "minItems": 1, "maxItems": 3,
                           "items": {"type": "string", "minLength": 2, "maxLength": 120},
                           "description": "GitHub repository search queries, e.g. 'topic:cross-chain solidity'."},
        "target_chains": {"type": "array", "maxItems": 5, "items": {"type": "integer", "enum": SUPPORTED_CHAINS},
                          "default": [8453], "description": "EVM chain ids (8453 = Base)."},
        "target_sectors": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 40},
                           "default": ["defi", "infrastructure"]},
        "seed_projects": {"type": "array", "maxItems": 5, "description": "Projects to include explicitly.",
                          "items": {"type": "object", "additionalProperties": False, "properties": {
                              "name": {"type": "string", "maxLength": 80},
                              "github_org": {"type": "string", "maxLength": 39},
                              "website": {"type": "string", "maxLength": 200},
                              "repos": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 140}},
                          }}},
        "max_candidates": {"type": "integer", "minimum": 1, "maximum": 8, "default": 6},
        "max_leads": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
        "freshness_days": {"type": "integer", "minimum": 7, "maximum": 365, "default": 90},
        "lang": {"type": "string", "enum": ["en", "fa"], "default": "en"},
        "include_markdown": {"type": "boolean", "default": False,
                             "description": "Also return each Opportunity Brief as Markdown."},
    },
}

OUTPUTS = {
    "type": "object",
    "description": "Ranked Web3 leads with auditable evidence (source URL and observation time for every claim), "
                   "lead/opportunity/data-confidence scores and a qualified | review | rejected decision. "
                   "Signals indicate a possible need, not proof of budget or purchase intent.",
    "properties": {
        "request_id": {"type": "string"},
        "status": {"type": "string", "enum": ["completed"]},
        "complete": {"type": "boolean", "description": "false when the time budget cut the run short"},
        "offer": {"type": "string"},
        "candidates_found": {"type": "integer"},
        "projects_after_dedup": {"type": "integer"},
        "qualified": {"type": "integer"},
        "review": {"type": "integer"},
        "rejected": {"type": "integer"},
        "leads": {"type": "array", "items": {"type": "object", "properties": {
            "rank": {"type": "integer"},
            "status": {"type": "string", "enum": ["qualified", "review", "rejected"]},
            "status_reasons": {"type": "array", "items": {"type": "string"}},
            "project": {"type": "object"},
            "opportunity": {"type": "object"},
            "scores": {"type": "object"},
            "signals": {"type": "array"},
            "contacts": {"type": "array"},
            "people": {"type": "array"},
            "suggested_action": {"type": "string"},
            "markdown": {"type": "string"},
        }}},
        "sources_used": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": {"type": "string"}},
        "duration_s": {"type": "number"},
        "content_notice": {"type": "string"},
        "payment": {"type": "object", "description": "Settled x402 payment"},
        "generated_at": {"type": "string"},
    },
}

TAGS = ["ai", "web3", "leads", "sales", "business-development", "github", "base", "analytics"]

DESCRIPTION = (
    "Finds Web3 projects that likely need what you sell (smart-contract development, bridge integration, "
    "security audits or onchain analytics). Searches GitHub, reads the projects' own websites and checks "
    "their contracts read-only onchain, then returns ranked leads with evidence links, scores and a "
    "qualified, review or rejected decision. Read-only; never signs or sends transactions.")

BAZAAR_DESCRIPTION = (
    "Web3 lead discovery: ranked projects from GitHub, official websites and read-only onchain checks, with "
    "evidence links, lead and opportunity scores and a qualified, review or rejected decision per project.")

EXAMPLE_INPUT = {"offer": "bridge_dev", "github_queries": ["topic:cross-chain solidity"], "target_chains": [8453],
                 "max_candidates": 5, "max_leads": 3}

EXAMPLE_OUTPUT = {"status": "completed", "complete": True, "offer": "Cross-chain / bridge integration",
                  "qualified": 1, "review": 2, "rejected": 2,
                  "leads": [{"rank": 1, "status": "qualified", "project": {"name": "Example Bridge"},
                             "scores": {"lead": {"total": 72.5}, "opportunity": {"total": 81.0}}}],
                  "payment": {"status": "settled", "amount_usdc": "0.50"}}


class ManifestNotConfigured(Exception):
    pass


def build_manifest(settings: ToolSettings) -> dict:
    problem = settings.manifest_problem()
    if problem:
        raise ManifestNotConfigured(problem)
    base = settings.public_base_url.rstrip("/")
    x402 = settings.x402()
    return {
        "type": MANIFEST_TYPE,
        "name": "ChainLead Web3 Lead Discovery",
        "description": f"{DESCRIPTION} {settings.price_usdc} USDC per call via x402.",
        "version": TOOL_VERSION,
        "endpoint": f"{base}{TOOL_PATH}",
        "image": f"{base}{ICON_PATH}",
        "inputs": INPUTS,
        "outputs": OUTPUTS,
        "creatorAddress": settings.creator_address,
        "tags": TAGS,
        "pricing": x402.pricing(),
        "verifiability": {
            "tier": "self-attested",
            "execution": "server",
            "description": "Reads the public GitHub REST API, the projects' own websites (robots.txt respected) and "
                           "read-only EVM JSON-RPC at request time; scoring and decisions are deterministic code, "
                           "no language model.",
            "dataRetention": "metadata-only",
        },
    }


def canonicalize(value) -> str:
    """RFC 8785 JCS for the JSON types used in the manifest (objects, arrays, strings, ints, bools, null)."""
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonicalize(v) for k, v in items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(v) for v in value) + "]"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite number")
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def manifest_hash(manifest: dict) -> str:
    """JCS keccak256, equal to `npx @opensea/tool-sdk hash`."""
    return keccak_hex(canonicalize(manifest).encode("utf-8"))
