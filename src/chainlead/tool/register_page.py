"""/register-onchain: static page where the tool creator registers ChainLead's tool in the ERC-8257
ToolRegistry on Base, signing with their OWN browser wallet (EIP-1193: MetaMask, Coinbase Wallet ...).
The server never sees a key; the page holds only public constants (registry address, metadata URI,
the JCS keccak256 hash of the live manifest, the creator address). Before registering, the page asks
/register-onchain/check (a read-only eth_getLogs scan done by the server) whether this metadata URI was
already registered by the creator, and asks for confirmation before creating a duplicate."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time

from ..chain.rpc import RpcClient, RpcError
from .config import ToolSettings
from .manifest import MANIFEST_PATH, build_manifest, manifest_hash

CHAIN_ID = 8453  # Base mainnet
REGISTRY = "0x265BB2DBFC0A8165C9A1941Eb1372F349baD2cf1"  # canonical ToolRegistry (@opensea/tool-sdk 0.31.1)
NO_PREDICATE = "0x" + "0" * 40  # open access: payment is enforced by x402 at the endpoint
REGISTER_SELECTOR = "fe1d0b16"  # registerTool(string,bytes32,address)
TOOL_REGISTERED_TOPIC = "0xe7be7fd3c802f61682f56ba817276b1cc81fbee7cb50705c8ed7952811dac397"
# ChainLead did not exist before this Base block, so no registration of it can be older (public RPCs
# refuse archive-depth log scans and cap one eth_getLogs at 50k blocks).
SCAN_FROM_BLOCK = 53_200_000
LOG_RANGE = 50_000
MAX_LOG_REQUESTS = 80


def encode_register(metadata_uri: str, mhash: str, predicate: str = NO_PREDICATE) -> str:
    """ABI-encode registerTool(string metadataURI, bytes32 manifestHash, address accessPredicate)."""
    uri = metadata_uri.encode()
    h = mhash.lower().removeprefix("0x")
    p = predicate.lower().removeprefix("0x")
    if len(h) != 64 or len(p) != 40:
        raise ValueError("bad hash or address")

    def word(n: int) -> str:
        return f"{n:064x}"
    padded = uri.hex() + "00" * ((32 - len(uri) % 32) % 32)
    return "0x" + REGISTER_SELECTOR + word(0x60) + h + p.rjust(64, "0") + word(len(uri)) + padded


def register_tx(settings: ToolSettings) -> dict:
    m = build_manifest(settings)
    uri = f"{settings.public_base_url}{MANIFEST_PATH}"
    h = manifest_hash(m)
    return {"metadataURI": uri, "manifestHash": h, "creator": settings.creator_address, "to": REGISTRY,
            "chainId": CHAIN_ID, "data": encode_register(uri, h)}


def decode_uri(data: str) -> str | None:
    try:
        h = data[2:]
        off = int(h[:64], 16) * 2
        n = int(h[off:off + 64], 16)
        return bytes.fromhex(h[off + 64:off + 64 + 2 * n]).decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


class DuplicateChecker:
    """Read-only scan of ToolRegistered logs by the creator for our metadata URI (cached 60 s)."""

    def __init__(self, settings: ToolSettings, client: RpcClient | None = None) -> None:
        self.settings = settings
        self.client = client or RpcClient(settings.registry_rpc_url, timeout=15)
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    def check(self) -> dict:
        with self._lock:
            if self._cache and time.time() - self._cache[0] < 60:
                return self._cache[1]
            tx = register_tx(self.settings)
            creator_topic = "0x" + tx["creator"][2:].rjust(64, "0")
            try:
                head = self.client.block_number()
                start = max(SCAN_FROM_BLOCK, head - LOG_RANGE * MAX_LOG_REQUESTS + 1)
                logs = self.client.get_logs(REGISTRY, start, head, topics=[TOOL_REGISTERED_TOPIC, None, creator_topic],
                                            max_range=LOG_RANGE)
            except (RpcError, ValueError) as e:
                return {"ok": False, "error": f"registry scan failed: {str(e)[:160]}"}
            same = [{"tool_id": int(lg["topics"][1], 16), "block": int(lg["blockNumber"], 16),
                     "tx": lg.get("transactionHash")}
                    for lg in logs if decode_uri(lg.get("data", "0x")) == tx["metadataURI"]]
            res = {"ok": True, "metadataURI": tx["metadataURI"], "manifestHash": tx["manifestHash"],
                   "registered": same, "scanned_from_block": start, "head": head,
                   "note": "Deregistered tools are listed too; check opensea.io/tools/erc8257/base/<id>."}
            self._cache = (time.time(), res)
            return res


SCRIPT = r"""
"use strict";
const T = JSON.parse(document.getElementById("tx").textContent);
const CHAIN_HEX = "0x2105";
const $ = (id) => document.getElementById(id);
const eth = window.ethereum;
let account = null, chainOk = false, dup = null, checked = false;
const LS = "chainlead-reg-" + T.manifestHash;
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const txLink = (h) => '<a target="_blank" rel="noopener" href="https://basescan.org/tx/' + esc(h) + '">' + esc(h) + "</a>";
const toolLink = (id) => '<a target="_blank" rel="noopener" href="https://opensea.io/tools/erc8257/base/' + esc(id) + '">opensea.io/tools/erc8257/base/' + esc(id) + "</a>";
function fmtEth(wei){ return (Number(wei) / 1e18).toFixed(9); }
async function checkDuplicate(){
  const el = $("dup");
  try {
    const r = await (await fetch("/register-onchain/check", {cache: "no-store"})).json();
    if (!r.ok) throw new Error(r.error || "scan failed");
    checked = true;
    if (r.registered.length) { dup = String(r.registered[r.registered.length - 1].tool_id);
      el.innerHTML = '<span class="warn">این ابزار قبلاً ثبت شده است: Tool #' + esc(dup) + " · Already registered as Tool #" + esc(dup) + "</span><br>" + toolLink(dup);
    } else el.innerHTML = '<span class="ok">ثبت قبلی برای این آدرس پیدا نشد · No earlier registration of this URL found</span>';
  } catch (e) { el.innerHTML = '<small class="warn">بررسی ثبت تکراری ممکن نشد؛ پیش از ثبت، صفحه‌ی ابزارها در OpenSea را نگاه کن · Could not check for duplicates (' + esc(e && e.message || e) + ")</small>"; }
}
function mismatch(){ return account && account.toLowerCase() !== T.creator.toLowerCase(); }
async function refresh(){
  if (!eth) return;
  account = (await eth.request({ method: "eth_accounts" }))[0] || null;
  const chainId = await eth.request({ method: "eth_chainId" });
  chainOk = String(chainId).toLowerCase() === CHAIN_HEX;
  $("switch").disabled = !account || chainOk;
  let s = account ? "حساب · Account: " + esc(account) : "وصل نیست · Not connected";
  s += "<br>شبکه · Network: " + (chainOk ? '<span class="ok">Base ✓</span>' : '<span class="warn">' + esc(chainId) + " (not Base)</span>");
  if (account && chainOk) {
    try { const bal = BigInt(await eth.request({ method: "eth_getBalance", params: [account, "latest"] }));
      s += "<br>موجودی · Balance: " + fmtEth(bal) + " ETH" + (bal === 0n ? ' <span class="err">— برای گس کمی ETH روی Base لازم است · you need a little ETH on Base</span>' : "");
    } catch (e) {}
  }
  $("status").innerHTML = s;
  $("acctWarn").innerHTML = mismatch() ? "هشدار: این حساب با سازنده‌ی مانیفست (" + esc(T.creator) + ") یکی نیست.<br><span class='en'>Connected account differs from the manifest creator; OpenSea would reject the listing.</span>" : "";
  const ready = account && chainOk && !mismatch();
  $("register").disabled = !ready;
  if (ready) estimate();
}
async function estimate(){
  try { const [gas, price] = await Promise.all([eth.request({ method: "eth_estimateGas", params: [{ from: account, to: T.to, data: T.data }] }), eth.request({ method: "eth_gasPrice" })]);
    $("est").innerHTML = "<small>Estimated gas: " + BigInt(gas) + " × " + (Number(BigInt(price)) / 1e9).toFixed(4) + " gwei ≈ " + fmtEth(BigInt(gas) * BigInt(price)) + " ETH (+ tiny L1 data fee)</small>";
  } catch (e) { $("est").innerHTML = '<small class="err">Simulation failed: ' + esc(e && e.message || e) + "</small>"; }
}
async function register(){
  const res = $("res");
  if (!checked && !confirm("بررسی ثبت تکراری انجام نشد. ادامه؟\nThe duplicate check did not complete. Continue anyway?")) return;
  if ((dup || localStorage.getItem(LS)) && !confirm("این ابزار قبلاً ثبت/ارسال شده. ثبت دوباره یک ابزار تکراری می‌سازد. ادامه؟\nAlready registered or sent. Registering again creates a duplicate. Continue?")) return;
  $("register").disabled = true;
  try {
    const hash = await eth.request({ method: "eth_sendTransaction", params: [{ from: account, to: T.to, data: T.data }] });
    localStorage.setItem(LS, hash);
    res.innerHTML = "ارسال شد، در انتظار تأیید · Sent, waiting for confirmation: " + txLink(hash);
    for (let i = 0; i < 100; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const rc = await eth.request({ method: "eth_getTransactionReceipt", params: [hash] });
      if (!rc) continue;
      if (rc.status !== "0x1") { res.innerHTML = '<span class="err">تراکنش ناموفق · Transaction failed</span> ' + txLink(hash); $("register").disabled = false; return; }
      const log = (rc.logs || []).find((l) => l.address.toLowerCase() === T.to.toLowerCase() && l.topics[0] === T.topic);
      const id = log ? BigInt(log.topics[1]).toString() : "?";
      res.innerHTML = '<span class="ok">ثبت شد ✓ · Registered ✓</span> Tool ID: <b>' + esc(id) + "</b><br>" + txLink(hash) + (id !== "?" ? "<br>" + toolLink(id) : "");
      return;
    }
    res.innerHTML += "<br>هنوز تأیید نشده؛ لینک بالا را در Basescan چک کن · Not confirmed yet, check Basescan.";
  } catch (e) {
    res.innerHTML = '<span class="err">' + (e && e.code === 4001 ? "لغو شد · Rejected in wallet" : esc(e && e.message || e)) + "</span>";
    $("register").disabled = false;
  }
}
$("connect").onclick = async () => {
  if (!eth) { $("status").innerHTML = '<span class="err">کیف‌پول مرورگر پیدا نشد · No browser wallet found (window.ethereum)</span>'; return; }
  try { await eth.request({ method: "eth_requestAccounts" }); } catch (e) { $("status").innerHTML = '<span class="err">' + esc(e.message) + "</span>"; return; }
  await refresh();
};
$("switch").onclick = async () => {
  try { await eth.request({ method: "wallet_switchEthereumChain", params: [{ chainId: CHAIN_HEX }] }); }
  catch (e) {
    if (e && (e.code === 4902 || (e.data && e.data.originalError && e.data.originalError.code === 4902))) {
      await eth.request({ method: "wallet_addEthereumChain", params: [{ chainId: CHAIN_HEX, chainName: "Base", nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 }, rpcUrls: ["https://mainnet.base.org"], blockExplorerUrls: ["https://basescan.org"] }] });
    } else $("status").innerHTML = '<span class="err">' + esc(e && e.message || e) + "</span>";
  }
  await refresh();
};
$("register").onclick = register;
const prev = localStorage.getItem(LS);
if (prev) $("res").innerHTML = '<span class="warn">قبلاً از این مرورگر ارسال شده · Already sent from this browser:</span> ' + txLink(prev);
checkDuplicate();
if (eth) { eth.on && eth.on("accountsChanged", refresh); eth.on && eth.on("chainChanged", refresh); refresh(); }
else $("status").innerHTML = "کیف‌پول مرورگر پیدا نشد · No browser wallet detected";
"""

SCRIPT_CSP_HASH = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"
REGISTER_PAGE_CSP = (f"default-src 'none'; style-src 'unsafe-inline'; script-src {SCRIPT_CSP_HASH}; "
                     "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


def register_page(settings: ToolSettings) -> str:
    tx = register_tx(settings)
    data = json.dumps({**tx, "topic": TOOL_REGISTERED_TOPIC}).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>ChainLead · Register on OpenSea Tools (Base)</title>
<style>
 body{{font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;background:#0f1115;color:#e8e8ea;margin:0;padding:24px;line-height:1.6}}
 main{{max-width:760px;margin:0 auto}} h1{{font-size:1.4rem;margin:0 0 4px}}
 .en{{color:#9aa0aa;font-size:.92em;direction:ltr;text-align:left}} .fa{{direction:rtl;text-align:right}}
 .card{{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin:14px 0}}
 button{{background:#2f6fed;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:1rem;cursor:pointer;margin:6px 0}}
 button:disabled{{background:#3a3f4a;color:#8a8f99;cursor:not-allowed}}
 code,.mono{{font-family:ui-monospace,Menlo,monospace;font-size:.85em;word-break:break-all;direction:ltr}}
 .warn{{color:#ffb84d}} .ok{{color:#4ddc8a}} .err{{color:#ff6b6b}} a{{color:#7fb2ff}} small{{color:#9aa0aa}}
</style>
</head>
<body><main>
<h1 class="fa">ثبت ابزار ChainLead در OpenSea روی شبکه‌ی Base</h1>
<div class="en">Register the ChainLead Web3 lead-discovery tool in the ERC-8257 ToolRegistry on Base mainnet</div>
<div class="card">
 <div class="fa">کیف‌پول خودت را وصل کن. همه‌چیز را خودت امضا می‌کنی؛ این سایت هیچ کلیدی نمی‌بیند. هزینه فقط گس ناچیز Base است.</div>
 <div class="en">Connect your own wallet. You sign the transaction yourself; this site never sees a key. Cost: a tiny Base gas fee.</div>
 <button id="connect">اتصال کیف‌پول · Connect wallet</button>
 <button id="switch" disabled>رفتن به شبکه‌ی Base · Switch to Base</button>
 <div id="status" class="mono">—</div>
 <div id="acctWarn" class="warn"></div>
</div>
<div class="card">
 <div class="fa"><b>کشف لید Web3</b></div><div class="en"><b>ChainLead Web3 Lead Discovery</b></div>
 <div id="dup" class="fa"><small>در حال بررسی ثبت قبلی · Checking for an earlier registration…</small></div>
 <button id="register" disabled>ثبت · Register tool</button>
 <div id="est" class="en"></div>
 <div id="res" class="mono"></div>
 <details><summary class="en"><small>Transaction details</small></summary><div class="mono">
 to: {_esc(tx["to"])}<br>function: registerTool(string,bytes32,address)<br>metadataURI: {_esc(tx["metadataURI"])}<br>manifestHash: {_esc(tx["manifestHash"])}<br>accessPredicate: {NO_PREDICATE}<br>creator: {_esc(tx["creator"])}<br>data: {_esc(tx["data"])}</div></details>
</div>
<div class="card"><div class="fa"><small>هر ابزار را فقط یک بار ثبت کن؛ ثبت دوباره یک ابزار تکراری می‌سازد.</small></div>
<div class="en"><small>Register only once; registering again creates a duplicate. Registry <a target="_blank" rel="noopener" href="https://basescan.org/address/{REGISTRY}"><code>{REGISTRY}</code></a> · access predicate 0x0 (open; payments via x402)</small></div></div>
</main>
<script type="application/json" id="tx">{data}</script>
<script>{SCRIPT}</script>
</body></html>"""
