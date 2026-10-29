"""The Graph gateway provider (BYOK: GRAPH_API_KEY).

Generic, read-only GraphQL access to a subgraph. Campaigns can attach `graph_queries` to a
seed project, e.g. a protocol's own subgraph, to add activity evidence. Each query is
charged against the campaign budget at the configured per-query estimate."""
from __future__ import annotations

import re

from .. import http
from ..config import Settings
from .base import Provider, empty_bundle

GATEWAY = "https://gateway.thegraph.com/api"


class TheGraphProvider(Provider):
    name = "the_graph"
    capabilities = frozenset({"graph"})

    def __init__(self, settings: Settings, gateway: str = GATEWAY, cost_per_query: float = 0.00004) -> None:
        self.settings = settings
        self.gateway = gateway.rstrip("/")
        self.costs = {"graph": cost_per_query}

    def available(self) -> bool:
        return bool(self.settings.graph_api_key)

    def query(self, subgraph_id: str, gql: str, variables: dict | None = None) -> dict:
        if not isinstance(subgraph_id, str) or not re.fullmatch(r"[A-Za-z0-9]{20,80}|[A-Za-z0-9_-]{1,80}", subgraph_id):
            raise ValueError("invalid subgraph id")      # never lets a seed steer the keyed gateway URL
        if not isinstance(gql, str) or len(gql) > 10_000:
            raise ValueError("query must be a string of at most 10000 characters")
        if re.search(r"\b(mutation|subscription)\b", gql, re.I):
            raise ValueError("only read-only GraphQL queries are allowed")
        url = f"{self.gateway}/{self.settings.graph_api_key}/subgraphs/id/{subgraph_id}"
        resp = http.post_json(url, {"query": gql, "variables": variables or {}},
                              timeout=self.settings.http_timeout, user_agent=self.settings.user_agent)
        if resp.get("errors"):
            raise ValueError(f"graph errors: {resp['errors']}")
        return resp.get("data") or {}

    def activity(self, spec: dict) -> dict:
        """spec = {subgraph_id, query, claim, kind?, count_path?} -> evidence bundle."""
        data = self.query(spec["subgraph_id"], spec["query"], spec.get("variables"))
        count = None
        if spec.get("count_path"):
            node = data
            for part in spec["count_path"].split("."):
                node = node.get(part) if isinstance(node, dict) else None
            count = len(node) if isinstance(node, list) else node
        b = empty_bundle()
        b["evidence"].append({
            "source_type": "graph", "url": f"thegraph://{spec['subgraph_id']}",
            "claim": spec.get("claim", "Subgraph activity observed") + (f" (n={count})" if count is not None else ""),
            "occurred_at": spec.get("occurred_at"), "official": bool(spec.get("official", False)), "verified": True,
            "data": {"kind": spec.get("kind", "dev_activity"), "graph_count": count},
        })
        return b
