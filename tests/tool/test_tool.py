"""ChainLead as a paid ERC-8257 tool: manifest, x402 v1/v2 challenge + Bazaar metadata, instant probes,
the verify -> reserve -> produce -> settle flow, replay protection, input validation before payment,
register page and duplicate check."""
import base64
import hashlib
import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from chainlead.config import Settings
from chainlead.engine import ChainLead
from chainlead.providers.github import GitHubProvider
from chainlead.providers.public_web import PublicWebProvider
from chainlead.storage import Storage
from chainlead.tool import manifest as mf
from chainlead.tool import payments
from chainlead.tool.config import ToolSettings
from chainlead.tool.icon import png_bytes
from chainlead.tool.keccak import keccak_hex
from chainlead.tool.register_page import (REGISTER_PAGE_CSP, SCRIPT, DuplicateChecker, decode_uri, encode_register,
                                          register_page, register_tx)
from chainlead.tool.server import ToolApp, create_server, validate_input

PAYEE = "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4"
CREATOR = PAYEE.lower()
PAYER = "0x" + "ab" * 20
TX = "0x" + "12" * 32
BASE = "https://chainlead.onrender.com"
# Computed with `npx @opensea/tool-sdk@0.31.1 hash` on the manifest served for these settings.
EXPECTED_HASH = "0xe0289b6a7a7624efc158f001d861a1f100ff12c4fd1332be51dd4c4448e700eb"
VALID = {"offer": "bridge_dev", "github_queries": ["topic:cross-chain solidity"], "max_candidates": 3}


def tool_settings(**over):
    d = dict(public_base_url=BASE, payee_address=PAYEE, creator_address=CREATOR, price_usdc="0.50",
             x402_network="base", db_path=":memory:", registry_rpc_url="https://rpc.invalid")
    d.update(over)
    return ToolSettings(**d)


class FakeFacilitator:
    kind = "fake"

    def __init__(self, valid=True, settle_ok=True, retryable=False):
        self.valid, self.settle_ok, self.retryable = valid, settle_ok, retryable
        self.calls = []

    def verify(self, payment, req):
        self.calls.append(("verify", payment, req))
        if not self.valid:
            raise payments.PaymentError(402, "invalid_exact_evm_payload_signature")
        return {"isValid": True, "payer": PAYER}

    def settle(self, payment, req):
        self.calls.append(("settle", payment, req))
        if not self.settle_ok:
            return None, self.retryable
        return {"success": True, "transaction": TX, "network": "base", "payer": PAYER}, False


FAKE_RESULT = {"request_id": "req_x", "status": "completed", "complete": True, "offer": "Cross-chain / bridge integration",
               "candidates_found": 2, "projects_after_dedup": 2, "qualified": 1, "review": 1, "rejected": 0,
               "leads": [{"rank": 1, "status": "qualified", "project": {"name": "Orbit"}}], "errors": []}


@pytest.fixture
def server():
    started = []

    def make(settings=None, facilitator=None, result=None, engine=None):
        fac = facilitator or FakeFacilitator()
        eng = engine or ChainLead(Storage(":memory:"), Settings(db_path=":memory:", api_keys={}), [], None)
        app = ToolApp(settings or tool_settings(), engine=eng, facilitator=fac)
        calls = []
        if result is not False:
            def fake_discover(req):
                calls.append(req)
                return json.loads(json.dumps(result or FAKE_RESULT))
            app.discover = fake_discover
        srv = create_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}", app, fac, calls
    yield make
    for s in started:
        s.shutdown()
        s.server_close()


def call(url, method="GET", body=None, headers=None):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                         **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), (json.loads(raw) if raw and "json" in r.headers.get("Content-Type", "") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, dict(e.headers), (json.loads(raw) if raw and "json" in e.headers.get("Content-Type", "") else raw)


def v1_payment(nonce="ef", amount="500000"):
    body = {"x402Version": 1, "scheme": "exact", "network": "base",
            "payload": {"signature": "0x" + "cd" * 65, "authorization": {
                "from": PAYER, "to": PAYEE, "value": amount, "validAfter": "0", "validBefore": "9999999999",
                "nonce": "0x" + nonce * 32}}}
    return base64.b64encode(json.dumps(body).encode()).decode()


def v2_payment(nonce="aa", **accepted_over):
    accepted = {"scheme": "exact", "network": "eip155:8453", "asset": payments.USDC["base"][1], "amount": "500000",
                "payTo": PAYEE, "maxTimeoutSeconds": 300, "extra": {"name": "USD Coin", "version": "2"}, **accepted_over}
    body = {"x402Version": 2, "accepted": accepted, "resource": {"url": BASE + mf.TOOL_PATH},
            "payload": {"signature": "0x" + "cd" * 65, "authorization": {
                "from": PAYER, "to": PAYEE, "value": "500000", "validAfter": "0", "validBefore": "9999999999",
                "nonce": "0x" + nonce * 32}}}
    return base64.b64encode(json.dumps(body).encode()).decode()


def v2_header(headers):
    return json.loads(base64.b64decode(headers["PAYMENT-REQUIRED"]))


# ------------------------------------------------------------------------------------- primitives
def test_keccak_vectors():
    assert keccak_hex(b"") == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak_hex(b"registerTool(string,bytes32,address)")[:10] == "0xfe1d0b16"
    assert keccak_hex(b"ToolRegistered(uint256,address,address,string,bytes32)") == \
        "0xe7be7fd3c802f61682f56ba817276b1cc81fbee7cb50705c8ed7952811dac397"


def test_jcs_sorts_keys_and_is_compact():
    assert mf.canonicalize({"b": 1, "a": [True, None, "x"], "A": {"z": 2, "y": "é"}}) == \
        '{"A":{"y":"é","z":2},"a":[true,null,"x"],"b":1}'


@pytest.mark.parametrize("price,units", [("0.50", "500000"), ("2", "2000000"), ("1.25", "1250000"), ("0.000001", "1")])
def test_price_is_configurable_and_converted_to_usdc_base_units(price, units):
    assert payments.X402Config(payee=PAYEE, price_usdc=price).amount == units


@pytest.mark.parametrize("bad", ["0", "-1", "1.0000001", "abc", ""])
def test_bad_prices_are_refused(bad):
    with pytest.raises(ValueError):
        payments.X402Config(payee=PAYEE, price_usdc=bad)


def test_icon_is_a_512_png():
    png = png_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[16:24] == (512).to_bytes(4, "big") * 2


# ------------------------------------------------------------------------------------- manifest
def test_manifest_shape_and_hash_matches_tool_sdk():
    m = mf.build_manifest(tool_settings())
    assert m["type"] == mf.MANIFEST_TYPE and m["endpoint"] == BASE + mf.TOOL_PATH
    assert m["creatorAddress"] == CREATOR and m["image"] == BASE + mf.ICON_PATH
    assert m["pricing"] == [{"amount": "500000", "asset": "eip155:8453/erc20:0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                             "recipient": f"eip155:8453:{PAYEE.lower()}", "protocol": "x402"}]
    assert mf.manifest_hash(m) == EXPECTED_HASH


def test_manifest_inputs_match_the_validator():
    props = set(mf.INPUTS["properties"])
    validate_input({**VALID, **{k: v for k, v in {"lang": "fa", "include_markdown": True, "freshness_days": 30,
                                                   "max_leads": 2, "target_chains": [8453, 1],
                                                   "target_sectors": ["defi"],
                                                   "seed_projects": [{"github_org": "acme"}]}.items()}}, 8)
    assert {"offer", "github_queries", "lang", "include_markdown", "seed_projects"} <= props


@pytest.mark.parametrize("over,problem", [({"public_base_url": "http://x.com"}, "https"),
                                          ({"public_base_url": "https://x.com/path"}, "bare"),
                                          ({"creator_address": "nope"}, "CREATOR"),
                                          ({"payee_address": ""}, "PAYEE")])
def test_manifest_refuses_incomplete_configuration(over, problem):
    with pytest.raises(mf.ManifestNotConfigured, match=problem):
        mf.build_manifest(tool_settings(**over))


def test_price_change_changes_the_manifest_hash():
    assert mf.manifest_hash(mf.build_manifest(tool_settings(price_usdc="1.00"))) != EXPECTED_HASH


# ------------------------------------------------------------------------------------- 402 challenges
@pytest.mark.parametrize("method,body", [("GET", None), ("POST", None), ("POST", b""), ("POST", b"{}"),
                                         ("POST", json.dumps(VALID).encode())])
def test_unpaid_requests_get_an_instant_402_with_v1_body_and_v2_header(server, method, body):
    base, app, fac, calls = server()
    st, h, b = call(base + mf.TOOL_PATH, method, body)
    assert st == 402 and calls == [] and fac.calls == []
    acc = b["accepts"][0]
    assert b["x402Version"] == 1 and acc["scheme"] == "exact" and acc["network"] == "base"
    assert acc["maxAmountRequired"] == "500000" and acc["payTo"] == PAYEE
    assert acc["asset"] == payments.USDC["base"][1] and acc["resource"] == BASE + mf.TOOL_PATH
    v2 = v2_header(h)
    assert v2["x402Version"] == 2 and v2["accepts"][0]["network"] == "eip155:8453"
    assert v2["accepts"][0]["amount"] == "500000"
    assert v2["resource"]["iconUrl"] == BASE + mf.ICON_PATH and v2["resource"]["serviceName"] == "ChainLead"
    bz = v2["extensions"]["bazaar"]
    assert bz["info"]["input"]["method"] == "POST" and bz["info"]["input"]["body"] == mf.EXAMPLE_INPUT
    assert bz["schema"]["properties"]["input"]["properties"]["body"] == mf.INPUTS


def test_head_probe_is_402_without_body(server):
    base, *_ = server()
    st, h, b = call(base + mf.TOOL_PATH, "HEAD")
    assert st == 402 and "PAYMENT-REQUIRED" in h and not b


def test_invalid_input_without_payment_is_422_not_402(server):
    base, *_ = server()
    st, _, b = call(base + mf.TOOL_PATH, "POST", {"offer": "nope", "github_queries": ["x y"]})
    assert st == 422 and b["error"] == "invalid input"


def test_invalid_input_with_payment_is_never_charged(server):
    base, app, fac, calls = server()
    st, _, _ = call(base + mf.TOOL_PATH, "POST", {"offer": "bridge_dev"}, {"X-PAYMENT": v1_payment()})
    assert st == 422 and fac.calls == [] and calls == []


# ------------------------------------------------------------------------------------- paid flow
def test_paid_v1_call_verifies_runs_then_settles(server):
    base, app, fac, calls = server()
    st, h, b = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment()})
    assert st == 200, b
    assert [c[0] for c in fac.calls] == ["verify", "settle"] and len(calls) == 1
    assert b["payment"] == {"status": "settled", "transaction": TX, "network": "base", "payer": PAYER,
                            "amount_usdc": "0.50"}
    receipt = json.loads(base64.b64decode(h["X-PAYMENT-RESPONSE"]))
    assert receipt["success"] is True and receipt["transaction"] == TX
    assert fac.calls[0][2]["maxAmountRequired"] == "500000" and fac.calls[0][2]["payTo"] == PAYEE


def test_same_authorization_cannot_be_used_twice(server):
    base, app, fac, calls = server()
    assert call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("01")})[0] == 200
    st, _, b = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("01")})
    assert st == 402 and b["error"] == "payment_authorization_already_used" and len(calls) == 1


def test_paid_v2_call_uses_v2_requirements_and_returns_payment_response(server):
    base, app, fac, calls = server()
    st, h, b = call(base + mf.TOOL_PATH, "POST", VALID, {"PAYMENT-SIGNATURE": v2_payment()})
    assert st == 200 and fac.calls[0][2] == app.x402.requirements_v2()
    assert json.loads(base64.b64decode(h["PAYMENT-RESPONSE"]))["transaction"] == TX


@pytest.mark.parametrize("over", [{"amount": "1"}, {"payTo": "0x" + "11" * 20}, {"network": "eip155:1"},
                                  {"asset": "0x" + "22" * 20}])
def test_v2_payment_for_other_terms_is_refused_before_verify(server, over):
    base, app, fac, calls = server()
    st, _, b = call(base + mf.TOOL_PATH, "POST", VALID, {"PAYMENT-SIGNATURE": v2_payment(**over)})
    assert st == 402 and b["error"] == "payment_requirements_mismatch" and fac.calls == [] and calls == []


def test_invalid_payment_is_402_and_nothing_runs(server):
    base, app, fac, calls = server(facilitator=FakeFacilitator(valid=False))
    st, _, b = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment()})
    assert st == 402 and b["error"] == "invalid_exact_evm_payload_signature" and calls == []


@pytest.mark.parametrize("header", ["not-base64!!", base64.b64encode(b"[]").decode(), "A" * 9000])
def test_garbage_payment_headers_are_402(server, header):
    base, app, fac, calls = server()
    st, _, b = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": header})
    assert st == 402 and calls == [] and fac.calls == []


def test_failed_settlement_returns_no_result(server):
    base, app, fac, calls = server(facilitator=FakeFacilitator(settle_ok=False, retryable=True))
    st, _, b = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("02")})
    assert st == 402 and b["error"] == "settlement_failed" and "leads" not in b
    assert app.replay.reserve("x402:base:%s:%s:0x%s" % (payments.USDC["base"][1], PAYER, "02" * 32))  # released


def test_non_retryable_settlement_failure_keeps_the_reservation(server):
    base, app, fac, calls = server(facilitator=FakeFacilitator(settle_ok=False, retryable=False))
    call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("03")})
    assert not app.replay.reserve("x402:base:%s:%s:0x%s" % (payments.USDC["base"][1], PAYER, "03" * 32))


def test_empty_result_is_not_charged(server):
    base, app, fac, calls = server(result={**FAKE_RESULT, "candidates_found": 0, "leads": []})
    st, _, b = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("04")})
    assert st == 200 and b["payment"]["status"] == "not_charged"
    assert [c[0] for c in fac.calls] == ["verify"]


def test_busy_server_refuses_before_taking_payment(server):
    base, app, fac, calls = server(settings=tool_settings(max_concurrent=1))
    app.slots.acquire()
    try:
        st, h, _ = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("05")})
    finally:
        app.slots.release()
    assert st == 503 and fac.calls == [] and h.get("Retry-After")


@pytest.mark.parametrize("headers,body,status", [({"Content-Length": "40000"}, b"x" * 40000, 413)])
def test_body_size_cap(server, headers, body, status):
    base, *_ = server()
    assert call(base + mf.TOOL_PATH, "POST", body, {"X-PAYMENT": v1_payment()})[0] == status


def test_unpaid_server_without_payee_refuses_paid_route(server):
    base, *_ = server(settings=tool_settings(payee_address=""))
    assert call(base + mf.TOOL_PATH, "POST", VALID)[0] == 503


# ------------------------------------------------------------------------------------- input validation
@pytest.mark.parametrize("bad", [
    {"offer": "bridge_dev"},
    {**VALID, "github_queries": []},
    {**VALID, "github_queries": ["a" * 121]},
    {**VALID, "github_queries": ["a", "b", "c", "d"]},
    {**VALID, "max_candidates": 9},
    {**VALID, "max_candidates": True},
    {**VALID, "target_chains": [999]},
    {**VALID, "budget_usd": 100},
    {**VALID, "allowed_sources": ["mock"]},
    {**VALID, "seed_projects": [{"repos": ["acme/../x"]}]},
    {**VALID, "seed_projects": [{"website": "http://127.0.0.1:8080/"}]},
    {**VALID, "seed_projects": [{"website": "localhost"}]},
    {**VALID, "seed_projects": [{"github_org": "acme", "private_key": "x"}]},
    {**VALID, "seed_projects": [{"name": "x"}]},
    {**VALID, "note": "0x" + "ab" * 32},
    [1, 2],
])
def test_validate_input_rejects(bad):
    with pytest.raises(ValueError):
        validate_input(bad, 8)


def test_validate_input_applies_server_cap():
    assert validate_input({**VALID, "max_candidates": 8}, 4)["max_candidates"] == 4


# ------------------------------------------------------------------------------------- other routes
def test_manifest_icon_health_and_index_routes(server):
    base, *_ = server()
    st, _, m = call(base + mf.MANIFEST_PATH)
    assert st == 200 and mf.manifest_hash(m) == EXPECTED_HASH
    st, h, png = call(base + mf.ICON_PATH)
    assert st == 200 and h["Content-Type"] == "image/png" and png[:4] == b"\x89PNG"
    st, _, hb = call(base + "/health")
    assert st == 200 and hb["paid"] is True and hb["manifest"] is True
    st, _, idx = call(base + "/")
    assert idx["endpoint"]["path"] == mf.TOOL_PATH and idx["price"]["amount_usdc"] == "0.50"
    assert call(base + "/nope")[0] == 404


def test_manifest_route_503_when_unconfigured(server):
    base, *_ = server(settings=tool_settings(public_base_url=""))
    assert call(base + mf.MANIFEST_PATH)[0] == 503


# ------------------------------------------------------------------------------------- register page
def test_register_tx_encodes_registerTool_for_the_live_manifest():
    tx = register_tx(tool_settings())
    assert tx["to"] == "0x265BB2DBFC0A8165C9A1941Eb1372F349baD2cf1" and tx["chainId"] == 8453
    assert tx["metadataURI"] == BASE + mf.MANIFEST_PATH and tx["manifestHash"] == EXPECTED_HASH
    d = tx["data"]
    assert d.startswith("0xfe1d0b16") and d[10:74] == f"{0x60:064x}" and d[74:138] == EXPECTED_HASH[2:]
    assert d[138:202] == "0" * 64                                    # access predicate 0x0
    n = int(d[202:266], 16)
    assert bytes.fromhex(d[266:266 + 2 * n]).decode() == tx["metadataURI"] and (len(d) - 2 - 8) % 64 == 0


def test_register_page_has_csp_matching_script_and_no_secrets(server):
    base, *_ = server()
    st, h, html = call(base + "/register-onchain")
    html = html.decode()
    assert st == 200 and h["Content-Security-Policy"] == REGISTER_PAGE_CSP
    digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in REGISTER_PAGE_CSP and "connect-src 'self'" in REGISTER_PAGE_CSP
    assert EXPECTED_HASH in html and "registerTool(string,bytes32,address)" in html
    assert "/register-onchain/check" in html and "eth_sendTransaction" in html
    assert not re.search(r"private.?key|mnemonic|seed phrase", html, re.I)


class FakeRegistryRpc:
    def __init__(self, uri):
        enc = uri.encode()
        data = "0x" + f"{0x40:064x}" + "11" * 32 + f"{len(enc):064x}" + enc.hex() + "00" * ((32 - len(enc) % 32) % 32)
        self.logs = [{"topics": ["0xe7", hex(901), "0xcreator"], "blockNumber": hex(53_300_000), "data": data,
                      "transactionHash": TX},
                     {"topics": ["0xe7", hex(902), "0xcreator"], "blockNumber": hex(53_300_001),
                      "data": "0x" + f"{0x40:064x}" + "11" * 32 + f"{3:064x}" + b"abc".hex() + "00" * 29}]
        self.ranges = []

    def block_number(self):
        return 53_400_000

    def get_logs(self, address, frm, to, topics=None, max_range=0):
        self.ranges.append((frm, to, max_range, topics))
        return self.logs


def test_duplicate_checker_finds_existing_registration_of_this_uri():
    s = tool_settings()
    rpc = FakeRegistryRpc(BASE + mf.MANIFEST_PATH)
    res = DuplicateChecker(s, client=rpc).check()
    assert res["ok"] and [r["tool_id"] for r in res["registered"]] == [901]
    frm, to, step, topics = rpc.ranges[0]
    assert frm == 53_200_000 and to == 53_400_000 and step == 50_000
    assert topics[2] == "0x" + CREATOR[2:].rjust(64, "0")


def test_duplicate_checker_reports_rpc_failure():
    class Broken(FakeRegistryRpc):
        def block_number(self):
            from chainlead.chain.rpc import RpcError
            raise RpcError("limit")
    assert DuplicateChecker(tool_settings(), client=Broken("x")).check()["ok"] is False


# ------------------------------------------------------------------------------------- real pipeline
def test_discover_runs_the_real_pipeline_offline_and_forgets_the_request(fake_http):
    from tests.integration.test_real_providers_http import _github_and_site
    site = fake_http.url + "/site/"
    _github_and_site(fake_http, site)
    s = Settings(db_path=":memory:", api_keys={}, github_token=None)
    eng = ChainLead(Storage(":memory:"), s, [GitHubProvider(s, api_base=fake_http.url),
                                             PublicWebProvider(s, allow_private_hosts=True)], None)
    app = ToolApp(tool_settings(), engine=eng, facilitator=FakeFacilitator())
    out = app.discover(validate_input({"offer": "bridge_dev", "github_queries": ["topic:bridge"],
                                       "include_markdown": True, "lang": "fa"}, 8))
    assert out["candidates_found"] >= 1 and out["leads"][0]["project"]["name"].lower().startswith("orbit")
    lead = out["leads"][0]
    assert lead["status"] in ("qualified", "review") and "markdown" in lead and lead["signals"]
    assert out["content_notice"].startswith("Project names")
    assert eng.storage.list_campaigns("tool") == []          # request-specific rows purged


def test_bridgepilot_style_env_aliases(monkeypatch):
    from chainlead.tool.config import ToolSettings
    for k in ("CHAINLEAD_PUBLIC_BASE_URL", "CHAINLEAD_PAYEE_ADDRESS", "CHAINLEAD_CREATOR_ADDRESS", "CHAINLEAD_PRICE_USDC"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://chainlead.onrender.com/")
    monkeypatch.setenv("X402_PAY_TO", "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4")
    monkeypatch.setenv("TOOL_CREATOR_ADDRESS", "0x93714D5970f511Ac22f0E5635D3fBF538f717cd4")
    monkeypatch.setenv("X402_MODE", "facilitator")
    monkeypatch.setenv("X402_PRICE_ATOMIC", "500000")
    s = ToolSettings()
    assert s.public_base_url == "https://chainlead.onrender.com"
    assert s.price_usdc == "0.5" and s.paid_enabled and s.manifest_problem() is None
    assert s.x402().requirements("https://x/v1", "d")["maxAmountRequired"] == "500000"
    monkeypatch.setenv("X402_MODE", "off")
    assert not ToolSettings().paid_enabled


# ------------------------------------------------------------------------------------- CDP Facilitator
# Optional CDP Facilitator (agentic.market / CDP Bazaar): only when BOTH CDP_API_KEY_ID and CDP_API_KEY_SECRET
# are set, otherwise PayAI exactly as before. Test keys are generated per run; none are in the repo.
CDP_KEY_ID = "organizations/00000000-0000-0000-0000-000000000000/apiKeys/11111111-1111-1111-1111-111111111111"


def _ed25519_secret():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    k = ed25519.Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(seed + pub).decode(), k.public_key()


def _ec_secret():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                          serialization.NoEncryption()).decode()
    return pem, k.public_key()


def _jwt_parts(token):
    def d(x):
        return base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
    h, c, s = token.split(".")
    return json.loads(d(h)), json.loads(d(c)), f"{h}.{c}".encode(), d(s)


class _Resp:
    def __init__(self, status, body):
        self.status, self._raw = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wire(monkeypatch):
    """Captures outgoing facilitator HTTP calls (url, headers, JSON body) made by payments.Facilitator."""
    sent = []
    real_urlopen = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        if not isinstance(req, urllib.request.Request) or not req.full_url.startswith("https://"):
            return real_urlopen(req, timeout=timeout)  # the test client talking to the local server
        sent.append({"url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()},
                     "body": json.loads(req.data)})
        if req.full_url.endswith("/verify"):
            return _Resp(200, {"isValid": True, "payer": PAYER})
        return _Resp(200, {"success": True, "transaction": TX, "network": "eip155:8453", "payer": PAYER})
    monkeypatch.setattr(payments.urllib.request, "urlopen", fake_urlopen)
    return sent


def v2_payment_bare(nonce="ba"):
    """v2 payment as a minimal client sends it: no resource, no extensions."""
    body = json.loads(base64.b64decode(v2_payment(nonce)))
    body.pop("resource")
    return base64.b64encode(json.dumps(body).encode()).decode()


@pytest.mark.parametrize("env", [{}, {"CDP_API_KEY_ID": CDP_KEY_ID}, {"CDP_API_KEY_SECRET": "x" * 88},
                                 {"CDP_API_KEY_ID": " ", "CDP_API_KEY_SECRET": "x" * 88}])
def test_payai_unless_both_cdp_keys_are_set(env):
    f = payments.make_facilitator(payments.PAYAI_FACILITATOR_URL, env)
    assert f.kind == "payai" and f.url == payments.PAYAI_FACILITATOR_URL and type(f) is payments.Facilitator


def test_both_cdp_keys_select_cdp_facilitator():
    secret, _ = _ed25519_secret()
    f = payments.make_facilitator(payments.PAYAI_FACILITATOR_URL, {"CDP_API_KEY_ID": CDP_KEY_ID,
                                                                   "CDP_API_KEY_SECRET": secret})
    assert f.kind == "cdp" and f.url == "https://api.cdp.coinbase.com/platform/v2/x402"
    assert secret not in repr(f) and secret not in str(vars(type(f)))


def test_unusable_cdp_secret_keeps_payai_and_is_never_logged(caplog):
    pytest.importorskip("cryptography")
    bad = "not-a-key-SECRETVALUE"
    with caplog.at_level("WARNING"):
        f = payments.make_facilitator(payments.PAYAI_FACILITATOR_URL, {"CDP_API_KEY_ID": CDP_KEY_ID,
                                                                       "CDP_API_KEY_SECRET": bad})
    assert f.kind == "payai" and "not a usable key" in caplog.text and bad not in caplog.text
    with pytest.raises(ValueError) as e:
        payments.parse_cdp_secret(bad)
    assert bad not in str(e.value)


def test_missing_cryptography_keeps_payai(monkeypatch, caplog):
    def no_crypto(*a, **k):
        raise ImportError("no cryptography")
    monkeypatch.setattr(payments, "parse_cdp_secret", no_crypto)
    with caplog.at_level("WARNING"):
        f = payments.make_facilitator(payments.PAYAI_FACILITATOR_URL, {"CDP_API_KEY_ID": CDP_KEY_ID,
                                                                       "CDP_API_KEY_SECRET": "x"})
    assert f.kind == "payai" and "not installed" in caplog.text


def test_cdp_jwt_ed25519_shape_and_signature():
    secret, pub = _ed25519_secret()
    h, c, signing_input, sig = _jwt_parts(payments.cdp_jwt(CDP_KEY_ID, secret, "POST", "/platform/v2/x402/verify",
                                                           now=1_800_000_000))
    assert h["alg"] == "EdDSA" and h["kid"] == CDP_KEY_ID and h["typ"] == "JWT" and re.fullmatch(r"\d{16}", h["nonce"])
    assert c == {"sub": CDP_KEY_ID, "iss": "cdp", "nbf": 1_800_000_000, "exp": 1_800_000_120,
                 "uris": ["POST api.cdp.coinbase.com/platform/v2/x402/verify"]}
    pub.verify(sig, signing_input)  # raises if invalid


@pytest.mark.parametrize("escaped", [False, True])
def test_cdp_jwt_es256_pem_secret(escaped):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    secret, pub = _ec_secret()
    if escaped:
        secret = secret.replace("\n", "\\n")
    h, c, signing_input, sig = _jwt_parts(payments.cdp_jwt(CDP_KEY_ID, secret, "POST", "/platform/v2/x402/settle"))
    assert h["alg"] == "ES256" and len(sig) == 64
    assert c["uris"] == ["POST api.cdp.coinbase.com/platform/v2/x402/settle"]
    pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")),
               signing_input, ec.ECDSA(hashes.SHA256()))


def test_cdp_jwt_matches_official_cdp_sdk():
    """Cross-check with Coinbase's own cdp-sdk (skipped where it is not installed)."""
    sdk = pytest.importorskip("cdp.auth.utils.jwt")
    jwt = pytest.importorskip("jwt")
    for secret, pub in (_ed25519_secret(), _ec_secret()):
        ours = payments.cdp_jwt(CDP_KEY_ID, secret, "POST", "/platform/v2/x402/settle")
        ref = sdk.generate_jwt(sdk.JwtOptions(api_key_id=CDP_KEY_ID, api_key_secret=secret, request_method="POST",
                                              request_host="api.cdp.coinbase.com",
                                              request_path="/platform/v2/x402/settle"))
        alg = jwt.get_unverified_header(ref)["alg"]
        a = jwt.decode(ours, pub, algorithms=[alg], options={"verify_aud": False})
        b = jwt.decode(ref, pub, algorithms=[alg], options={"verify_aud": False})
        assert jwt.get_unverified_header(ours)["alg"] == alg
        assert sorted(jwt.get_unverified_header(ours)) == sorted(jwt.get_unverified_header(ref))
        assert {k: v for k, v in a.items() if k not in ("nbf", "exp")} == \
            {k: v for k, v in b.items() if k not in ("nbf", "exp", "aud")}
        assert a["exp"] - a["nbf"] == b["exp"] - b["nbf"] == 120


def test_cdp_v2_payment_is_authenticated_and_carries_resource_and_bazaar(server, wire):
    secret, pub = _ed25519_secret()
    fac = payments.CdpFacilitator(CDP_KEY_ID, secret)
    base, app, _, calls = server(facilitator=fac)
    _, h402, _ = call(base + mf.TOOL_PATH, "POST", {})
    challenge = v2_header(h402)
    st, h, b = call(base + mf.TOOL_PATH, "POST", VALID, {"PAYMENT-SIGNATURE": v2_payment_bare("c1")})
    assert st == 200 and b["payment"]["status"] == "settled"
    assert [w["url"] for w in wire] == ["https://api.cdp.coinbase.com/platform/v2/x402/verify",
                                        "https://api.cdp.coinbase.com/platform/v2/x402/settle"]
    for w in wire:
        hd, cl, si, sig = _jwt_parts(w["headers"]["authorization"].removeprefix("Bearer "))
        pub.verify(sig, si)
        assert cl["uris"] == ["POST api.cdp.coinbase.com" + w["url"].split("api.cdp.coinbase.com")[1]]
        assert "source=x402" in w["headers"]["correlation-context"]
        p = w["body"]["paymentPayload"]
        assert w["body"]["x402Version"] == 2 and w["body"]["paymentRequirements"] == challenge["accepts"][0]
        assert p["resource"] == challenge["resource"]
        assert p["extensions"]["bazaar"] == challenge["extensions"]["bazaar"]
        assert p["payload"] == json.loads(base64.b64decode(v2_payment_bare("c1")))["payload"]  # signed part untouched
        assert secret not in json.dumps(w)


def test_cdp_keeps_client_sent_resource_and_extensions(server, wire):
    secret, _ = _ed25519_secret()
    base, app, _, _ = server(facilitator=payments.CdpFacilitator(CDP_KEY_ID, secret))
    own = json.loads(base64.b64decode(v2_payment("c2")))
    own["extensions"] = {"other": {"a": 1}}
    st, _, _ = call(base + mf.TOOL_PATH, "POST", VALID,
                    {"PAYMENT-SIGNATURE": base64.b64encode(json.dumps(own).encode()).decode()})
    assert st == 200
    p = wire[-1]["body"]["paymentPayload"]
    assert p["resource"] == {"url": BASE + mf.TOOL_PATH}
    assert p["extensions"]["other"] == {"a": 1} and p["extensions"]["bazaar"]["info"]["input"]["body"] == mf.EXAMPLE_INPUT


def test_cdp_v1_payment_is_authenticated_and_forwarded_unchanged(server, wire):
    secret, _ = _ed25519_secret()
    base, _, _, _ = server(facilitator=payments.CdpFacilitator(CDP_KEY_ID, secret))
    st, _, _ = call(base + mf.TOOL_PATH, "POST", VALID, {"X-PAYMENT": v1_payment("c3")})
    assert st == 200 and all(w["headers"]["authorization"].startswith("Bearer ") for w in wire)
    assert wire[-1]["body"]["paymentPayload"] == json.loads(base64.b64decode(v1_payment("c3")))


def test_payai_v2_payload_and_headers_are_unchanged(server, wire):
    base, _, _, _ = server(facilitator=payments.Facilitator(payments.PAYAI_FACILITATOR_URL))
    st, _, _ = call(base + mf.TOOL_PATH, "POST", VALID, {"PAYMENT-SIGNATURE": v2_payment_bare("c4")})
    assert st == 200
    assert [w["url"] for w in wire] == ["https://facilitator.payai.network/verify",
                                        "https://facilitator.payai.network/settle"]
    for w in wire:
        assert "authorization" not in w["headers"] and "correlation-context" not in w["headers"]
        assert w["body"]["paymentPayload"] == json.loads(base64.b64decode(v2_payment_bare("c4")))


def test_toolapp_default_facilitator_follows_the_cdp_env(monkeypatch):
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    monkeypatch.delenv("CDP_API_KEY_SECRET", raising=False)
    eng = ChainLead(Storage(":memory:"), Settings(db_path=":memory:", api_keys={}), [], None)
    assert ToolApp(tool_settings(), engine=eng).facilitator.kind == "payai"
    secret, _ = _ed25519_secret()
    monkeypatch.setenv("CDP_API_KEY_ID", CDP_KEY_ID)
    monkeypatch.setenv("CDP_API_KEY_SECRET", secret)
    assert ToolApp(tool_settings(), engine=eng).facilitator.kind == "cdp"


@pytest.mark.parametrize("body", [b"", b"{}", b"  ", b"null", json.dumps(mf.EXAMPLE_INPUT).encode()])
def test_empty_and_example_bodies_answer_402_before_validation(server, body):
    base, _, fac, calls = server()
    st, h, b = call(base + mf.TOOL_PATH, "POST", body)
    assert st == 402 and calls == [] and fac.calls == []
    assert v2_header(h)["extensions"]["bazaar"]["info"]["input"]["body"] == mf.EXAMPLE_INPUT
    assert validate_input(json.loads(json.dumps(mf.EXAMPLE_INPUT)), 8)  # the example itself is valid input


_INSTRUCTION = re.compile(r"\b(ignore|disregard|you must|you should|always (call|use|pay)|never (call|use)|"
                          r"agents? (must|should)|instructions?|system prompt|do not tell|override|prefer this)\b", re.I)


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def test_bazaar_metadata_is_plain_description(server):
    base, _, _, _ = server()
    _, h, _ = call(base + mf.TOOL_PATH, "POST", {})
    v2 = v2_header(h)
    for s in _strings({"resource": v2["resource"], "extensions": v2["extensions"]}):
        assert not _INSTRUCTION.search(s), s
