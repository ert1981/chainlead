"""Command line: chainlead {demo,run,serve,mcp,leads,brief,budget,offers}"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from .config import load_settings
from .engine import ChainLead
from .models import OFFER_CATALOG
from .storage import Storage


def _engine(args, mock: bool = False) -> ChainLead:
    s = load_settings()
    if getattr(args, "db", None):
        s.db_path = args.db
    return ChainLead.build(s, Storage(s.db_path), mock=mock)


def _load_spec(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise SystemExit("PyYAML not installed: pip install 'chainlead[yaml]' or use a .json campaign")
        return yaml.safe_load(text)
    return json.loads(text)


def _table(report: dict) -> str:
    rows = [f"{'#':>2}  {'project':<24} {'status':<9} {'L':>5} {'O':>5} {'conf':>5}  reason"]
    for x in report["leads"]:
        rows.append(f"{x['rank']:>2}  {x['project'][:24]:<24} {x['status']:<9} {x['lead_score']:>5} "
                    f"{x['opportunity_score']:>5} {x['data_confidence']:>5}  {x['reasons'][0][:70]}")
    b = report["budget"]
    rows.append("")
    rows.append(f"qualified={report['qualified']} review={report['review']} rejected={report['rejected']}  "
                f"spent=${b['spent_usd']:.4f} of ${b['limit_usd']:.2f}  cost/qualified lead="
                f"{'$%.4f' % report['cost_per_qualified_lead_usd'] if report['cost_per_qualified_lead_usd'] is not None else 'n/a'}")
    if report["errors"]:
        rows.append(f"provider errors: {len(report['errors'])} (first: {report['errors'][0][:120]})")
    return "\n".join(rows)


def _write_briefs(engine: ChainLead, tenant: str, report: dict, out: str, lang: str) -> None:
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for x in report["leads"]:
        if x["status"] == "rejected":
            continue
        md = engine.brief(tenant, x["lead_id"], "markdown", lang)
        js = engine.brief(tenant, x["lead_id"], "json")
        safe_key = re.sub(r"[^a-z0-9-]+", "-", str(js["project"]["key"]).lower()).strip("-")[:80] or "project"
        stem = f"{int(x['rank']):02d}-{x['status']}-{safe_key}"
        (d / f"{stem}.md").write_text(md, encoding="utf-8")
        (d / f"{stem}.json").write_text(json.dumps(js, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"briefs written to {d.resolve()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chainlead", description="Evidence-based Web3 lead & opportunity discovery")
    ap.add_argument("--db", help="SQLite path (default $CHAINLEAD_DB or chainlead.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="run on the built-in FICTIONAL ecosystem (offline)")
    d.add_argument("--offer", default="bridge_dev", choices=sorted(OFFER_CATALOG))
    d.add_argument("--budget", type=float, default=0.10)
    d.add_argument("--lang", default="fa", choices=["fa", "en"])
    d.add_argument("--out", default="chainlead-demo")

    r = sub.add_parser("run", help="create + run a campaign from a JSON/YAML spec")
    r.add_argument("spec")
    r.add_argument("--out", help="directory for report + briefs")
    r.add_argument("--lang", default="fa", choices=["fa", "en"])
    r.add_argument("--mock", action="store_true", help="also enable the fictional mock ecosystem")

    s = sub.add_parser("serve", help="REST API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    s.add_argument("--mock", action="store_true")

    t = sub.add_parser("tool-serve", help="paid x402 endpoint for agents (OpenSea Tools / ERC-8257)")
    t.add_argument("--host", default="0.0.0.0")  # nosec B104 - container entrypoint
    t.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))

    m = sub.add_parser("mcp", help="MCP server over stdio")
    m.add_argument("--mock", action="store_true")
    m.add_argument("--tenant", default="local")

    lp = sub.add_parser("leads")
    lp.add_argument("campaign_id")
    lp.add_argument("--status", choices=["qualified", "review", "rejected"])
    bp = sub.add_parser("brief")
    bp.add_argument("lead_id")
    bp.add_argument("--format", default="markdown", choices=["markdown", "json"])
    bp.add_argument("--lang", default="fa", choices=["fa", "en"])
    gp = sub.add_parser("budget")
    gp.add_argument("campaign_id")
    gp.add_argument("--set", type=float, help="change the limit (human-only operation)")
    sub.add_parser("offers")

    a = ap.parse_args(argv)
    if a.cmd == "offers":
        for o in OFFER_CATALOG.values():
            print(f"{o.key:<20} {o.title}")
        return 0
    if a.cmd == "demo":
        if not a.db:
            a.db = ":memory:"
        eng = _engine(a, mock=True)
        c = eng.create_campaign({"name": f"demo-{a.offer}", "offer": a.offer, "allowed_sources": ["mock"],
                                 "target_sectors": ["defi", "infrastructure"], "target_chains": [8453],
                                 "budget_usd": a.budget})
        rep = eng.run_campaign("local", c["id"])
        print("DEMO — fictional dataset (no real projects)\n")
        print(_table(rep))
        _write_briefs(eng, "local", rep, a.out, a.lang)
        return 0
    if a.cmd == "run":
        eng = _engine(a, mock=a.mock)
        c = eng.create_campaign(_load_spec(a.spec))
        print(f"campaign {c['id']} created; running…", file=sys.stderr)
        rep = eng.run_campaign("local", c["id"])
        print(_table(rep))
        if a.out:
            _write_briefs(eng, "local", rep, a.out, a.lang)
        return 0
    if a.cmd == "serve":
        from .api import serve
        serve(_engine(a, mock=a.mock), a.host, a.port)
        return 0
    if a.cmd == "tool-serve":
        from .tool.server import serve as tool_serve
        tool_serve(a.host, a.port)
        return 0
    if a.cmd == "mcp":
        from .mcp_server import McpServer
        McpServer(_engine(a, mock=a.mock), tenant=a.tenant).serve()
        return 0
    eng = _engine(a)
    if a.cmd == "leads":
        for x in eng.leads("local", a.campaign_id, a.status):
            print(f"{x['rank']:>2} {x['lead_id']} {x['project']['name']:<24} {x['status']:<9} "
                  f"L={x['scores']['lead']['total']} O={x['scores']['opportunity']['total']}")
    elif a.cmd == "brief":
        res = eng.brief("local", a.lead_id, a.format, a.lang)
        print(res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, indent=2))
    elif a.cmd == "budget":
        if a.set is not None:
            eng.set_budget("local", a.campaign_id, a.set)
        print(json.dumps(eng.budget("local", a.campaign_id), indent=2))
    return 0


def entry() -> int:
    try:
        return main()
    except (ValueError, KeyError) as e:      # includes SecretMaterialRejected
        print(f"error: {str(e).strip(chr(39))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(entry())
