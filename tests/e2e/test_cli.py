import json
import os
import subprocess
import sys

import pytest


def _cli(*args, env=None, cwd=None):
    return subprocess.run([sys.executable, "-m", "chainlead", *args], capture_output=True, text=True,
                          timeout=120, env={**os.environ, **(env or {})}, cwd=cwd)


def test_demo_writes_report_and_briefs(tmp_path):
    out = tmp_path / "demo"
    r = _cli("demo", "--out", str(out), "--lang", "en")
    assert r.returncode == 0, r.stderr
    assert "Lumen Bridge" in r.stdout and "qualified=3" in r.stdout
    rep = json.loads((out / "report.json").read_text())
    assert rep["qualified"] == 3
    mds = sorted(p.name for p in out.glob("*.md"))
    assert mds[0] == "01-qualified-lumen-bridge.md" and not any("rejected" in m for m in mds)


def test_run_spec_then_query_with_persistent_db(tmp_path):
    spec = tmp_path / "c.json"
    spec.write_text(json.dumps({"name": "cli", "offer": "security_audit", "allowed_sources": ["mock"],
                                "target_chains": [8453], "budget_usd": 0.05}))
    db = str(tmp_path / "x.db")
    r = _cli("--db", db, "run", str(spec), "--mock")
    assert r.returncode == 0, r.stderr
    cid = r.stderr.split()[1]
    r2 = _cli("--db", db, "leads", cid, "--status", "qualified")
    assert r2.returncode == 0 and "Quartz Lending" in r2.stdout
    lead_id = r2.stdout.split()[1]
    r3 = _cli("--db", db, "brief", lead_id, "--lang", "fa")
    assert "گزارش فرصت" in r3.stdout
    r4 = _cli("--db", db, "budget", cid)
    assert json.loads(r4.stdout)["limit_usd"] == 0.05


def test_cli_rejects_secret_in_spec(tmp_path):
    spec = tmp_path / "bad.json"
    spec.write_text(json.dumps({"name": "x", "offer": "bridge_dev", "private_key": "0x" + "1" * 64}))
    r = _cli("--db", str(tmp_path / "b.db"), "run", str(spec))
    assert r.returncode != 0 and "never accepts" in r.stderr


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("CHAINLEAD_LIVE") != "1", reason="set CHAINLEAD_LIVE=1 to hit real GitHub")
def test_live_github_smoke(tmp_path):
    spec = tmp_path / "live.json"
    spec.write_text(json.dumps({"name": "live", "offer": "bridge_dev", "allowed_sources": ["github", "public_web"],
                                "github_queries": ["topic:cross-chain solidity"], "max_candidates": 3,
                                "target_chains": [8453, 1], "budget_usd": 0}))
    r = _cli("--db", str(tmp_path / "live.db"), "run", str(spec), "--out", str(tmp_path / "o"), "--lang", "en")
    assert r.returncode == 0, r.stderr
    rep = json.loads((tmp_path / "o" / "report.json").read_text())
    assert rep["projects_after_dedup"] >= 1, rep["errors"]
