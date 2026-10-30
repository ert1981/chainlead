"""GET /x402-pay: the owner pays each paid route once with their own browser wallet (x402 v2).

One real, paid v2 call per route, settled through the configured facilitator, is what gets a route indexed in
the CDP x402 Bazaar (agentic.market). The page:
  * POSTs the route's example body unpaid and reads the price from the live 402's PAYMENT-REQUIRED header;
  * hard-checks x402Version 2, scheme exact, network eip155:8453, asset == USDC on Base, payTo == our payee and
    a sanity ceiling on the amount;
  * asks for an explicit confirm() with the amount, switches the wallet to Base if needed, and has the wallet
    sign an EIP-3009 TransferWithAuthorization (no keys ever reach this page or the server);
  * retries with PAYMENT-SIGNATURE (resource + extensions.bazaar copied from the 402) and links the
    PAYMENT-RESPONSE transaction on BaseScan. One payment per click; duplicate submits are blocked.
Strict CSP: inline script pinned by sha256, same-origin fetch only.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

SCRIPT = r'''"use strict";
// x402 v2 pay page: the owner pays each paid route once with their OWN injected wallet (EIP-3009 signature
// in the wallet; this page and the server never see a key). Same-origin fetches only.
const C = JSON.parse(document.getElementById("cfg").textContent);
const CHAIN_HEX = "0x2105";
const NETWORK = "eip155:8453";
const USDC = C.usdc.toLowerCase();
let eth = null, account = null, busy = false, listening = false, lastQuote = null;
const $ = (id) => document.getElementById(id);
function esc(s){ return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
function errMsg(e){ return esc(e && e.code === 4001 ? "لغو شد · Rejected in wallet" : (e && e.message ? e.message : e)); }
function usdc(atomic){ const v = BigInt(atomic); const f = (v % 1000000n).toString().padStart(6, "0").replace(/0+$/, ""); return (v / 1000000n).toString() + (f ? "." + f : ""); }
function txLink(h){ return '<a target="_blank" rel="noopener" href="https://basescan.org/tx/' + esc(h) + '">' + esc(h) + "</a>"; }
function errText(body, res){
  if (body && typeof body.error === "string") return body.error;
  if (body && body.error && typeof body.error.message === "string") return body.error.message;
  if (body && typeof body.detail === "string") return body.detail;
  if (body && typeof body.message === "string") return body.message;
  const h = res && res.headers.get("PAYMENT-REQUIRED");
  if (h) { try { const pr = b64json(h); if (pr && pr.error) return String(pr.error); } catch (e) {} }
  return "failed";
}
function setStatus(html){ $("status").innerHTML = html; }
function b64json(s){ const bin = atob(s); const bytes = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i); return JSON.parse(new TextDecoder().decode(bytes)); }
function tob64(obj){ const bytes = new TextEncoder().encode(JSON.stringify(obj)); let bin = ""; for (const b of bytes) bin += String.fromCharCode(b); return btoa(bin); }
function hex32(){ const b = new Uint8Array(32); crypto.getRandomValues(b); return "0x" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join(""); }
function call(method, params, ms){
  const p = eth.request(params ? { method, params } : { method });
  if (!ms) return p;
  return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout (" + method + ")")), ms))]);
}
// Wallet discovery: window.ethereum (also when injected late), EIP-6963 announcements, multi-provider arrays.
const announced = [];
window.addEventListener("eip6963:announceProvider", (ev) => { if (ev && ev.detail && ev.detail.provider) announced.push(ev.detail); });
function pickProvider(){
  const w = window.ethereum;
  if (w && Array.isArray(w.providers) && w.providers.length) return w.providers.find(p => p.isMetaMask) || w.providers.find(p => p.isCoinbaseWallet) || w.providers[0];
  if (w && typeof w.request === "function") return w;
  if (announced.length) return (announced.find(d => /metamask/i.test(d.info && d.info.rdns || "")) || announced[0]).provider;
  return null;
}
async function findProvider(waitMs){
  try { window.dispatchEvent(new Event("eip6963:requestProvider")); } catch (e) {}
  const end = Date.now() + waitMs;
  for (;;) { const p = pickProvider(); if (p || Date.now() >= end) return p; await new Promise(r => setTimeout(r, 150)); }
}
function noWallet(){
  const here = location.host + location.pathname;
  setStatus('<span class="err">کیف‌پول مرورگر پیدا نشد · No browser wallet found.</span> ' +
    '<a href="https://metamask.app.link/dapp/' + esc(here) + '">Open in MetaMask</a> · <a href="https://go.cb-w.com/dapp?cb_url=' + encodeURIComponent(location.href) + '">Open in Coinbase Wallet</a>');
}
function listen(){
  if (listening || !eth || typeof eth.on !== "function") return;
  listening = true;
  eth.on("accountsChanged", () => refresh().catch(showErr));
  eth.on("chainChanged", () => refresh().catch(showErr));
}
function showErr(e){ setStatus('<span class="err">' + errMsg(e) + "</span>"); }
function setButtons(){ for (const r of C.routes) $("pay-" + r.id).disabled = busy || !account || !r.price; }
async function refresh(){
  if (!eth) return;
  const accts = await call("eth_accounts", null, 10000);
  account = (accts && accts[0]) || null;
  const chainId = String(await call("eth_chainId", null, 10000)).toLowerCase();
  setStatus((account ? "حساب · Account: " + esc(account) : "وصل نیست · Not connected") + "<br>شبکه · Network: " +
    (chainId === CHAIN_HEX ? '<span class="ok">Base ✓</span>' : '<span class="warn">' + esc(chainId) + " (هنگام پرداخت به Base می‌رود · will switch to Base when you pay)</span>"));
  setButtons();
}
async function ensureBase(){
  const chainId = String(await call("eth_chainId", null, 10000)).toLowerCase();
  if (chainId === CHAIN_HEX) return;
  try { await call("wallet_switchEthereumChain", [{ chainId: CHAIN_HEX }]); }
  catch (e) {
    if (e && (e.code === 4902 || (e.data && e.data.originalError && e.data.originalError.code === 4902))) {
      await call("wallet_addEthereumChain", [{ chainId: CHAIN_HEX, chainName: "Base", nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
        rpcUrls: ["https://mainnet.base.org"], blockExplorerUrls: ["https://basescan.org"] }]);
    } else throw e;
  }
  const now = String(await call("eth_chainId", null, 10000)).toLowerCase();
  if (now !== CHAIN_HEX) throw new Error("wallet is not on Base mainnet (8453)");
}
function bodyFor(r){
  const b = JSON.parse(JSON.stringify(r.example));
  if (r.useAccount && account) { for (const k of r.useAccount) b[k] = account; }
  const best = lastQuote && (lastQuote.best_route || (lastQuote.result && lastQuote.result.best_route));
  if (r.fromQuote && best) {
    b.provider = best.provider || b.provider;
    if (best.expected_output) b.accepted_output = String(best.expected_output);
  }
  return b;
}
/** Unpaid POST -> 402 -> decoded, checked x402 v2 PAYMENT-REQUIRED. */
async function challenge(r){
  const res = await fetch(r.path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(bodyFor(r)), credentials: "omit", cache: "no-store" });
  const hdr = res.headers.get("PAYMENT-REQUIRED");
  if (res.status !== 402 || !hdr) throw new Error("expected HTTP 402 with a PAYMENT-REQUIRED header, got " + res.status);
  const pr = b64json(hdr);
  const a = pr && pr.accepts && pr.accepts[0];
  if (!pr || pr.x402Version !== 2 || !a) throw new Error("not an x402 v2 payment requirement");
  if (a.scheme !== "exact" || a.network !== NETWORK) throw new Error("unexpected scheme/network: " + a.scheme + " " + a.network);
  if (String(a.asset).toLowerCase() !== USDC) throw new Error("unexpected asset " + a.asset + " (expected USDC on Base " + C.usdc + ")");
  if (String(a.payTo).toLowerCase() !== C.payee.toLowerCase()) throw new Error("unexpected payTo " + a.payTo);
  if (!/^[0-9]+$/.test(String(a.amount)) || BigInt(a.amount) <= 0n || BigInt(a.amount) > BigInt(C.maxAtomic)) throw new Error("unexpected amount " + a.amount);
  return { pr, a };
}
async function loadPrices(){
  for (const r of C.routes) {
    try { const { a } = await challenge(r); r.price = a.amount; $("price-" + r.id).innerHTML = "<b>" + usdc(a.amount) + " USDC</b>"; }
    catch (e) { r.price = null; $("price-" + r.id).innerHTML = '<span class="err">' + errMsg(e) + "</span>"; }
  }
  const total = C.routes.reduce((s, r) => s + (r.price ? BigInt(r.price) : 0n), 0n);
  $("total").textContent = usdc(total) + " USDC";
  setButtons();
}
async function pay(r){
  if (busy) return;
  const out = $("res-" + r.id);
  if (!eth || !account) { out.innerHTML = '<span class="err">اول کیف‌پول را وصل کن · Connect your wallet first.</span>'; return; }
  busy = true; setButtons();
  try {
    await ensureBase();
    const { pr, a } = await challenge(r);
    let code = "0x";
    try { code = String(await call("eth_getCode", [account, "latest"], 10000) || "0x"); } catch (e) {}
    const smart = code !== "0x" && code !== "0x0";
    const smartNote = !smart ? "" : "\nهشدار: این حساب کد قرارداد دارد (اسمارت‌اکانت یا EIP-7702). USDC امضا را با isValidSignature خود حساب (ERC-1271) چک می‌کند و بسیاری از این حساب‌ها امضای عادی را رد می‌کنند؛ احتمالاً پرداخت رد می‌شود. از یک حساب عادی (EOA بدون EIP-7702) پرداخت کن." +
      "\nWARNING: this account has contract code (" + (code.slice(0, 8) === "0xef0100" ? "EIP-7702 delegation to 0x" + code.slice(8, 48) : "smart account") +
      "). USDC then checks the signature through the account's ERC-1271 isValidSignature, and many such accounts reject a plain wallet signature, so this payment will likely be refused. Pay from a plain EOA (no EIP-7702 delegation).\n";
    const prev = localStorage.getItem("x402pay-" + r.path);
    const msg = "پرداخت واقعی USDC روی Base · REAL payment in USDC on Base mainnet\n\n" + r.name + "\n" + r.path +
      "\nمبلغ · Amount: " + usdc(a.amount) + " USDC\nبه · To: " + a.payTo + "\n" +
      smartNote + (prev ? "\nهشدار: این مسیر قبلاً از این مرورگر پرداخت شده · Already paid from this browser: " + prev + "\n" : "") + "\nادامه؟ · Continue?";
    if (!confirm(msg)) { out.innerHTML = "لغو شد · Cancelled."; return; }
    const now = Math.floor(Date.now() / 1000);
    const auth = { from: account, to: a.payTo, value: String(a.amount), validAfter: String(now - 600),
      validBefore: String(now + Number(a.maxTimeoutSeconds || 300)), nonce: hex32() };
    const typed = {
      types: { EIP712Domain: [{ name: "name", type: "string" }, { name: "version", type: "string" }, { name: "chainId", type: "uint256" }, { name: "verifyingContract", type: "address" }],
        TransferWithAuthorization: [{ name: "from", type: "address" }, { name: "to", type: "address" }, { name: "value", type: "uint256" },
          { name: "validAfter", type: "uint256" }, { name: "validBefore", type: "uint256" }, { name: "nonce", type: "bytes32" }] },
      primaryType: "TransferWithAuthorization",
      domain: { name: (a.extra && a.extra.name) || "USD Coin", version: (a.extra && a.extra.version) || "2", chainId: 8453, verifyingContract: a.asset },
      message: auth,
    };
    out.innerHTML = "در انتظار امضا در کیف‌پول… · Sign the USDC authorization in your wallet…";
    const signature = await call("eth_signTypedData_v4", [account, JSON.stringify(typed)]);
    const payment = { x402Version: 2, resource: pr.resource, accepted: a, payload: { signature, authorization: auth } };
    if (pr.extensions) payment.extensions = pr.extensions;
    out.innerHTML = "ارسال پرداخت و اجرای درخواست… (ممکن است تا چند دقیقه طول بکشد) · Sending payment and running the request… (can take a few minutes)";
    const res = await fetch(r.path, { method: "POST", headers: { "Content-Type": "application/json", "PAYMENT-SIGNATURE": tob64(payment) },
      body: JSON.stringify(bodyFor(r)), credentials: "omit", cache: "no-store" });
    let receipt = null;
    const rh = res.headers.get("PAYMENT-RESPONSE") || res.headers.get("X-PAYMENT-RESPONSE");
    if (rh) { try { receipt = b64json(rh); } catch (e) {} }
    let body = null; try { body = await res.json(); } catch (e) {}
    if (r.id === "quote" && res.ok && body) lastQuote = body;
    if (!receipt && body && body.payment && body.payment.transaction) receipt = { transaction: body.payment.transaction };
    let html = "HTTP " + res.status + " ";
    if (receipt && receipt.transaction) {
      localStorage.setItem("x402pay-" + r.path, receipt.transaction);
      html += '<span class="ok">پرداخت تسویه شد ✓ · Settled ✓</span><br>tx: ' + txLink(receipt.transaction);
    } else if (res.ok) html += '<span class="warn">پاسخ بدون رسید پرداخت (پرداخت گرفته نشد) · No payment receipt (not charged)</span>';
    else html += '<span class="err">' + esc(errText(body, res)) + "</span>";
    if (body) html += "<details><summary>پاسخ · Response</summary><pre>" + esc(JSON.stringify(body, null, 2).slice(0, 4000)) + "</pre></details>";
    out.innerHTML = html;
  } catch (e) {
    out.innerHTML = '<span class="err">' + errMsg(e) + "</span>";
  } finally {
    busy = false; setButtons();
  }
}
for (const r of C.routes) $("pay-" + r.id).onclick = () => pay(r);
$("connect").onclick = async () => {
  setStatus("در حال پیدا کردن کیف‌پول… · Looking for a browser wallet…");
  eth = eth || await findProvider(1500);
  if (!eth) { noWallet(); return; }
  listen();
  try { await call("eth_requestAccounts"); await refresh(); } catch (e) { showErr(e); }
};
findProvider(2000).then((p) => {
  if (!p) { setStatus("کیف‌پول هنوز پیدا نشد؛ «اتصال کیف‌پول» را بزن · No wallet detected yet — press Connect wallet."); return; }
  eth = p; listen(); refresh().catch(showErr);
});
loadPrices();
'''

PAGE = r'''<!doctype html>
<html lang="fa">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>__NAME__ · Pay each paid route once (x402, USDC on Base)</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;background:#0f1115;color:#e8e8ea;margin:0;padding:24px;line-height:1.6}
 main{max-width:760px;margin:0 auto}
 h1{font-size:1.4rem;margin:0 0 4px} .en{color:#9aa0aa;font-size:.92em;direction:ltr;text-align:left}
 .fa{direction:rtl;text-align:right}
 .card{background:#181b22;border:1px solid #2a2f3a;border-radius:12px;padding:16px;margin:14px 0}
 button{background:#2f6fed;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:1rem;cursor:pointer;margin:6px 0}
 button:disabled{background:#3a3f4a;color:#8a8f99;cursor:not-allowed}
 code,.mono,pre{font-family:ui-monospace,Menlo,monospace;font-size:.85em;word-break:break-all;direction:ltr;white-space:pre-wrap}
 .warn{color:#ffb84d} .ok{color:#4ddc8a} .err{color:#ff6b6b}
 a{color:#7fb2ff} small{color:#9aa0aa}
</style>
</head>
<body><main>
<h1 class="fa">پرداخت یک‌باره‌ی هر مسیر پولی __NAME__</h1>
<div class="en">Pay each paid __NAME__ route once with x402 v2 (so the CDP Bazaar / agentic.market can index it)</div>

<div class="card warn">
 <div class="fa"><b>توجه: این پرداخت واقعی است</b> — USDC واقعی روی شبکه‌ی اصلی Base از کیف‌پول خودت به <code>__PAYEE__</code>. امضا فقط در کیف‌پول خودت انجام می‌شود؛ این صفحه و سرور هیچ کلیدی نمی‌بینند. گس ندارد (تسویه را فسیلیتیتور انجام می‌دهد). اگر اجرای درخواست شکست بخورد، پولی کم نمی‌شود.</div>
 <div class="en"><b>Note: this is a real payment</b> in real USDC on Base mainnet, from your own wallet to <code>__PAYEE__</code>. You sign an EIP-3009 authorization in your own wallet; this page and the server never see a key. No gas for you (the facilitator settles). If the request itself fails, nothing is charged.</div>
</div>

<div class="card">
 <button id="connect">اتصال کیف‌پول · Connect wallet</button>
 <div id="status" class="mono">—</div>
</div>

__ROUTES__

<div class="card"><div class="fa">جمع همه · Total for all routes: <b id="total">…</b></div></div>
</main>
<script type="application/json" id="cfg">__CFG__</script>
<script>__SCRIPT__</script>
</body></html>
'''

ROUTE = r'''<div class="card">
 <div><b>__RNAME__</b> · <code>POST __RPATH__</code></div>
 <div>قیمت · Price (from the live 402): <span id="price-__RID__">…</span></div>
 <button id="pay-__RID__" disabled>پرداخت · Pay</button>
 <div id="res-__RID__" class="mono"></div>
</div>
'''

SCRIPT_SHA256 = base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode("ascii")
CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-" + SCRIPT_SHA256 + "'; "
       "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
HEADERS = {"Content-Security-Policy": CSP, "Cache-Control": "no-store", "X-Robots-Tag": "noindex",
           "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


def page(service: str, payee: str, routes: list[dict], max_atomic: int) -> str:
    """routes: [{id, name, path, example, useAccount?: [field...], fromQuote?: bool}]"""
    cfg = {"service": service, "payee": payee, "usdc": USDC_BASE, "maxAtomic": str(int(max_atomic)), "routes": routes}
    cfg_json = json.dumps(cfg, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    cards = "".join(ROUTE.replace("__RID__", html.escape(r["id"], quote=True))
                    .replace("__RNAME__", html.escape(r["name"]))
                    .replace("__RPATH__", html.escape(r["path"])) for r in routes)
    return (PAGE.replace("__NAME__", html.escape(service)).replace("__PAYEE__", html.escape(payee))
            .replace("__ROUTES__", cards).replace("__CFG__", cfg_json).replace("__SCRIPT__", SCRIPT))
